"""Inline-клавиатуры. Callback data короткая и предсказуемая."""

from __future__ import annotations

from decimal import Decimal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from services.assets import Asset, get_asset


def _rows(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def main_menu() -> InlineKeyboardMarkup:
    return _rows(
        [
            [("📈 Рынок", "mkt:menu"), ("💼 Портфель", "pf:view")],
            [("🎯 Квиз", "quiz:menu"), ("🏆 Лидеры", "pf:top")],
            [("👤 Профиль", "pf:profile"), ("⚙️ Настройки", "set:view")],
            [("❓ Помощь", "menu:help")],
        ]
    )


def back_to_menu() -> InlineKeyboardMarkup:
    return _rows([[("🏠 В меню", "menu:main")]])


def back_to_market() -> InlineKeyboardMarkup:
    return _rows(
        [
            [("📈 К рынку", "mkt:menu"), ("🏠 В меню", "menu:main")],
        ]
    )


def market_menu() -> InlineKeyboardMarkup:
    return _rows(
        [
            [("🇺🇸 Акции", "mkt:list:stock"), ("🪙 Крипта", "mkt:list:crypto")],
            [("💱 Валюты", "mkt:list:fx"), ("🔎 Поиск тикера", "mkt:search")],
            [("⭐ Купить", "trade:start:buy"), ("🏠 В меню", "menu:main")],
        ]
    )


def assets_list(assets: list[Asset], quotes: dict) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    pair: list[tuple[str, str]] = []
    for asset in assets:
        quote = quotes.get(asset.ticker)
        if quote is not None:
            price = Decimal(quote.price)
            label = f"{asset.emoji} {asset.display_name} · {_short_price(price, quote.currency)}"
        else:
            label = f"{asset.emoji} {asset.display_name}"
        pair.append((label, f"mkt:view:{asset.ticker}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([("🏠 В меню", "menu:main")])
    return _rows(rows)


def _short_price(price: Decimal, currency: str = "USD") -> str:
    symbol = "$" if currency == "USD" else "₽" if currency == "RUB" else ""
    if price >= 1000:
        return f"{symbol}{price:,.0f}"
    if price >= 10:
        return f"{symbol}{price:,.2f}"
    return f"{symbol}{price:,.4f}"


def asset_detail(
    ticker: str, *, in_portfolio: bool = False, tradable: bool = True
) -> InlineKeyboardMarkup:
    rows = []
    if tradable:
        rows.append(
            [
                ("🟢 Купить", f"trade:pick:buy:{ticker}"),
                ("🔴 Продать", f"trade:pick:sell:{ticker}"),
            ]
        )
    rows.append([("📈 График", f"mkt:chart:{ticker}"), ("🔔 Алерт", f"alert:help:{ticker}")])
    if in_portfolio:
        rows.append([("💼 Мой портфель", "pf:view")])
    rows.append([("⬅️ К рынку", "mkt:menu"), ("🏠 В меню", "menu:main")])
    return _rows(rows)


def trade_asset_picker(side: str, assets: list[Asset]) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    pair: list[tuple[str, str]] = []
    for asset in assets:
        pair.append((f"{asset.emoji} {asset.display_name}", f"trade:pick:{side}:{asset.ticker}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([("⌨️ Ввести тикер", "trade:manual")])
    rows.append([("🏠 В меню", "menu:main")])
    return _rows(rows)


def quantity_keyboard(side: str, ticker: str) -> InlineKeyboardMarkup:
    rows = [
        [("1 шт.", "trade:qty:1"), ("5 шт.", "trade:qty:5"), ("10 шт.", "trade:qty:10")],
    ]
    if side == "sell":
        rows.append([("📤 Продать всё", "trade:qty:all")])
    rows.append([("⌨️ Своё количество", "trade:qty:custom")])
    rows.append([("❌ Отмена", "trade:cancel")])
    return _rows(rows)


def trade_confirm(side: str, ticker: str) -> InlineKeyboardMarkup:
    action = "✅ Купить" if side == "buy" else "✅ Продать"
    return _rows(
        [
            [(action, "trade:confirm"), ("❌ Отмена", "trade:cancel")],
        ]
    )


def trade_result(ticker: str) -> InlineKeyboardMarkup:
    return _rows(
        [
            [("📈 К инструменту", f"mkt:view:{ticker}"), ("💼 Портфель", "pf:view")],
            [("🏠 В меню", "menu:main")],
        ]
    )


def chart_keyboard(ticker: str) -> InlineKeyboardMarkup:
    return _rows(
        [
            [("🟢 Купить", f"trade:pick:buy:{ticker}"), ("🔴 Продать", f"trade:pick:sell:{ticker}")],
            [("⬅️ К инструменту", f"mkt:view:{ticker}")],
        ]
    )


def settings_keyboard(notifications_enabled: bool) -> InlineKeyboardMarkup:
    notif = "🔔 Уведомления: ВКЛ" if notifications_enabled else "🔕 Уведомления: ВЫКЛ"
    return _rows(
        [
            [(notif, "set:notify")],
            [("🧹 Сбросить прогресс", "set:reset")],
            [("🏠 В меню", "menu:main")],
        ]
    )


def reset_confirm() -> InlineKeyboardMarkup:
    return _rows(
        [
            [("⚠️ Да, сбросить всё", "set:reset_confirm")],
            [("❌ Нет, оставить", "set:view")],
        ]
    )


def quiz_categories() -> InlineKeyboardMarkup:
    return _rows(
        [
            [("📈 Акции и биржа", "quiz:cat:stocks")],
            [("🪙 Криптовалюты", "quiz:cat:crypto")],
            [("💡 Финграмотность", "quiz:cat:finance")],
            [("🏠 В меню", "menu:main")],
        ]
    )


def quiz_options(question_index: int, options: tuple[str, ...]) -> InlineKeyboardMarkup:
    letters = ("A", "B", "C", "D")
    rows = [
        [(f"{letters[index]}. {option[:48]}", f"quiz:a:{question_index}:{index}")]
        for index, option in enumerate(options)
    ]
    return _rows(rows)


def quiz_next(question_index: int) -> InlineKeyboardMarkup:
    return _rows([[("➡️ Дальше", f"quiz:next:{question_index}")]])


def quiz_result() -> InlineKeyboardMarkup:
    return _rows(
        [
            [("🔁 Ещё раз", "quiz:menu")],
            [("🏆 Лидеры", "pf:top"), ("🏠 В меню", "menu:main")],
        ]
    )


def portfolio_menu(has_positions: bool) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = [
        [("📜 История сделок", "pf:history")],
    ]
    if has_positions:
        rows.append([("🟢 Купить", "trade:start:buy"), ("🔴 Продать", "trade:start:sell")])
    else:
        rows.append([("📈 На рынок", "mkt:menu")])
    rows.append([("🏠 В меню", "menu:main")])
    return _rows(rows)


def alerts_list(alerts: list) -> InlineKeyboardMarkup:
    rows = []
    for alert in alerts:
        asset = get_asset(alert.ticker)
        currency = asset.currency if asset else "USD"
        symbol = "₽" if currency == "RUB" else "$"
        rows.append(
            [
                (
                    f"❌ {alert.ticker} "
                    f"{'≥' if alert.direction == 'above' else '≤'} "
                    f"{symbol}{Decimal(alert.target_price):,.2f}",
                    f"alert:del:{alert.id}",
                )
            ]
        )
    rows.append([("🏠 В меню", "menu:main")])
    return _rows(rows)


def back_menu_only() -> InlineKeyboardMarkup:
    return _rows([[("🏠 В меню", "menu:main")]])
