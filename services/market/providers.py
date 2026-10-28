"""Провайдеры рыночных данных.

Основные источники (все — внешние HTTP API):
* Yahoo Finance Chart API — акции: цена, изменение, история;
* Stooq CSV API — резервный источник котировок акций;
* CoinGecko API — криптовалюты;
* Frankfurter API — курсы валют (ECB);
* DemoProvider — последний рубеж, если сеть недоступна.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from services.assets import Asset
from services.http import HttpClient, join_url
from services.market.base import History, HistoryPoint, ProviderError, Quote


def _dec(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"не число: {value!r}") from exc


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _change_percent(price: Decimal, previous: Decimal | None) -> Decimal | None:
    if previous is None or previous <= 0:
        return None
    return ((price - previous) / previous * 100).quantize(Decimal("0.01"))


class YahooFinanceProvider:
    name = "Yahoo Finance"

    def __init__(self, http: HttpClient, base_url: str) -> None:
        self.http = http
        self.base_url = base_url

    async def quote(self, asset: Asset) -> Quote:
        symbol = asset.yahoo_symbol or asset.ticker
        payload = await self.http.get_json(
            join_url(self.base_url, f"/v8/finance/chart/{symbol}"),
            params={"range": "5d", "interval": "1d", "includePrePost": "false"},
            provider=self.name,
        )
        return parse_yahoo_quote(payload, asset)

    async def history(self, asset: Asset, days: int = 30) -> History:
        symbol = asset.yahoo_symbol or asset.ticker
        period = "1mo" if days <= 31 else "3mo"
        payload = await self.http.get_json(
            join_url(self.base_url, f"/v8/finance/chart/{symbol}"),
            params={"range": period, "interval": "1d"},
            provider=self.name,
        )
        return parse_yahoo_history(payload, asset)


def _extract_chart(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        chart = payload["chart"]
        if chart.get("error"):
            raise ProviderError("Yahoo Finance", str(chart["error"])[:160])
        result = chart["result"]
        if not result:
            raise ProviderError("Yahoo Finance", "пустой результат")
        return result[0]
    except (KeyError, TypeError, IndexError) as exc:
        raise ProviderError("Yahoo Finance", "неожиданная структура ответа") from exc


def parse_yahoo_quote(payload: dict[str, Any], asset: Asset) -> Quote:
    result = _extract_chart(payload)
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    closes: list[Decimal] = []
    try:
        quote_block = result["indicators"]["quote"][0]
        for value in quote_block.get("close") or []:
            if value is not None:
                closes.append(_dec(value))
    except (KeyError, IndexError, TypeError, ValueError):
        closes = []

    price = meta.get("regularMarketPrice")
    if price is None and closes:
        price = closes[-1]
    try:
        price_dec = _dec(price)
    except ValueError as exc:
        raise ProviderError("Yahoo Finance", "нет цены в ответе") from exc
    if price_dec <= 0:
        raise ProviderError("Yahoo Finance", "некорректная цена")

    previous = meta.get("previousClose") or meta.get("chartPreviousClose")
    if previous is None and len(closes) >= 2:
        previous = closes[-2]
    try:
        previous_dec = _dec(previous) if previous is not None else None
    except ValueError:
        previous_dec = None

    market_time = meta.get("regularMarketTime")
    if market_time:
        as_of = datetime.fromtimestamp(int(market_time), tz=timezone.utc).replace(tzinfo=None)
    elif timestamps:
        as_of = datetime.fromtimestamp(int(timestamps[-1]), tz=timezone.utc).replace(tzinfo=None)
    else:
        as_of = _utcnow()

    return Quote(
        ticker=asset.ticker,
        name=asset.name,
        kind=asset.kind,
        price=price_dec.quantize(Decimal("0.0001")),
        currency=str(meta.get("currency") or asset.currency),
        change_percent=_change_percent(price_dec, previous_dec),
        source=YahooFinanceProvider.name,
        as_of=as_of,
        extra={
            "previous_close": str(previous_dec) if previous_dec is not None else None,
            "day_high": str(meta.get("regularMarketDayHigh") or ""),
            "day_low": str(meta.get("regularMarketDayLow") or ""),
            "volume": str(meta.get("regularMarketVolume") or ""),
        },
    )


def parse_yahoo_history(payload: dict[str, Any], asset: Asset) -> History:
    result = _extract_chart(payload)
    timestamps = result.get("timestamp") or []
    try:
        closes = result["indicators"]["quote"][0].get("close") or []
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError("Yahoo Finance", "нет истории цен") from exc

    points: list[HistoryPoint] = []
    seen: set[date] = set()
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        try:
            day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
            value = _dec(close)
        except (ValueError, TypeError, OSError):
            continue
        if day in seen or value <= 0:
            continue
        seen.add(day)
        points.append(HistoryPoint(day=day, close=value))
    if len(points) < 2:
        raise ProviderError("Yahoo Finance", "недостаточно точек истории")
    points.sort(key=lambda point: point.day)
    return History(ticker=asset.ticker, points=points, source=YahooFinanceProvider.name)


class StooqProvider:
    name = "Stooq"

    def __init__(self, http: HttpClient, base_url: str) -> None:
        self.http = http
        self.base_url = base_url

    async def quote(self, asset: Asset) -> Quote:
        symbol = asset.stooq_symbol or f"{asset.ticker.lower()}.us"
        text = await self.http.get_text(
            join_url(self.base_url, "/q/l/"),
            params={"s": symbol, "f": "sd2t2ohlcv", "h": "", "e": "csv"},
            provider=self.name,
        )
        return parse_stooq_quote(text, asset)


def parse_stooq_quote(text: str, asset: Asset) -> Quote:
    if not text or "Symbol" not in text:
        raise ProviderError("Stooq", "пустой CSV")
    try:
        row = next(csv.DictReader(io.StringIO(text)))
    except (StopIteration, csv.Error) as exc:
        raise ProviderError("Stooq", "не удалось разобрать CSV") from exc

    close_raw = (row.get("Close") or "").strip()
    open_raw = (row.get("Open") or "").strip()
    if close_raw in ("", "N/D", "0"):
        raise ProviderError("Stooq", "тикер не найден")
    try:
        price = _dec(close_raw)
    except ValueError as exc:
        raise ProviderError("Stooq", "некорректная цена") from exc
    try:
        open_price: Decimal | None = _dec(open_raw)
    except ValueError:
        open_price = None

    day_raw = (row.get("Date") or "").strip()
    time_raw = (row.get("Time") or "").strip()
    try:
        as_of = datetime.strptime(f"{day_raw} {time_raw}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        as_of = _utcnow()

    return Quote(
        ticker=asset.ticker,
        name=asset.name,
        kind=asset.kind,
        price=price.quantize(Decimal("0.0001")),
        currency=asset.currency,
        change_percent=_change_percent(price, open_price),
        source=StooqProvider.name,
        as_of=as_of,
        extra={
            "open": str(open_price) if open_price is not None else "",
            "day_high": (row.get("High") or "").strip(),
            "day_low": (row.get("Low") or "").strip(),
            "volume": (row.get("Volume") or "").strip(),
        },
    )


class CoinGeckoProvider:
    name = "CoinGecko"

    def __init__(self, http: HttpClient, base_url: str, api_key: str | None = None) -> None:
        self.http = http
        self.base_url = base_url
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"x-cg-demo-api-key": self.api_key} if self.api_key else {}

    async def quote(self, asset: Asset) -> Quote:
        if not asset.coingecko_id:
            raise ProviderError(self.name, "нет coin id")
        payload = await self.http.get_json(
            join_url(self.base_url, "/coins/markets"),
            params={
                "vs_currency": "usd",
                "ids": asset.coingecko_id,
                "price_change_percentage": "24h",
            },
            headers=self._headers(),
            provider=self.name,
        )
        return parse_coingecko_quote(payload, asset)

    async def history(self, asset: Asset, days: int = 30) -> History:
        if not asset.coingecko_id:
            raise ProviderError(self.name, "нет coin id")
        payload = await self.http.get_json(
            join_url(self.base_url, f"/coins/{asset.coingecko_id}/market_chart"),
            params={"vs_currency": "usd", "days": str(max(2, days)), "interval": "daily"},
            headers=self._headers(),
            provider=self.name,
        )
        return parse_coingecko_history(payload, asset)


def parse_coingecko_quote(payload: Any, asset: Asset) -> Quote:
    if not isinstance(payload, list) or not payload:
        raise ProviderError("CoinGecko", "монета не найдена")
    item = payload[0]
    try:
        price = _dec(item["current_price"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ProviderError("CoinGecko", "нет цены") from exc
    if price <= 0:
        raise ProviderError("CoinGecko", "некорректная цена")
    change = item.get("price_change_percentage_24h")
    try:
        change_dec = _dec(change) if change is not None else None
    except ValueError:
        change_dec = None
    updated = item.get("last_updated")
    try:
        as_of = (
            datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
            if updated
            else _utcnow()
        )
    except ValueError:
        as_of = _utcnow()
    return Quote(
        ticker=asset.ticker,
        name=asset.name,
        kind=asset.kind,
        price=price.quantize(Decimal("0.0001")),
        currency="USD",
        change_percent=change_dec.quantize(Decimal("0.01")) if change_dec is not None else None,
        source=CoinGeckoProvider.name,
        as_of=as_of,
        extra={
            "market_cap": str(item.get("market_cap") or ""),
            "day_high": str(item.get("high_24h") or ""),
            "day_low": str(item.get("low_24h") or ""),
            "volume": str(item.get("total_volume") or ""),
        },
    )


def parse_coingecko_history(payload: dict[str, Any], asset: Asset) -> History:
    prices = payload.get("prices") if isinstance(payload, dict) else None
    if not isinstance(prices, list):
        raise ProviderError("CoinGecko", "нет истории цен")
    by_day: dict[date, Decimal] = {}
    for row in prices:
        try:
            ts, value = row
            day = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).date()
            by_day[day] = _dec(value)
        except (ValueError, TypeError, IndexError):
            continue
    points = [
        HistoryPoint(day=day, close=value)
        for day, value in sorted(by_day.items())
        if value > 0
    ]
    if len(points) < 2:
        raise ProviderError("CoinGecko", "недостаточно точек истории")
    return History(ticker=asset.ticker, points=points, source=CoinGeckoProvider.name)


class FrankfurterProvider:
    name = "Frankfurter (ECB)"

    def __init__(self, http: HttpClient, base_url: str) -> None:
        self.http = http
        self.base_url = base_url

    async def quote(self, asset: Asset) -> Quote:
        if not asset.fx_base or not asset.fx_quote:
            raise ProviderError(self.name, "не валютная пара")
        payload = await self.http.get_json(
            join_url(self.base_url, "/latest"),
            params={"from": asset.fx_base, "to": asset.fx_quote},
            provider=self.name,
        )
        return parse_frankfurter_quote(payload, asset)

    async def history(self, asset: Asset, days: int = 30) -> History:
        if not asset.fx_base or not asset.fx_quote:
            raise ProviderError(self.name, "не валютная пара")
        start = (datetime.now(timezone.utc).date() - timedelta(days=max(7, days))).isoformat()
        payload = await self.http.get_json(
            join_url(self.base_url, f"/{start}.."),
            params={"from": asset.fx_base, "to": asset.fx_quote},
            provider=self.name,
        )
        return parse_frankfurter_history(payload, asset)


def parse_frankfurter_quote(payload: dict[str, Any], asset: Asset) -> Quote:
    try:
        rate = _dec(payload["rates"][asset.fx_quote])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderError("Frankfurter", "нет курса в ответе") from exc
    day_raw = payload.get("date")
    try:
        as_of = datetime.fromisoformat(str(day_raw)) if day_raw else _utcnow()
    except ValueError:
        as_of = _utcnow()
    return Quote(
        ticker=asset.ticker,
        name=asset.name,
        kind=asset.kind,
        price=rate.quantize(Decimal("0.0001")),
        currency=asset.currency,
        change_percent=None,
        source=FrankfurterProvider.name,
        as_of=as_of,
        extra={"base": asset.fx_base, "quote": asset.fx_quote, "date": str(day_raw or "")},
    )


def parse_frankfurter_history(payload: dict[str, Any], asset: Asset) -> History:
    rates = payload.get("rates") if isinstance(payload, dict) else None
    if not isinstance(rates, dict) or not rates:
        raise ProviderError("Frankfurter", "нет истории курсов")
    points: list[HistoryPoint] = []
    for day_raw, values in rates.items():
        try:
            rate = _dec(values[asset.fx_quote])
            day = date.fromisoformat(day_raw)
        except (KeyError, ValueError, TypeError):
            continue
        if rate > 0:
            points.append(HistoryPoint(day=day, close=rate))
    points.sort(key=lambda point: point.day)
    if len(points) < 2:
        raise ProviderError("Frankfurter", "недостаточно точек истории")
    return History(ticker=asset.ticker, points=points, source=FrankfurterProvider.name)


DEMO_BASE_PRICES: dict[str, float] = {
    "AAPL": 232.0,
    "MSFT": 428.0,
    "GOOGL": 176.0,
    "AMZN": 205.0,
    "NVDA": 141.0,
    "TSLA": 248.0,
    "META": 585.0,
    "NFLX": 765.0,
    "JPM": 245.0,
    "KO": 68.0,
    "BTC": 68400.0,
    "ETH": 2520.0,
    "SOL": 168.0,
    "TON": 5.4,
    "BNB": 590.0,
    "USDRUB": 92.5,
    "EURRUB": 100.2,
    "CNYRUB": 12.8,
}


class DemoProvider:
    """Детерминированные демо-котировки для offline-режима."""

    name = "Демо-данные"

    def _seed(self, ticker: str) -> float:
        # Меняется раз в 5 минут, но одинаково для всех запросов в окне.
        bucket = int(datetime.now(timezone.utc).timestamp() // 300)
        digest = hashlib.sha256(f"{ticker}:{bucket}".encode()).hexdigest()
        return int(digest[:8], 16) / 0xFFFFFFFF

    async def quote(self, asset: Asset) -> Quote:
        return self.quote_sync(asset)

    def quote_sync(self, asset: Asset) -> Quote:
        base = DEMO_BASE_PRICES.get(asset.ticker, 100.0)
        seed = self._seed(asset.ticker)
        wave = math.sin(seed * math.tau)
        price = base * (1 + 0.025 * wave)
        change = wave * 2.4
        return Quote(
            ticker=asset.ticker,
            name=asset.name,
            kind=asset.kind,
            price=Decimal(str(round(price, 4))),
            currency=asset.currency,
            change_percent=Decimal(str(round(change, 2))),
            source=self.name,
            as_of=_utcnow(),
            is_fallback=True,
            extra={"demo": True},
        )

    async def history(self, asset: Asset, days: int = 30) -> History:
        return self.history_sync(asset, days)

    def history_sync(self, asset: Asset, days: int = 30) -> History:
        base = DEMO_BASE_PRICES.get(asset.ticker, 100.0)
        today = datetime.now(timezone.utc).date()
        seed = self._seed(asset.ticker)
        points: list[HistoryPoint] = []
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            x = (days - offset) / max(days, 1)
            value = base * (
                1
                + 0.05 * math.sin(x * math.tau * 1.7 + seed * 6)
                + 0.02 * math.cos(x * math.tau * 3.1 + seed * 2)
            )
            points.append(HistoryPoint(day=day, close=Decimal(str(round(value, 4)))))
        return History(ticker=asset.ticker, points=points, source=self.name, is_fallback=True)
