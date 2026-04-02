from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands

from .config import MAX_STAGE_COUNT, SETUP_GUIDANCE, logger
from .formatters import (
    build_setup_home_message,
    build_status_config_message,
    describe_record_next_change,
    stage_display_name,
)
from .permissions import can_manage_target, has_manage_guild, has_manage_roles
from .validation import default_stage_name, get_stage, stage_path_is_ready
from .views import SetupHomeView

if TYPE_CHECKING:
    from .app import StatusBot


def register_commands(bot: "StatusBot") -> None:
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

        view = SetupHomeView(bot, interaction.user.id)
        await interaction.response.send_message(
            build_setup_home_message(interaction.guild, bot.store.get_status_config(interaction.guild.id)),
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
            build_status_config_message(interaction.guild, bot.store.get_status_config(interaction.guild.id)),
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

        config = bot.store.get_status_config(interaction.guild.id)
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
        if not stage_path_is_ready(config, stage):
            await interaction.response.send_message(
                f"{default_stage_name(stage)} から到達するステータス設定が未完了です。\n先に {SETUP_GUIDANCE}",
                ephemeral=True,
            )
            return

        current_stage = get_stage(config, stage)
        if current_stage is None:
            await interaction.response.send_message("段階設定の取得に失敗しました。", ephemeral=True)
            return

        reason = reason or ""
        try:
            row = await bot.service.assign_status(
                interaction.guild.id,
                member,
                stage,
                reason,
                interaction.user,
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

        next_change = describe_record_next_change(config, row) if row is not None else "不明"
        await interaction.response.send_message(
            f"{member.mention} に {stage_display_name(current_stage)} を付与しました。\n"
            f"- 次回変更: {next_change}\n"
            f"- 理由: {reason or '（なし）'}",
            ephemeral=True,
        )

    @bot.tree.command(name="status_clear", description="ステータスロールを即時解除します")
    @app_commands.describe(member="対象メンバー")
    async def status_clear(interaction: discord.Interaction, member: discord.Member) -> None:
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

        try:
            await bot.service.clear_status(interaction.guild.id, member, interaction.user)
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
    async def status_view(interaction: discord.Interaction, member: discord.Member) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        row = bot.store.get_status_record(interaction.guild.id, member.id)
        if row is None:
            await interaction.response.send_message(
                f"{member.mention} に有効なステータス状態はありません。",
                ephemeral=True,
            )
            return

        await bot.service.reconcile_record(row)
        row = bot.store.get_status_record(interaction.guild.id, member.id)
        if row is None:
            await interaction.response.send_message(
                f"{member.mention} に有効なステータス状態はありません。",
                ephemeral=True,
            )
            return

        config = bot.store.get_status_config(interaction.guild.id)
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
