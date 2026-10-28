"""Валидация пользовательского ввода и защитные хелперы."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

TICKER_RE = re.compile(r"^[A-Z0-9]{1,10}$")
TEXT_RE = re.compile(r"^[^\x00-\x08\x0b\x0c\x0e-\x1f]{1,120}$")


def clean_text(raw: str, limit: int = 120) -> str:
    """Убирает управляющие символы и ограничивает длину."""
    value = (raw or "").strip()
    value = "".join(ch for ch in value if ch.isprintable() or ch in " \t")
    return value[:limit]


def parse_positive_decimal(raw: str, *, max_value: Decimal = Decimal("1000000")) -> Decimal:
    value = Decimal((raw or "").strip().replace(",", "."))
    if not value.is_finite() or value <= 0 or value > max_value:
        raise ValueError("Некорректное число")
    return value


def looks_like_ticker(raw: str) -> bool:
    return bool(TICKER_RE.fullmatch((raw or "").strip().upper()))


def safe_int(raw: str, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def coerce_decimal(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw)
        return value if value.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None
