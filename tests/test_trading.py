from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from config.settings import Settings
from database.models import Badge, Base, Portfolio, Transaction, User
from database.repo import UserRepository
from services.market.base import Quote
from services.trading import (
    InsufficientFundsError,
    InsufficientPositionError,
    TradeValidationError,
    TradingService,
    validate_quantity,
)


def test_settings() -> Settings:
    return Settings(
        TELEGRAM_TOKEN="123456789:test_token",
        STARTING_BALANCE=Decimal("10000.00"),
        COMMISSION_RATE=Decimal("0.002"),
    )


def quote(price: str = "100", ticker: str = "AAPL") -> Quote:
    return Quote(
        ticker=ticker,
        name="Apple Inc.",
        kind="stock",
        price=Decimal(price),
        currency="USD",
        change_percent=Decimal("1.0"),
        source="test",
        as_of=datetime.now(timezone.utc).replace(tzinfo=None),
    )


class TradingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = test_settings()
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.factory()
        self.user, _ = await UserRepository.get_or_create(
            self.session,
            user_id=1001,
            username="tester",
            first_name="Тест",
            starting_balance=self.settings.STARTING_BALANCE,
        )
        await self.session.commit()
        self.service = TradingService(self.settings)

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def test_buy_updates_balance_position_and_history(self) -> None:
        result = await self.service.execute(
            self.session,
            user_id=self.user.id,
            side="buy",
            ticker="AAPL",
            quantity="2",
            quote=quote("100"),
        )
        self.assertEqual(result.new_balance, Decimal("9799.60"))
        self.assertEqual(result.fee, Decimal("0.40"))
        self.assertEqual(result.cash_flow, Decimal("-200.40"))
        self.assertIn("FIRST_TRADE", result.new_badges)

        await self.session.refresh(self.user)
        self.assertEqual(Decimal(self.user.balance), Decimal("9799.60"))
        position = (
            await self.session.execute(select(Portfolio).where(Portfolio.user_id == 1001))
        ).scalar_one()
        self.assertEqual(Decimal(position.quantity), Decimal("2"))
        self.assertEqual(Decimal(position.avg_buy_price), Decimal("100"))
        transaction = (
            await self.session.execute(select(Transaction).where(Transaction.user_id == 1001))
        ).scalar_one()
        self.assertEqual(transaction.side, "buy")
        self.assertEqual(Decimal(transaction.total_amount), Decimal("200"))
        badges = (
            await self.session.execute(select(Badge).where(Badge.user_id == 1001))
        ).scalars().all()
        self.assertEqual([badge.badge_code for badge in badges], ["FIRST_TRADE"])
        self.assertGreater(self.user.xp, 0)

    async def test_sell_realizes_pnl(self) -> None:
        await self.service.execute(
            self.session,
            user_id=self.user.id,
            side="buy",
            ticker="AAPL",
            quantity="2",
            quote=quote("100"),
        )
        result = await self.service.execute(
            self.session,
            user_id=self.user.id,
            side="sell",
            ticker="AAPL",
            quantity="2",
            quote=quote("120"),
        )
        self.assertEqual(result.realized_pnl, Decimal("39.52"))
        self.assertEqual(result.new_balance, Decimal("10039.12"))
        self.assertIn("FIRST_PROFIT", result.new_badges)
        position = (
            await self.session.execute(select(Portfolio).where(Portfolio.user_id == 1001))
        ).scalar_one_or_none()
        self.assertIsNone(position)

    async def test_insufficient_funds_rolls_back(self) -> None:
        with self.assertRaises(InsufficientFundsError):
            await self.service.execute(
                self.session,
                user_id=self.user.id,
                side="buy",
                ticker="AAPL",
                quantity="1000",
                quote=quote("100"),
            )
        await self.session.refresh(self.user)
        self.assertEqual(Decimal(self.user.balance), Decimal("10000.00"))
        positions = (
            await self.session.execute(select(Portfolio).where(Portfolio.user_id == 1001))
        ).scalars().all()
        self.assertEqual(positions, [])

    async def test_cannot_sell_more_than_owned(self) -> None:
        await self.service.execute(
            self.session,
            user_id=self.user.id,
            side="buy",
            ticker="AAPL",
            quantity="1",
            quote=quote("100"),
        )
        with self.assertRaises(InsufficientPositionError):
            await self.service.execute(
                self.session,
                user_id=self.user.id,
                side="sell",
                ticker="AAPL",
                quantity="2",
                quote=quote("110"),
            )

    async def test_invalid_quantity(self) -> None:
        for raw in ("0", "-5", "abc", "1.1234567", "1000001"):
            with self.subTest(raw=raw):
                with self.assertRaises(TradeValidationError):
                    validate_quantity(raw)

    async def test_fx_is_read_only(self) -> None:
        with self.assertRaises(TradeValidationError):
            await self.service.execute(
                self.session,
                user_id=self.user.id,
                side="buy",
                ticker="USDRUB",
                quantity="1",
                quote=quote("92.5", ticker="USDRUB"),
            )


if __name__ == "__main__":
    unittest.main()
