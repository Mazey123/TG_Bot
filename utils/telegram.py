"""Вспомогательные функции для безопасного редактирования сообщений."""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


def _is_not_modified(error: TelegramBadRequest) -> bool:
    return "message is not modified" in str(error).lower()


async def safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Редактирует текст, а если сообщение — фото/альбом, отправляет новое."""
    message = callback.message
    if message is None:
        await callback.answer("Сообщение устарело, откройте меню заново.", show_alert=True)
        return False
    try:
        await message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except TelegramBadRequest as exc:
        if _is_not_modified(exc):
            return True
        logger.debug("edit_text_fallback error=%s", type(exc).__name__)
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        try:
            await message.answer(
                text,
                reply_markup=reply_markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True
        except TelegramBadRequest:
            return False


async def safe_answer(callback: CallbackQuery, text: str = "", alert: bool = False) -> None:
    try:
        await callback.answer(text[:190], show_alert=alert)
    except TelegramBadRequest:
        pass
