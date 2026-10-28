"""Построение графика цены через QuickChart (внешний image API).

Telegram сам загружает изображение по URL, поэтому боту не нужны
тяжёлые графические библиотеки. Если сервис недоступен, Telegram
покажет ошибку отправки — обработчик это перехватывает и присылает
текстовую сводку.
"""

from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import quote

from services.market.base import History


class ChartService:
    BASE_URL = "https://quickchart.io/chart"

    @staticmethod
    def url_for(history: History, *, title: str = "") -> str:
        points = history.points[-45:]
        if len(points) < 2:
            raise ValueError("Недостаточно данных для графика")
        labels = [point.day.strftime("%d.%m") for point in points]
        values = [float(point.close) for point in points]
        is_up = values[-1] >= values[0]
        color = "#16a34a" if is_up else "#dc2626"
        fill = "rgba(22,163,74,0.12)" if is_up else "rgba(220,38,38,0.12)"
        config = {
            "type": "line",
            "data": {
                "labels": labels,
                "datasets": [
                    {
                        "label": title or history.ticker,
                        "data": values,
                        "borderColor": color,
                        "backgroundColor": fill,
                        "borderWidth": 3,
                        "pointRadius": 0,
                        "fill": True,
                        "tension": 0.28,
                    }
                ],
            },
            "options": {
                "plugins": {"legend": {"display": False}},
                "layout": {"padding": {"top": 10, "bottom": 10, "left": 8, "right": 8}},
                "scales": {
                    "x": {
                        "grid": {"display": False},
                        "ticks": {"maxTicksLimit": 7, "font": {"size": 11}},
                    },
                    "y": {
                        "grid": {"color": "rgba(148,163,184,0.25)"},
                        "ticks": {"beginAtZero": False, "font": {"size": 11}},
                    },
                },
            },
        }
        encoded = quote(json.dumps(config, separators=(",", ":"), ensure_ascii=False))
        return (
            f"{ChartService.BASE_URL}?w=900&h=420&bkg=white"
            f"&f=png&c={encoded}"
        )

    @staticmethod
    def summary(history: History) -> tuple[Decimal, Decimal, Decimal]:
        """(минимум, максимум, изменение в %)."""
        closes = [Decimal(point.close) for point in history.points]
        low, high = min(closes), max(closes)
        change = ((closes[-1] - closes[0]) / closes[0] * 100) if closes[0] else Decimal("0")
        return low, high, change.quantize(Decimal("0.01"))
