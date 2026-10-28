"""Портфель, история сделок, профиль и таблица лидеров."""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from database.models import User
from database.repo import BadgeRepository, QuizRepository, TransactionRepository, UserRepository
from keyboards.inline import back_to_menu, main_menu, portfolio_menu
from services.gamification import BADGES
from services.portfolio import PortfolioService
from services.quiz import CATEGORIES
from utils.formatting import (
    as_of,
    change_percent,
    display_name,
    escape,
    money,
    price,
    quantity,
    short_date,
    user_progress,
)
from utils.telegram import safe_answer, safe_edit

router = Router(name="portfolio")
logger = logging.getLogger(__name__)


@router.message(Command("portfolio"))
async def cmd_portfolio(
    message: Message, user: User, session, portfolio_service: PortfolioService
) -> None:
    text = await _portfolio_text(user, session, portfolio_service)
    await message.answer(text, reply_markup=portfolio_menu(True), parse_mode="HTML")


@router.message(Command("history"))
async def cmd_history(message: Message, user: User, session) -> None:
    text = await _history_text(user, session)
    await message.answer(text, reply_markup=back_to_menu(), parse_mode="HTML")


@router.message(Command("leaderboard"))
@router.message(Command("top"))
async def cmd_leaderboard(message: Message, user: User, session) -> None:
    text = await _leaderboard_text(user, session)
    await message.answer(text, reply_markup=back_to_menu(), parse_mode="HTML")


@router.message(Command("profile"))
async def cmd_profile(message: Message, user: User, session) -> None:
    text = await _profile_text(user, session)
    await message.answer(text, reply_markup=back_to_menu(), parse_mode="HTML")


@router.callback_query(F.data == "pf:view")
async def cb_portfolio(
    callback: CallbackQuery,
    user: User,
    session,
    portfolio_service: PortfolioService,
) -> None:
    text = await _portfolio_text(user, session, portfolio_service)
    await safe_edit(callback, text, portfolio_menu(True))
    await safe_answer(callback)


@router.callback_query(F.data == "pf:history")
async def cb_history(callback: CallbackQuery, user: User, session) -> None:
    text = await _history_text(user, session)
    await safe_edit(callback, text, portfolio_menu(True))
    await safe_answer(callback)


@router.callback_query(F.data == "pf:top")
async def cb_leaderboard(callback: CallbackQuery, user: User, session) -> None:
    text = await _leaderboard_text(user, session)
    await safe_edit(callback, text, back_to_menu())
    await safe_answer(callback)


@router.callback_query(F.data == "pf:profile")
async def cb_profile(callback: CallbackQuery, user: User, session) -> None:
    text = await _profile_text(user, session)
    await safe_edit(callback, text, back_to_menu())
    await safe_answer(callback)


async def _portfolio_text(user: User, session, service: PortfolioService) -> str:
    snapshot = await service.snapshot(session, user)
    lines = [
        "💼 <b>Портфель</b>",
        "",
        f"💵 Свободно: <b>{money(snapshot.cash)}</b>",
        f"📦 Активы: <b>{money(snapshot.positions_value)}</b>",
        f"🏦 Капитал: <b>{money(snapshot.net_worth)}</b>",
        f"📈 Нереализованный PnL: <b>{_signed(snapshot.unrealized_pnl)}</b>",
    ]
    if not snapshot.positions:
        lines.extend(
            [
                "",
                "Пока пусто. Купите первый актив на рынке — например, "
                "акцию AAPL или Bitcoin.",
            ]
        )
        return "\n".join(lines)

    lines.append("")
    for index, position in enumerate(snapshot.positions, start=1):
        sign = "+" if position.pnl >= 0 else ""
        lines.extend(
            [
                f"<b>{index}. {position.ticker}</b> · {quantity(position.quantity)} шт.",
                f"   {price(position.avg_price, position.quote.currency)} → "
                f"{price(position.current_price, position.quote.currency)} "
                f"({change_percent(position.pnl_percent)})",
                f"   Стоимость: {money(position.value)} · PnL: "
                f"<b>{sign}{money(position.pnl)}</b>",
            ]
        )
    if snapshot.has_fallback:
        lines.append(
            "\n⚠️ <i>Часть котировок — резервные данные, оценка может быть неточной.</i>"
        )
    return "\n".join(lines)


async def _history_text(user: User, session) -> str:
    transactions = await TransactionRepository.list_for_user(session, user.id, limit=10)
    if not transactions:
        return "📜 <b>История сделок</b>\n\nСделок пока нет."
    lines = ["📜 <b>Последние сделки</b>", ""]
    for transaction in transactions:
        side = "🟢 Покупка" if transaction.side == "buy" else "🔴 Продажа"
        line = (
            f"{side} <b>{transaction.ticker}</b> · "
            f"{quantity(transaction.quantity)} шт. × "
            f"{money(transaction.price)}"
        )
        if transaction.realized_pnl is not None:
            sign = "+" if transaction.realized_pnl >= 0 else ""
            line += f"\n   PnL: {sign}{money(transaction.realized_pnl)}"
        line += f"\n   <i>{as_of(transaction.created_at)}</i>"
        lines.append(line)
    return "\n".join(lines)


async def _leaderboard_text(user: User, session) -> str:
    top = await UserRepository.top_by_xp(session, limit=10)
    rank = await UserRepository.rank_by_xp(session, user)
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["🏆 <b>Топ-10 инвесторов</b>", "<i>Сортировка по опыту (XP)</i>", ""]
    for index, player in enumerate(top, start=1):
        medal = medals.get(index, f"{index}.")
        name = escape((player.first_name or "Инвестор")[:20])
        marker = " ← вы" if player.id == user.id else ""
        lines.append(
            f"{medal} <b>{name}</b>{marker} — ур. {player.level}, "
            f"{player.xp} XP"
        )
    lines.append("")
    lines.append(f"Ваше место: <b>#{rank}</b> · {user.xp} XP")
    return "\n".join(lines)


async def _profile_text(user: User, session) -> str:
    badges = await BadgeRepository.list_for_user(session, user.id)
    trades = await TransactionRepository.count_for_user(session, user.id)
    quizzes = await QuizRepository.count_for_user(session, user.id)
    attempts = await QuizRepository.list_for_user(session, user.id, limit=3)
    lines = [
        f"👤 <b>{display_name(user)}</b>",
        "",
        f"🆔 Профиль: <code>{user.id}</code>",
        f"📅 В игре с {short_date(user.created_at)}",
        f"💵 Баланс: <b>{money(user.balance)}</b>",
        "",
        user_progress(user),
        f"📊 Сделок: <b>{trades}</b> · квизов: <b>{quizzes}</b>",
        "",
        f"🏅 <b>Достижения ({len(badges)}/{len(BADGES)})</b>",
    ]
    if badges:
        for badge in badges:
            info = BADGES.get(badge.badge_code)
            if info:
                lines.append(f"{info.emoji} <b>{info.title}</b> — <i>{info.description}</i>")
    else:
        lines.append("<i>Пока нет. Совершите первую сделку!</i>")

    if attempts:
        lines.extend(["", "🎯 <b>Последние квизы</b>"])
        for attempt in attempts:
            category = CATEGORIES.get(attempt.category, attempt.category)
            lines.append(
                f"• {category}: <b>{attempt.correct_answers}/{attempt.total_questions}</b> "
                f"<i>({short_date(attempt.created_at)})</i>"
            )
    return "\n".join(lines)


def _signed(value: Decimal) -> str:
    return ("+" if value >= 0 else "") + money(value)
