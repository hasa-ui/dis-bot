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
                raise RuntimeError("このサーバーの違反設定が未完了です。/config_roles と /config_durations を確認してください。")

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

    guild_id = interaction.guild.id
    previous_role_ids = role_ids_from_settings(bot.get_guild_settings(guild_id))

    await interaction.response.defer(ephemeral=True)

    bot.upsert_guild_roles(guild_id, light.id, medium.id, heavy.id)
    refreshed, failed = await bot.refresh_guild_violation_roles(
        guild_id,
        remove_role_ids=previous_role_ids,
    )

    await interaction.followup.send(
        "このサーバーの違反ロールを保存しました。\n"
        f"- 軽度: {light.mention}\n"
        f"- 中度: {medium.mention}\n"
        f"- 重度: {heavy.mention}\n"
        f"- 既存違反者への再適用: {refreshed}件中 {failed}件失敗",
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

    bot.upsert_guild_durations(
        interaction.guild.id,
        days_to_seconds(light_days),
        days_to_seconds(medium_days),
        days_to_seconds(heavy_days),
    )

    await interaction.response.send_message(
        "このサーバーの違反期間を保存しました。\n"
        f"- 軽度: {light_days}日\n"
        f"- 中度: {medium_days}日\n"
        f"- 重度: {heavy_days}日\n"
        "既存違反者の現在の期限は変わりません。次回の降格以降から新しい期間が適用されます。",
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
            "先に /config_roles と /config_durations を実行してください。",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    light_role = guild.get_role(settings["light_role_id"]) if settings["light_role_id"] else None
    medium_role = guild.get_role(settings["medium_role_id"]) if settings["medium_role_id"] else None
    heavy_role = guild.get_role(settings["heavy_role_id"]) if settings["heavy_role_id"] else None

    await interaction.response.send_message(
        "現在の設定\n"
        f"- 軽度ロール: {light_role.mention if light_role else '未設定'}\n"
        f"- 中度ロール: {medium_role.mention if medium_role else '未設定'}\n"
        f"- 重度ロール: {heavy_role.mention if heavy_role else '未設定'}\n"
        f"- 軽度期間: {settings['light_seconds'] // 86400}日\n"
        f"- 中度期間: {settings['medium_seconds'] // 86400}日\n"
        f"- 重度期間: {settings['heavy_seconds'] // 86400}日",
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
            "先に /config_roles と /config_durations を実行してください。",
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
