"""Конфигурация приложения.

Все секреты читаются из переменных окружения (.env), в коде их нет.
Pydantic-settings также валидирует типы и не даст запустить бота
с некорректным, например, отрицательным стартовым балансом.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram ---------------------------------------------------------
    TELEGRAM_TOKEN: SecretStr
    BOT_TIMEZONE: str = "Europe/Moscow"

    # SOCKS5/HTTP-прокси для Bot API и внешних API (не MTProto-прокси!).
    # Пример: socks5://user:pass@127.0.0.1:1080
    PROXY_URL: str | None = None

    # --- База данных ------------------------------------------------------
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/stock_bot.db"

    # --- Экономика игры ---------------------------------------------------
    STARTING_BALANCE: Decimal = Decimal("10000.00")
    COMMISSION_RATE: Decimal = Decimal("0.002")
    DAILY_BONUS: Decimal = Decimal("250.00")
    BONUS_COOLDOWN_HOURS: int = 24
    BONUS_STREAK_WINDOW_HOURS: int = 48

    # --- Внешние API ------------------------------------------------------
    # auto  — пробуем сеть, при сбое показываем честно помеченные демо-данные
    # demo  — не ходим в сеть (удобно для проверки лабы без интернета)
    MARKET_DATA_MODE: Literal["auto", "demo"] = "auto"
    HTTP_TIMEOUT_SECONDS: float = Field(default=6.0, ge=1.0, le=30.0)
    HTTP_RETRIES: int = Field(default=2, ge=1, le=5)
    PROVIDER_COOLDOWN_SECONDS: int = Field(default=90, ge=10, le=3600)
    COINGECKO_API_KEY: SecretStr | None = None
    YAHOO_BASE_URL: str = "https://query1.finance.yahoo.com"
    STOOQ_BASE_URL: str = "https://stooq.com"
    COINGECKO_BASE_URL: str = "https://api.coingecko.com/api/v3"
    FRANKFURTER_BASE_URL: str = "https://api.frankfurter.app"

    # --- Кэш --------------------------------------------------------------
    QUOTE_CACHE_TTL_SECONDS: int = Field(default=60, ge=5, le=3600)
    HISTORY_CACHE_TTL_SECONDS: int = Field(default=1800, ge=60, le=86400)
    FX_CACHE_TTL_SECONDS: int = Field(default=3600, ge=60, le=86400)
    STALE_CACHE_MAX_HOURS: int = Field(default=24, ge=1, le=168)

    # --- Уведомления ------------------------------------------------------
    ALERTS_ENABLED: bool = True
    ALERT_CHECK_INTERVAL_SECONDS: int = Field(default=120, ge=30, le=3600)

    # --- Безопасность / UX ------------------------------------------------
    RATE_LIMIT_EVENTS: int = Field(default=30, ge=5, le=300)
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=10, ge=1, le=120)
    LOG_LEVEL: str = "INFO"
    LOG_SALT: str = "local-lab-salt"

    @field_validator("STARTING_BALANCE", "DAILY_BONUS")
    @classmethod
    def _positive_money(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("денежное значение должно быть больше нуля")
        return value.quantize(Decimal("0.01"))

    @field_validator("COMMISSION_RATE")
    @classmethod
    def _valid_commission(cls, value: Decimal) -> Decimal:
        if not (Decimal("0") <= value < Decimal("0.1")):
            raise ValueError("комиссия должна быть в диапазоне [0, 0.1)")
        return value

    @field_validator("PROXY_URL")
    @classmethod
    def _valid_proxy(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        proxy = value.strip()
        scheme = proxy.split("://", 1)[0].lower() if "://" in proxy else ""
        if scheme not in {"http", "socks4", "socks5"}:
            raise ValueError(
                "PROXY_URL должен начинаться с http://, socks4:// или socks5://. "
                "MTProto-прокси Telegram (mtproto://) для Bot API не подходит."
            )
        parsed = urlparse(proxy)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Некорректный порт в PROXY_URL") from exc
        if not parsed.hostname or not port:
            raise ValueError(
                "В PROXY_URL нужен host и port, например "
                "socks5://127.0.0.1:1080"
            )
        return proxy

    @property
    def token(self) -> str:
        return self.TELEGRAM_TOKEN.get_secret_value()

    @property
    def coingecko_key(self) -> str | None:
        if self.COINGECKO_API_KEY is None:
            return None
        value = self.COINGECKO_API_KEY.get_secret_value().strip()
        return value or None

    def validate_runtime(self) -> None:
        """Проверки, которые неудобно делать на уровне отдельного поля."""
        if ":" not in self.token or len(self.token) < 20:
            raise ValueError(
                "TELEGRAM_TOKEN не похож на токен BotFather. "
                "Проверьте файл .env."
            )


settings = Settings()
