"""Команды ценовых алертов и управление ими."""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from database.models import User
from database.repo import AlertRepository
from keyboards.inline import alerts_list, asset_detail, back_menu_only
from services.alerts import create_alert, format_alert_direction, parse_target_price
from services.assets import get_asset, normalize_ticker
from services.market.service import MarketService
from utils.formatting import price
from utils.security import looks_like_ticker
from utils.telegram import safe_answer, safe_edit

router = Router(name="alerts")
logger = logging.getLogger(__name__)


@router.message(Command("alert"))
async def cmd_alert(
    message: Message,
    command: CommandObject,
    user: User,
    session,
    market: MarketService,
) -> None:
    args = (command.args or "").strip().split()
    if len(args) != 2:
        await message.answer(
            "🔔 <b>Ценовой алерт</b>\n\n"
            "Формат: <code>/alert AAPL 250</code>\n"
            "Бот сообщит, когда цена пересечёт указанный уровень.\n\n"
            "Посмотреть активные: /alerts",
            reply_markup=back_menu_only(),
            parse_mode="HTML",
        )
        return
    raw_ticker, raw_price = args
    if not looks_like_ticker(raw_ticker):
        await message.answer("⚠️ Некорректный тикер.")
        return
    ticker = normalize_ticker(raw_ticker)
    if get_asset(ticker) is None:
        await message.answer("⚠️ Тикер не поддерживается. Список: /market")
        return
    try:
        target = parse_target_price(raw_price)
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return

    try:
        quote = await market.get_quote(session, ticker)
        alert = await create_alert(
            session,
            user_id=user.id,
            ticker=ticker,
            target_price=target,
            current_price=Decimal(quote.price),
        )
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    except Exception as exc:
        logger.error("alert_create_failed ticker=%s error=%s", ticker, type(exc).__name__)
        await message.answer("⚠️ Не удалось создать алерт. Попробуйте позже.")
        return

    text = (
        "🔔 <b>Алерт создан</b>\n\n"
        f"{ticker}: {format_alert_direction(alert.direction)} "
        f"<b>{price(alert.target_price, quote.currency)}</b>\n"
        f"Текущая цена: {price(quote.price, quote.currency)}\n\n"
        f"Активных алертов: <b>{await AlertRepository.count_active_for_user(session, user.id)}</b>"
    )
    if not user.notifications_enabled:
        text += "\n\n🔕 <i>Уведомления выключены — алерт сработает, но сообщение не придёт, пока вы не включите их в /settings.</i>"
    if quote.is_fallback:
        text += "\n\n⚠️ <i>Сейчас доступна только резервная цена. Алерт будет проверяться по реальным данным, когда API восстановится.</i>"
    await message.answer(text, reply_markup=back_menu_only(), parse_mode="HTML")


@router.message(Command("alerts"))
async def cmd_alerts(message: Message, user: User, session) -> None:
    alerts = await AlertRepository.list_for_user(session, user.id)
    text, keyboard = _alerts_view(alerts)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data.startswith("alert:help:"))
async def cb_alert_help(callback: CallbackQuery) -> None:
    ticker = (callback.data or "").split(":")[-1]
    asset = get_asset(ticker)
    await safe_edit(
        callback,
        f"🔔 <b>Алерт по {ticker}</b>\n\n"
        f"Отправьте команду:\n<code>/alert {ticker} 250</code>\n\n"
        "Бот пришлёт уведомление, когда цена пересечёт уровень "
        "вверх или вниз (направление определяется автоматически).",
        asset_detail(ticker, tradable=bool(asset and asset.kind != "fx")),
    )
    await safe_answer(callback)


@router.callback_query(F.data.startswith("alert:del:"))
async def cb_alert_delete(callback: CallbackQuery, user: User, session) -> None:
    try:
        alert_id = int((callback.data or "").split(":")[-1])
    except ValueError:
        await safe_answer(callback, "Некорректный алерт", alert=True)
        return
    alert = await AlertRepository.get(session, alert_id)
    if alert is None or alert.user_id != user.id:
        await safe_answer(callback, "Алерт не найден", alert=True)
        return
    await AlertRepository.deactivate(session, alert)
    await session.commit()
    alerts = await AlertRepository.list_for_user(session, user.id)
    text, keyboard = _alerts_view(alerts)
    await safe_edit(callback, text, keyboard)
    await safe_answer(callback, "Алерт удалён")


def _alerts_view(alerts: list):
    if not alerts:
        return (
            "🔔 <b>Мои алерты</b>\n\n"
            "Активных алертов нет.\n"
            "Создать: <code>/alert BTC 70000</code>",
            back_menu_only(),
        )
    lines = ["🔔 <b>Мои алерты</b>", ""]
    for alert in alerts[:8]:
        asset = get_asset(alert.ticker)
        currency = asset.currency if asset else "USD"
        lines.append(
            f"• <b>{alert.ticker}</b> "
            f"{'≥' if alert.direction == 'above' else '≤'} "
            f"{price(alert.target_price, currency)}"
        )
    lines.append("\nНажмите на алерт, чтобы удалить его.")
    return "\n".join(lines), alerts_list(alerts[:8])
