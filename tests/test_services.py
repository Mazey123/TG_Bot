from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from pydantic import ValidationError

from config.settings import Settings
from database.models import Base, User
from database.repo import UserRepository
from services.cache import CacheService, MemoryTTLCache
from services.gamification import claim_daily_bonus, level_from_xp, level_progress
from services.quiz import CATEGORIES, QUESTION_LIMIT, QuizService


def test_settings() -> Settings:
    return Settings(
        TELEGRAM_TOKEN="123456789:test_token",
        STARTING_BALANCE=Decimal("10000.00"),
        DAILY_BONUS=Decimal("250.00"),
    )


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.factory()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def test_cache_persists_in_database(self) -> None:
        first_memory = MemoryTTLCache()
        first = CacheService(self.session, first_memory)
        await first.set("quote:AAPL", {"price": "232.5"}, ttl_seconds=60)

        # Новая «память» имитирует перезапуск процесса: значение читается из БД.
        second = CacheService(self.session, MemoryTTLCache())
        value = await second.get("quote:AAPL")
        self.assertEqual(value, {"price": "232.5"})

    async def test_memory_ttl_expiry(self) -> None:
        memory = MemoryTTLCache()
        memory.set("key", {"x": 1}, ttl_seconds=1)
        self.assertEqual(memory.get("key")[0], {"x": 1})
        # Вручную сдвигаем expiry, не ожидая секунду в тесте.
        memory._items["key"].expires_at = 0
        self.assertIsNone(memory.get("key")[0])


class GamificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = test_settings()
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.factory()
        self.user, _ = await UserRepository.get_or_create(
            self.session, 2002, "bonus", "Бонус", self.settings.STARTING_BALANCE
        )
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    def test_levels(self) -> None:
        self.assertEqual(level_from_xp(0), 1)
        self.assertEqual(level_from_xp(100), 2)
        self.assertEqual(level_from_xp(399), 2)
        self.assertEqual(level_from_xp(400), 3)
        current, need, percent = level_progress(150, 2)
        self.assertEqual(current, 50)
        self.assertEqual(need, 300)
        self.assertEqual(percent, 16)

    async def test_daily_bonus_once(self) -> None:
        first = await claim_daily_bonus(self.session, self.user, self.settings)
        self.assertTrue(first.granted)
        self.assertEqual(first.amount, Decimal("250.00"))
        self.assertEqual(self.user.daily_streak, 1)
        second = await claim_daily_bonus(self.session, self.user, self.settings)
        self.assertFalse(second.granted)
        self.assertIsNotNone(second.next_available_at)


class QuizTests(unittest.TestCase):
    def test_every_category_has_questions(self) -> None:
        rng = random.Random(42)
        for category in CATEGORIES:
            questions = QuizService.questions_for(category, rng)
            self.assertEqual(len(questions), QUESTION_LIMIT)
            self.assertEqual(len({q.id for q in questions}), QUESTION_LIMIT)
            for question in questions:
                self.assertEqual(len(question.options), 4)
                self.assertIn(question.correct_index, range(4))

    def test_scoring_and_xp(self) -> None:
        self.assertEqual(QuizService.score([True, False, True]), 2)
        self.assertEqual(QuizService.xp_for(5, 5), 100)
        self.assertEqual(QuizService.xp_for(3, 5), 45)
        self.assertEqual(QuizService.xp_for(0, 0), 0)


class ProxySettingsTests(unittest.TestCase):
    def test_supported_proxy_schemes(self) -> None:
        for proxy in (
            "socks5://127.0.0.1:1080",
            "socks4://127.0.0.1:1080",
            "http://user:pass@127.0.0.1:8080",
        ):
            with self.subTest(proxy=proxy):
                settings = Settings(TELEGRAM_TOKEN="123456789:test", PROXY_URL=proxy)
                self.assertEqual(settings.PROXY_URL, proxy)

    def test_mtproto_proxy_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(TELEGRAM_TOKEN="123456789:test", PROXY_URL="mtproto://host:443")

    def test_proxy_requires_host_and_port(self) -> None:
        for proxy in ("socks5://", "socks5://127.0.0.1", "http://:8080"):
            with self.subTest(proxy=proxy):
                with self.assertRaises(ValidationError):
                    Settings(TELEGRAM_TOKEN="123456789:test", PROXY_URL=proxy)

    def test_empty_proxy_is_none(self) -> None:
        settings = Settings(TELEGRAM_TOKEN="123456789:test", PROXY_URL="")
        self.assertIsNone(settings.PROXY_URL)


if __name__ == "__main__":
    unittest.main()
