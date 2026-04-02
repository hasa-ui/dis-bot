import sqlite3
from typing import Optional

from .config import LEGACY_LEVEL_TO_STAGE
from .models import GuildStatusConfig, StatusStageConfig
from .validation import default_stage_config, now_ts


class StatusStore:
    def __init__(self, db_path: str) -> None:
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id        INTEGER PRIMARY KEY,
                light_role_id   INTEGER,
                medium_role_id  INTEGER,
                heavy_role_id   INTEGER,
                light_seconds   INTEGER NOT NULL DEFAULT 604800,
                medium_seconds  INTEGER NOT NULL DEFAULT 1209600,
                heavy_seconds   INTEGER NOT NULL DEFAULT 5184000,
                updated_at      INTEGER NOT NULL
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS sanctions (
                guild_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                level      TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                reason     TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_status_settings (
                guild_id    INTEGER PRIMARY KEY,
                stage_count INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_status_stages (
                guild_id          INTEGER NOT NULL,
                stage_index       INTEGER NOT NULL,
                label             TEXT NOT NULL DEFAULT '',
                role_id           INTEGER,
                duration_seconds  INTEGER NOT NULL,
                on_expire_action  TEXT NOT NULL,
                updated_at        INTEGER NOT NULL,
                PRIMARY KEY (guild_id, stage_index)
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS status_records (
                guild_id    INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                stage_index INTEGER NOT NULL,
                expires_at  INTEGER,
                reason      TEXT NOT NULL DEFAULT '',
                updated_at  INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        self.commit()
        self._migrate_legacy_data()

    def _migrate_legacy_data(self) -> None:
        changed = False
        legacy_settings = self.db.execute(
            """
            SELECT guild_id, light_role_id, medium_role_id, heavy_role_id,
                   light_seconds, medium_seconds, heavy_seconds, updated_at
            FROM guild_settings
            """
        ).fetchall()

        for row in legacy_settings:
            exists = self.db.execute(
                "SELECT 1 FROM guild_status_settings WHERE guild_id = ?",
                (row["guild_id"],),
            ).fetchone()
            if exists is not None:
                continue

            updated_at = row["updated_at"] or now_ts()
            self.db.execute(
                """
                INSERT INTO guild_status_settings (guild_id, stage_count, updated_at)
                VALUES (?, ?, ?)
                """,
                (row["guild_id"], 3, updated_at),
            )
            stages = (
                StatusStageConfig(1, "", row["light_role_id"], row["light_seconds"], "clear"),
                StatusStageConfig(2, "", row["medium_role_id"], row["medium_seconds"], "next"),
                StatusStageConfig(3, "", row["heavy_role_id"], row["heavy_seconds"], "next"),
            )
            for stage in stages:
                self.db.execute(
                    """
                    INSERT INTO guild_status_stages (
                        guild_id, stage_index, label, role_id,
                        duration_seconds, on_expire_action, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["guild_id"],
                        stage.stage_index,
                        stage.label,
                        stage.role_id,
                        stage.duration_seconds,
                        stage.on_expire_action,
                        updated_at,
                    ),
                )
            changed = True

        legacy_records = self.db.execute(
            """
            SELECT guild_id, user_id, level, expires_at, reason, updated_at
            FROM sanctions
            """
        ).fetchall()
        for row in legacy_records:
            stage_index = LEGACY_LEVEL_TO_STAGE.get(row["level"])
            if stage_index is None:
                continue
            self.db.execute(
                """
                INSERT INTO status_records (
                    guild_id, user_id, stage_index, expires_at, reason, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO NOTHING
                """,
                (
                    row["guild_id"],
                    row["user_id"],
                    stage_index,
                    row["expires_at"],
                    row["reason"],
                    row["updated_at"],
                ),
            )
            changed = True

        if changed:
            self.commit()

    def close(self) -> None:
        self.db.close()

    def commit(self) -> None:
        self.db.commit()

    def get_status_config(self, guild_id: int) -> Optional[GuildStatusConfig]:
        settings_row = self.db.execute(
            """
            SELECT guild_id, stage_count, updated_at
            FROM guild_status_settings
            WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()
        if settings_row is None:
            return None

        stage_rows = self.db.execute(
            """
            SELECT guild_id, stage_index, label, role_id,
                   duration_seconds, on_expire_action, updated_at
            FROM guild_status_stages
            WHERE guild_id = ?
            ORDER BY stage_index ASC
            """,
            (guild_id,),
        ).fetchall()
        loaded = {
            row["stage_index"]: StatusStageConfig(
                stage_index=row["stage_index"],
                label=row["label"],
                role_id=row["role_id"],
                duration_seconds=row["duration_seconds"],
                on_expire_action=row["on_expire_action"],
            )
            for row in stage_rows
        }
        stages = [
            loaded.get(idx, default_stage_config(idx))
            for idx in range(1, settings_row["stage_count"] + 1)
        ]
        return GuildStatusConfig(
            guild_id=guild_id,
            stage_count=settings_row["stage_count"],
            stages=stages,
        )

    def set_stage_count_value(self, guild_id: int, stage_count: int) -> None:
        self.db.execute(
            """
            INSERT INTO guild_status_settings (guild_id, stage_count, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                stage_count = excluded.stage_count,
                updated_at = excluded.updated_at
            """,
            (guild_id, stage_count, now_ts()),
        )

    def upsert_status_stage(self, guild_id: int, stage: StatusStageConfig) -> None:
        self.db.execute(
            """
            INSERT INTO guild_status_stages (
                guild_id, stage_index, label, role_id,
                duration_seconds, on_expire_action, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, stage_index) DO UPDATE SET
                label = excluded.label,
                role_id = excluded.role_id,
                duration_seconds = excluded.duration_seconds,
                on_expire_action = excluded.on_expire_action,
                updated_at = excluded.updated_at
            """,
            (
                guild_id,
                stage.stage_index,
                stage.label,
                stage.role_id,
                stage.duration_seconds,
                stage.on_expire_action,
                now_ts(),
            ),
        )

    def ensure_stage_rows(self, guild_id: int, stage_count: int) -> None:
        current = self.get_status_config(guild_id)
        existing = {stage.stage_index for stage in current.stages} if current is not None else set()
        for stage_index in range(1, stage_count + 1):
            if stage_index not in existing:
                self.upsert_status_stage(guild_id, default_stage_config(stage_index))

    def delete_stages_above(self, guild_id: int, stage_count: int) -> None:
        self.db.execute(
            "DELETE FROM guild_status_stages WHERE guild_id = ? AND stage_index > ?",
            (guild_id, stage_count),
        )

    def count_records_above_stage(self, guild_id: int, stage_index: int) -> int:
        row = self.db.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM status_records
            WHERE guild_id = ? AND stage_index > ?
            """,
            (guild_id, stage_index),
        ).fetchone()
        return 0 if row is None else row["cnt"]

    def clamp_records_to_stage(
        self,
        guild_id: int,
        stage_index: int,
        expires_at: Optional[int],
    ) -> None:
        self.db.execute(
            """
            UPDATE status_records
            SET
                stage_index = ?,
                expires_at = CASE
                    WHEN expires_at IS NULL THEN NULL
                    WHEN ? IS NULL THEN expires_at
                    ELSE ?
                END,
                updated_at = ?
            WHERE guild_id = ? AND stage_index > ?
            """,
            (
                stage_index,
                expires_at,
                expires_at,
                now_ts(),
                guild_id,
                stage_index,
            ),
        )

    def get_status_record(self, guild_id: int, user_id: int):
        return self.db.execute(
            """
            SELECT guild_id, user_id, stage_index, expires_at, reason, updated_at
            FROM status_records
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

    def upsert_status_record(
        self,
        guild_id: int,
        user_id: int,
        stage_index: int,
        expires_at: Optional[int],
        reason: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO status_records (
                guild_id, user_id, stage_index, expires_at, reason, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                stage_index = excluded.stage_index,
                expires_at = excluded.expires_at,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, stage_index, expires_at, reason, now_ts()),
        )

    def delete_status_record(self, guild_id: int, user_id: int) -> None:
        self.db.execute(
            "DELETE FROM status_records WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )

    def get_due_records(self, current_ts: int):
        return self.db.execute(
            """
            SELECT guild_id, user_id, stage_index, expires_at, reason, updated_at
            FROM status_records
            WHERE expires_at IS NOT NULL AND expires_at <= ?
            ORDER BY expires_at ASC
            """,
            (current_ts,),
        ).fetchall()

    def get_active_records_by_guild(self, guild_id: int):
        return self.db.execute(
            """
            SELECT guild_id, user_id, stage_index, expires_at, reason, updated_at
            FROM status_records
            WHERE guild_id = ?
            ORDER BY updated_at ASC
            """,
            (guild_id,),
        ).fetchall()
