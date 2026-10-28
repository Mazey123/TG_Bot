"""Рынок: списки инструментов, котировки, поиск и графики."""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from database.models import User
from database.repo import PortfolioRepository
from keyboards.inline import (
    asset_detail,
    assets_list,
    back_menu_only,
    back_to_market,
    chart_keyboard,
    market_menu,
)
from services.assets import KIND_TITLES, asset_or_stub, assets_by_kind, get_asset, normalize_ticker
from services.chart import ChartService
from services.market.base import Quote
from services.market.service import MarketService
from utils.formatting import (
    as_of,
    change_percent,
    escape,
    money,
    price,
    quantity,
    source_line,
    trend_emoji,
)
from utils.security import looks_like_ticker
from utils.telegram import safe_answer, safe_edit

router = Router(name="market")
logger = logging.getLogger(__name__)


class MarketStates(StatesGroup):
    waiting_ticker = State()


@router.message(Command("price"))
async def cmd_price(
    message: Message,
    command: CommandObject,
    user: User,
    session,
    market: MarketService,
    state: FSMContext,
) -> None:
    await state.clear()
    raw = (command.args or "").strip()
    if not raw:
        await state.set_state(MarketStates.waiting_ticker)
        await message.answer(
            "🔎 <b>Поиск инструмента</b>\n\n"
            "Введите тикер, например <code>AAPL</code>, <code>BTC</code> "
            "или <code>USDRUB</code>.\n"
            "Доступные тикеры: /market",
            reply_markup=back_menu_only(),
            parse_mode="HTML",
        )
        return
    await _send_quote(message, raw, user, session, market)


@router.message(Command("market"))
async def cmd_market(message: Message) -> None:
    await message.answer(
        "📈 <b>Рынок</b>\n\nВыберите класс активов. Все цены — "
        "виртуальные, торговля не затрагивает реальные деньги.",
        reply_markup=market_menu(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "mkt:menu")
async def cb_market_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(
        callback,
        "📈 <b>Рынок</b>\n\n"
        "🇺🇸 <b>Акции</b> — 10 крупных компаний США\n"
        "🪙 <b>Крипта</b> — Bitcoin, Ethereum, Solana и другие\n"
        "💱 <b>Валюты</b> — курсы ЦБ через Frankfurter\n\n"
        "Выберите класс активов:",
        market_menu(),
    )
    await safe_answer(callback)


@router.callback_query(F.data.startswith("mkt:list:"))
async def cb_market_list(
    callback: CallbackQuery,
    session,
    market: MarketService,
) -> None:
    kind = (callback.data or "").split(":")[-1]
    if kind not in {"stock", "crypto", "fx"}:
        await safe_answer(callback, "Неизвестный раздел", alert=True)
        return
    assets = assets_by_kind(kind)
    quotes = await market.get_quotes(session, [asset.ticker for asset in assets])
    fallback = any(quote.is_fallback for quote in quotes.values())
    movers = sorted(
        (quote for quote in quotes.values() if quote.change_percent is not None),
        key=lambda quote: abs(Decimal(quote.change_percent or 0)),
        reverse=True,
    )[:3]
    lines = [f"<b>{KIND_TITLES[kind]}</b>\n"]
    for quote in movers:
        lines.append(
            f"{trend_emoji(quote.change_percent)} <b>{quote.ticker}</b> "
            f"{price(quote.price, quote.currency)} · {change_percent(quote.change_percent)}"
        )
    lines.append("\nНажмите на инструмент, чтобы открыть карточку:")
    if fallback:
        lines.append(
            "\n⚠️ <i>Часть данных — резервные (API недоступен или включён demo-режим).</i>"
        )
    await safe_edit(
        callback,
        "\n".join(lines),
        assets_list(assets, quotes),
    )
    await safe_answer(callback)


@router.callback_query(F.data.startswith("mkt:view:"))
async def cb_view_asset(
    callback: CallbackQuery,
    user: User,
    session,
    market: MarketService,
) -> None:
    ticker = (callback.data or "").split(":", 2)[-1]
    if get_asset(ticker) is None:
        await safe_answer(callback, "Инструмент не найден", alert=True)
        return
    quote, text = await _quote_card(ticker, user, session, market)
    await safe_edit(
        callback,
        text,
        asset_detail(
            quote.ticker,
            in_portfolio=quote.extra.get("in_portfolio", False),
            tradable=get_asset(quote.ticker).kind != "fx",
        ),
    )
    await safe_answer(callback)


@router.callback_query(F.data.startswith("mkt:chart:"))
async def cb_chart(
    callback: CallbackQuery,
    session,
    market: MarketService,
) -> None:
    ticker = (callback.data or "").split(":", 2)[-1]
    asset = get_asset(ticker)
    if asset is None:
        await safe_answer(callback, "Инструмент не найден", alert=True)
        return
    await safe_answer(callback, "Строю график…")
    try:
        quote = await market.get_quote(session, ticker)
        history = await market.get_history(session, ticker, 30)
        low, high, change = ChartService.summary(history)
        chart_url = ChartService.url_for(history, title=asset.name)
        caption = (
            f"📈 <b>{escape(asset.name)}</b> · {asset.ticker}\n"
            f"Текущая цена: <b>{price(quote.price, quote.currency)}</b>\n"
            f"За 30 дней: <b>{change:+.2f}%</b>\n"
            f"Диапазон: {price(low, quote.currency)} — {price(high, quote.currency)}\n\n"
            f"{source_line(quote)}"
        )
        await callback.message.answer_photo(  # type: ignore[union-attr]
            photo=chart_url,
            caption=caption[:1024],
            reply_markup=chart_keyboard(asset.ticker),
            parse_mode="HTML",
        )
    except (TelegramBadRequest, ValueError) as exc:
        logger.warning("chart_failed ticker=%s error=%s", ticker, type(exc).__name__)
        try:
            history = await market.get_history(session, ticker, 30)
            low, high, change = ChartService.summary(history)
            text = (
                f"📉 Не удалось отправить изображение.\n\n"
                f"<b>{escape(asset.name)}</b> · {ticker}\n"
                f"Минимум: {price(low)} · максимум: {price(high)}\n"
                f"Изменение за 30 дней: <b>{change:+.2f}%</b>"
            )
        except Exception:
            text = "⚠️ График временно недоступен."
        await safe_edit(callback, text, chart_keyboard(asset.ticker))


@router.callback_query(F.data == "mkt:search")
async def cb_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MarketStates.waiting_ticker)
    await safe_edit(
        callback,
        "🔎 <b>Поиск инструмента</b>\n\n"
        "Введите тикер: <code>AAPL</code>, <code>BTC</code>, "
        "<code>USDRUB</code>…",
        back_to_market(),
    )
    await safe_answer(callback)


@router.message(MarketStates.waiting_ticker, F.text, ~F.text.startswith("/"))
async def msg_search(
    message: Message,
    user: User,
    session,
    market: MarketService,
    state: FSMContext,
) -> None:
    raw = (message.text or "").strip()
    if not looks_like_ticker(raw) and "/" not in raw:
        await message.answer(
            "⚠️ Некорректный тикер. Используйте 1–10 латинских букв или цифр.",
            reply_markup=back_menu_only(),
        )
        return
    await state.clear()
    await _send_quote(message, raw, user, session, market)


async def _send_quote(
    message: Message,
    raw_ticker: str,
    user: User,
    session,
    market: MarketService,
) -> None:
    ticker = normalize_ticker(raw_ticker)
    if get_asset(ticker) is None:
        await message.answer(
            f"🔎 Тикер <code>{escape(raw_ticker[:12])}</code> не найден в справочнике.\n"
            "Посмотрите список: /market",
            reply_markup=back_to_market(),
            parse_mode="HTML",
        )
        return
    quote, text = await _quote_card(ticker, user, session, market)
    asset = get_asset(ticker) or asset_or_stub(ticker)
    await message.answer(
        text,
        reply_markup=asset_detail(
            quote.ticker,
            in_portfolio=quote.extra.get("in_portfolio", False),
            tradable=asset.kind != "fx",
        ),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def _quote_card(
    ticker: str,
    user: User,
    session,
    market: MarketService,
) -> tuple[Quote, str]:
    asset = asset_or_stub(ticker)
    quote = await market.get_quote(session, ticker)
    position = await PortfolioRepository.get_position(session, user.id, quote.ticker)
    quote.extra["in_portfolio"] = position is not None

    lines = [
        f"{asset.emoji} <b>{escape(asset.name)}</b> · <code>{asset.ticker}</code>",
        "",
        f"Цена: <b>{price(quote.price, quote.currency)}</b>",
        f"Изменение: <b>{change_percent(quote.change_percent)}</b>",
    ]
    high = quote.extra.get("day_high")
    low = quote.extra.get("day_low")
    if high and low:
        try:
            lines.append(f"День: {price(low, quote.currency)} — {price(high, quote.currency)}")
        except Exception:
            pass

    if position is not None:
        qty = Decimal(position.quantity)
        avg = Decimal(position.avg_buy_price)
        pnl = (Decimal(quote.price) - avg) * qty
        lines.extend(
            [
                "",
                "💼 <b>В портфеле</b>",
                f"{quantity(qty)} шт. · средняя {price(avg, quote.currency)}",
                f"Нереализованный PnL: <b>{'+' if pnl >= 0 else ''}{money(pnl)}</b>",
            ]
        )

    lines.extend(
        [
            "",
            f"{source_line(quote)} · {as_of(quote.as_of)}",
        ]
    )
    if quote.is_fallback:
        lines.append(
            "⚠️ <i>Цена может быть устаревшей. Сделки в симуляторе выполняются "
            "по этой цене.</i>"
        )
    return quote, "\n".join(lines)
