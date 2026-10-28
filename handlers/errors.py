"""Глобальный обработчик необработанных исключений."""

from __future__ import annotations

import logging

from aiogram.types import ErrorEvent
from aiogram.exceptions import TelegramAPIError

from config.settings import settings
from utils.logging import user_ref

logger = logging.getLogger(__name__)


async def error_handler(event: ErrorEvent) -> None:
    exception = event.exception
    tg_user = None
    update = event.update
    if update.message and update.message.from_user:
        tg_user = update.message.from_user
    elif update.callback_query and update.callback_query.from_user:
        tg_user = update.callback_query.from_user

    # Не логируем текст ошибки целиком: в нём могут быть пользовательские
    # данные или токен в URL. Тип + псевдоним пользователя достаточно.
    logger.error(
        "unhandled_error type=%s user_ref=%s",
        type(exception).__name__,
        user_ref(tg_user.id, settings.LOG_SALT) if tg_user else "none",
    )

    text = "⚠️ Что-то пошло не так. Попробуйте ещё раз или откройте /menu."
    try:
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.message:
            await update.message.answer(text)
    except TelegramAPIError:
        pass
