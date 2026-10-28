"""FSM-сценарий покупки и продажи: тикер → количество → подтверждение."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config.settings import settings
from database.models import User
from database.repo import PortfolioRepository
from keyboards.inline import (
    main_menu,
    quantity_keyboard,
    trade_asset_picker,
    trade_confirm,
    trade_result,
)
from services.assets import ASSETS, get_asset, normalize_ticker, tradable_assets
from services.gamification import BADGES
from services.market.service import MarketService
from services.trading import (
    TradeResult,
    TradeValidationError,
    TradingService,
    money,
    validate_quantity,
)
from utils.formatting import change_percent, quantity as fmt_qty, source_line
from utils.security import looks_like_ticker
from utils.telegram import safe_answer, safe_edit

router = Router(name="trade")
logger = logging.getLogger(__name__)
_confirm_locks: dict[int, asyncio.Lock] = {}


class TradeStates(StatesGroup):
    waiting_ticker = State()
    waiting_quantity = State()
    confirming = State()


@router.message(Command("buy"))
@router.message(Command("sell"))
async def cmd_trade(
    message: Message,
    command: CommandObject,
    user: User,
    session,
    market: MarketService,
    state: FSMContext,
) -> None:
    side = "buy" if (message.text or "").lower().startswith("/buy") else "sell"
    await state.clear()
    args = (command.args or "").strip().split()
    if not args:
        if side == "sell":
            await _show_sell_picker(message, user, session, state)
        else:
            await state.set_state(TradeStates.waiting_ticker)
            await state.update_data(side="buy")
            await message.answer(
                "🟢 <b>Покупка</b>\n\nВыберите инструмент или введите тикер вручную:",
                reply_markup=trade_asset_picker("buy", tradable_assets()),
                parse_mode="HTML",
            )
        return

    raw_ticker = args[0]
    if get_asset(raw_ticker) is None:
        await message.answer(
            "⚠️ Этот тикер не торгуется в симуляторе. Список: /market",
            reply_markup=main_menu(),
        )
        return
    ticker = normalize_ticker(raw_ticker)
    if get_asset(ticker).kind == "fx":
        await message.answer(
            "💱 Валютные пары доступны только как справочные котировки, "
            "торговля по ним не проводится.",
            reply_markup=main_menu(),
        )
        return
    if len(args) > 2:
        await message.answer("Формат: <code>/buy AAPL 2</code>", parse_mode="HTML")
        return
    if side == "sell":
        position = await PortfolioRepository.get_position(session, user.id, ticker)
        if position is None:
            await message.answer(
                f"У вас нет позиции по {ticker}.",
                reply_markup=main_menu(),
            )
            return

    if len(args) == 2:
        try:
            qty = validate_quantity(args[1])
        except TradeValidationError as exc:
            await message.answer(f"⚠️ {exc}")
            return
        await state.set_state(TradeStates.confirming)
        await state.update_data(side=side, ticker=ticker, quantity=str(qty))
        await _send_confirmation_message(message, side, ticker, qty, user, session, market, state)
        return

    await state.set_state(TradeStates.waiting_quantity)
    await state.update_data(side=side, ticker=ticker, custom=True)
    await _send_quantity_prompt(message, side, ticker, user, session, market, state)


@router.callback_query(F.data.startswith("trade:start:"))
async def cb_trade_start(
    callback: CallbackQuery,
    user: User,
    session,
    state: FSMContext,
) -> None:
    side = (callback.data or "").split(":")[-1]
    if side not in {"buy", "sell"}:
        await safe_answer(callback, "Неизвестная операция", alert=True)
        return
    await state.clear()
    if side == "sell":
        positions = await PortfolioRepository.list_positions(session, user.id)
        if not positions:
            await safe_answer(callback, "Портфель пуст — сначала купите актив.", alert=True)
            return
        assets = [ASSETS[p.ticker] for p in positions if p.ticker in ASSETS]
        await state.set_state(TradeStates.waiting_ticker)
        await state.update_data(side="sell")
        await safe_edit(
            callback,
            "🔴 <b>Продажа</b>\n\nВыберите позицию из портфеля:",
            trade_asset_picker("sell", assets),
        )
        await safe_answer(callback)
        return

    await state.set_state(TradeStates.waiting_ticker)
    await state.update_data(side="buy")
    await safe_edit(
        callback,
        "🟢 <b>Покупка</b>\n\nВыберите инструмент или введите тикер вручную:",
        trade_asset_picker("buy", tradable_assets()),
    )
    await safe_answer(callback)


@router.callback_query(F.data.startswith("trade:pick:"))
async def cb_trade_pick(
    callback: CallbackQuery,
    user: User,
    session,
    market: MarketService,
    state: FSMContext,
) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await safe_answer(callback, "Некорректная команда", alert=True)
        return
    _, _, side, raw_ticker = parts
    if side not in {"buy", "sell"} or get_asset(raw_ticker) is None:
        await safe_answer(callback, "Инструмент не найден", alert=True)
        return
    ticker = normalize_ticker(raw_ticker)
    if get_asset(ticker).kind == "fx":
        await safe_answer(
            callback, "Валютные пары — только справочные котировки.", alert=True
        )
        return
    if side == "sell":
        position = await PortfolioRepository.get_position(session, user.id, ticker)
        if position is None:
            await safe_answer(callback, f"Нет позиции по {ticker}", alert=True)
            return

    await state.set_state(TradeStates.waiting_quantity)
    await state.update_data(side=side, ticker=ticker, custom=False)
    await _edit_quantity_prompt(callback, side, ticker, user, session, market, state)
    await safe_answer(callback)


@router.callback_query(F.data == "trade:manual")
async def cb_manual_ticker(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    side = data.get("side", "buy")
    await state.set_state(TradeStates.waiting_ticker)
    await state.update_data(side=side)
    await safe_edit(
        callback,
        "⌨️ Введите тикер (например, <code>AAPL</code>):",
        main_menu(),
    )
    await safe_answer(callback)


@router.message(TradeStates.waiting_ticker, F.text, ~F.text.startswith("/"))
async def msg_ticker(
    message: Message,
    user: User,
    session,
    market: MarketService,
    state: FSMContext,
) -> None:
    raw = (message.text or "").strip()
    if not looks_like_ticker(raw) and "/" not in raw:
        await message.answer(
            "⚠️ Тикер должен состоять из 1–10 латинских букв или цифр.",
            reply_markup=main_menu(),
        )
        return
    ticker = normalize_ticker(raw)
    if get_asset(ticker) is None:
        await message.answer(
            "⚠️ Такого инструмента нет в симуляторе. Посмотрите /market.",
            reply_markup=main_menu(),
        )
        return
    if get_asset(ticker).kind == "fx":
        await message.answer(
            "💱 Валютные пары нельзя купить или продать в этом симуляторе.",
            reply_markup=main_menu(),
        )
        return
    data = await state.get_data()
    side = data.get("side", "buy")
    if side == "sell":
        position = await PortfolioRepository.get_position(session, user.id, ticker)
        if position is None:
            await message.answer(
                f"У вас нет позиции по {ticker}.", reply_markup=main_menu()
            )
            return
    await state.set_state(TradeStates.waiting_quantity)
    await state.update_data(side=side, ticker=ticker, custom=True)
    await _send_quantity_prompt(message, side, ticker, user, session, market, state)


@router.callback_query(F.data.startswith("trade:qty:"))
async def cb_quantity(
    callback: CallbackQuery,
    user: User,
    session,
    market: MarketService,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    side = data.get("side")
    ticker = data.get("ticker")
    if side not in {"buy", "sell"} or not ticker:
        await safe_answer(callback, "Начните заново из меню.", alert=True)
        return
    raw = (callback.data or "").split(":")[-1]
    if raw == "custom":
        await state.set_state(TradeStates.waiting_quantity)
        await safe_edit(
            callback,
            f"⌨️ Введите количество <b>{ticker}</b> (можно дробное, до 6 знаков):",
            quantity_keyboard(side, ticker),
        )
        await safe_answer(callback)
        return

    if raw == "all":
        if side != "sell":
            await safe_answer(callback, "«Продать всё» доступно только для продажи", alert=True)
            return
        position = await PortfolioRepository.get_position(session, user.id, ticker)
        if position is None:
            await safe_answer(callback, "Позиция не найдена", alert=True)
            return
        qty = Decimal(position.quantity)
    else:
        try:
            qty = validate_quantity(raw)
        except TradeValidationError as exc:
            await safe_answer(callback, str(exc), alert=True)
            return

    error = await _validate_holding(session, user.id, side, ticker, qty)
    if error:
        await safe_answer(callback, error, alert=True)
        return

    await state.set_state(TradeStates.confirming)
    await state.update_data(quantity=str(qty))
    await _edit_confirmation(callback, side, ticker, qty, user, session, market)
    await safe_answer(callback)


@router.message(TradeStates.waiting_quantity, F.text, ~F.text.startswith("/"))
async def msg_quantity(
    message: Message,
    user: User,
    session,
    market: MarketService,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    side = data.get("side")
    ticker = data.get("ticker")
    if side not in {"buy", "sell"} or not ticker:
        await state.clear()
        await message.answer("Сессия истекла. Начните заново.", reply_markup=main_menu())
        return
    try:
        qty = validate_quantity(message.text or "")
    except TradeValidationError as exc:
        await message.answer(f"⚠️ {exc}\nПопробуйте ещё раз.")
        return
    error = await _validate_holding(session, user.id, side, ticker, qty)
    if error:
        await message.answer(f"⚠️ {error}")
        return
    await state.set_state(TradeStates.confirming)
    await state.update_data(quantity=str(qty))
    await _send_confirmation_message(message, side, ticker, qty, user, session, market, state)


@router.callback_query(F.data == "trade:confirm")
async def cb_confirm(
    callback: CallbackQuery,
    user: User,
    session,
    market: MarketService,
    trading: TradingService,
    state: FSMContext,
) -> None:
    lock = _confirm_locks.get(user.id)
    if lock is None:
        lock = asyncio.Lock()
        _confirm_locks[user.id] = lock
    # Защита от двойного клика: второй callback дождётся первого,
    # увидит очищенное состояние и не выполнит сделку повторно.
    async with lock:
        data = await state.get_data()
        side = data.get("side")
        ticker = data.get("ticker")
        raw_qty = data.get("quantity")
        if side not in {"buy", "sell"} or not ticker or not raw_qty:
            await state.clear()
            await safe_answer(callback, "Сессия истекла, начните заново.", alert=True)
            return
        await state.clear()
        try:
            quote = await market.get_quote(session, ticker)
            result = await trading.execute(
                session,
                user_id=user.id,
                side=side,
                ticker=ticker,
                quantity=raw_qty,
                quote=quote,
            )
        except TradeValidationError as exc:
            await safe_answer(callback, str(exc), alert=True)
            await safe_edit(callback, f"⚠️ <b>Сделка отклонена</b>\n\n{exc}", main_menu())
            return
        except Exception as exc:
            logger.error("trade_failed ticker=%s error=%s", ticker, type(exc).__name__)
            await safe_answer(callback, "Ошибка сделки. Попробуйте позже.", alert=True)
            return

    await safe_edit(callback, _result_text(result, quote.is_fallback), trade_result(result.ticker))
    await safe_answer(callback, "Готово!")


@router.callback_query(F.data == "trade:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(
        callback,
        "🏠 <b>Главное меню</b>\n\nСделка отменена.",
        main_menu(),
    )
    await safe_answer(callback)


async def _validate_holding(
    session, user_id: int, side: str, ticker: str, qty: Decimal
) -> str | None:
    if side != "sell":
        return None
    position = await PortfolioRepository.get_position(session, user_id, ticker)
    if position is None:
        return f"У вас нет позиции по {ticker}."
    if qty > Decimal(position.quantity):
        return f"В портфеле только {fmt_qty(position.quantity)} {ticker}."
    return None


async def _show_sell_picker(message: Message, user: User, session, state: FSMContext) -> None:
    positions = await PortfolioRepository.list_positions(session, user.id)
    if not positions:
        await message.answer(
            "💼 Портфель пуст — сначала купите актив.",
            reply_markup=main_menu(),
        )
        return
    assets = [ASSETS[p.ticker] for p in positions if p.ticker in ASSETS]
    await state.set_state(TradeStates.waiting_ticker)
    await state.update_data(side="sell")
    await message.answer(
        "🔴 <b>Продажа</b>\n\nВыберите позицию:",
        reply_markup=trade_asset_picker("sell", assets),
        parse_mode="HTML",
    )


async def _send_quantity_prompt(
    message: Message,
    side: str,
    ticker: str,
    user: User,
    session,
    market: MarketService,
    state: FSMContext,
) -> None:
    quote = await market.get_quote(session, ticker)
    text = await _quantity_text(side, ticker, quote, user, session)
    await message.answer(text, reply_markup=quantity_keyboard(side, ticker), parse_mode="HTML")


async def _edit_quantity_prompt(
    callback: CallbackQuery,
    side: str,
    ticker: str,
    user: User,
    session,
    market: MarketService,
    state: FSMContext,
) -> None:
    quote = await market.get_quote(session, ticker)
    text = await _quantity_text(side, ticker, quote, user, session)
    await safe_edit(callback, text, quantity_keyboard(side, ticker))


async def _quantity_text(side: str, ticker: str, quote, user: User, session) -> str:
    asset = get_asset(ticker)
    lines = [
        ("🟢 <b>Покупка</b>" if side == "buy" else "🔴 <b>Продажа</b>"),
        "",
        f"{asset.emoji if asset else '📈'} <b>{ticker}</b> · {quote.price:,.2f} {quote.currency}",
        f"Изменение: {change_percent(quote.change_percent)}",
        "",
        "Сколько единиц?",
    ]
    if side == "sell":
        position = await PortfolioRepository.get_position(session, user.id, ticker)
        if position:
            lines.append(f"В портфеле: <b>{fmt_qty(position.quantity)}</b> шт.")
    else:
        approx = int(Decimal(user.balance) / Decimal(quote.price)) if Decimal(quote.price) > 0 else 0
        lines.append(f"Максимум без комиссии: ≈ {approx} шт.")
    lines.append(f"\n{source_line(quote)}")
    return "\n".join(lines)


async def _edit_confirmation(
    callback: CallbackQuery,
    side: str,
    ticker: str,
    qty: Decimal,
    user: User,
    session,
    market: MarketService,
) -> None:
    quote = await market.get_quote(session, ticker)
    await safe_edit(
        callback,
        _confirmation_text(side, ticker, qty, quote, user, session),
        trade_confirm(side, ticker),
    )


async def _send_confirmation_message(
    message: Message,
    side: str,
    ticker: str,
    qty: Decimal,
    user: User,
    session,
    market: MarketService,
    state: FSMContext,
) -> None:
    quote = await market.get_quote(session, ticker)
    await message.answer(
        _confirmation_text(side, ticker, qty, quote, user, session),
        reply_markup=trade_confirm(side, ticker),
        parse_mode="HTML",
    )


def _confirmation_text(side: str, ticker: str, qty: Decimal, quote, user: User, session) -> str:
    gross = money(qty * Decimal(quote.price))
    fee = money(gross * settings.COMMISSION_RATE)
    if side == "buy":
        total = money(gross + fee)
        action = "купить"
        balance_line = f"Спишется: <b>{total}</b>"
    else:
        total = money(gross - fee)
        action = "продать"
        balance_line = f"Зачислится: <b>{total}</b>"
    text = (
        f"🧾 <b>Подтверждение: {action} {fmt_qty(qty)} {ticker}</b>\n\n"
        f"Цена: <b>{quote.price:,.2f} {quote.currency}</b>\n"
        f"Сумма: <b>{money(gross)}</b>\n"
        f"Комиссия ({settings.COMMISSION_RATE * 100:.2f}%): <b>{money(fee)}</b>\n"
        f"{balance_line}\n"
        f"Свободный баланс: {money(user.balance)}\n"
        f"{source_line(quote)}"
    )
    if quote.is_fallback:
        text += "\n⚠️ <i>Источник недоступен, сделка пройдёт по резервной цене.</i>"
    return text


def _result_text(result: TradeResult, fallback: bool) -> str:
    action = "Куплено" if result.side == "buy" else "Продано"
    emoji = "🟢" if result.side == "buy" else "🔴"
    lines = [
        f"{emoji} <b>{action}: {fmt_qty(result.quantity)} {result.ticker}</b>",
        "",
        f"Цена: <b>{result.price:,.2f} USD</b>",
        f"Сумма: {money(result.gross)}",
        f"Комиссия: {money(result.fee)}",
    ]
    if result.realized_pnl is not None:
        sign = "+" if result.realized_pnl >= 0 else ""
        lines.append(f"Реализованный PnL: <b>{sign}{money(result.realized_pnl)}</b>")
    lines.extend(
        [
            "",
            f"💵 Новый баланс: <b>{money(result.new_balance)}</b>",
            f"⭐ +{result.xp_gained} XP",
        ]
    )
    if result.level_up:
        lines.append(f"🎉 Новый уровень: <b>{result.new_level}</b>!")
    if result.new_badges:
        for code in result.new_badges:
            badge = BADGES.get(code)
            if badge:
                lines.append(f"🏅 {badge.emoji} <b>{badge.title}</b>")
    if fallback:
        lines.append("\n⚠️ <i>Сделка совершена по резервной цене.</i>")
    return "\n".join(lines)
