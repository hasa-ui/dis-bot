import os
import sqlite3
import tempfile
import unittest

from status_bot.store import StatusStore


class StoreMigrationTests(unittest.TestCase):
    def test_legacy_rows_migrate_into_status_tables(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.execute(
                """
                CREATE TABLE guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    light_role_id INTEGER,
                    medium_role_id INTEGER,
                    heavy_role_id INTEGER,
                    light_seconds INTEGER NOT NULL,
                    medium_seconds INTEGER NOT NULL,
                    heavy_seconds INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE sanctions (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            db.execute(
                "INSERT INTO guild_settings VALUES (1, 11, 22, 33, 86400, 172800, 259200, 123456)"
            )
            db.execute(
                "INSERT INTO sanctions VALUES (1, 99, 'heavy', 9999999999, 'legacy', 123456)"
            )
            db.commit()
            db.close()

            store = StatusStore(path)
            try:
                history_table = store.db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'status_history_records'"
                ).fetchone()
                self.assertIsNotNone(history_table)

                config = store.get_status_config(1)
                self.assertIsNotNone(config)
                self.assertEqual(config.stage_count, 3)
                self.assertEqual([stage.role_id for stage in config.stages], [11, 22, 33])

                row = store.get_status_record(1, 99)
                self.assertIsNotNone(row)
                self.assertEqual(row["stage_index"], 3)
                self.assertEqual(store.get_status_history_for_member(1, 99), [])
            finally:
                store.close()
        finally:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
