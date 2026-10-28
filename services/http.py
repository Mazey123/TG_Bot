"""HTTP-клиент с таймаутами, повторами и аккуратной обработкой ошибок."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any
from urllib.parse import urljoin

import aiohttp
import certifi
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError
from aiohttp_socks import ProxyConnector

from services.market.base import ProviderError

logger = logging.getLogger(__name__)


class HttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 6.0,
        retries: int = 2,
        user_agent: str = "StockLabBot/1.0",
        proxy_url: str | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries = max(1, retries)
        self.user_agent = user_agent
        self.proxy_url = proxy_url
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            kwargs: dict[str, Any] = {
                "timeout": timeout,
                "headers": {
                    "User-Agent": self.user_agent,
                    "Accept": "application/json,text/csv,*/*",
                },
            }
            if self.proxy_url:
                # aiohttp_socks умеет и HTTP CONNECT, и SOCKS4/SOCKS5.
                ssl_context = ssl.create_default_context(cafile=certifi.where())
                kwargs["connector"] = ProxyConnector.from_url(
                    self.proxy_url, ssl=ssl_context, rdns=True
                )
            self._session = aiohttp.ClientSession(**kwargs)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        provider: str = "http",
    ) -> str:
        await self.start()
        assert self._session is not None

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                async with self._session.get(url, params=params, headers=headers) as response:
                    if response.status in (429, 500, 502, 503, 504):
                        raise ProviderError(
                            provider,
                            f"временная ошибка HTTP {response.status}",
                            status=response.status,
                        )
                    if response.status >= 400:
                        raise ProviderError(
                            provider,
                            f"HTTP {response.status}",
                            status=response.status,
                        )
                    return await response.text()
            except (
                aiohttp.ClientError,
                ProxyError,
                ProxyConnectionError,
                ProxyTimeoutError,
                asyncio.TimeoutError,
            ) as exc:
                last_error = exc
                logger.warning(
                    "http_request_failed provider=%s attempt=%s/%s error=%s",
                    provider,
                    attempt,
                    self.retries,
                    type(exc).__name__,
                )
            except ProviderError as exc:
                last_error = exc
                if exc.status not in (429, 500, 502, 503, 504):
                    raise
                logger.warning(
                    "http_temporary_error provider=%s attempt=%s/%s status=%s",
                    provider,
                    attempt,
                    self.retries,
                    exc.status,
                )
            if attempt < self.retries:
                await asyncio.sleep(0.35 * (2 ** (attempt - 1)))

        raise ProviderError(provider, f"сервис недоступен ({type(last_error).__name__})")

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        provider: str = "http",
    ) -> Any:
        text = await self.get_text(url, params=params, headers=headers, provider=provider)
        try:
            return json.loads(text)
        except (TypeError, ValueError) as exc:
            raise ProviderError(provider, "некорректный JSON в ответе") from exc


def join_url(base: str, path: str) -> str:
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))
