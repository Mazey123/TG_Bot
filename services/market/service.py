"""Сервис рыночных данных: кэш, retry, fallback и circuit breaker."""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from services.assets import Asset, asset_or_stub, normalize_ticker
from services.cache import CacheService
from services.http import HttpClient
from services.market.base import History, Quote
from services.market.providers import (
    CoinGeckoProvider,
    DemoProvider,
    FrankfurterProvider,
    StooqProvider,
    YahooFinanceProvider,
)

logger = logging.getLogger(__name__)


class MarketService:
    def __init__(self, settings: Settings, http: HttpClient) -> None:
        self.settings = settings
        self.http = http
        self.demo = DemoProvider()
        self.yahoo = YahooFinanceProvider(http, settings.YAHOO_BASE_URL)
        self.stooq = StooqProvider(http, settings.STOOQ_BASE_URL)
        self.coingecko = CoinGeckoProvider(
            http, settings.COINGECKO_BASE_URL, settings.coingecko_key
        )
        self.frankfurter = FrankfurterProvider(http, settings.FRANKFURTER_BASE_URL)
        self._disabled_until: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Публичное API
    # ------------------------------------------------------------------
    async def get_quote(self, session: AsyncSession, ticker: str) -> Quote:
        quotes = await self.get_quotes(session, [ticker])
        return quotes[normalize_ticker(ticker)]

    async def get_quotes(
        self, session: AsyncSession, tickers: list[str]
    ) -> dict[str, Quote]:
        normalized: list[str] = []
        for raw in tickers:
            ticker = normalize_ticker(raw)
            if ticker and ticker not in normalized:
                normalized.append(ticker)

        cache = CacheService(session)
        result: dict[str, Quote] = {}
        misses: list[str] = []

        # Проверки кэша — последовательно: AsyncSession не потокобезопасна.
        for ticker in normalized:
            key = self._quote_key(ticker)
            try:
                payload = await cache.get(key)
            except Exception:
                payload = None
            if payload:
                try:
                    result[ticker] = Quote.from_cache(payload)
                    continue
                except (KeyError, TypeError, ValueError):
                    logger.warning("quote_cache_payload_invalid ticker=%s", ticker)
            misses.append(ticker)

        if misses:
            # Сетевые запросы — параллельно. В БД здесь ничего не пишем.
            fetched = await asyncio.gather(
                *(self._fetch_quote(asset_or_stub(ticker)) for ticker in misses),
                return_exceptions=True,
            )
            for ticker, item in zip(misses, fetched):
                if isinstance(item, Quote):
                    result[ticker] = item
                else:
                    # Последний рубеж: демо-данные, чтобы интерфейс не падал.
                    logger.error(
                        "quote_total_failure ticker=%s error=%s",
                        ticker,
                        type(item).__name__,
                    )
                    result[ticker] = self.demo.quote_sync(asset_or_stub(ticker))

            # Если сеть не ответила, сначала пробуем устаревший кэш.
            if self.settings.MARKET_DATA_MODE != "demo":
                for ticker in misses:
                    quote = result.get(ticker)
                    if quote is None or not quote.is_fallback:
                        continue
                    stale = await self._stale_quote(cache, ticker)
                    if stale is not None:
                        result[ticker] = stale

            # Запись кэша — снова последовательно, уже после gather.
            for ticker in misses:
                quote = result.get(ticker)
                if quote is None:
                    continue
                ttl = self._cache_ttl_for_quote(quote)
                try:
                    await cache.set(self._quote_key(ticker), quote.to_cache(), ttl)
                except Exception:
                    logger.warning("quote_cache_write_failed ticker=%s", ticker)

        return result

    async def get_history(
        self, session: AsyncSession, ticker: str, days: int = 30
    ) -> History:
        ticker = normalize_ticker(ticker)
        asset = asset_or_stub(ticker)
        key = f"history:{ticker}:{days}"
        cache = CacheService(session)
        try:
            payload = await cache.get(key)
            if payload:
                return History.from_cache(payload)
        except (KeyError, TypeError, ValueError):
            logger.warning("history_cache_payload_invalid ticker=%s", ticker)
        except Exception:
            logger.warning("history_cache_read_failed ticker=%s", ticker)

        if self.settings.MARKET_DATA_MODE == "demo":
            history = await self.demo.history(asset, days)
            await self._safe_cache_set(cache, key, history.to_cache())
            return history

        providers = self._history_providers(asset)
        last_error: Exception | None = None
        for provider in providers:
            if not self._is_available(provider.name):
                continue
            try:
                history = await provider.history(asset, days)  # type: ignore[attr-defined]
                await self._safe_cache_set(cache, key, history.to_cache())
                return history
            except Exception as exc:
                last_error = exc
                self._disable_provider(provider.name)
                logger.warning(
                    "history_provider_failed provider=%s ticker=%s error=%s",
                    provider.name,
                    ticker,
                    type(exc).__name__,
                )

        stale = await self._stale_history(cache, key)
        if stale is not None:
            stale.is_fallback = True
            stale.source = f"{stale.source} · кэш"
            return stale

        if last_error:
            logger.warning("history_demo_fallback ticker=%s", ticker)
        history = await self.demo.history(asset, days)
        await self._safe_cache_set(cache, key, history.to_cache())
        return history

    async def close(self) -> None:
        await self.http.close()

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------
    def _quote_key(self, ticker: str) -> str:
        return f"quote:{ticker}"

    def _quote_ttl(self, kind: str) -> int:
        if kind == "fx":
            return self.settings.FX_CACHE_TTL_SECONDS
        return self.settings.QUOTE_CACHE_TTL_SECONDS

    def _cache_ttl_for_quote(self, quote: Quote) -> int:
        # Демо/устаревшие данные кэшируем ненадолго, чтобы быстрее
        # попробовать реальный источник снова.
        return 20 if quote.is_fallback else self._quote_ttl(quote.kind)

    def _quote_providers(self, asset: Asset) -> list[object]:
        if asset.kind == "crypto":
            return [self.coingecko]
        if asset.kind == "fx":
            return [self.frankfurter]
        return [self.yahoo, self.stooq]

    def _history_providers(self, asset: Asset) -> list[object]:
        if asset.kind == "crypto":
            return [self.coingecko]
        if asset.kind == "fx":
            return [self.frankfurter]
        return [self.yahoo]

    async def _fetch_quote(self, asset: Asset) -> Quote:
        if self.settings.MARKET_DATA_MODE == "demo":
            return self.demo.quote_sync(asset)

        for provider in self._quote_providers(asset):
            name = getattr(provider, "name", provider.__class__.__name__)
            if not self._is_available(name):
                continue
            try:
                quote = await provider.quote(asset)  # type: ignore[attr-defined]
                logger.info(
                    "quote_received ticker=%s provider=%s fallback=%s",
                    asset.ticker,
                    quote.source,
                    quote.is_fallback,
                )
                return quote
            except Exception as exc:
                self._disable_provider(name)
                logger.warning(
                    "quote_provider_failed provider=%s ticker=%s error=%s",
                    name,
                    asset.ticker,
                    type(exc).__name__,
                )

        # Последний рубеж — демо-данные. Устаревший кэш подставляется
        # вызывающим кодом после gather (AsyncSession нельзя использовать
        # параллельно из нескольких задач).
        return self.demo.quote_sync(asset)

    async def _stale_quote(self, cache: CacheService, ticker: str) -> Quote | None:
        try:
            payload = await cache.get(
                self._quote_key(ticker),
                allow_stale=True,
                max_stale_seconds=self.settings.STALE_CACHE_MAX_HOURS * 3600,
            )
        except Exception:
            return None
        if not payload:
            return None
        try:
            quote = Quote.from_cache(payload)
        except (KeyError, TypeError, ValueError):
            return None
        quote.is_fallback = True
        quote.source = f"{quote.source} · кэш"
        return quote

    async def _stale_history(self, cache: CacheService, key: str) -> History | None:
        try:
            payload = await cache.get(
                key,
                allow_stale=True,
                max_stale_seconds=self.settings.STALE_CACHE_MAX_HOURS * 3600,
            )
        except Exception:
            return None
        if not payload:
            return None
        try:
            return History.from_cache(payload)
        except (KeyError, TypeError, ValueError):
            return None

    async def _safe_cache_set(self, cache: CacheService, key: str, payload: dict) -> None:
        try:
            await cache.set(key, payload, self.settings.HISTORY_CACHE_TTL_SECONDS)
        except Exception:
            logger.warning("history_cache_write_failed key=%s", key)

    def _is_available(self, provider_name: str) -> bool:
        return time.monotonic() >= self._disabled_until.get(provider_name, 0.0)

    def _disable_provider(self, provider_name: str) -> None:
        self._disabled_until[provider_name] = (
            time.monotonic() + self.settings.PROVIDER_COOLDOWN_SECONDS
        )
