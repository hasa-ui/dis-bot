import os
import time
import sqlite3
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.environ["DISCORD_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "violations.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("violation-bot")


def now_ts() -> int:
    return int(time.time())


def days_to_seconds(days: int) -> int:
    return days * 24 * 60 * 60


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


ROLE_LABELS = {
    "light": "🟢 軽度違反",
    "medium": "🟡 中度違反",
    "heavy": "⚫ 重度違反",
}
LEVEL_NAMES = {
    "light": "軽度",
    "medium": "中度",
    "heavy": "重度",
}
ROLE_LEVELS = ("light", "medium", "heavy")
SETUP_GUIDANCE = "/setup を実行してください。必要なら /config_roles と /config_durations でも設定できます。"


def next_level(level: str) -> Optional[str]:
    if level == "heavy":
        return "medium"
    if level == "medium":
        return "light"
    if level == "light":
        return None
    raise ValueError(f"Unknown level: {level}")


def role_id_from_settings(settings: sqlite3.Row, level: str) -> Optional[int]:
    if level == "light":
        return settings["light_role_id"]
    if level == "medium":
        return settings["medium_role_id"]
    if level == "heavy":
        return settings["heavy_role_id"]
    raise ValueError(f"Unknown level: {level}")


def duration_from_settings(settings: sqlite3.Row, level: str) -> int:
    if level == "light":
        return settings["light_seconds"]
    if level == "medium":
        return settings["medium_seconds"]
    if level == "heavy":
        return settings["heavy_seconds"]
    raise ValueError(f"Unknown level: {level}")


def role_ids_from_settings(settings: Optional[sqlite3.Row]) -> set[int]:
    if settings is None:
        return set()

    role_ids = set()
    for key in ("light_role_id", "medium_role_id", "heavy_role_id"):
        value = settings[key]
        if value is not None:
            role_ids.add(value)
    return role_ids


def settings_complete(settings: Optional[sqlite3.Row]) -> bool:
    if settings is None:
        return False
    return (
        settings["light_role_id"] is not None and
        settings["medium_role_id"] is not None and
        settings["heavy_role_id"] is not None and
        settings["light_seconds"] > 0 and
        settings["medium_seconds"] > 0 and
        settings["heavy_seconds"] > 0
    )


class ViolationBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True

        super().__init__(command_prefix="!", intents=intents)

        self.db = sqlite3.connect(DB_PATH)
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
        self.db.commit()

    async def setup_hook(self) -> None:
        await self.tree.sync()
        self.expiry_loop.start()

    async def close(self) -> None:
        if self.expiry_loop.is_running():
            self.expiry_loop.cancel()
        self.db.close()
        await super().close()

    # ===== guild settings =====

    def get_guild_settings(self, guild_id: int):
        cur = self.db.execute(
            """
            SELECT guild_id, light_role_id, medium_role_id, heavy_role_id,
                   light_seconds, medium_seconds, heavy_seconds, updated_at
            FROM guild_settings
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
        return cur.fetchone()

    def upsert_guild_roles(
        self,
        guild_id: int,
        light_role_id: int,
        medium_role_id: int,
        heavy_role_id: int,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO guild_settings (
                guild_id, light_role_id, medium_role_id, heavy_role_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                light_role_id = excluded.light_role_id,
                medium_role_id = excluded.medium_role_id,
                heavy_role_id = excluded.heavy_role_id,
                updated_at = excluded.updated_at
            """,
            (guild_id, light_role_id, medium_role_id, heavy_role_id, now_ts()),
        )
        self.db.commit()

    def upsert_guild_durations(
        self,
        guild_id: int,
        light_seconds: int,
        medium_seconds: int,
        heavy_seconds: int,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO guild_settings (
                guild_id, light_seconds, medium_seconds, heavy_seconds, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                light_seconds = excluded.light_seconds,
                medium_seconds = excluded.medium_seconds,
                heavy_seconds = excluded.heavy_seconds,
                updated_at = excluded.updated_at
            """,
            (guild_id, light_seconds, medium_seconds, heavy_seconds, now_ts()),
        )
        self.db.commit()

    # ===== sanctions =====

    def get_record(self, guild_id: int, user_id: int):
        cur = self.db.execute(
            """
            SELECT guild_id, user_id, level, expires_at, reason, updated_at
            FROM sanctions
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )
        return cur.fetchone()

    def upsert_record(
        self,
        guild_id: int,
        user_id: int,
        level: str,
        expires_at: int,
        reason: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO sanctions (guild_id, user_id, level, expires_at, reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                level = excluded.level,
                expires_at = excluded.expires_at,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, level, expires_at, reason, now_ts()),
        )
        self.db.commit()

    def delete_record(self, guild_id: int, user_id: int) -> None:
        self.db.execute(
            "DELETE FROM sanctions WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        self.db.commit()

    def get_due_records(self, current_ts: int):
        cur = self.db.execute(
            """
            SELECT guild_id, user_id, level, expires_at, reason, updated_at
            FROM sanctions
            WHERE expires_at <= ?
            ORDER BY expires_at ASC
            """,
            (current_ts,),
        )
        return cur.fetchall()

    def get_active_records_by_guild(self, guild_id: int):
        cur = self.db.execute(
            """
            SELECT guild_id, user_id, level, expires_at, reason, updated_at
            FROM sanctions
            WHERE guild_id = ?
            ORDER BY updated_at ASC
            """,
            (guild_id,),
        )
        return cur.fetchall()

    # ===== discord helpers =====

    async def fetch_member_if_needed(
        self, guild_id: int, user_id: int
    ) -> Optional[discord.Member]:
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

    async def apply_violation_role(
        self,
        guild_id: int,
        user_id: int,
        level: Optional[str],
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

        settings = self.get_guild_settings(guild_id)
        configured_role_ids = role_ids_from_settings(settings)
        if remove_role_ids is not None:
            configured_role_ids.update(remove_role_ids)

        new_roles = [r for r in member.roles if r.id not in configured_role_ids]

        if level is not None:
            if not settings_complete(settings):
                raise RuntimeError(f"このサーバーの違反設定が未完了です。{SETUP_GUIDANCE}")

            role_id = role_id_from_settings(settings, level)
            role = guild.get_role(role_id)
            if role is None:
                raise RuntimeError(f"{ROLE_LABELS[level]} のロールが見つかりません。設定を見直してください。")
            new_roles.append(role)

        await member.edit(roles=new_roles, reason=reason)

    async def reconcile_record(self, row: sqlite3.Row) -> None:
        guild_id = row["guild_id"]
        user_id = row["user_id"]
        level = row["level"]
        expires_at = row["expires_at"]
        reason = row["reason"]

        settings = self.get_guild_settings(guild_id)
        if not settings_complete(settings):
            logger.warning("Guild %s settings are incomplete; skipping reconcile for user %s", guild_id, user_id)
            return

        current_ts = now_ts()
        changed = False

        while level is not None and expires_at <= current_ts:
            nxt = next_level(level)
            if nxt is None:
                level = None
                expires_at = None
            else:
                level = nxt
                expires_at = expires_at + duration_from_settings(settings, level)
            changed = True

        if not changed:
            return

        if level is None:
            self.delete_record(guild_id, user_id)
            try:
                await self.apply_violation_role(
                    guild_id,
                    user_id,
                    None,
                    reason="Violation expired -> cleared",
                )
            except discord.Forbidden:
                logger.exception("Failed to clear roles for user %s", user_id)
            except RuntimeError:
                logger.exception("Failed to clear roles for user %s", user_id)
            return

        self.upsert_record(guild_id, user_id, level, expires_at, reason)
        try:
            await self.apply_violation_role(
                guild_id,
                user_id,
                level,
                reason=f"Violation auto-downgraded -> {level}",
            )
        except discord.Forbidden:
            logger.exception("Failed to update roles for user %s", user_id)
        except RuntimeError:
            logger.exception("Failed to update roles for user %s", user_id)

    async def refresh_guild_violation_roles(
        self,
        guild_id: int,
        *,
        remove_role_ids: Optional[set[int]] = None,
    ) -> tuple[int, int]:
        total = 0
        failed = 0

        for row in self.get_active_records_by_guild(guild_id):
            await self.reconcile_record(row)

            current = self.get_record(guild_id, row["user_id"])
            if current is None:
                continue

            total += 1
            try:
                await self.apply_violation_role(
                    guild_id,
                    current["user_id"],
                    current["level"],
                    reason="Refreshed violation roles after config_roles",
                    remove_role_ids=remove_role_ids,
                )
            except discord.Forbidden:
                failed += 1
                logger.exception("Failed to refresh roles for user %s", current["user_id"])
            except RuntimeError:
                failed += 1
                logger.exception("Failed to refresh roles for user %s", current["user_id"])

        return total, failed

    # ===== background task =====

    @tasks.loop(minutes=1)
    async def expiry_loop(self) -> None:
        due = self.get_due_records(now_ts())
        for row in due:
            await self.reconcile_record(row)

    @expiry_loop.before_loop
    async def before_expiry_loop(self) -> None:
        await self.wait_until_ready()

    # ===== events =====

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")

    async def on_member_join(self, member: discord.Member) -> None:
        row = self.get_record(member.guild.id, member.id)
        if row is None:
            return

        await self.reconcile_record(row)

        row = self.get_record(member.guild.id, member.id)
        if row is None:
            return

        try:
            await self.apply_violation_role(
                member.guild.id,
                member.id,
                row["level"],
                reason="Re-applied active violation on rejoin",
            )
        except discord.Forbidden:
            logger.exception("Failed to re-apply roles on rejoin for user %s", member.id)
        except RuntimeError:
            logger.exception("Failed to re-apply roles on rejoin for user %s", member.id)


bot = ViolationBot()


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


LEVEL_CHOICES = [
    app_commands.Choice(name="🟢 軽度違反", value="light"),
    app_commands.Choice(name="🟡 中度違反", value="medium"),
    app_commands.Choice(name="⚫ 重度違反", value="heavy"),
]


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


def build_settings_summary_lines(guild: discord.Guild) -> list[str]:
    settings = bot.get_guild_settings(guild.id)
    lines = []

    for level in ROLE_LEVELS:
        role_id = settings[f"{level}_role_id"] if settings is not None else None
        seconds = settings[f"{level}_seconds"] if settings is not None else None
        lines.append(f"- {LEVEL_NAMES[level]}ロール: {format_role_setting(guild, role_id)}")
        lines.append(f"- {LEVEL_NAMES[level]}期間: {format_duration_setting(seconds)}")

    return lines


def get_missing_setup_items(settings: Optional[sqlite3.Row]) -> list[str]:
    missing = []

    for level in ROLE_LEVELS:
        if settings is None or settings[f"{level}_role_id"] is None:
            missing.append(f"{LEVEL_NAMES[level]}ロール")
        if settings is None or settings[f"{level}_seconds"] <= 0:
            missing.append(f"{LEVEL_NAMES[level]}期間")

    return missing


def build_setup_home_message(guild: discord.Guild, *, notice: Optional[str] = None) -> str:
    settings = bot.get_guild_settings(guild.id)
    missing = get_missing_setup_items(settings)
    lines = ["違反設定セットアップ"]

    if notice:
        lines.append(notice)
        lines.append("")

    lines.append(f"- 設定状態: {'完了' if settings_complete(settings) else '未完了'}")
    lines.extend(build_settings_summary_lines(guild))

    if missing:
        lines.append(f"- 未設定項目: {', '.join(missing)}")
        lines.append("違反付与を使う前に /setup を完了してください。")

    lines.append("下のボタンからロール設定または期間設定を行えます。")
    return "\n".join(lines)


def build_role_setup_message(selected_roles: dict[str, Optional[discord.Role]]) -> str:
    lines = ["違反ロール設定"]

    for level in ROLE_LEVELS:
        role = selected_roles[level]
        lines.append(f"- {LEVEL_NAMES[level]}: {role.mention if role is not None else '未選択'}")

    lines.append("3つとも別のロールを選んで保存してください。")
    return "\n".join(lines)


def build_role_save_message(
    light: discord.Role,
    medium: discord.Role,
    heavy: discord.Role,
    refreshed: int,
    failed: int,
) -> str:
    return (
        "このサーバーの違反ロールを保存しました。\n"
        f"- 軽度: {light.mention}\n"
        f"- 中度: {medium.mention}\n"
        f"- 重度: {heavy.mention}\n"
        f"- 既存違反者への再適用: {refreshed}件中 {failed}件失敗"
    )


def build_duration_save_message(light_days: int, medium_days: int, heavy_days: int) -> str:
    return (
        "このサーバーの違反期間を保存しました。\n"
        f"- 軽度: {light_days}日\n"
        f"- 中度: {medium_days}日\n"
        f"- 重度: {heavy_days}日\n"
        "既存違反者の現在の期限は変わりません。次回の降格以降から新しい期間が適用されます。"
    )


async def save_guild_role_settings(
    guild_id: int,
    light_role_id: int,
    medium_role_id: int,
    heavy_role_id: int,
) -> tuple[int, int]:
    previous_role_ids = role_ids_from_settings(bot.get_guild_settings(guild_id))
    bot.upsert_guild_roles(guild_id, light_role_id, medium_role_id, heavy_role_id)
    return await bot.refresh_guild_violation_roles(
        guild_id,
        remove_role_ids=previous_role_ids,
    )


def save_guild_duration_settings(
    guild_id: int,
    light_days: int,
    medium_days: int,
    heavy_days: int,
) -> None:
    bot.upsert_guild_durations(
        guild_id,
        days_to_seconds(light_days),
        days_to_seconds(medium_days),
        days_to_seconds(heavy_days),
    )


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


class OwnerOnlyView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=600)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            "この setup 画面はコマンド実行者のみ操作できます。",
            ephemeral=True,
        )
        return False


class SetupHomeView(OwnerOnlyView):
    @discord.ui.button(label="ロール設定", style=discord.ButtonStyle.primary)
    async def configure_roles(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        await interaction.response.edit_message(
            content=build_role_setup_message(load_selected_roles(guild)),
            view=RoleSetupView(self.owner_id, guild),
        )

    @discord.ui.button(label="期間設定", style=discord.ButtonStyle.secondary)
    async def configure_durations(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        await interaction.response.send_modal(DurationSetupModal(self.owner_id, guild))

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

        await interaction.response.edit_message(
            content=build_setup_home_message(guild),
            view=SetupHomeView(self.owner_id),
        )


def load_selected_roles(guild: discord.Guild) -> dict[str, Optional[discord.Role]]:
    settings = bot.get_guild_settings(guild.id)
    selected_roles: dict[str, Optional[discord.Role]] = {}

    for level in ROLE_LEVELS:
        role_id = settings[f"{level}_role_id"] if settings is not None else None
        selected_roles[level] = guild.get_role(role_id) if role_id is not None else None

    return selected_roles


class SetupRoleSelect(discord.ui.RoleSelect):
    def __init__(
        self,
        setup_view: "RoleSetupView",
        level: str,
        current_role: Optional[discord.Role],
        row: int,
    ) -> None:
        default_values = [current_role] if current_role is not None else []
        super().__init__(
            placeholder=f"{LEVEL_NAMES[level]}違反ロールを選択",
            min_values=1,
            max_values=1,
            default_values=default_values,
            row=row,
        )
        self.setup_view = setup_view
        self.level = level

    async def callback(self, interaction: discord.Interaction) -> None:
        self.setup_view.selected_roles[self.level] = self.values[0]
        await interaction.response.edit_message(
            content=self.setup_view.render_content(),
            view=self.setup_view,
        )


class RoleSetupView(OwnerOnlyView):
    def __init__(self, owner_id: int, guild: discord.Guild) -> None:
        super().__init__(owner_id)
        self.guild_id = guild.id
        self.selected_roles = load_selected_roles(guild)

        for row, level in enumerate(ROLE_LEVELS):
            self.add_item(
                SetupRoleSelect(
                    self,
                    level,
                    self.selected_roles[level],
                    row,
                )
            )

    def render_content(self) -> str:
        return build_role_setup_message(self.selected_roles)

    @discord.ui.button(label="保存", style=discord.ButtonStyle.success, row=3)
    async def save_roles(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        missing = [LEVEL_NAMES[level] for level in ROLE_LEVELS if self.selected_roles[level] is None]
        if missing:
            await interaction.response.send_message(
                f"{', '.join(missing)}ロールを選択してください。",
                ephemeral=True,
            )
            return

        selected_roles = [self.selected_roles[level] for level in ROLE_LEVELS]
        role_ids = [role.id for role in selected_roles if role is not None]
        if len(set(role_ids)) != len(ROLE_LEVELS):
            await interaction.response.send_message(
                "3つとも別のロールを選択してください。",
                ephemeral=True,
            )
            return

        light_role = self.selected_roles["light"]
        medium_role = self.selected_roles["medium"]
        heavy_role = self.selected_roles["heavy"]
        if light_role is None or medium_role is None or heavy_role is None:
            await interaction.response.send_message("ロール選択が不完全です。もう一度選択してください。", ephemeral=True)
            return

        await interaction.response.defer()
        refreshed, failed = await save_guild_role_settings(
            guild.id,
            light_role.id,
            medium_role.id,
            heavy_role.id,
        )
        notice = build_role_save_message(light_role, medium_role, heavy_role, refreshed, failed)
        await interaction.edit_original_response(
            content=build_setup_home_message(guild, notice=notice),
            view=SetupHomeView(self.owner_id),
        )

    @discord.ui.button(label="戻る", style=discord.ButtonStyle.secondary, row=3)
    async def back_to_home(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        await interaction.response.edit_message(
            content=build_setup_home_message(guild),
            view=SetupHomeView(self.owner_id),
        )


class DurationSetupModal(discord.ui.Modal, title="違反期間設定"):
    def __init__(self, owner_id: int, guild: discord.Guild) -> None:
        super().__init__()
        self.owner_id = owner_id
        self.guild_id = guild.id

        settings = bot.get_guild_settings(guild.id)
        self.light_days = discord.ui.TextInput(
            label="軽度違反の日数",
            default=str(settings["light_seconds"] // 86400) if settings is not None and settings["light_seconds"] > 0 else "",
            placeholder="1〜3650",
            min_length=1,
            max_length=4,
        )
        self.medium_days = discord.ui.TextInput(
            label="中度違反の日数",
            default=str(settings["medium_seconds"] // 86400) if settings is not None and settings["medium_seconds"] > 0 else "",
            placeholder="1〜3650",
            min_length=1,
            max_length=4,
        )
        self.heavy_days = discord.ui.TextInput(
            label="重度違反の日数",
            default=str(settings["heavy_seconds"] // 86400) if settings is not None and settings["heavy_seconds"] > 0 else "",
            placeholder="1〜3650",
            min_length=1,
            max_length=4,
        )

        self.add_item(self.light_days)
        self.add_item(self.medium_days)
        self.add_item(self.heavy_days)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この setup 画面はコマンド実行者のみ操作できます。",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        try:
            light_days = parse_duration_days(self.light_days.value, "軽度")
            medium_days = parse_duration_days(self.medium_days.value, "中度")
            heavy_days = parse_duration_days(self.heavy_days.value, "重度")
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        save_guild_duration_settings(guild.id, light_days, medium_days, heavy_days)
        notice = build_duration_save_message(light_days, medium_days, heavy_days)
        await interaction.response.send_message(
            build_setup_home_message(guild, notice=notice),
            view=SetupHomeView(self.owner_id),
            ephemeral=True,
        )


@bot.tree.command(name="setup", description="このサーバーの違反設定をまとめて行います")
async def setup(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    if not has_manage_guild(interaction):
        await interaction.response.send_message("Manage Server 権限か管理者権限が必要です。", ephemeral=True)
        return

    await interaction.response.send_message(
        build_setup_home_message(interaction.guild),
        view=SetupHomeView(interaction.user.id),
        ephemeral=True,
    )


@bot.tree.command(name="config_roles", description="このサーバーの違反ロールを設定します")
@app_commands.describe(
    light="軽度違反ロール",
    medium="中度違反ロール",
    heavy="重度違反ロール",
)
async def config_roles(
    interaction: discord.Interaction,
    light: discord.Role,
    medium: discord.Role,
    heavy: discord.Role,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    if not has_manage_guild(interaction):
        await interaction.response.send_message("Manage Server 権限か管理者権限が必要です。", ephemeral=True)
        return

    if len({light.id, medium.id, heavy.id}) != 3:
        await interaction.response.send_message("3つとも別のロールを指定してください。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    refreshed, failed = await save_guild_role_settings(
        interaction.guild.id,
        light.id,
        medium.id,
        heavy.id,
    )

    await interaction.followup.send(
        build_role_save_message(light, medium, heavy, refreshed, failed),
        ephemeral=True,
    )


@bot.tree.command(name="config_durations", description="このサーバーの違反期間を設定します")
@app_commands.describe(
    light_days="軽度違反の日数",
    medium_days="中度違反の日数",
    heavy_days="重度違反の日数",
)
async def config_durations(
    interaction: discord.Interaction,
    light_days: app_commands.Range[int, 1, 3650],
    medium_days: app_commands.Range[int, 1, 3650],
    heavy_days: app_commands.Range[int, 1, 3650],
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    if not has_manage_guild(interaction):
        await interaction.response.send_message("Manage Server 権限か管理者権限が必要です。", ephemeral=True)
        return

    save_guild_duration_settings(interaction.guild.id, light_days, medium_days, heavy_days)

    await interaction.response.send_message(
        build_duration_save_message(light_days, medium_days, heavy_days),
        ephemeral=True,
    )


@bot.tree.command(name="config_show", description="このサーバーの違反設定を表示します")
async def config_show(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    settings = bot.get_guild_settings(interaction.guild.id)
    if settings is None:
        await interaction.response.send_message(
            "このサーバーにはまだ設定がありません。\n"
            f"先に {SETUP_GUIDANCE}",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "現在の設定\n" + "\n".join(build_settings_summary_lines(interaction.guild)),
        ephemeral=True,
    )


@bot.tree.command(name="violation_set", description="違反ロールを付与または上書きします")
@app_commands.describe(member="対象メンバー", level="違反段階", reason="理由")
@app_commands.choices(level=LEVEL_CHOICES)
async def violation_set(
    interaction: discord.Interaction,
    member: discord.Member,
    level: app_commands.Choice[str],
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

    settings = bot.get_guild_settings(interaction.guild.id)
    if not settings_complete(settings):
        await interaction.response.send_message(
            "このサーバーの違反設定が未完了です。\n"
            f"先に {SETUP_GUIDANCE}",
            ephemeral=True,
        )
        return

    reason = reason or ""
    duration = duration_from_settings(settings, level.value)
    expires_at = now_ts() + duration

    bot.upsert_record(
        interaction.guild.id,
        member.id,
        level.value,
        expires_at,
        reason,
    )

    try:
        await bot.apply_violation_role(
            interaction.guild.id,
            member.id,
            level.value,
            reason=f"Manual violation set by {interaction.user}",
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

    nxt = next_level(level.value)

    await interaction.response.send_message(
        f"{member.mention} に {ROLE_LABELS[level.value]} を付与しました。\n"
        f"次回変更: {format_remaining(duration)}後に {'解除' if nxt is None else ROLE_LABELS[nxt]}\n"
        f"理由: {reason or '（なし）'}",
        ephemeral=True,
    )


@bot.tree.command(name="violation_clear", description="違反ロールを即時解除します")
@app_commands.describe(member="対象メンバー")
async def violation_clear(
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

    bot.delete_record(interaction.guild.id, member.id)

    try:
        await bot.apply_violation_role(
            interaction.guild.id,
            member.id,
            None,
            reason=f"Manual violation clear by {interaction.user}",
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
        f"{member.mention} の違反ロールを解除しました。",
        ephemeral=True,
    )


@bot.tree.command(name="violation_status", description="現在の違反状態を確認します")
@app_commands.describe(member="対象メンバー")
async def violation_status(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    row = bot.get_record(interaction.guild.id, member.id)
    if row is None:
        await interaction.response.send_message(
            f"{member.mention} に有効な違反状態はありません。",
            ephemeral=True,
        )
        return

    await bot.reconcile_record(row)
    row = bot.get_record(interaction.guild.id, member.id)

    if row is None:
        await interaction.response.send_message(
            f"{member.mention} に有効な違反状態はありません。",
            ephemeral=True,
        )
        return

    current_level = row["level"]
    expires_at = row["expires_at"]
    remaining = max(0, expires_at - now_ts())
    nxt = next_level(current_level)

    await interaction.response.send_message(
        f"{member.mention} の現在状態\n"
        f"- 現在: {ROLE_LABELS[current_level]}\n"
        f"- 次回変更: {format_remaining(remaining)}後に {'解除' if nxt is None else ROLE_LABELS[nxt]}\n"
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
