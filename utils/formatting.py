"""Форматирование сообщений и безопасный HTML."""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from config.settings import settings
from database.models import User
from services.gamification import level_progress
from services.market.base import Quote


def escape(value: object) -> str:
    return html.escape(str(value), quote=False)


def display_name(user: User | object | None) -> str:
    first_name = getattr(user, "first_name", None) if user else None
    return escape((first_name or "инвестор")[:40])


def money(value: Decimal | int | float | str, currency: str = "USD") -> str:
    amount = Decimal(str(value))
    symbol = {"USD": "$", "RUB": "₽", "EUR": "€"}.get(currency, "")
    sign = "-" if amount < 0 else ""
    return f"{sign}{symbol}{abs(amount):,.2f}" if symbol else f"{sign}{abs(amount):,.2f} {currency}"


def price(value: Decimal | int | float | str, currency: str = "USD") -> str:
    amount = Decimal(str(value))
    decimals = 4 if abs(amount) < 1 else 2
    symbol = {"USD": "$", "RUB": "₽", "EUR": "€"}.get(currency, "")
    sign = "-" if amount < 0 else ""
    return f"{sign}{symbol}{abs(amount):,.{decimals}f}"


def quantity(value: Decimal | str) -> str:
    amount = Decimal(str(value))
    text = format(amount.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def signed_money(value: Decimal | int | float | str) -> str:
    amount = Decimal(str(value))
    sign = "+" if amount > 0 else ""
    return f"{sign}{money(amount)}"


def change_percent(value: Decimal | None) -> str:
    if value is None:
        return "—"
    amount = Decimal(value)
    arrow = "▲" if amount > 0 else "▼" if amount < 0 else "■"
    return f"{arrow} {amount:+.2f}%"


def trend_emoji(value: Decimal | None) -> str:
    if value is None:
        return "▫️"
    if value > 0:
        return "🟢"
    if value < 0:
        return "🔴"
    return "⚪️"


def source_line(quote: Quote) -> str:
    marker = "⚠️ резервные данные" if quote.is_fallback else "🛰️"
    return f"{marker} {escape(quote.source)}"


def as_of(value: datetime) -> str:
    """Показывает UTC-время в часовом поясе бота.

    На Windows без пакета tzdata база IANA может отсутствовать, поэтому
    для Москвы предусмотрен явный fallback UTC+3 (в РФ нет перехода на летнее
    время с 2014 года).
    """
    try:
        zone = ZoneInfo(settings.BOT_TIMEZONE)
        label = "МСК" if settings.BOT_TIMEZONE == "Europe/Moscow" else "UTC"
    except Exception:
        if settings.BOT_TIMEZONE == "Europe/Moscow":
            zone = timezone(timedelta(hours=3))
            label = "МСК"
        else:
            zone = timezone.utc
            label = "UTC"
    local = value.replace(tzinfo=timezone.utc).astimezone(zone)
    return local.strftime(f"%d.%m.%Y %H:%M {label}")


def progress_bar(percent: int, width: int = 10) -> str:
    percent = max(0, min(100, int(percent)))
    filled = round(width * percent / 100)
    return "▰" * filled + "▱" * (width - filled)


def user_progress(user: User) -> str:
    current, need, percent = level_progress(int(user.xp), int(user.level))
    return (
        f"⭐ Уровень <b>{user.level}</b> · {current}/{need} XP\n"
        f"{progress_bar(percent)} {percent}%"
    )


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def short_date(value: datetime) -> str:
    return value.strftime("%d.%m.%Y")
