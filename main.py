"""Точка входа биржевого Telegram-бота."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiohttp import ClientError
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from config.settings import settings
from database.session import async_session_factory, dispose_db, init_db
from handlers import (
    alerts,
    errors,
    market,
    portfolio,
    quiz as quiz_handler,
    settings as settings_handler,
    start,
    trade,
)
from middlewares.context import AppContextMiddleware, ThrottlingMiddleware
from services.cache import CacheService
from services.chart import ChartService
from services.http import HttpClient
from services.market.service import MarketService
from services.portfolio import PortfolioService
from services.quiz import QuizService
from services.trading import TradingService
from utils.logging import configure_logging

logger = logging.getLogger(__name__)


COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="market", description="Рынок и котировки"),
    BotCommand(command="price", description="Цена по тикеру"),
    BotCommand(command="buy", description="Купить актив"),
    BotCommand(command="sell", description="Продать актив"),
    BotCommand(command="portfolio", description="Портфель"),
    BotCommand(command="history", description="История сделок"),
    BotCommand(command="profile", description="Профиль и достижения"),
    BotCommand(command="quiz", description="Квиз"),
    BotCommand(command="leaderboard", description="Топ-10"),
    BotCommand(command="bonus", description="Ежедневный бонус"),
    BotCommand(command="alert", description="Ценовой алерт"),
    BotCommand(command="alerts", description="Мои алерты"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="help", description="Помощь"),
]


def build_dispatcher(market_service: MarketService) -> Dispatcher:
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    trading = TradingService(settings)
    portfolio_service = PortfolioService(market_service)

    services = {
        "market": market_service,
        "trading": trading,
        "portfolio_service": portfolio_service,
        "quiz_service": QuizService(),
        "chart_service": ChartService(),
    }
    # Сначала антифлуд (не пускает лишние запросы к БД), затем контекст.
    dp.update.outer_middleware(
        ThrottlingMiddleware(settings.RATE_LIMIT_EVENTS, settings.RATE_LIMIT_WINDOW_SECONDS)
    )
    dp.update.outer_middleware(AppContextMiddleware(settings, services))

    dp.include_router(start.router)
    dp.include_router(market.router)
    dp.include_router(trade.router)
    dp.include_router(portfolio.router)
    dp.include_router(quiz_handler.router)
    dp.include_router(settings_handler.router)
    dp.include_router(alerts.router)
    dp.errors.register(errors.error_handler)
    return dp


NETWORK_HINT = (
    "Telegram Bot API недоступен. Что проверить:\n"
    "1) Работает ли интернет/VPN на этом компьютере.\n"
    "2) MTProto-прокси, настроенный внутри приложения Telegram, НЕ проксирует "
    "Python-бота. Нужен системный VPN, либо HTTP/SOCKS5-прокси в PROXY_URL.\n"
    "   Пример: PROXY_URL=socks5://127.0.0.1:1080\n"
    "3) Прокси должен быть доступен именно с этого компьютера. Если его нет — "
    "запустите бота на VPS вне РФ."
)


async def main() -> None:
    settings.validate_runtime()
    configure_logging(settings.LOG_LEVEL, [settings.token, settings.coingecko_key or ""])
    logger.info("starting stock simulator bot")

    await init_db()
    async with async_session_factory() as session:
        removed = await CacheService(session).purge_expired()
        if removed:
            logger.info("expired_cache_purged count=%s", removed)

    http = HttpClient(
        timeout_seconds=settings.HTTP_TIMEOUT_SECONDS,
        retries=settings.HTTP_RETRIES,
        proxy_url=settings.PROXY_URL,
    )
    market_service = MarketService(settings, http)
    # AiohttpSession(proxy=...) умеет socks4/socks5/http CONNECT.
    bot_session = AiohttpSession(proxy=settings.PROXY_URL)
    bot = Bot(token=settings.token, session=bot_session)
    dp = build_dispatcher(market_service)

    try:
        try:
            me = await bot.get_me()
            logger.info(
                "telegram_connected username=@%s proxy=%s",
                me.username,
                "on" if settings.PROXY_URL else "off",
            )
        except TelegramUnauthorizedError:
            logger.error(
                "telegram_unauthorized: TELEGRAM_TOKEN отклонён. "
                "Проверьте токен в .env у @BotFather."
            )
            return
        except (
            TelegramNetworkError,
            ClientError,
            ProxyError,
            ProxyConnectionError,
            ProxyTimeoutError,
            OSError,
            asyncio.TimeoutError,
        ):
            logger.error("telegram_unreachable\n%s", NETWORK_HINT)
            return
        except TelegramAPIError as exc:
            logger.error("telegram_api_error type=%s", type(exc).__name__)
            return

        try:
            await bot.set_my_commands(COMMANDS)
        except (
            TelegramAPIError,
            ClientError,
            ProxyError,
            ProxyConnectionError,
            ProxyTimeoutError,
            OSError,
        ):
            logger.warning("set_my_commands_failed")

        alert_task: asyncio.Task | None = None
        if settings.ALERTS_ENABLED:
            alert_task = asyncio.create_task(
                alerts_run_alert_worker(bot, market_service),
                name="price-alerts",
            )

        try:
            logger.info("bot polling started")
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        except (
            TelegramNetworkError,
            ClientError,
            ProxyError,
            ProxyConnectionError,
            ProxyTimeoutError,
            OSError,
        ):
            logger.error("telegram_connection_lost\n%s", NETWORK_HINT)
        finally:
            if alert_task is not None:
                alert_task.cancel()
                try:
                    await alert_task
                except asyncio.CancelledError:
                    pass
    finally:
        await market_service.close()
        await bot.session.close()
        await dp.storage.close()
        await dispose_db()
        logger.info("bot stopped")


async def alerts_run_alert_worker(bot: Bot, market_service: MarketService) -> None:
    from services.alerts import run_alert_worker

    await run_alert_worker(bot, market_service, settings)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("shutdown requested")
