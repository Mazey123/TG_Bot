"""Базовые типы рыночных данных и ошибки провайдеров."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


class MarketError(Exception):
    """Базовая ошибка рыночного слоя."""


class ProviderError(MarketError):
    """Внешний API недоступен или вернул некорректный ответ."""

    def __init__(self, provider: str, message: str, *, status: int | None = None) -> None:
        self.provider = provider
        self.status = status
        super().__init__(f"{provider}: {message}")


@dataclass(slots=True)
class Quote:
    ticker: str
    name: str
    kind: str
    price: Decimal
    currency: str
    change_percent: Decimal | None
    source: str
    as_of: datetime
    is_fallback: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_cache(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "kind": self.kind,
            "price": str(self.price),
            "currency": self.currency,
            "change_percent": None if self.change_percent is None else str(self.change_percent),
            "source": self.source,
            "as_of": self.as_of.isoformat(),
            "is_fallback": self.is_fallback,
            "extra": self.extra,
        }

    @classmethod
    def from_cache(cls, payload: dict[str, Any]) -> "Quote":
        return cls(
            ticker=str(payload["ticker"]),
            name=str(payload["name"]),
            kind=str(payload["kind"]),
            price=Decimal(str(payload["price"])),
            currency=str(payload["currency"]),
            change_percent=(
                None
                if payload.get("change_percent") is None
                else Decimal(str(payload["change_percent"]))
            ),
            source=str(payload["source"]),
            as_of=datetime.fromisoformat(str(payload["as_of"])),
            is_fallback=bool(payload.get("is_fallback", False)),
            extra=dict(payload.get("extra") or {}),
        )


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    day: date
    close: Decimal


@dataclass(slots=True)
class History:
    ticker: str
    points: list[HistoryPoint]
    source: str
    is_fallback: bool = False

    def to_cache(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "points": [
                {"day": point.day.isoformat(), "close": str(point.close)}
                for point in self.points
            ],
            "source": self.source,
            "is_fallback": self.is_fallback,
        }

    @classmethod
    def from_cache(cls, payload: dict[str, Any]) -> "History":
        return cls(
            ticker=str(payload["ticker"]),
            points=[
                HistoryPoint(
                    day=date.fromisoformat(str(row["day"])),
                    close=Decimal(str(row["close"])),
                )
                for row in payload.get("points", [])
            ],
            source=str(payload["source"]),
            is_fallback=bool(payload.get("is_fallback", False)),
        )
