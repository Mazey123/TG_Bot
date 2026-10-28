"""Расчёт стоимости портфеля по актуальным котировкам."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from database.repo import PortfolioRepository
from services.market.base import Quote
from services.market.service import MarketService
from services.trading import money


@dataclass(slots=True)
class PositionView:
    ticker: str
    quantity: Decimal
    avg_price: Decimal
    current_price: Decimal
    value: Decimal
    cost: Decimal
    pnl: Decimal
    pnl_percent: Decimal
    quote: Quote


@dataclass(slots=True)
class PortfolioSnapshot:
    cash: Decimal
    positions_value: Decimal
    net_worth: Decimal
    invested: Decimal
    unrealized_pnl: Decimal
    positions: list[PositionView] = field(default_factory=list)
    has_fallback: bool = False


class PortfolioService:
    def __init__(self, market: MarketService) -> None:
        self.market = market

    async def snapshot(self, session: AsyncSession, user: User) -> PortfolioSnapshot:
        positions = await PortfolioRepository.list_positions(session, user.id)
        cash = money(user.balance)
        if not positions:
            return PortfolioSnapshot(
                cash=cash,
                positions_value=Decimal("0.00"),
                net_worth=cash,
                invested=Decimal("0.00"),
                unrealized_pnl=Decimal("0.00"),
            )

        quotes = await self.market.get_quotes(session, [p.ticker for p in positions])
        views: list[PositionView] = []
        positions_value = Decimal("0")
        invested = Decimal("0")
        has_fallback = False
        for position in positions:
            quote = quotes.get(position.ticker)
            if quote is None:
                continue
            has_fallback = has_fallback or quote.is_fallback
            qty = Decimal(position.quantity)
            avg = Decimal(position.avg_buy_price)
            current = Decimal(quote.price)
            value = money(qty * current)
            cost = money(qty * avg)
            pnl = money(value - cost)
            pnl_percent = (
                ((current - avg) / avg * 100).quantize(Decimal("0.01"))
                if avg > 0
                else Decimal("0.00")
            )
            positions_value += value
            invested += cost
            views.append(
                PositionView(
                    ticker=position.ticker,
                    quantity=qty,
                    avg_price=avg,
                    current_price=current,
                    value=value,
                    cost=cost,
                    pnl=pnl,
                    pnl_percent=pnl_percent,
                    quote=quote,
                )
            )

        positions_value = money(positions_value)
        invested = money(invested)
        return PortfolioSnapshot(
            cash=cash,
            positions_value=positions_value,
            net_worth=money(cash + positions_value),
            invested=invested,
            unrealized_pnl=money(positions_value - invested),
            positions=views,
            has_fallback=has_fallback,
        )
