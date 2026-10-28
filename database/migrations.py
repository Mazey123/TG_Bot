"""Небольшие автоматические миграции SQLite.

Лабораторный проект не тянет Alembic: при старте ``init_db`` сравнивает
фактические колонки с текущими моделями и аккуратно добавляет недостающие.
Это позволяет обновить старую базу (первую версию бота) без удаления
профилей и портфелей.

Миграции идемпотентны: повторный запуск ничего не меняет.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)


def _columns(connection: Connection, table: str) -> set[str]:
    return {column["name"] for column in inspect(connection).get_columns(table)}


def _add_column(connection: Connection, table: str, column: str, ddl: str) -> bool:
    if column in _columns(connection, table):
        return False
    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    logger.info("migration_add_column table=%s column=%s", table, column)
    return True


def migrate_schema(connection: Connection) -> list[str]:
    """Приводит старую SQLite-схему к текущей модели.

    Возвращает список применённых изменений (для лога/тестов).
    """
    if connection.dialect.name != "sqlite":
        return []

    applied: list[str] = []
    tables = set(inspect(connection).get_table_names())

    # --- users ---------------------------------------------------------
    if "users" in tables:
        if _add_column(connection, "users", "daily_streak", "INTEGER NOT NULL DEFAULT 0"):
            applied.append("users.daily_streak")
        if _add_column(connection, "users", "last_bonus_at", "DATETIME"):
            applied.append("users.last_bonus_at")
        if _add_column(
            connection, "users", "updated_at", "DATETIME"
        ):
            applied.append("users.updated_at")
        if _add_column(
            connection,
            "users",
            "notifications_enabled",
            "BOOLEAN NOT NULL DEFAULT 1",
        ):
            applied.append("users.notifications_enabled")

        # Старые nullable-поля не должны ломать форматирование профиля.
        connection.exec_driver_sql(
            "UPDATE users SET created_at = CURRENT_TIMESTAMP "
            "WHERE created_at IS NULL"
        )
        connection.exec_driver_sql(
            "UPDATE users SET last_active = CURRENT_TIMESTAMP "
            "WHERE last_active IS NULL"
        )
        connection.exec_driver_sql(
            "UPDATE users SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP) "
            "WHERE updated_at IS NULL"
        )
        connection.exec_driver_sql(
            "UPDATE users SET notifications_enabled = 1 "
            "WHERE notifications_enabled IS NULL"
        )

    # --- transactions --------------------------------------------------
    if "transactions" in tables:
        columns = _columns(connection, "transactions")
        if "side" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE transactions "
                "ADD COLUMN side VARCHAR(8) NOT NULL DEFAULT 'buy'"
            )
            applied.append("transactions.side")
            if "type" in columns:
                connection.exec_driver_sql(
                    "UPDATE transactions SET side = type "
                    "WHERE type IN ('buy', 'sell')"
                )
        if _add_column(
            connection,
            "transactions",
            "fee",
            "NUMERIC(18, 2) NOT NULL DEFAULT 0",
        ):
            applied.append("transactions.fee")
        if _add_column(connection, "transactions", "realized_pnl", "NUMERIC(18, 2)"):
            applied.append("transactions.realized_pnl")
        connection.exec_driver_sql(
            "UPDATE transactions SET created_at = CURRENT_TIMESTAMP "
            "WHERE created_at IS NULL"
        )

        # В старой схеме было обязательное поле type, которого больше нет
        # в модели. Без его удаления новые INSERT'ы падали бы.
        if "type" in _columns(connection, "transactions"):
            try:
                connection.exec_driver_sql(
                    "ALTER TABLE transactions DROP COLUMN type"
                )
                applied.append("transactions.drop_legacy_type")
                logger.info("migration_drop_legacy_column table=transactions column=type")
            except Exception:
                # SQLite < 3.35 не умеет DROP COLUMN. Пересобираем таблицу.
                _rebuild_transactions_without_type(connection)
                applied.append("transactions.rebuild_without_type")

        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_transactions_user_created "
            "ON transactions (user_id, created_at)"
        )

    return applied


def _rebuild_transactions_without_type(connection: Connection) -> None:
    """Fallback для старого SQLite: пересобрать transactions без type."""
    connection.exec_driver_sql("ALTER TABLE transactions RENAME TO transactions_legacy")
    # Структура повторяет модель Transaction (SQLite-совместимый DDL).
    connection.exec_driver_sql(
        """
        CREATE TABLE transactions (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT NOT NULL,
            ticker VARCHAR(12) NOT NULL,
            side VARCHAR(8) NOT NULL,
            quantity NUMERIC(18, 6) NOT NULL,
            price NUMERIC(18, 4) NOT NULL,
            total_amount NUMERIC(18, 2) NOT NULL,
            fee NUMERIC(18, 2) NOT NULL DEFAULT 0,
            realized_pnl NUMERIC(18, 2),
            created_at DATETIME,
            FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO transactions
            (id, user_id, ticker, side, quantity, price, total_amount,
             fee, realized_pnl, created_at)
        SELECT id, user_id, ticker, side, quantity, price, total_amount,
               fee, realized_pnl, created_at
        FROM transactions_legacy
        """
    )
    connection.exec_driver_sql("DROP TABLE transactions_legacy")
    logger.info("migration_rebuilt_table table=transactions")
