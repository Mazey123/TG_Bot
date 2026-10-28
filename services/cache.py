"""Двухуровневый TTL-кэш: память + таблица api_cache.

Зачем два уровня:
* память — самые горячие запросы внутри процесса, без SQL;
* SQLite — переживает перезапуск и позволяет отдать устаревшие данные,
  если внешний API временно недоступен (graceful degradation).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from database.repo import CacheRepository

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class _MemoryItem:
    value: Any
    expires_at: float
    created_at: float


class MemoryTTLCache:
    """Простой кэш с TTL и ограничением по числу записей."""

    def __init__(self, max_items: int = 1024) -> None:
        self._items: dict[str, _MemoryItem] = {}
        self._max_items = max_items

    def get(self, key: str) -> tuple[Any | None, bool]:
        item = self._items.get(key)
        if item is None:
            return None, False
        now = time.monotonic()
        if item.expires_at <= now:
            self._items.pop(key, None)
            return None, False
        return item.value, True

    def get_stale(self, key: str, max_age_seconds: int) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        if time.monotonic() - item.created_at > max_age_seconds:
            return None
        return item.value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if len(self._items) >= self._max_items:
            # Удаляем самый старый по created_at, а не случайный.
            oldest = min(self._items, key=lambda k: self._items[k].created_at)
            self._items.pop(oldest, None)
        now = time.monotonic()
        self._items[key] = _MemoryItem(
            value=value,
            expires_at=now + max(1, ttl_seconds),
            created_at=now,
        )

    def delete(self, key: str) -> None:
        self._items.pop(key, None)

    def clear(self) -> None:
        self._items.clear()


memory_cache = MemoryTTLCache()


class CacheService:
    """JSON-кэш, привязанный к текущей сессии БД."""

    def __init__(self, session: AsyncSession, memory: MemoryTTLCache | None = None) -> None:
        self.session = session
        self.memory = memory or memory_cache

    async def get(self, key: str, *, allow_stale: bool = False, max_stale_seconds: int = 86400) -> Any | None:
        value, fresh = self.memory.get(key)
        if fresh:
            return value

        try:
            entry = await CacheRepository.get(self.session, key)
        except SQLAlchemyError:
            logger.warning("cache_db_read_failed key=%s", key, exc_info=False)
            return self.memory.get_stale(key, max_stale_seconds)

        if entry is None:
            return None

        now = _utcnow()
        try:
            payload = json.loads(entry.payload)
        except (TypeError, ValueError):
            return None

        if entry.expires_at > now:
            ttl = int((entry.expires_at - now).total_seconds())
            self.memory.set(key, payload, ttl)
            return payload

        if allow_stale and (now - entry.created_at).total_seconds() <= max_stale_seconds:
            return payload
        return None

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        ttl = max(1, int(ttl_seconds))
        self.memory.set(key, value, ttl)
        try:
            await CacheRepository.upsert(
                self.session,
                key,
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                _utcnow() + timedelta(seconds=ttl),
            )
            await self.session.commit()
        except SQLAlchemyError:
            # Кэш не должен ломать основной сценарий.
            logger.warning("cache_db_write_failed key=%s", key, exc_info=False)
            await self.session.rollback()

    async def purge_expired(self) -> int:
        try:
            removed = await CacheRepository.purge_expired(self.session)
            await self.session.commit()
            return removed
        except SQLAlchemyError:
            await self.session.rollback()
            return 0
