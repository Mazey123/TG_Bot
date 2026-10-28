"""Квиз с FSM: выбор категории → вопрос → проверка → результат."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config.settings import settings
from database.models import User
from database.repo import QuizRepository, UserRepository
from keyboards.inline import main_menu, quiz_categories, quiz_next, quiz_options, quiz_result
from services.gamification import BADGES, award_badges
from services.quiz import CATEGORIES, QUESTION_LIMIT, QuizService
from utils.formatting import escape
from utils.telegram import safe_answer, safe_edit

router = Router(name="quiz")
logger = logging.getLogger(__name__)


class QuizStates(StatesGroup):
    answering = State()


@router.message(Command("quiz"))
async def cmd_quiz(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🎯 <b>Квиз «Проверь себя»</b>\n\n"
        "5 вопросов, +15 XP за верный ответ и бонус за идеальный результат.\n"
        "Выберите категорию:",
        reply_markup=quiz_categories(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "quiz:menu")
async def cb_quiz_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(
        callback,
        "🎯 <b>Квиз «Проверь себя»</b>\n\n"
        "5 вопросов. +15 XP за верный ответ, +25 XP за идеальный результат.\n"
        "Выберите категорию:",
        quiz_categories(),
    )
    await safe_answer(callback)


@router.callback_query(F.data.startswith("quiz:cat:"))
async def cb_quiz_category(
    callback: CallbackQuery,
    state: FSMContext,
    session,
    user: User,
) -> None:
    category = (callback.data or "").split(":")[-1]
    if category not in CATEGORIES:
        await safe_answer(callback, "Неизвестная категория", alert=True)
        return
    questions = QuizService.questions_for(category)
    await state.set_state(QuizStates.answering)
    await state.set_data(
        {
            "category": category,
            "question_ids": [question.id for question in questions],
            "index": 0,
            "answers": [],
            "answered": False,
        }
    )
    await _show_question(callback, state, session)
    await safe_answer(callback)


@router.callback_query(F.data.startswith("quiz:a:"))
async def cb_quiz_answer(
    callback: CallbackQuery,
    state: FSMContext,
    session,
    user: User,
) -> None:
    if await state.get_state() != QuizStates.answering.state:
        await safe_answer(callback, "Квиз устарел. Начните заново.", alert=True)
        return
    data = await state.get_data()
    if data.get("answered"):
        await safe_answer(callback, "Ответ на этот вопрос уже принят.", alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await safe_answer(callback, "Некорректный ответ", alert=True)
        return
    try:
        question_index = int(parts[2])
        option_index = int(parts[3])
    except ValueError:
        await safe_answer(callback, "Некорректный ответ", alert=True)
        return
    if question_index != int(data.get("index", 0)):
        await safe_answer(callback, "Этот вопрос уже неактуален.", alert=True)
        return

    ids: list[int] = list(data.get("question_ids", []))
    if not 0 <= question_index < len(ids):
        await state.clear()
        await safe_answer(callback, "Ошибка состояния квиза.", alert=True)
        return
    question = QuizService.question_by_id(ids[question_index])
    if question is None or not 0 <= option_index < len(question.options):
        await safe_answer(callback, "Некорректный вариант", alert=True)
        return

    is_correct = option_index == question.correct_index
    answers = list(data.get("answers", []))
    answers.append(is_correct)
    await state.update_data(answers=answers, answered=True)

    if question_index + 1 >= len(ids):
        await _finish_quiz(callback, state, session, user)
        await safe_answer(callback, "Верно!" if is_correct else "Неверно")
        return

    feedback = _answer_feedback(question, option_index, is_correct, question_index, len(ids))
    await safe_edit(callback, feedback, quiz_next(question_index))
    await safe_answer(callback, "Верно!" if is_correct else "Неверно")


@router.callback_query(F.data.startswith("quiz:next:"))
async def cb_quiz_next(
    callback: CallbackQuery,
    state: FSMContext,
    session,
) -> None:
    if await state.get_state() != QuizStates.answering.state:
        await safe_answer(callback, "Квиз завершён. Начните заново.", alert=True)
        return
    data = await state.get_data()
    if not data.get("answered"):
        await safe_answer(callback, "Сначала ответьте на вопрос.", alert=True)
        return
    parts = (callback.data or "").split(":")
    try:
        feedback_index = int(parts[-1])
    except ValueError:
        feedback_index = -1
    if feedback_index != int(data.get("index", 0)):
        await safe_answer(callback, "Кнопка устарела.", alert=True)
        return
    next_index = feedback_index + 1
    ids = list(data.get("question_ids", []))
    if next_index >= len(ids):
        await _finish_quiz(callback, state, session, None)
        await safe_answer(callback)
        return
    await state.update_data(index=next_index, answered=False)
    await _show_question(callback, state, session)
    await safe_answer(callback)


async def _show_question(callback: CallbackQuery, state: FSMContext, session) -> None:
    data = await state.get_data()
    ids: list[int] = list(data.get("question_ids", []))
    index = int(data.get("index", 0))
    question = QuizService.question_by_id(ids[index])
    if question is None:
        await state.clear()
        await safe_edit(callback, "⚠️ Вопрос не найден. Начните квиз заново.", quiz_categories())
        return
    category = CATEGORIES.get(question.category, question.category)
    text = (
        f"🎯 <b>{category}</b> · вопрос {index + 1}/{len(ids)}\n\n"
        f"<b>{escape(question.text)}</b>\n\n"
        "Выберите вариант:"
    )
    await safe_edit(callback, text, quiz_options(index, question.options))


def _answer_feedback(
    question,
    option_index: int,
    is_correct: bool,
    question_index: int,
    total: int,
) -> str:
    if is_correct:
        head = "✅ <b>Верно!</b>"
    else:
        correct = question.options[question.correct_index]
        head = f"❌ <b>Неверно.</b>\nПравильный ответ: <b>{escape(correct)}</b>"
    return (
        f"{head}\n\n"
        f"{escape(question.explanation)}\n\n"
        f"<i>Вопрос {question_index + 1}/{total}</i>"
    )


async def _finish_quiz(
    callback: CallbackQuery,
    state: FSMContext,
    session,
    user: User | None,
) -> None:
    data = await state.get_data()
    category = str(data.get("category", "stocks"))
    answers: list[bool] = list(data.get("answers", []))
    total = len(data.get("question_ids", [])) or QUESTION_LIMIT
    correct = QuizService.score(answers)
    xp = QuizService.xp_for(correct, total)
    await state.clear()

    # При финальном ответе user передан; при кнопке «Дальше» — берём по ID.
    if user is None:
        # callback.from_user доступен, но профиль уже загружен middleware;
        # здесь достаточно повторно прочитать его из БД.
        user = await session.get(User, callback.from_user.id)
    if user is None:
        await safe_edit(callback, "⚠️ Профиль не найден.", main_menu())
        return

    try:
        await QuizRepository.save_attempt(
            session,
            user_id=user.id,
            category=category,
            correct_answers=correct,
            total_questions=total,
            score=xp,
        )
        old_level = user.level
        await UserRepository.add_xp(session, user, xp)
        badges = await award_badges(
            session,
            user,
            starting_balance=settings.STARTING_BALANCE,
            quiz_perfect=(correct == total and total > 0),
        )
        await session.commit()
        await session.refresh(user)
    except Exception as exc:
        await session.rollback()
        logger.error("quiz_save_failed category=%s error=%s", category, type(exc).__name__)
        await safe_edit(callback, "⚠️ Не удалось сохранить результат. Попробуйте ещё раз.", main_menu())
        return

    if correct == total:
        verdict = "🏆 Идеально! Вы знаток рынка."
    elif correct >= total * 0.6:
        verdict = "👍 Хороший результат."
    else:
        verdict = "📚 Есть куда расти — попробуйте ещё раз."
    text = (
        f"🏁 <b>Квиз завершён</b>\n\n"
        f"Категория: <b>{CATEGORIES.get(category, category)}</b>\n"
        f"Правильных ответов: <b>{correct}/{total}</b>\n"
        f"Начислено: <b>+{xp} XP</b>\n\n"
        f"{verdict}"
    )
    if user.level > old_level:
        text += f"\n🎉 Новый уровень: <b>{user.level}</b>!"
    for code in badges:
        info = BADGES.get(code)
        if info:
            text += f"\n🏅 {info.emoji} <b>{info.title}</b>"
    await safe_edit(callback, text, quiz_result())
