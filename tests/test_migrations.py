from __future__ import annotations

import unittest

from sqlalchemy import create_engine, inspect

from database.migrations import migrate_schema


LEGACY_SQL = [
    """
    CREATE TABLE users (
        id INTEGER NOT NULL PRIMARY KEY,
        username VARCHAR(255),
        first_name VARCHAR(255),
        balance FLOAT NOT NULL,
        xp INTEGER NOT NULL,
        level INTEGER NOT NULL,
        notifications_enabled BOOLEAN,
        created_at DATETIME,
        last_active DATETIME
    )
    """,
    """
    CREATE TABLE transactions (
        id INTEGER NOT NULL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        ticker VARCHAR(10) NOT NULL,
        type VARCHAR(10) NOT NULL,
        quantity FLOAT NOT NULL,
        price FLOAT NOT NULL,
        total_amount FLOAT NOT NULL,
        created_at DATETIME,
        FOREIGN KEY(user_id) REFERENCES users (id)
    )
    """,
    "INSERT INTO users VALUES (1, 'legacy', 'Legacy', 10000, 0, 1, 1, "
    "'2026-01-01 12:00:00', '2026-01-01 12:00:00')",
    "INSERT INTO transactions VALUES "
    "(1, 1, 'AAPL', 'buy', 2, 100, 200, '2026-01-01 12:05:00')",
]


class MigrationTests(unittest.TestCase):
    def test_legacy_schema_is_upgraded_without_data_loss(self) -> None:
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            for statement in LEGACY_SQL:
                connection.exec_driver_sql(statement)

            applied = migrate_schema(connection)
            self.assertIn("users.daily_streak", applied)
            self.assertIn("transactions.side", applied)

            user_columns = {
                column["name"] for column in inspect(connection).get_columns("users")
            }
            self.assertIn("daily_streak", user_columns)
            self.assertIn("last_bonus_at", user_columns)
            self.assertIn("updated_at", user_columns)

            transaction_columns = {
                column["name"]
                for column in inspect(connection).get_columns("transactions")
            }
            self.assertIn("side", transaction_columns)
            self.assertIn("fee", transaction_columns)
            self.assertIn("realized_pnl", transaction_columns)
            self.assertNotIn("type", transaction_columns)

            side = connection.exec_driver_sql(
                "SELECT side FROM transactions WHERE id = 1"
            ).scalar_one()
            self.assertEqual(side, "buy")

            # Новые INSERT'ы должны работать без legacy-колонки type.
            connection.exec_driver_sql(
                "INSERT INTO transactions "
                "(user_id, ticker, side, quantity, price, total_amount, fee) "
                "VALUES (1, 'BTC', 'sell', 1, 100, 100, 0)"
            )

            # Повторная миграция идемпотентна.
            self.assertEqual(migrate_schema(connection), [])
        engine.dispose()

    def test_fresh_schema_requires_no_migration(self) -> None:
        from database.models import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            self.assertEqual(migrate_schema(connection), [])
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
