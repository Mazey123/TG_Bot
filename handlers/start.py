"""Команды /start, /help, /menu, /balance, /bonus и главное меню."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config.settings import settings
from database.models import User
from keyboards.inline import back_to_menu, main_menu
from services.gamification import BADGES, claim_daily_bonus
from utils.formatting import as_of, display_name, money, user_progress
from utils.telegram import safe_answer, safe_edit

router = Router(name="start")
logger = logging.getLogger(__name__)


HELP_TEXT = (
    "📖 <b>Биржевой симулятор — как играть</b>\n\n"
    "<b>Рынок</b>\n"
    "• /price AAPL — котировка и график\n"
    "• /buy AAPL 2 — купить 2 акции\n"
    "• /sell AAPL 1 — продать 1 акцию\n"
    "• /alert AAPL 250 — уведомить о цене\n"
    "• /alerts — мои уведомления\n\n"
    "<b>Прогресс</b>\n"
    "• /portfolio — портфель и PnL\n"
    "• /history — последние сделки\n"
    "• /leaderboard — топ-10 по XP\n"
    "• /quiz — квиз и бонусный опыт\n"
    "• /bonus — ежедневный бонус\n"
    "• /settings — уведомления и сброс\n\n"
    "💡 Стартовый капитал — виртуальные деньги. "
    "Цены приходят из внешних API; если сеть недоступна, бот честно "
    "помечает резервные демо-данные. Это учебный проект, не инвестиционный совет."
)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    user: User,
    is_new_user: bool,
    state: FSMContext,
) -> None:
    await state.clear()
    greeting = "С возвращением" if not is_new_user else "Добро пожаловать"
    text = (
        f"👋 <b>{greeting}, {display_name(user)}!</b>\n\n"
        "Это <b>Биржевой симулятор</b>: торгуйте акциями, криптой и "
        "валютными парами на виртуальный капитал.\n\n"
        f"💵 Баланс: <code>{money(user.balance)}</code>\n"
        f"{user_progress(user)}\n\n"
        "Выберите раздел меню или введите /help."
    )
    await message.answer(text, reply_markup=main_menu(), parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=back_to_menu(), parse_mode="HTML")


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🏠 <b>Главное меню</b>\n\nВыберите действие:",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


@router.message(Command("balance"))
async def cmd_balance(message: Message, user: User) -> None:
    await message.answer(_balance_text(user), reply_markup=back_to_menu(), parse_mode="HTML")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "✅ Действие отменено. Вы в главном меню.",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


@router.message(Command("bonus"))
async def cmd_bonus(message: Message, user: User, session) -> None:
    result = await claim_daily_bonus(session, user, settings)
    if not result.granted:
        when = as_of(result.next_available_at) if result.next_available_at else "позже"
        await message.answer(
            "🎁 <b>Бонус уже получен</b>\n\n"
            f"Следующий бонус будет доступен: <b>{when}</b>\n"
            f"🔥 Серия: <b>{result.streak}</b>",
            reply_markup=back_to_menu(),
            parse_mode="HTML",
        )
        return

    text = (
        "🎁 <b>Ежедневный бонус получен!</b>\n\n"
        f"💵 +<code>{money(result.amount)}</code>\n"
        f"🔥 Серия визитов: <b>{result.streak}</b>\n"
        f"⭐ +20 XP"
    )
    if result.level_up:
        text += f"\n\n🎉 Новый уровень: <b>{user.level}</b>!"
    if result.new_badges:
        for code in result.new_badges:
            badge = BADGES.get(code)
            if badge:
                text += f"\n🏅 Достижение: {badge.emoji} <b>{badge.title}</b>"
    await message.answer(text, reply_markup=back_to_menu(), parse_mode="HTML")


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(
        callback,
        "🏠 <b>Главное меню</b>\n\nВыберите действие:",
        main_menu(),
    )
    await safe_answer(callback)


@router.callback_query(F.data == "menu:help")
async def cb_help(callback: CallbackQuery) -> None:
    await safe_edit(callback, HELP_TEXT, back_to_menu())
    await safe_answer(callback)


@router.callback_query(F.data == "menu:balance")
async def cb_balance(callback: CallbackQuery, user: User) -> None:
    await safe_edit(callback, _balance_text(user), back_to_menu())
    await safe_answer(callback)


def _balance_text(user: User) -> str:
    return (
        "💰 <b>Баланс</b>\n\n"
        f"💵 Свободные средства: <code>{money(user.balance)}</code>\n"
        f"{user_progress(user)}\n\n"
        "Стоимость портфеля и PnL смотрите в разделе «Портфель»."
    )
