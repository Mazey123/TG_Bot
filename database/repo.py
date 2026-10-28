"""Репозитории: весь SQL/ORM-доступ сосредоточен здесь.

Репозитории не управляют транзакциями (кроме случаев, где это явно
описано): commit/rollback — ответственность сервисного слоя. Это
позволяет выполнять покупку/продажу одной атомарной операцией.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    Badge,
    CacheEntry,
    Portfolio,
    PriceAlert,
    QuizAttempt,
    Transaction,
    User,
    utcnow,
)


class UserRepository:
    @staticmethod
    async def get(session: AsyncSession, user_id: int) -> User | None:
        return await session.get(User, user_id)

    @staticmethod
    async def get_or_create(
        session: AsyncSession,
        user_id: int,
        username: str | None,
        first_name: str | None,
        starting_balance: Decimal,
    ) -> tuple[User, bool]:
        user = await session.get(User, user_id)
        created = False
        if user is None:
            user = User(
                id=user_id,
                username=(username or None),
                first_name=(first_name or "Инвестор")[:128],
                balance=starting_balance,
            )
            session.add(user)
            created = True
        else:
            user.username = username or user.username
            if first_name:
                user.first_name = first_name[:128]
            # Не пишем в БД на каждый клик: last_active обновляем раз в 5 минут.
            if user.last_active is None or (utcnow() - user.last_active).total_seconds() > 300:
                user.last_active = utcnow()
        await session.flush()
        return user, created

    @staticmethod
    async def add_xp(session: AsyncSession, user: User, amount: int) -> bool:
        """Начисляет XP. Возвращает True, если пользователь получил новый уровень."""
        if amount <= 0:
            return False
        old_level = user.level
        user.xp = int(user.xp) + int(amount)
        user.level = level_from_xp(user.xp)
        await session.flush()
        return user.level > old_level

    @staticmethod
    async def top_by_xp(session: AsyncSession, limit: int = 10) -> list[User]:
        result = await session.execute(
            select(User).order_by(User.xp.desc(), User.id.asc()).limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def rank_by_xp(session: AsyncSession, user: User) -> int:
        result = await session.execute(
            select(func.count(User.id)).where(User.xp > user.xp)
        )
        return int(result.scalar_one()) + 1

    @staticmethod
    async def reset_progress(session: AsyncSession, user: User, starting_balance: Decimal) -> None:
        await session.execute(delete(Portfolio).where(Portfolio.user_id == user.id))
        await session.execute(delete(Transaction).where(Transaction.user_id == user.id))
        await session.execute(delete(Badge).where(Badge.user_id == user.id))
        await session.execute(delete(QuizAttempt).where(QuizAttempt.user_id == user.id))
        await session.execute(delete(PriceAlert).where(PriceAlert.user_id == user.id))
        user.balance = starting_balance
        user.xp = 0
        user.level = 1
        user.daily_streak = 0
        user.last_bonus_at = None
        await session.flush()


class PortfolioRepository:
    @staticmethod
    async def list_positions(session: AsyncSession, user_id: int) -> list[Portfolio]:
        result = await session.execute(
            select(Portfolio).where(Portfolio.user_id == user_id).order_by(Portfolio.ticker)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_position(
        session: AsyncSession, user_id: int, ticker: str, *, for_update: bool = False
    ) -> Portfolio | None:
        stmt = select(Portfolio).where(
            Portfolio.user_id == user_id, Portfolio.ticker == ticker.upper()
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def buy(
        session: AsyncSession, user_id: int, ticker: str, quantity: Decimal, price: Decimal
    ) -> Portfolio:
        position = await PortfolioRepository.get_position(session, user_id, ticker)
        if position is None:
            position = Portfolio(
                user_id=user_id,
                ticker=ticker.upper(),
                quantity=quantity,
                avg_buy_price=price,
            )
            session.add(position)
        else:
            total_qty = Decimal(position.quantity) + quantity
            position.avg_buy_price = (
                (Decimal(position.avg_buy_price) * Decimal(position.quantity) + price * quantity)
                / total_qty
            ).quantize(Decimal("0.0001"))
            position.quantity = total_qty
            position.updated_at = utcnow()
        await session.flush()
        return position

    @staticmethod
    async def sell(
        session: AsyncSession, user_id: int, ticker: str, quantity: Decimal
    ) -> Portfolio | None:
        position = await PortfolioRepository.get_position(
            session, user_id, ticker, for_update=True
        )
        if position is None:
            return None
        left = Decimal(position.quantity) - quantity
        if left <= 0:
            await session.delete(position)
            await session.flush()
            return None
        position.quantity = left
        position.updated_at = utcnow()
        await session.flush()
        return position


class TransactionRepository:
    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        user_id: int,
        ticker: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        total_amount: Decimal,
        fee: Decimal,
        realized_pnl: Decimal | None = None,
    ) -> Transaction:
        transaction = Transaction(
            user_id=user_id,
            ticker=ticker.upper(),
            side=side,
            quantity=quantity,
            price=price,
            total_amount=total_amount,
            fee=fee,
            realized_pnl=realized_pnl,
        )
        session.add(transaction)
        await session.flush()
        return transaction

    @staticmethod
    async def list_for_user(
        session: AsyncSession, user_id: int, limit: int = 10
    ) -> list[Transaction]:
        result = await session.execute(
            select(Transaction)
            .where(Transaction.user_id == user_id)
            .order_by(Transaction.created_at.desc(), Transaction.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def count_for_user(session: AsyncSession, user_id: int) -> int:
        result = await session.execute(
            select(func.count(Transaction.id)).where(Transaction.user_id == user_id)
        )
        return int(result.scalar_one())


class BadgeRepository:
    @staticmethod
    async def award(session: AsyncSession, user_id: int, badge_code: str) -> bool:
        exists = await session.execute(
            select(Badge.id).where(
                Badge.user_id == user_id, Badge.badge_code == badge_code
            )
        )
        if exists.scalar_one_or_none() is not None:
            return False
        session.add(Badge(user_id=user_id, badge_code=badge_code))
        await session.flush()
        return True

    @staticmethod
    async def list_for_user(session: AsyncSession, user_id: int) -> list[Badge]:
        result = await session.execute(
            select(Badge).where(Badge.user_id == user_id).order_by(Badge.earned_at)
        )
        return list(result.scalars().all())


class QuizRepository:
    @staticmethod
    async def save_attempt(
        session: AsyncSession,
        *,
        user_id: int,
        category: str,
        correct_answers: int,
        total_questions: int,
        score: int,
    ) -> QuizAttempt:
        attempt = QuizAttempt(
            user_id=user_id,
            category=category,
            correct_answers=correct_answers,
            total_questions=total_questions,
            score=score,
        )
        session.add(attempt)
        await session.flush()
        return attempt

    @staticmethod
    async def list_for_user(
        session: AsyncSession, user_id: int, limit: int = 5
    ) -> list[QuizAttempt]:
        result = await session.execute(
            select(QuizAttempt)
            .where(QuizAttempt.user_id == user_id)
            .order_by(QuizAttempt.created_at.desc(), QuizAttempt.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def count_for_user(session: AsyncSession, user_id: int) -> int:
        result = await session.execute(
            select(func.count(QuizAttempt.id)).where(QuizAttempt.user_id == user_id)
        )
        return int(result.scalar_one())


class AlertRepository:
    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        user_id: int,
        ticker: str,
        direction: str,
        target_price: Decimal,
    ) -> PriceAlert:
        alert = PriceAlert(
            user_id=user_id,
            ticker=ticker.upper(),
            direction=direction,
            target_price=target_price,
        )
        session.add(alert)
        await session.flush()
        return alert

    @staticmethod
    async def list_active(session: AsyncSession, limit: int = 500) -> list[PriceAlert]:
        result = await session.execute(
            select(PriceAlert)
            .where(PriceAlert.is_active.is_(True))
            .order_by(PriceAlert.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_for_user(session: AsyncSession, user_id: int) -> list[PriceAlert]:
        result = await session.execute(
            select(PriceAlert)
            .where(PriceAlert.user_id == user_id, PriceAlert.is_active.is_(True))
            .order_by(PriceAlert.id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get(session: AsyncSession, alert_id: int) -> PriceAlert | None:
        return await session.get(PriceAlert, alert_id)

    @staticmethod
    async def deactivate(session: AsyncSession, alert: PriceAlert) -> None:
        alert.is_active = False
        alert.triggered_at = alert.triggered_at or utcnow()
        await session.flush()

    @staticmethod
    async def count_active_for_user(session: AsyncSession, user_id: int) -> int:
        result = await session.execute(
            select(func.count(PriceAlert.id)).where(
                PriceAlert.user_id == user_id, PriceAlert.is_active.is_(True)
            )
        )
        return int(result.scalar_one())


class CacheRepository:
    @staticmethod
    async def get(session: AsyncSession, key: str) -> CacheEntry | None:
        return await session.get(CacheEntry, key)

    @staticmethod
    async def upsert(
        session: AsyncSession, key: str, payload: str, expires_at: datetime
    ) -> None:
        entry = await session.get(CacheEntry, key)
        if entry is None:
            entry = CacheEntry(
                key=key, payload=payload, expires_at=expires_at, created_at=utcnow()
            )
            session.add(entry)
        else:
            entry.payload = payload
            entry.expires_at = expires_at
            entry.created_at = utcnow()
        await session.flush()

    @staticmethod
    async def purge_expired(session: AsyncSession) -> int:
        result = await session.execute(
            delete(CacheEntry).where(
                CacheEntry.expires_at < datetime.now(timezone.utc).replace(tzinfo=None)
            )
        )
        return int(result.rowcount or 0)


def level_from_xp(xp: int) -> int:
    """Уровень 1 при 0 XP, уровень 2 при 100 XP, 3 при 400 XP и т.д."""
    if xp <= 0:
        return 1
    return int((xp / 100) ** 0.5) + 1


def xp_for_level(level: int) -> int:
    return max(0, (level - 1) ** 2 * 100)
