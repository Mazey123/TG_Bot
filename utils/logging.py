"""Логирование без утечки секретов и персональных данных."""

from __future__ import annotations

import hashlib
import logging
import sys


class SecretFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self.secrets = [secret for secret in secrets if secret and len(secret) > 5]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        for secret in self.secrets:
            if secret in message:
                message = message.replace(secret, "***")
        record.msg = message
        record.args = ()
        return True


def configure_logging(level: str, secrets: list[str]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(SecretFilter(secrets))
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def user_ref(user_id: int, salt: str) -> str:
    """Псевдоним пользователя для логов: без имени, username и Telegram ID."""
    return hashlib.sha256(f"{user_id}:{salt}".encode("utf-8")).hexdigest()[:10]
