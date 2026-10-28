"""Настройки: уведомления и безопасный сброс игрового прогресса."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from database.models import User
from keyboards.inline import main_menu, reset_confirm, settings_keyboard
from services.trading import TradingService
from utils.formatting import short_date
from utils.logging import user_ref
from utils.telegram import safe_answer, safe_edit

router = Router(name="settings")
logger = logging.getLogger(__name__)


@router.message(Command("settings"))
async def cmd_settings(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        _settings_text(user),
        reply_markup=settings_keyboard(user.notifications_enabled),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "set:view")
async def cb_settings(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(
        callback,
        _settings_text(user),
        settings_keyboard(user.notifications_enabled),
    )
    await safe_answer(callback)


@router.callback_query(F.data == "set:notify")
async def cb_toggle_notifications(
    callback: CallbackQuery, user: User, session, state: FSMContext
) -> None:
    await state.clear()
    user.notifications_enabled = not user.notifications_enabled
    await session.commit()
    logger.info(
        "notifications_toggled enabled=%s user_ref=%s",
        user.notifications_enabled,
        user_ref(user.id, "settings"),
    )
    await safe_edit(
        callback,
        _settings_text(user),
        settings_keyboard(user.notifications_enabled),
    )
    await safe_answer(
        callback,
        "Уведомления включены" if user.notifications_enabled else "Уведомления выключены",
    )


@router.callback_query(F.data == "set:reset")
async def cb_reset_request(callback: CallbackQuery) -> None:
    await safe_edit(
        callback,
        "⚠️ <b>Сброс прогресса</b>\n\n"
        "Будут удалены портфель, сделки, достижения, результаты квизов "
        "и алерты. Баланс вернётся к стартовому, XP обнулится.\n\n"
        "Это действие нельзя отменить. Продолжить?",
        reset_confirm(),
    )
    await safe_answer(callback)


@router.callback_query(F.data == "set:reset_confirm")
async def cb_reset_confirm(
    callback: CallbackQuery,
    user: User,
    session,
    trading: TradingService,
    state: FSMContext,
) -> None:
    await state.clear()
    await trading.reset_progress(session, user)
    await safe_edit(
        callback,
        "🧹 <b>Прогресс сброшен</b>\n\n"
        "Баланс и уровень восстановлены. Можно начинать заново!",
        main_menu(),
    )
    await safe_answer(callback, "Готово", alert=False)


def _settings_text(user: User) -> str:
    status = "включены ✅" if user.notifications_enabled else "выключены 🔕"
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"🔔 Уведомления: <b>{status}</b>\n"
        "Ценовые алерты приходят только при включённых уведомлениях.\n\n"
        f"👤 Профиль: <code>{user.id}</code>\n"
        f"📅 Регистрация: {short_date(user.created_at)}\n\n"
        "<i>Сброс прогресса удаляет игровые данные, но не профиль.</i>"
    )
