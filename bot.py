import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.environ["DISCORD_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "violations.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("status-bot")

MAX_STAGE_COUNT = 10
DEFAULT_STAGE_COUNT = 3
DEFAULT_STAGE_DURATION_DAYS = 7
SETUP_GUIDANCE = "/setup を実行してください。"

ACTION_NEXT = "next"
ACTION_CLEAR = "clear"
ACTION_HOLD = "hold"
VALID_EXPIRE_ACTIONS = {ACTION_NEXT, ACTION_CLEAR, ACTION_HOLD}
ACTION_LABELS = {
    ACTION_NEXT: "次の弱い段階へ移行",
    ACTION_CLEAR: "解除",
    ACTION_HOLD: "同じ段階を維持",
}

LEGACY_LEVEL_TO_STAGE = {
    "light": 1,
    "medium": 2,
    "heavy": 3,
}


@dataclass(frozen=True)
class StatusStageConfig:
    stage_index: int
    label: str
    role_id: Optional[int]
    duration_seconds: int
    on_expire_action: str


@dataclass(frozen=True)
class GuildStatusConfig:
    guild_id: int
    stage_count: int
    stages: list[StatusStageConfig]


def now_ts() -> int:
    return int(time.time())


def days_to_seconds(days: int) -> int:
    return days * 24 * 60 * 60


def seconds_to_days(seconds: int) -> int:
    if seconds <= 0:
        return DEFAULT_STAGE_DURATION_DAYS
    return max(1, seconds // 86400)


def format_remaining(seconds: int) -> str:
    if seconds <= 0:
        return "0分"

    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days}日")
    if hours:
        parts.append(f"{hours}時間")
    if minutes and days == 0:
        parts.append(f"{minutes}分")
    return "".join(parts) if parts else "1分未満"


def default_stage_name(stage_index: int) -> str:
    return f"段階{stage_index}"


def normalize_label(value: str) -> str:
    return value.strip()


def default_stage_action(stage_index: int) -> str:
    return ACTION_CLEAR if stage_index == 1 else ACTION_NEXT


def default_stage_config(stage_index: int) -> StatusStageConfig:
    return StatusStageConfig(
        stage_index=stage_index,
        label="",
        role_id=None,
        duration_seconds=days_to_seconds(DEFAULT_STAGE_DURATION_DAYS),
        on_expire_action=default_stage_action(stage_index),
    )


def stage_display_name(stage: StatusStageConfig) -> str:
    custom = normalize_label(stage.label)
    base = default_stage_name(stage.stage_index)
    if not custom:
        return base
    return f"{base}（{custom}）"


def build_stage_map(config: GuildStatusConfig) -> dict[int, StatusStageConfig]:
    return {stage.stage_index: stage for stage in config.stages}


def get_stage(config: GuildStatusConfig, stage_index: int) -> Optional[StatusStageConfig]:
    return build_stage_map(config).get(stage_index)


def configured_role_ids(config: Optional[GuildStatusConfig]) -> set[int]:
    if config is None:
        return set()
    return {stage.role_id for stage in config.stages if stage.role_id is not None}


def is_stage_ready(stage: Optional[StatusStageConfig]) -> bool:
    if stage is None:
        return False
    if stage.role_id is None:
        return False
    if stage.duration_seconds <= 0:
        return False
    if stage.on_expire_action not in VALID_EXPIRE_ACTIONS:
        return False
    if stage.stage_index == 1 and stage.on_expire_action == ACTION_NEXT:
        return False
    return True


def config_complete(config: Optional[GuildStatusConfig]) -> bool:
    if config is None:
        return False
    if not 1 <= config.stage_count <= MAX_STAGE_COUNT:
        return False
    if len(config.stages) != config.stage_count:
        return False
    return all(is_stage_ready(stage) for stage in config.stages)


def stages_ready_up_to(config: Optional[GuildStatusConfig], stage_index: int) -> bool:
    if config is None:
        return False
    if not 1 <= stage_index <= config.stage_count:
        return False

    stage_map = build_stage_map(config)
    for idx in range(1, stage_index + 1):
        if not is_stage_ready(stage_map.get(idx)):
            return False
    return True


def format_role_setting(guild: discord.Guild, role_id: Optional[int]) -> str:
    if role_id is None:
        return "未設定"
    role = guild.get_role(role_id)
    if role is None:
        return f"見つからないロール (ID: {role_id})"
    return role.mention


def format_duration_setting(seconds: Optional[int]) -> str:
    if seconds is None or seconds <= 0:
        return "未設定"
    return f"{seconds // 86400}日"


def describe_stage_expire_action(stage: StatusStageConfig, config: GuildStatusConfig) -> str:
    if stage.on_expire_action == ACTION_CLEAR:
        return "解除"
    if stage.on_expire_action == ACTION_HOLD:
        return f"{stage_display_name(stage)}を維持"

    next_stage = get_stage(config, stage.stage_index - 1)
    if next_stage is None:
        return "未設定"
    return f"{stage_display_name(next_stage)}へ移行"


def get_missing_setup_items(config: Optional[GuildStatusConfig]) -> list[str]:
    if config is None:
        return ["段階数"]

    missing = []
    for stage in config.stages:
        name = default_stage_name(stage.stage_index)
        if stage.role_id is None:
            missing.append(f"{name}ロール")
        if stage.duration_seconds <= 0:
            missing.append(f"{name}期間")
        if stage.on_expire_action not in VALID_EXPIRE_ACTIONS:
            missing.append(f"{name}満了時動作")
        if stage.stage_index == 1 and stage.on_expire_action == ACTION_NEXT:
            missing.append(f"{name}満了時動作")
    return missing


def build_stage_summary_lines(guild: discord.Guild, config: GuildStatusConfig) -> list[str]:
    lines = []
    for stage in reversed(config.stages):
        lines.append(
            f"- {stage_display_name(stage)}: "
            f"ロール {format_role_setting(guild, stage.role_id)} / "
            f"期間 {format_duration_setting(stage.duration_seconds)} / "
            f"満了時 {describe_stage_expire_action(stage, config)}"
        )
    return lines


def build_setup_home_message(guild: discord.Guild, *, notice: Optional[str] = None) -> str:
    config = bot.get_status_config(guild.id)
    lines = ["ステータス設定セットアップ"]

    if notice:
        lines.append(notice)
        lines.append("")

    if config is None:
        lines.append("- 設定状態: 未設定")
        lines.append("- 段階数: 未設定")
        lines.append("先に段階数を設定してください。")
        lines.append("下のボタンから段階数設定または段階編集を行えます。")
        return "\n".join(lines)

    lines.append(f"- 設定状態: {'完了' if config_complete(config) else '未完了'}")
    lines.append(f"- 段階数: {config.stage_count}")
    lines.extend(build_stage_summary_lines(guild, config))

    missing = get_missing_setup_items(config)
    if missing:
        lines.append(f"- 未設定項目: {', '.join(missing)}")
        lines.append("ステータス付与を使う前に /setup を完了してください。")

    lines.append("下のボタンから段階数設定または段階編集を行えます。")
    return "\n".join(lines)


def build_status_config_message(guild: discord.Guild) -> str:
    config = bot.get_status_config(guild.id)
    if config is None:
        return "このサーバーにはまだステータス設定がありません。\n先に /setup を実行してください。"

    lines = ["現在のステータス設定"]
    lines.append(f"- 設定状態: {'完了' if config_complete(config) else '未完了'}")
    lines.append(f"- 段階数: {config.stage_count}")
    lines.extend(build_stage_summary_lines(guild, config))

    missing = get_missing_setup_items(config)
    if missing:
        lines.append(f"- 未設定項目: {', '.join(missing)}")
        lines.append(f"設定変更は {SETUP_GUIDANCE}")

    return "\n".join(lines)


def build_status_count_save_message(stage_count: int, refreshed: int, failed: int) -> str:
    return (
        f"このサーバーの段階数を {stage_count} に保存しました。\n"
        f"- 既存ステータス保持者への再適用: {refreshed}件中 {failed}件失敗"
    )


def build_stage_save_message(stage: StatusStageConfig, refreshed: int, failed: int) -> str:
    return (
        f"{stage_display_name(stage)} の設定を保存しました。\n"
        f"- 期間: {format_duration_setting(stage.duration_seconds)}\n"
        f"- 満了時: {ACTION_LABELS[stage.on_expire_action]}\n"
        f"- 既存ステータス保持者への再適用: {refreshed}件中 {failed}件失敗"
    )


def build_stage_editor_message(
    guild: discord.Guild,
    config: GuildStatusConfig,
    stage: StatusStageConfig,
    *,
    selected_role: Optional[discord.Role],
    duration_days: int,
    selected_action: str,
    notice: Optional[str] = None,
) -> str:
    lines = [f"ステータス段階編集 ({stage.stage_index}/{config.stage_count})"]

    if notice:
        lines.append(notice)
        lines.append("")

    lines.append(f"- 表示名: {stage_display_name(stage)}")
    lines.append(f"- ロール: {selected_role.mention if selected_role is not None else '未設定'}")
    lines.append(f"- 期間: {duration_days}日")

    draft_stage = StatusStageConfig(
        stage_index=stage.stage_index,
        label=stage.label,
        role_id=selected_role.id if selected_role is not None else None,
        duration_seconds=days_to_seconds(duration_days),
        on_expire_action=selected_action,
    )
    lines.append(f"- 満了時: {describe_stage_expire_action(draft_stage, config)}")
    lines.append("ロール選択・詳細編集・満了時動作を調整して保存してください。")
    return "\n".join(lines)


def parse_duration_days(value: str, label: str) -> int:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{label}の日数を入力してください。")
    if not stripped.isdigit():
        raise ValueError(f"{label}の日数は整数で入力してください。")

    days = int(stripped)
    if not 1 <= days <= 3650:
        raise ValueError(f"{label}の日数は 1〜3650 の範囲で入力してください。")
    return days


def parse_stage_count(value: str) -> int:
    stripped = value.strip()
    if not stripped:
        raise ValueError("段階数を入力してください。")
    if not stripped.isdigit():
        raise ValueError("段階数は整数で入力してください。")

    count = int(stripped)
    if not 1 <= count <= MAX_STAGE_COUNT:
        raise ValueError(f"段階数は 1〜{MAX_STAGE_COUNT} の範囲で入力してください。")
    return count


def can_manage_target(guild: discord.Guild, target: discord.Member) -> tuple[bool, str]:
    me = guild.me
    if me is None:
        return False, "Botのメンバー情報が取得できません。"

    if target.id == guild.owner_id:
        return False, "サーバーオーナーには変更できません。"

    if target == me:
        return False, "Bot自身には実行できません。"

    if target.top_role >= me.top_role:
        return False, "Botより上位または同位のロールを持つ相手には変更できません。"

    return True, ""


def has_manage_roles(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    if not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.manage_roles


def has_manage_guild(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    if not isinstance(interaction.user, discord.Member):
        return False
    perms = interaction.user.guild_permissions
    return perms.manage_guild or perms.administrator


def describe_record_next_change(config: GuildStatusConfig, row: sqlite3.Row) -> str:
    current_stage = get_stage(config, row["stage_index"])
    if current_stage is None:
        return "不明"

    if row["expires_at"] is None:
        return "なし（現在の段階を維持中）"

    remaining = max(0, row["expires_at"] - now_ts())
    return f"{format_remaining(remaining)}後に {describe_stage_expire_action(current_stage, config)}"


def validate_stage_configuration(config: GuildStatusConfig, replacement: StatusStageConfig) -> None:
    if replacement.role_id is None:
        raise ValueError(f"{default_stage_name(replacement.stage_index)}のロールを選択してください。")
    if replacement.duration_seconds <= 0:
        raise ValueError(f"{default_stage_name(replacement.stage_index)}の期間を設定してください。")
    if replacement.on_expire_action not in VALID_EXPIRE_ACTIONS:
        raise ValueError("満了時動作が不正です。")
    if replacement.stage_index == 1 and replacement.on_expire_action == ACTION_NEXT:
        raise ValueError("段階1は次の弱い段階へ移行できません。解除か維持を選択してください。")

    seen: dict[int, int] = {}
    for stage in config.stages:
        current = replacement if stage.stage_index == replacement.stage_index else stage
        if current.role_id is None:
            continue
        if current.role_id in seen:
            raise ValueError(
                f"{default_stage_name(seen[current.role_id])} と "
                f"{default_stage_name(current.stage_index)} で同じロールは使えません。"
            )
        seen[current.role_id] = current.stage_index


class StatusBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

        self.db = sqlite3.connect(DB_PATH)
        self.db.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        # 旧テーブルは移行元として残す。
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
        self.db.commit()
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
                StatusStageConfig(1, "", row["light_role_id"], row["light_seconds"], ACTION_CLEAR),
                StatusStageConfig(2, "", row["medium_role_id"], row["medium_seconds"], ACTION_NEXT),
                StatusStageConfig(3, "", row["heavy_role_id"], row["heavy_seconds"], ACTION_NEXT),
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
            self.db.commit()

    async def setup_hook(self) -> None:
        await self.tree.sync()
        self.expiry_loop.start()

    async def close(self) -> None:
        if self.expiry_loop.is_running():
            self.expiry_loop.cancel()
        self.db.close()
        await super().close()

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

    def upsert_stage_count(self, guild_id: int, stage_count: int) -> None:
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
        current_map = build_stage_map(current) if current is not None else {}
        for stage_index in range(1, stage_count + 1):
            if stage_index in current_map:
                continue
            self.upsert_status_stage(guild_id, default_stage_config(stage_index))

    def set_stage_count(self, guild_id: int, stage_count: int) -> None:
        previous = self.get_status_config(guild_id)
        previous_count = previous.stage_count if previous is not None else 0

        self.upsert_stage_count(guild_id, stage_count)
        self.ensure_stage_rows(guild_id, stage_count)

        if previous_count > stage_count:
            self.db.execute(
                """
                UPDATE status_records
                SET stage_index = ?, updated_at = ?
                WHERE guild_id = ? AND stage_index > ?
                """,
                (stage_count, now_ts(), guild_id, stage_count),
            )
            self.db.execute(
                "DELETE FROM guild_status_stages WHERE guild_id = ? AND stage_index > ?",
                (guild_id, stage_count),
            )

        self.db.commit()

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
        self.db.commit()

    def delete_status_record(self, guild_id: int, user_id: int) -> None:
        self.db.execute(
            "DELETE FROM status_records WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        self.db.commit()

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

    async def fetch_member_if_needed(self, guild_id: int, user_id: int) -> Optional[discord.Member]:
        guild = self.get_guild(guild_id)
        if guild is None:
            logger.warning("Guild not found in cache: %s", guild_id)
            return None

        member = guild.get_member(user_id)
        if member is not None:
            return member

        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None

    async def apply_status_role(
        self,
        guild_id: int,
        user_id: int,
        stage_index: Optional[int],
        *,
        reason: str,
        remove_role_ids: Optional[set[int]] = None,
    ) -> None:
        guild = self.get_guild(guild_id)
        if guild is None:
            logger.warning("Guild not found: %s", guild_id)
            return

        member = await self.fetch_member_if_needed(guild_id, user_id)
        if member is None:
            logger.info("Member %s is not currently in guild %s", user_id, guild_id)
            return

        config = self.get_status_config(guild_id)
        configured_ids = configured_role_ids(config)
        if remove_role_ids is not None:
            configured_ids.update(remove_role_ids)

        new_roles = [role for role in member.roles if role.id not in configured_ids]

        if stage_index is not None:
            if config is None:
                raise RuntimeError(f"このサーバーのステータス設定が未完了です。{SETUP_GUIDANCE}")

            stage = get_stage(config, stage_index)
            if not is_stage_ready(stage):
                raise RuntimeError(
                    f"{default_stage_name(stage_index)} の設定が未完了です。{SETUP_GUIDANCE}"
                )

            role = guild.get_role(stage.role_id)
            if role is None:
                raise RuntimeError(
                    f"{stage_display_name(stage)} のロールが見つかりません。設定を見直してください。"
                )
            new_roles.append(role)

        await member.edit(roles=new_roles, reason=reason)

    async def reconcile_record(self, row: sqlite3.Row) -> None:
        guild_id = row["guild_id"]
        user_id = row["user_id"]
        stage_index = row["stage_index"]
        expires_at = row["expires_at"]
        reason = row["reason"]

        config = self.get_status_config(guild_id)
        if config is None:
            logger.warning("Guild %s has no status config; skipping reconcile for user %s", guild_id, user_id)
            return

        if expires_at is None:
            return

        current_ts = now_ts()
        changed = False
        stage_map = build_stage_map(config)

        while expires_at is not None and expires_at <= current_ts:
            current_stage = stage_map.get(stage_index)
            if not is_stage_ready(current_stage):
                logger.warning(
                    "Guild %s stage %s is incomplete; skipping reconcile for user %s",
                    guild_id,
                    stage_index,
                    user_id,
                )
                return

            if current_stage.on_expire_action == ACTION_CLEAR:
                self.delete_status_record(guild_id, user_id)
                try:
                    await self.apply_status_role(
                        guild_id,
                        user_id,
                        None,
                        reason="Status expired -> cleared",
                    )
                except discord.Forbidden:
                    logger.exception("Failed to clear status roles for user %s", user_id)
                except RuntimeError:
                    logger.exception("Failed to clear status roles for user %s", user_id)
                return

            if current_stage.on_expire_action == ACTION_HOLD:
                self.upsert_status_record(guild_id, user_id, stage_index, None, reason)
                try:
                    await self.apply_status_role(
                        guild_id,
                        user_id,
                        stage_index,
                        reason=f"Status expiry -> hold stage {stage_index}",
                    )
                except discord.Forbidden:
                    logger.exception("Failed to hold status roles for user %s", user_id)
                except RuntimeError:
                    logger.exception("Failed to hold status roles for user %s", user_id)
                return

            next_stage = stage_map.get(stage_index - 1)
            if not is_stage_ready(next_stage):
                logger.warning(
                    "Guild %s next stage %s is incomplete; skipping reconcile for user %s",
                    guild_id,
                    stage_index - 1,
                    user_id,
                )
                return

            stage_index = stage_index - 1
            expires_at = expires_at + next_stage.duration_seconds
            changed = True

        if not changed:
            return

        self.upsert_status_record(guild_id, user_id, stage_index, expires_at, reason)
        try:
            await self.apply_status_role(
                guild_id,
                user_id,
                stage_index,
                reason=f"Status auto-transitioned -> stage {stage_index}",
            )
        except discord.Forbidden:
            logger.exception("Failed to update status roles for user %s", user_id)
        except RuntimeError:
            logger.exception("Failed to update status roles for user %s", user_id)

    async def refresh_guild_status_roles(
        self,
        guild_id: int,
        *,
        remove_role_ids: Optional[set[int]] = None,
    ) -> tuple[int, int]:
        total = 0
        failed = 0

        for row in self.get_active_records_by_guild(guild_id):
            await self.reconcile_record(row)

            current = self.get_status_record(guild_id, row["user_id"])
            if current is None:
                continue

            total += 1
            try:
                await self.apply_status_role(
                    guild_id,
                    current["user_id"],
                    current["stage_index"],
                    reason="Refreshed status roles after config change",
                    remove_role_ids=remove_role_ids,
                )
            except discord.Forbidden:
                failed += 1
                logger.exception("Failed to refresh status roles for user %s", current["user_id"])
            except RuntimeError:
                failed += 1
                logger.exception("Failed to refresh status roles for user %s", current["user_id"])

        return total, failed

    @tasks.loop(minutes=1)
    async def expiry_loop(self) -> None:
        due = self.get_due_records(now_ts())
        for row in due:
            await self.reconcile_record(row)

    @expiry_loop.before_loop
    async def before_expiry_loop(self) -> None:
        await self.wait_until_ready()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")

    async def on_member_join(self, member: discord.Member) -> None:
        row = self.get_status_record(member.guild.id, member.id)
        if row is None:
            return

        await self.reconcile_record(row)

        row = self.get_status_record(member.guild.id, member.id)
        if row is None:
            return

        try:
            await self.apply_status_role(
                member.guild.id,
                member.id,
                row["stage_index"],
                reason="Re-applied active status on rejoin",
            )
        except discord.Forbidden:
            logger.exception("Failed to re-apply status roles on rejoin for user %s", member.id)
        except RuntimeError:
            logger.exception("Failed to re-apply status roles on rejoin for user %s", member.id)


bot = StatusBot()


async def save_stage_count_settings(guild_id: int, stage_count: int) -> tuple[int, int]:
    previous = bot.get_status_config(guild_id)
    if previous is not None and stage_count < previous.stage_count:
        if bot.count_records_above_stage(guild_id, stage_count) > 0:
            target_stage = get_stage(previous, stage_count)
            if not is_stage_ready(target_stage):
                raise ValueError(
                    f"段階数を {stage_count} に減らす前に {default_stage_name(stage_count)} を設定してください。"
                )

    previous_role_ids = configured_role_ids(previous)
    bot.set_stage_count(guild_id, stage_count)
    return await bot.refresh_guild_status_roles(
        guild_id,
        remove_role_ids=previous_role_ids,
    )


async def save_stage_settings(guild_id: int, stage: StatusStageConfig) -> tuple[int, int]:
    config = bot.get_status_config(guild_id)
    if config is None:
        raise ValueError("先に段階数を設定してください。")
    if not 1 <= stage.stage_index <= config.stage_count:
        raise ValueError("存在しない段階です。")

    validate_stage_configuration(config, stage)
    previous_role_ids = configured_role_ids(config)
    bot.upsert_status_stage(guild_id, stage)
    bot.db.commit()
    return await bot.refresh_guild_status_roles(
        guild_id,
        remove_role_ids=previous_role_ids,
    )


class OwnerOnlyView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.message: Optional[discord.InteractionMessage] = None

    async def bind_message(self, interaction: discord.Interaction) -> None:
        self.message = await interaction.original_response()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この setup 画面はコマンド実行者のみ操作できます。",
                ephemeral=True,
            )
            return False

        if not has_manage_guild(interaction):
            await interaction.response.send_message(
                "Manage Server 権限か管理者権限が必要です。",
                ephemeral=True,
            )
            return False

        return True


class StageCountModal(discord.ui.Modal, title="段階数設定"):
    def __init__(self, home_view: "SetupHomeView", guild: discord.Guild) -> None:
        super().__init__()
        self.home_view = home_view
        self.owner_id = home_view.owner_id
        config = bot.get_status_config(guild.id)
        self.stage_count_input = discord.ui.TextInput(
            label="段階数",
            default=str(config.stage_count if config is not None else DEFAULT_STAGE_COUNT),
            placeholder=f"1〜{MAX_STAGE_COUNT}",
            min_length=1,
            max_length=2,
        )
        self.add_item(self.stage_count_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この setup 画面はコマンド実行者のみ操作できます。",
                ephemeral=True,
            )
            return

        if not has_manage_guild(interaction):
            await interaction.response.send_message(
                "Manage Server 権限か管理者権限が必要です。",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        try:
            stage_count = parse_stage_count(self.stage_count_input.value)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        await interaction.response.defer()
        try:
            refreshed, failed = await save_stage_count_settings(guild.id, stage_count)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        notice = build_status_count_save_message(stage_count, refreshed, failed)
        new_view = SetupHomeView(self.owner_id)
        content = build_setup_home_message(guild, notice=notice)

        if self.home_view.message is not None:
            await self.home_view.message.edit(content=content, view=new_view)
            new_view.message = self.home_view.message
            return

        await interaction.followup.send(content, view=new_view, ephemeral=True)


class SetupHomeView(OwnerOnlyView):
    @discord.ui.button(label="段階数設定", style=discord.ButtonStyle.primary)
    async def configure_stage_count(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        await interaction.response.send_modal(StageCountModal(self, guild))

    @discord.ui.button(label="段階編集", style=discord.ButtonStyle.secondary)
    async def configure_stages(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        config = bot.get_status_config(guild.id)
        if config is None:
            await interaction.response.send_message(
                "先に段階数を設定してください。",
                ephemeral=True,
            )
            return

        stage_view = StageSetupView(self.owner_id, guild, config.stage_count)
        await interaction.response.edit_message(
            content=stage_view.render_content(),
            view=stage_view,
        )
        await stage_view.bind_message(interaction)

    @discord.ui.button(label="再表示", style=discord.ButtonStyle.secondary)
    async def refresh_home(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        new_view = SetupHomeView(self.owner_id)
        await interaction.response.edit_message(
            content=build_setup_home_message(guild),
            view=new_view,
        )
        await new_view.bind_message(interaction)


class StageActionSelect(discord.ui.Select):
    def __init__(self, stage_view: "StageSetupView") -> None:
        options = []
        if stage_view.stage_index > 1:
            options.append(
                discord.SelectOption(
                    label=ACTION_LABELS[ACTION_NEXT],
                    value=ACTION_NEXT,
                    default=stage_view.selected_action == ACTION_NEXT,
                )
            )
        options.append(
            discord.SelectOption(
                label=ACTION_LABELS[ACTION_CLEAR],
                value=ACTION_CLEAR,
                default=stage_view.selected_action == ACTION_CLEAR,
            )
        )
        options.append(
            discord.SelectOption(
                label=ACTION_LABELS[ACTION_HOLD],
                value=ACTION_HOLD,
                default=stage_view.selected_action == ACTION_HOLD,
            )
        )
        super().__init__(
            placeholder="満了時動作を選択",
            options=options,
            row=1,
        )
        self.stage_view = stage_view

    async def callback(self, interaction: discord.Interaction) -> None:
        self.stage_view.selected_action = self.values[0]
        self.stage_view.notice = None
        await interaction.response.edit_message(
            content=self.stage_view.render_content(),
            view=self.stage_view,
        )
        await self.stage_view.bind_message(interaction)


class StageRoleSelect(discord.ui.RoleSelect):
    def __init__(self, stage_view: "StageSetupView") -> None:
        defaults = [stage_view.selected_role] if stage_view.selected_role is not None else []
        super().__init__(
            placeholder=f"{default_stage_name(stage_view.stage_index)}のロールを選択",
            min_values=1,
            max_values=1,
            default_values=defaults,
            row=0,
        )
        self.stage_view = stage_view

    async def callback(self, interaction: discord.Interaction) -> None:
        self.stage_view.selected_role = self.values[0]
        self.stage_view.notice = None
        await interaction.response.edit_message(
            content=self.stage_view.render_content(),
            view=self.stage_view,
        )
        await self.stage_view.bind_message(interaction)


class StageDetailsModal(discord.ui.Modal, title="段階詳細編集"):
    def __init__(self, stage_view: "StageSetupView") -> None:
        super().__init__()
        self.stage_view = stage_view
        self.owner_id = stage_view.owner_id

        stage = stage_view.current_stage_config()
        self.label_input = discord.ui.TextInput(
            label="表示名（空なら既定名）",
            default=stage.label,
            required=False,
            max_length=50,
        )
        self.duration_input = discord.ui.TextInput(
            label="期間（日数）",
            default=str(stage_view.duration_days),
            placeholder="1〜3650",
            min_length=1,
            max_length=4,
        )
        self.add_item(self.label_input)
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この setup 画面はコマンド実行者のみ操作できます。",
                ephemeral=True,
            )
            return

        if not has_manage_guild(interaction):
            await interaction.response.send_message(
                "Manage Server 権限か管理者権限が必要です。",
                ephemeral=True,
            )
            return

        try:
            duration_days = parse_duration_days(
                self.duration_input.value,
                default_stage_name(self.stage_view.stage_index),
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        self.stage_view.label_value = normalize_label(self.label_input.value)
        self.stage_view.duration_days = duration_days
        self.stage_view.notice = "詳細を更新しました。保存すると反映されます。"

        await interaction.response.defer()
        if self.stage_view.message is not None:
            await self.stage_view.message.edit(
                content=self.stage_view.render_content(),
                view=self.stage_view,
            )


class StageSetupView(OwnerOnlyView):
    def __init__(
        self,
        owner_id: int,
        guild: discord.Guild,
        stage_index: int,
        *,
        notice: Optional[str] = None,
    ) -> None:
        super().__init__(owner_id)
        self.guild_id = guild.id
        self.guild_name = guild.name
        config = bot.get_status_config(guild.id)
        if config is None:
            config = GuildStatusConfig(guild.id, 1, [default_stage_config(1)])

        self.stage_count = config.stage_count
        self.stage_index = max(1, min(stage_index, self.stage_count))
        self.notice = notice
        self._persisted_config = config
        persisted_stage = get_stage(config, self.stage_index) or default_stage_config(self.stage_index)

        self.label_value = persisted_stage.label
        self.duration_days = seconds_to_days(persisted_stage.duration_seconds)
        self.selected_action = (
            ACTION_CLEAR
            if self.stage_index == 1 and persisted_stage.on_expire_action == ACTION_NEXT
            else persisted_stage.on_expire_action
        )
        self.selected_role = (
            guild.get_role(persisted_stage.role_id) if persisted_stage.role_id is not None else None
        )

        self.add_item(StageRoleSelect(self))
        self.add_item(StageActionSelect(self))

    def current_stage_config(self) -> StatusStageConfig:
        return StatusStageConfig(
            stage_index=self.stage_index,
            label=self.label_value,
            role_id=self.selected_role.id if self.selected_role is not None else None,
            duration_seconds=days_to_seconds(self.duration_days),
            on_expire_action=self.selected_action,
        )

    def render_content(self) -> str:
        guild = bot.get_guild(self.guild_id)
        if guild is None:
            return "サーバー情報が見つかりません。"
        return build_stage_editor_message(
            guild,
            self._persisted_config,
            self.current_stage_config(),
            selected_role=self.selected_role,
            duration_days=self.duration_days,
            selected_action=self.selected_action,
            notice=self.notice,
        )

    @discord.ui.button(label="前の段階", style=discord.ButtonStyle.secondary, row=2)
    async def previous_stage(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        new_view = StageSetupView(self.owner_id, guild, max(1, self.stage_index - 1))
        await interaction.response.edit_message(content=new_view.render_content(), view=new_view)
        await new_view.bind_message(interaction)

    @discord.ui.button(label="詳細編集", style=discord.ButtonStyle.secondary, row=2)
    async def edit_details(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(StageDetailsModal(self))

    @discord.ui.button(label="保存", style=discord.ButtonStyle.success, row=2)
    async def save_stage(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            refreshed, failed = await save_stage_settings(guild.id, self.current_stage_config())
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        config = bot.get_status_config(guild.id)
        current_stage = get_stage(config, self.stage_index) if config is not None else None
        notice = build_stage_save_message(
            current_stage or self.current_stage_config(),
            refreshed,
            failed,
        )
        new_view = StageSetupView(self.owner_id, guild, self.stage_index, notice=notice)
        await interaction.edit_original_response(
            content=new_view.render_content(),
            view=new_view,
        )
        await new_view.bind_message(interaction)

    @discord.ui.button(label="戻る", style=discord.ButtonStyle.secondary, row=2)
    async def back_to_home(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        new_view = SetupHomeView(self.owner_id)
        await interaction.response.edit_message(
            content=build_setup_home_message(guild),
            view=new_view,
        )
        await new_view.bind_message(interaction)

    @discord.ui.button(label="次の段階", style=discord.ButtonStyle.secondary, row=2)
    async def next_stage(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        new_view = StageSetupView(
            self.owner_id,
            guild,
            min(self.stage_count, self.stage_index + 1),
        )
        await interaction.response.edit_message(content=new_view.render_content(), view=new_view)
        await new_view.bind_message(interaction)


@bot.tree.command(name="setup", description="このサーバーのステータス設定をまとめて行います")
async def setup(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    if not has_manage_guild(interaction):
        await interaction.response.send_message(
            "Manage Server 権限か管理者権限が必要です。",
            ephemeral=True,
        )
        return

    view = SetupHomeView(interaction.user.id)
    await interaction.response.send_message(
        build_setup_home_message(interaction.guild),
        view=view,
        ephemeral=True,
    )
    await view.bind_message(interaction)


@bot.tree.command(name="status_config", description="このサーバーのステータス設定を表示します")
async def status_config(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    await interaction.response.send_message(
        build_status_config_message(interaction.guild),
        ephemeral=True,
    )


@bot.tree.command(name="status_set", description="ステータスロールを付与または上書きします")
@app_commands.describe(member="対象メンバー", stage="対象段階 (1〜10)", reason="理由")
async def status_set(
    interaction: discord.Interaction,
    member: discord.Member,
    stage: app_commands.Range[int, 1, MAX_STAGE_COUNT],
    reason: Optional[str] = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    if not has_manage_roles(interaction):
        await interaction.response.send_message("Manage Roles 権限が必要です。", ephemeral=True)
        return

    ok, msg = can_manage_target(interaction.guild, member)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return

    config = bot.get_status_config(interaction.guild.id)
    if config is None:
        await interaction.response.send_message(
            f"このサーバーのステータス設定が未完了です。\n先に {SETUP_GUIDANCE}",
            ephemeral=True,
        )
        return

    if stage > config.stage_count:
        await interaction.response.send_message(
            f"このサーバーは 1〜{config.stage_count} 段階だけ設定されています。",
            ephemeral=True,
        )
        return

    if not stages_ready_up_to(config, stage):
        await interaction.response.send_message(
            f"{default_stage_name(stage)} までの設定が未完了です。\n先に {SETUP_GUIDANCE}",
            ephemeral=True,
        )
        return

    current_stage = get_stage(config, stage)
    if current_stage is None:
        await interaction.response.send_message("段階設定の取得に失敗しました。", ephemeral=True)
        return

    reason = reason or ""
    expires_at = now_ts() + current_stage.duration_seconds

    bot.upsert_status_record(
        interaction.guild.id,
        member.id,
        stage,
        expires_at,
        reason,
    )

    try:
        await bot.apply_status_role(
            interaction.guild.id,
            member.id,
            stage,
            reason=f"Manual status set by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "ロール変更に失敗しました。Botの Manage Roles 権限とロール順を確認してください。",
            ephemeral=True,
        )
        return
    except RuntimeError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    row = bot.get_status_record(interaction.guild.id, member.id)
    next_change = describe_record_next_change(config, row) if row is not None else "不明"
    await interaction.response.send_message(
        f"{member.mention} に {stage_display_name(current_stage)} を付与しました。\n"
        f"- 次回変更: {next_change}\n"
        f"- 理由: {reason or '（なし）'}",
        ephemeral=True,
    )


@bot.tree.command(name="status_clear", description="ステータスロールを即時解除します")
@app_commands.describe(member="対象メンバー")
async def status_clear(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    if not has_manage_roles(interaction):
        await interaction.response.send_message("Manage Roles 権限が必要です。", ephemeral=True)
        return

    ok, msg = can_manage_target(interaction.guild, member)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return

    bot.delete_status_record(interaction.guild.id, member.id)

    try:
        await bot.apply_status_role(
            interaction.guild.id,
            member.id,
            None,
            reason=f"Manual status clear by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "ロール解除に失敗しました。Botの Manage Roles 権限とロール順を確認してください。",
            ephemeral=True,
        )
        return
    except RuntimeError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    await interaction.response.send_message(
        f"{member.mention} のステータスロールを解除しました。",
        ephemeral=True,
    )


@bot.tree.command(name="status_view", description="現在のステータス状態を確認します")
@app_commands.describe(member="対象メンバー")
async def status_view(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    row = bot.get_status_record(interaction.guild.id, member.id)
    if row is None:
        await interaction.response.send_message(
            f"{member.mention} に有効なステータス状態はありません。",
            ephemeral=True,
        )
        return

    await bot.reconcile_record(row)
    row = bot.get_status_record(interaction.guild.id, member.id)
    if row is None:
        await interaction.response.send_message(
            f"{member.mention} に有効なステータス状態はありません。",
            ephemeral=True,
        )
        return

    config = bot.get_status_config(interaction.guild.id)
    if config is None:
        await interaction.response.send_message(
            f"このサーバーのステータス設定が未完了です。\n先に {SETUP_GUIDANCE}",
            ephemeral=True,
        )
        return

    current_stage = get_stage(config, row["stage_index"])
    if current_stage is None:
        await interaction.response.send_message("段階設定の取得に失敗しました。", ephemeral=True)
        return

    await interaction.response.send_message(
        f"{member.mention} の現在状態\n"
        f"- 現在: {stage_display_name(current_stage)}\n"
        f"- 次回変更: {describe_record_next_change(config, row)}\n"
        f"- 理由: {row['reason'] or '（なし）'}",
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    logger.exception("App command error: %s", error)

    if interaction.response.is_done():
        await interaction.followup.send("エラーが発生しました。ログを確認してください。", ephemeral=True)
    else:
        await interaction.response.send_message("エラーが発生しました。ログを確認してください。", ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
