from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from config.settings import Settings
from database.models import Base, CacheEntry
from services.cache import CacheService, MemoryTTLCache, memory_cache
from services.http import HttpClient
from services.market.base import ProviderError, Quote
from services.market.service import MarketService


class MarketFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        memory_cache.clear()
        self.settings = Settings(
            TELEGRAM_TOKEN="123456789:test_token",
            MARKET_DATA_MODE="auto",
            HTTP_RETRIES=1,
            HTTP_TIMEOUT_SECONDS=1,
        )
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.http = HttpClient(timeout_seconds=1, retries=1)
        self.market = MarketService(self.settings, self.http)

        async def fail(asset):
            raise ProviderError("test", "offline")

        self.market.yahoo.quote = fail  # type: ignore[method-assign]
        self.market.stooq.quote = fail  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.market.close()
        await self.engine.dispose()

    async def test_demo_fallback_when_all_providers_fail(self) -> None:
        quotes = await self.market.get_quotes(self.session, ["AAPL"])
        quote = quotes["AAPL"]
        self.assertTrue(quote.is_fallback)
        self.assertEqual(quote.source, "Демо-данные")

    async def test_stale_cache_is_preferred_over_demo(self) -> None:
        cached = Quote(
            ticker="AAPL",
            name="Apple Inc.",
            kind="stock",
            price=Decimal("222.22"),
            currency="USD",
            change_percent=Decimal("0.5"),
            source="Yahoo Finance",
            as_of=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        cache = CacheService(self.session, MemoryTTLCache())
        await cache.set("quote:AAPL", cached.to_cache(), ttl_seconds=60)
        # Делаем запись истёкшей, но не старше разрешённого stale-окна.
        await self.session.execute(
            update(CacheEntry)
            .where(CacheEntry.key == "quote:AAPL")
            .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await self.session.commit()

        quotes = await self.market.get_quotes(self.session, ["AAPL"])
        quote = quotes["AAPL"]
        self.assertTrue(quote.is_fallback)
        self.assertIn("кэш", quote.source)
        self.assertEqual(quote.price, Decimal("222.22"))


if __name__ == "__main__":
    unittest.main()
