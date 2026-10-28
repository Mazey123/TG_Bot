"""Middleware: сессия БД, профиль пользователя и внедрение сервисов."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from sqlalchemy.exc import SQLAlchemyError

from config.settings import Settings
from database.repo import UserRepository
from database.session import async_session_factory
from utils.logging import user_ref

logger = logging.getLogger(__name__)


def _telegram_user(event: TelegramObject, data: dict[str, Any]):
    user = data.get("event_from_user")
    if user is not None:
        return user
    if isinstance(event, Update):
        for attr in ("message", "edited_message", "callback_query", "inline_query"):
            nested = getattr(event, attr, None)
            if nested is not None and getattr(nested, "from_user", None) is not None:
                return nested.from_user
    return getattr(event, "from_user", None)


def _event_message(event: TelegramObject) -> Message | None:
    if isinstance(event, Update):
        return event.message or event.edited_message
    if isinstance(event, Message):
        return event
    if isinstance(event, CallbackQuery):
        return event.message
    return None


class AppContextMiddleware(BaseMiddleware):
    """Одна сессия на update + актуальный профиль в data["user"]."""

    def __init__(self, settings: Settings, services: dict[str, Any]) -> None:
        self.settings = settings
        self.services = services

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data.update(self.services)
        tg_user = _telegram_user(event, data)
        if tg_user is None or getattr(tg_user, "is_bot", False):
            return await handler(event, data)

        async with async_session_factory() as session:
            try:
                user, created = await UserRepository.get_or_create(
                    session,
                    tg_user.id,
                    getattr(tg_user, "username", None),
                    getattr(tg_user, "first_name", None),
                    self.settings.STARTING_BALANCE,
                )
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                logger.error(
                    "profile_load_failed user_ref=%s error=%s",
                    user_ref(tg_user.id, self.settings.LOG_SALT),
                    type(exc).__name__,
                )
                message = _event_message(event)
                if message is not None:
                    try:
                        await message.answer(
                            "⚠️ Временная ошибка базы данных. Попробуйте ещё раз."
                        )
                    except Exception:
                        pass
                return None

            data["session"] = session
            data["user"] = user
            data["is_new_user"] = created
            return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    """Простой антифлуд: не более N событий за окно на пользователя."""

    def __init__(self, events: int, window_seconds: int) -> None:
        self.limit = events
        self.window = window_seconds
        self._events: dict[int, deque[float]] = defaultdict(deque)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        import time

        tg_user = _telegram_user(event, data)
        if tg_user is None:
            return await handler(event, data)

        now = time.monotonic()
        bucket = self._events[tg_user.id]
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            logger.info("rate_limit user_ref=%s", user_ref(tg_user.id, "rate"))
            if isinstance(event, Update):
                if event.callback_query:
                    try:
                        await event.callback_query.answer(
                            "⏳ Слишком часто. Подождите пару секунд.", show_alert=True
                        )
                    except Exception:
                        pass
                elif event.message:
                    try:
                        await event.message.answer("⏳ Слишком часто. Подождите пару секунд.")
                    except Exception:
                        pass
            return None
        bucket.append(now)
        return await handler(event, data)
