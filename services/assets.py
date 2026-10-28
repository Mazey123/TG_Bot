"""Справочник торгуемых инструментов.

Это не «жёстко зашитые котировки»: здесь только метаданные — какие
тикеры показывать в меню и как их сопоставить с внешними API.
Цены всегда приходят из провайдеров.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Asset:
    ticker: str
    name: str
    kind: str  # stock | crypto | fx
    currency: str
    emoji: str
    yahoo_symbol: str | None = None
    stooq_symbol: str | None = None
    coingecko_id: str | None = None
    fx_base: str | None = None
    fx_quote: str | None = None
    short_name: str = ""

    @property
    def display_name(self) -> str:
        return self.short_name or self.name


ASSETS: dict[str, Asset] = {
    # --- Акции США -----------------------------------------------------
    "AAPL": Asset("AAPL", "Apple Inc.", "stock", "USD", "🍎", "AAPL", "aapl.us"),
    "MSFT": Asset("MSFT", "Microsoft Corporation", "stock", "USD", "🪟", "MSFT", "msft.us"),
    "GOOGL": Asset("GOOGL", "Alphabet Inc.", "stock", "USD", "🔎", "GOOGL", "googl.us"),
    "AMZN": Asset("AMZN", "Amazon.com Inc.", "stock", "USD", "📦", "AMZN", "amzn.us"),
    "NVDA": Asset("NVDA", "NVIDIA Corporation", "stock", "USD", "🎮", "NVDA", "nvda.us"),
    "TSLA": Asset("TSLA", "Tesla Inc.", "stock", "USD", "🚗", "TSLA", "tsla.us"),
    "META": Asset("META", "Meta Platforms", "stock", "USD", "📱", "META", "meta.us"),
    "NFLX": Asset("NFLX", "Netflix Inc.", "stock", "USD", "🎬", "NFLX", "nflx.us"),
    "JPM": Asset("JPM", "JPMorgan Chase", "stock", "USD", "🏦", "JPM", "jpm.us"),
    "KO": Asset("KO", "The Coca-Cola Company", "stock", "USD", "🥤", "KO", "ko.us"),
    # --- Криптовалюты --------------------------------------------------
    "BTC": Asset("BTC", "Bitcoin", "crypto", "USD", "₿", coingecko_id="bitcoin"),
    "ETH": Asset("ETH", "Ethereum", "crypto", "USD", "Ξ", coingecko_id="ethereum"),
    "SOL": Asset("SOL", "Solana", "crypto", "USD", "☀️", coingecko_id="solana"),
    "TON": Asset("TON", "Toncoin", "crypto", "USD", "💎", coingecko_id="the-open-network"),
    "BNB": Asset("BNB", "BNB", "crypto", "USD", "🟡", coingecko_id="binancecoin"),
    # --- Валютные пары -------------------------------------------------
    "USDRUB": Asset(
        "USDRUB", "Доллар США / Российский рубль", "fx", "RUB", "🇺🇸",
        fx_base="USD", fx_quote="RUB", short_name="USD/RUB",
    ),
    "EURRUB": Asset(
        "EURRUB", "Евро / Российский рубль", "fx", "RUB", "🇪🇺",
        fx_base="EUR", fx_quote="RUB", short_name="EUR/RUB",
    ),
    "CNYRUB": Asset(
        "CNYRUB", "Китайский юань / Российский рубль", "fx", "RUB", "🇨🇳",
        fx_base="CNY", fx_quote="RUB", short_name="CNY/RUB",
    ),
}

KIND_TITLES = {
    "stock": "🇺🇸 Акции США",
    "crypto": "🪙 Криптовалюты",
    "fx": "💱 Валютные пары",
}

TRADABLE_KINDS = {"stock", "crypto"}

KIND_BY_ALIAS = {
    "stock": "stock",
    "stocks": "stock",
    "акции": "stock",
    "crypto": "crypto",
    "крипта": "crypto",
    "fx": "fx",
    "валюта": "fx",
}

ALIASES = {
    "APPLE": "AAPL",
    "MICROSOFT": "MSFT",
    "GOOGLE": "GOOGL",
    "ALPHABET": "GOOGL",
    "AMAZON": "AMZN",
    "TESLA": "TSLA",
    "NVIDIA": "NVDA",
    "NETFLIX": "NFLX",
    "BITCOIN": "BTC",
    "ETHEREUM": "ETH",
    "SOLANA": "SOL",
    "TONCOIN": "TON",
    "USD/RUB": "USDRUB",
    "USDRUB": "USDRUB",
    "EUR/RUB": "EURRUB",
    "EURRUB": "EURRUB",
    "CNY/RUB": "CNYRUB",
    "CNYRUB": "CNYRUB",
    "ЮАНЬ": "CNYRUB",
    "ДОЛЛАР": "USDRUB",
    "ЕВРО": "EURRUB",
}


def normalize_ticker(raw: str) -> str:
    """Нормализует пользовательский ввод, не доверяя ему."""
    value = (raw or "").strip().upper().replace(" ", "")
    value = ALIASES.get(value, value)
    return value


def get_asset(ticker: str) -> Asset | None:
    return ASSETS.get(normalize_ticker(ticker))


def assets_by_kind(kind: str) -> list[Asset]:
    return [asset for asset in ASSETS.values() if asset.kind == kind]


def tradable_assets() -> list[Asset]:
    """Валютные пары — справочная информация, не торгуемый инструмент."""
    return [asset for asset in ASSETS.values() if asset.kind in TRADABLE_KINDS]


def is_supported(ticker: str) -> bool:
    return normalize_ticker(ticker) in ASSETS


def asset_or_stub(ticker: str) -> Asset:
    """Позволяет смотреть котировки по тикерам вне меню (например, /price IBM)."""
    normalized = normalize_ticker(ticker)
    asset = ASSETS.get(normalized)
    if asset:
        return asset
    return Asset(
        ticker=normalized,
        name=normalized,
        kind="stock",
        currency="USD",
        emoji="📈",
        yahoo_symbol=normalized,
        stooq_symbol=f"{normalized.lower()}.us",
    )
