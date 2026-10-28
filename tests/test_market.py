from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from services.assets import ASSETS, get_asset, normalize_ticker
from services.chart import ChartService
from services.market.base import ProviderError
from services.market.providers import (
    DemoProvider,
    parse_coingecko_quote,
    parse_frankfurter_history,
    parse_frankfurter_quote,
    parse_stooq_quote,
    parse_yahoo_history,
    parse_yahoo_quote,
)


class ParserTests(unittest.TestCase):
    def test_yahoo_quote_and_history(self) -> None:
        asset = ASSETS["AAPL"]
        payload = {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {
                            "currency": "USD",
                            "regularMarketPrice": 232.5,
                            "previousClose": 230.0,
                            "regularMarketTime": 1730000000,
                            "regularMarketDayHigh": 233.0,
                            "regularMarketDayLow": 229.0,
                            "regularMarketVolume": 1000000,
                        },
                        "timestamp": [1729900000, 1730000000],
                        "indicators": {"quote": [{"close": [230.0, 232.5]}]},
                    }
                ],
            }
        }
        quote = parse_yahoo_quote(payload, asset)
        self.assertEqual(quote.price, Decimal("232.5"))
        self.assertEqual(quote.change_percent, Decimal("1.09"))
        self.assertEqual(quote.source, "Yahoo Finance")

        history_payload = {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {},
                        "timestamp": [1729900000, 1730000000],
                        "indicators": {"quote": [{"close": [230.0, 232.5]}]},
                    }
                ],
            }
        }
        history = parse_yahoo_history(history_payload, asset)
        self.assertEqual(len(history.points), 2)
        self.assertEqual(history.points[-1].close, Decimal("232.5"))

    def test_stooq_csv(self) -> None:
        text = (
            "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
            "AAPL.US,2026-10-24,22:00:00,230.0,234.0,228.5,232.0,1234567\n"
        )
        quote = parse_stooq_quote(text, ASSETS["AAPL"])
        self.assertEqual(quote.price, Decimal("232.0"))
        self.assertEqual(quote.change_percent, Decimal("0.87"))
        self.assertEqual(quote.source, "Stooq")

    def test_stooq_missing_ticker(self) -> None:
        text = (
            "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
            "NOPE.US,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"
        )
        with self.assertRaises(ProviderError):
            parse_stooq_quote(text, ASSETS["AAPL"])

    def test_coingecko(self) -> None:
        payload = [
            {
                "id": "bitcoin",
                "current_price": 68400.0,
                "price_change_percentage_24h": 1.25,
                "market_cap": 1_350_000_000_000,
                "high_24h": 69000,
                "low_24h": 67000,
                "total_volume": 30_000_000_000,
                "last_updated": "2026-10-27T12:00:00.000Z",
            }
        ]
        quote = parse_coingecko_quote(payload, ASSETS["BTC"])
        self.assertEqual(quote.price, Decimal("68400.0"))
        self.assertEqual(quote.change_percent, Decimal("1.25"))
        self.assertEqual(quote.currency, "USD")

    def test_frankfurter(self) -> None:
        asset = ASSETS["USDRUB"]
        quote = parse_frankfurter_quote(
            {"amount": 1, "base": "USD", "date": "2026-10-27", "rates": {"RUB": 92.5}},
            asset,
        )
        self.assertEqual(quote.price, Decimal("92.5"))
        history = parse_frankfurter_history(
            {
                "amount": 1,
                "base": "USD",
                "rates": {
                    "2026-10-25": {"RUB": 92.0},
                    "2026-10-26": {"RUB": 92.5},
                },
            },
            asset,
        )
        self.assertEqual(len(history.points), 2)
        self.assertEqual(history.points[-1].day, date(2026, 10, 26))

    def test_demo_is_deterministic_and_has_history(self) -> None:
        provider = DemoProvider()
        first = provider.quote_sync(ASSETS["BTC"])
        second = provider.quote_sync(ASSETS["BTC"])
        self.assertEqual(first.price, second.price)
        self.assertTrue(first.is_fallback)
        history = provider.history_sync(ASSETS["BTC"], days=30)
        self.assertEqual(len(history.points), 30)
        self.assertTrue(history.is_fallback)

    def test_aliases(self) -> None:
        self.assertEqual(normalize_ticker("bitcoin"), "BTC")
        self.assertEqual(normalize_ticker("usd/rub"), "USDRUB")
        self.assertEqual(get_asset("apple").ticker, "AAPL")

    def test_chart_url_and_summary(self) -> None:
        history = DemoProvider().history_sync(ASSETS["BTC"], days=30)
        url = ChartService.url_for(history, title="Bitcoin")
        self.assertTrue(url.startswith("https://quickchart.io/chart?"))
        self.assertLess(len(url), 4096)
        low, high, change = ChartService.summary(history)
        self.assertLessEqual(low, high)
        self.assertEqual(change, Decimal(change).quantize(Decimal("0.01")))


if __name__ == "__main__":
    unittest.main()
