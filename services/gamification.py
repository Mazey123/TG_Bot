"""Уровни, XP, достижения и ежедневный бонус."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from database.models import User
from database.repo import BadgeRepository, UserRepository


@dataclass(frozen=True, slots=True)
class BadgeInfo:
    title: str
    emoji: str
    description: str


BADGES: dict[str, BadgeInfo] = {
    "FIRST_TRADE": BadgeInfo("Первая сделка", "🥇", "Совершить первую покупку или продажу"),
    "FIRST_PROFIT": BadgeInfo("В плюсе", "💵", "Закрыть сделку с положительным PnL"),
    "DIVERSIFIED": BadgeInfo("Диверсификация", "🧺", "Держать три разных актива"),
    "ACTIVE_TRADER": BadgeInfo("Активный трейдер", "⚡", "Совершить 10 сделок"),
    "WHALE": BadgeInfo("Кит", "🐋", "Удвоить стартовый капитал"),
    "QUIZ_ACE": BadgeInfo("Знаток рынка", "🧠", "Пройти квиз без ошибок"),
    "STREAK_3": BadgeInfo("Дисциплина", "🔥", "Забрать бонус 3 дня подряд"),
}


def level_from_xp(xp: int) -> int:
    if xp <= 0:
        return 1
    return int((xp / 100) ** 0.5) + 1


def xp_for_next_level(level: int) -> int:
    return max(0, level**2 * 100)


def level_progress(xp: int, level: int) -> tuple[int, int, int]:
    """Возвращает (xp_внутри_уровня, нужно_для_уровня, процент)."""
    start = xp_for_next_level(level - 1) if level > 1 else 0
    end = xp_for_next_level(level)
    span = max(1, end - start)
    current = max(0, xp - start)
    percent = min(100, int(current / span * 100))
    return current, span, percent


async def award_badges(
    session: AsyncSession,
    user: User,
    *,
    starting_balance: Decimal,
    trade_count: int | None = None,
    position_count: int | None = None,
    realized_pnl: Decimal | None = None,
    quiz_perfect: bool = False,
) -> list[str]:
    """Выдаёт бейджи по условиям. Возвращает коды новых достижений."""
    candidates: list[str] = []

    if trade_count is None:
        trade_count = 0  # вызывающий код передаёт актуальное значение
    if position_count is None:
        position_count = 0

    if trade_count >= 1:
        candidates.append("FIRST_TRADE")
    if realized_pnl is not None and realized_pnl > 0:
        candidates.append("FIRST_PROFIT")
    if position_count >= 3:
        candidates.append("DIVERSIFIED")
    if trade_count >= 10:
        candidates.append("ACTIVE_TRADER")
    if Decimal(user.balance) >= starting_balance * 2:
        candidates.append("WHALE")
    if quiz_perfect:
        candidates.append("QUIZ_ACE")
    if user.daily_streak >= 3:
        candidates.append("STREAK_3")

    awarded: list[str] = []
    for code in candidates:
        if await BadgeRepository.award(session, user.id, code):
            awarded.append(code)
    return awarded


@dataclass(slots=True)
class BonusResult:
    granted: bool
    amount: Decimal = Decimal("0")
    streak: int = 0
    next_available_at: datetime | None = None
    new_badges: list[str] | None = None
    level_up: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_bonus_locks: dict[int, asyncio.Lock] = {}


async def claim_daily_bonus(
    session: AsyncSession, user: User, settings: Settings
) -> BonusResult:
    lock = _bonus_locks.get(user.id)
    if lock is None:
        lock = asyncio.Lock()
        _bonus_locks[user.id] = lock
    async with lock:
        return await _claim_daily_bonus_locked(session, user, settings)


async def _claim_daily_bonus_locked(
    session: AsyncSession, user: User, settings: Settings
) -> BonusResult:
    # Профиль мог быть загружен до того, как другой запрос уже выдал бонус.
    await session.refresh(user)
    now = _utcnow()
    cooldown = timedelta(hours=settings.BONUS_COOLDOWN_HOURS)
    if user.last_bonus_at is not None:
        elapsed = now - user.last_bonus_at
        if elapsed < cooldown:
            return BonusResult(
                granted=False,
                streak=user.daily_streak,
                next_available_at=user.last_bonus_at + cooldown,
            )

    if user.last_bonus_at is not None and now - user.last_bonus_at <= timedelta(
        hours=settings.BONUS_STREAK_WINDOW_HOURS
    ):
        user.daily_streak = int(user.daily_streak) + 1
    else:
        user.daily_streak = 1

    user.balance = Decimal(user.balance) + settings.DAILY_BONUS
    user.last_bonus_at = now
    level_up = await UserRepository.add_xp(session, user, 20)
    badges = await award_badges(
        session, user, starting_balance=settings.STARTING_BALANCE
    )
    await session.commit()
    return BonusResult(
        granted=True,
        amount=settings.DAILY_BONUS,
        streak=user.daily_streak,
        new_badges=badges,
        level_up=level_up,
    )
