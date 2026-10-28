"""Фоновые ценовые уведомления.

Пользователь создаёт алерт командой /alert TICKER PRICE. Отдельная
asyncio-задача периодически проверяет активные алерты и отправляет
сообщение при пересечении цены. Учитывается настройка уведомлений.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config.settings import Settings
from database.models import PriceAlert, User
from database.repo import AlertRepository
from database.session import async_session_factory
from services.assets import get_asset, normalize_ticker
from services.market.service import MarketService

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _user_ref(user_id: int, salt: str) -> str:
    return hashlib.sha256(f"{user_id}:{salt}".encode()).hexdigest()[:10]


def parse_target_price(raw: str) -> Decimal:
    try:
        value = Decimal(raw.strip().replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Цена должна быть числом") from exc
    if not value.is_finite() or value <= 0 or value > Decimal("1000000000"):
        raise ValueError("Некорректная цена")
    return value.quantize(Decimal("0.0001"))


async def create_alert(
    session,
    *,
    user_id: int,
    ticker: str,
    target_price: Decimal,
    current_price: Decimal,
) -> PriceAlert:
    normalized = normalize_ticker(ticker)
    if get_asset(normalized) is None:
        raise ValueError("Этот тикер не поддерживается")
    active_count = await AlertRepository.count_active_for_user(session, user_id)
    if active_count >= 8:
        raise ValueError("Уже 8 активных алертов. Удалите лишние в /alerts")
    direction = "above" if target_price >= current_price else "below"
    alert = await AlertRepository.create(
        session,
        user_id=user_id,
        ticker=normalized,
        direction=direction,
        target_price=target_price,
    )
    await session.commit()
    return alert


def _is_triggered(alert: PriceAlert, price: Decimal) -> bool:
    target = Decimal(alert.target_price)
    if alert.direction == "above":
        return price >= target
    return price <= target


async def run_alert_worker(
    bot: Bot,
    market: MarketService,
    settings: Settings,
) -> None:
    """Бесконечный цикл проверки алертов. Отменяется при остановке бота."""
    logger.info("alert_worker_started interval=%s", settings.ALERT_CHECK_INTERVAL_SECONDS)
    await asyncio.sleep(10)
    while True:
        try:
            async with async_session_factory() as session:
                alerts = await AlertRepository.list_active(session)
                if alerts:
                    tickers = sorted({alert.ticker for alert in alerts})
                    quotes = await market.get_quotes(session, tickers)

                    for alert in alerts:
                        quote = quotes.get(alert.ticker)
                        if quote is None or quote.is_fallback:
                            # Не будим пользователя по демо/устаревшей цене.
                            continue
                        if not _is_triggered(alert, Decimal(quote.price)):
                            continue
                        user = await session.get(User, alert.user_id)
                        if user is None or not user.notifications_enabled:
                            continue
                        text = (
                            "🔔 <b>Ценовой алерт</b>\n\n"
                            f"{quote.ticker} достиг <code>{_price(quote.price, quote.currency)}</code>\n"
                            f"Ваш порог: <code>{_price(alert.target_price, quote.currency)}</code> "
                            f"({'выше' if alert.direction == 'above' else 'ниже'})\n"
                            f"Источник: {quote.source}"
                        )
                        try:
                            await bot.send_message(alert.user_id, text, parse_mode="HTML")
                        except TelegramAPIError as exc:
                            logger.warning(
                                "alert_send_failed user_ref=%s error=%s",
                                _user_ref(alert.user_id, settings.LOG_SALT),
                                type(exc).__name__,
                            )
                            continue
                        alert.is_active = False
                        alert.triggered_at = _utcnow()
                        await session.commit()
                        logger.info(
                            "alert_triggered user_ref=%s ticker=%s",
                            _user_ref(alert.user_id, settings.LOG_SALT),
                            alert.ticker,
                        )
        except asyncio.CancelledError:
            logger.info("alert_worker_stopped")
            raise
        except Exception as exc:
            logger.error("alert_worker_failed error=%s", type(exc).__name__)
        await asyncio.sleep(settings.ALERT_CHECK_INTERVAL_SECONDS)


def format_alert_direction(direction: str) -> str:
    return "выше" if direction == "above" else "ниже"


def _price(value, currency: str) -> str:
    symbol = {"USD": "$", "RUB": "₽", "EUR": "€"}.get(currency, "")
    amount = Decimal(value)
    return f"{symbol}{amount:,.2f}"
