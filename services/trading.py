"""Торговая логика: атомарные сделки, комиссия, PnL, XP, бейджи."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from database.models import Portfolio, Transaction, User
from database.repo import (
    PortfolioRepository,
    TransactionRepository,
    UserRepository,
)
from services.assets import get_asset, normalize_ticker
from services.gamification import award_badges, level_from_xp
from services.market.base import Quote

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
QTY_STEP = Decimal("0.000001")
MAX_QUANTITY = Decimal("1000000")


class TradeValidationError(Exception):
    """Ошибка бизнес-валидации, безопасная для показа пользователю."""


class InsufficientFundsError(TradeValidationError):
    pass


class InsufficientPositionError(TradeValidationError):
    pass


@dataclass(slots=True)
class TradeResult:
    side: str
    ticker: str
    quantity: Decimal
    price: Decimal
    gross: Decimal
    fee: Decimal
    cash_flow: Decimal
    new_balance: Decimal
    realized_pnl: Decimal | None
    xp_gained: int
    level_up: bool
    new_level: int
    new_badges: list[str] = field(default_factory=list)


def money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def validate_quantity(raw: str | Decimal) -> Decimal:
    try:
        value = Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise TradeValidationError("Количество должно быть числом.") from exc
    if not value.is_finite() or value <= 0:
        raise TradeValidationError("Количество должно быть больше нуля.")
    if value > MAX_QUANTITY:
        raise TradeValidationError("Слишком большое количество для одной сделки.")
    if value.as_tuple().exponent < -6:
        raise TradeValidationError("Максимум 6 знаков после запятой.")
    return value.quantize(QTY_STEP)


def validate_quote(quote: Quote) -> Decimal:
    price = Decimal(quote.price)
    if not price.is_finite() or price <= 0:
        raise TradeValidationError("Некорректная цена инструмента.")
    return price


class TradingService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Сериализует сделки одного пользователя в рамках процесса.
        # В БД дополнительно используются транзакция и условный UPDATE.
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def execute(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        side: str,
        ticker: str,
        quantity: str | Decimal,
        quote: Quote,
    ) -> TradeResult:
        async with self._lock_for(user_id):
            return await self._execute_locked(
                session,
                user_id=user_id,
                side=side,
                ticker=ticker,
                quantity=quantity,
                quote=quote,
            )

    async def _execute_locked(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        side: str,
        ticker: str,
        quantity: str | Decimal,
        quote: Quote,
    ) -> TradeResult:
        side = side.lower().strip()
        if side not in {"buy", "sell"}:
            raise TradeValidationError("Неизвестная операция.")
        ticker = normalize_ticker(ticker)
        asset = get_asset(ticker)
        if asset is None:
            raise TradeValidationError("Этот инструмент не торгуется в симуляторе.")
        if asset.kind == "fx":
            raise TradeValidationError(
                "Валютные пары доступны только как справочные котировки."
            )
        qty = validate_quantity(quantity)
        price = validate_quote(quote)

        try:
            user = await session.get(User, user_id)
            if user is None:
                raise TradeValidationError("Профиль не найден. Отправьте /start.")

            gross = money(qty * price)
            fee = money(gross * self.settings.COMMISSION_RATE)
            realized_pnl: Decimal | None = None

            if side == "buy":
                cash_needed = money(gross + fee)
                balance_before = Decimal(user.balance)
                result = await session.execute(
                    update(User)
                    .where(User.id == user_id, User.balance >= cash_needed)
                    .values(balance=User.balance - cash_needed)
                )
                if not result.rowcount:
                    await session.rollback()
                    raise InsufficientFundsError(
                        f"Недостаточно средств. Нужно ${cash_needed:,.2f}, "
                        f"доступно ${balance_before:,.2f} (с учётом комиссии)."
                    )
                await session.refresh(user)
                await PortfolioRepository.buy(session, user_id, ticker, qty, price)
                cash_flow = -cash_needed
            else:
                position = await PortfolioRepository.get_position(
                    session, user_id, ticker, for_update=True
                )
                held = Decimal(position.quantity) if position else Decimal("0")
                if position is None or held < qty:
                    await session.rollback()
                    raise InsufficientPositionError(
                        f"В портфеле только {held.normalize():f} {ticker}."
                    )
                avg_price = Decimal(position.avg_buy_price)
                cash_received = money(gross - fee)
                realized_pnl = money((price - avg_price) * qty - fee)
                await session.execute(
                    update(User)
                    .where(User.id == user_id)
                    .values(balance=User.balance + cash_received)
                )
                await session.refresh(user)
                await PortfolioRepository.sell(session, user_id, ticker, qty)
                cash_flow = cash_received

            await TransactionRepository.create(
                session,
                user_id=user_id,
                ticker=ticker,
                side=side,
                quantity=qty,
                price=price,
                total_amount=gross,
                fee=fee,
                realized_pnl=realized_pnl,
            )

            trade_count = await TransactionRepository.count_for_user(session, user_id)
            positions = await PortfolioRepository.list_positions(session, user_id)
            xp_gained = 10 + min(int(gross / Decimal("1000")), 20)
            if realized_pnl is not None and realized_pnl > 0:
                xp_gained += 5
            old_level = user.level
            await UserRepository.add_xp(session, user, xp_gained)
            user.level = level_from_xp(user.xp)

            badges = await award_badges(
                session,
                user,
                starting_balance=self.settings.STARTING_BALANCE,
                trade_count=trade_count,
                position_count=len(positions),
                realized_pnl=realized_pnl,
            )

            await session.commit()
            await session.refresh(user)
            logger.info(
                "trade_completed ticker=%s side=%s qty=%s user_ref=%s",
                ticker,
                side,
                qty.normalize(),
                _user_ref(user_id),
            )
            return TradeResult(
                side=side,
                ticker=ticker,
                quantity=qty,
                price=price,
                gross=gross,
                fee=fee,
                cash_flow=cash_flow,
                new_balance=Decimal(user.balance),
                realized_pnl=realized_pnl,
                xp_gained=xp_gained,
                level_up=user.level > old_level,
                new_level=user.level,
                new_badges=badges,
            )
        except TradeValidationError:
            raise
        except Exception:
            await session.rollback()
            raise

    async def reset_progress(self, session: AsyncSession, user: User) -> None:
        async with self._lock_for(user.id):
            await UserRepository.reset_progress(
                session, user, self.settings.STARTING_BALANCE
            )
            await session.commit()
            logger.info("progress_reset user_ref=%s", _user_ref(user.id))


def _user_ref(user_id: int) -> str:
    """Псевдоним для логов вместо Telegram ID/имени."""
    import hashlib

    from config.settings import settings

    return hashlib.sha256(
        f"{user_id}:{settings.LOG_SALT}".encode("utf-8")
    ).hexdigest()[:10]


def portfolio_realized_pnl(transactions: list[Transaction]) -> Decimal:
    """Суммарный реализованный PnL по истории сделок."""
    total = Decimal("0")
    for transaction in transactions:
        if transaction.realized_pnl is not None:
            total += Decimal(transaction.realized_pnl)
    return money(total)


def portfolio_book_value(positions: list[Portfolio]) -> Decimal:
    return money(
        sum(
            (Decimal(position.quantity) * Decimal(position.avg_buy_price) for position in positions),
            start=Decimal("0"),
        )
    )
