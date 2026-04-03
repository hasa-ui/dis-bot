import re
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands

from .config import MAX_STAGE_COUNT, SETUP_GUIDANCE, logger
from .formatters import (
    build_bulk_operation_message,
    build_status_notify_config_message,
    build_setup_home_message,
    build_status_config_message,
    describe_record_next_change,
    stage_display_name,
)
from .models import BulkOperationResult, GuildStatusNotificationConfig
from .permissions import can_manage_target, has_manage_guild, has_manage_roles
from .validation import default_stage_name, get_stage, stage_path_is_ready
from .views import SetupHomeView, StatusHistoryView, StatusListView

if TYPE_CHECKING:
    from .app import StatusBot


_BULK_TARGET_PATTERN = re.compile(r"^<@!?(\d+)>$")


def _parse_bulk_target_id(token: str) -> Optional[int]:
    stripped = token.strip()
    if stripped.isdigit():
        return int(stripped)

    match = _BULK_TARGET_PATTERN.fullmatch(stripped)
    if match is None:
        return None
    return int(match.group(1))


async def _resolve_bulk_targets(
    interaction: discord.Interaction,
    attachment: discord.Attachment,
    bot: "StatusBot",
) -> tuple[list[discord.Member], list[str], int]:
    guild = interaction.guild
    if guild is None:
        return [], [], 0

    try:
        raw = await attachment.read()
    except discord.DiscordException as exc:
        raise ValueError(f"添付ファイルの読み込みに失敗しました: {exc}") from exc

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("添付ファイルは UTF-8 テキストである必要があります。") from exc

    members: list[discord.Member] = []
    skipped_lines: list[str] = []
    seen_ids: set[int] = set()
    input_count = 0

    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        token = raw_line.strip()
        if not token:
            continue

        input_count += 1
        user_id = _parse_bulk_target_id(token)
        if user_id is None:
            skipped_lines.append(f"- {line_no}行目: `{token}` はメンバーIDまたはメンションではありません。")
            continue

        if user_id in seen_ids:
            skipped_lines.append(f"- {line_no}行目: <@{user_id}> は重複しているため除外しました。")
            continue
        seen_ids.add(user_id)

        member = await bot.service.fetch_member_if_needed(guild.id, user_id)
        if member is None:
            skipped_lines.append(f"- {line_no}行目: <@{user_id}> はこのサーバーで見つかりませんでした。")
            continue

        ok, msg = can_manage_target(guild, member)
        if not ok:
            skipped_lines.append(f"- {line_no}行目: {msg}")
            continue

        members.append(member)

    return members, skipped_lines, input_count


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

    @bot.tree.command(name="status_notify_config", description="このサーバーの通知設定を表示または更新します")
    @app_commands.describe(
        channel="通知先テキストチャンネル",
        manual_set="手動付与を通知するか",
        manual_clear="手動解除を通知するか",
        auto_transition="自動遷移と自動解除を通知するか",
        auto_hold="期限満了後の維持を通知するか",
        config_change="設定変更を通知するか",
        disable_all="通知先と全通知をまとめて無効化するか",
    )
    async def status_notify_config(
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        manual_set: Optional[bool] = None,
        manual_clear: Optional[bool] = None,
        auto_transition: Optional[bool] = None,
        auto_hold: Optional[bool] = None,
        config_change: Optional[bool] = None,
        disable_all: bool = False,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        if not has_manage_guild(interaction):
            await interaction.response.send_message(
                "Manage Server 権限か管理者権限が必要です。",
                ephemeral=True,
            )
            return

        updates_requested = (
            channel is not None
            or manual_set is not None
            or manual_clear is not None
            or auto_transition is not None
            or auto_hold is not None
            or config_change is not None
            or disable_all
        )
        current = bot.store.get_status_notification_config(interaction.guild.id)
        if not updates_requested:
            await interaction.response.send_message(
                build_status_notify_config_message(current),
                ephemeral=True,
            )
            return

        if disable_all and any(
            value is not None
            for value in (channel, manual_set, manual_clear, auto_transition, auto_hold, config_change)
        ):
            await interaction.response.send_message(
                "`disable_all` を使うときは他の更新引数を同時指定できません。",
                ephemeral=True,
            )
            return

        if disable_all:
            updated = GuildStatusNotificationConfig(
                guild_id=interaction.guild.id,
                channel_id=None,
                notify_manual_set=False,
                notify_manual_clear=False,
                notify_auto_transition=False,
                notify_auto_hold=False,
                notify_config_change=False,
            )
        else:
            updated = GuildStatusNotificationConfig(
                guild_id=interaction.guild.id,
                channel_id=channel.id if channel is not None else current.channel_id,
                notify_manual_set=current.notify_manual_set if manual_set is None else manual_set,
                notify_manual_clear=current.notify_manual_clear if manual_clear is None else manual_clear,
                notify_auto_transition=(
                    current.notify_auto_transition if auto_transition is None else auto_transition
                ),
                notify_auto_hold=current.notify_auto_hold if auto_hold is None else auto_hold,
                notify_config_change=current.notify_config_change if config_change is None else config_change,
            )

        enabled = (
            updated.notify_manual_set
            or updated.notify_manual_clear
            or updated.notify_auto_transition
            or updated.notify_auto_hold
            or updated.notify_config_change
        )
        if enabled and updated.channel_id is None:
            await interaction.response.send_message(
                "通知を有効化する場合は通知先テキストチャンネルを指定してください。",
                ephemeral=True,
            )
            return

        if channel is not None:
            me = interaction.guild.me
            if me is None:
                await interaction.response.send_message(
                    "Botのメンバー情報が取得できません。",
                    ephemeral=True,
                )
                return
            perms = channel.permissions_for(me)
            if not perms.view_channel or not perms.send_messages:
                await interaction.response.send_message(
                    "指定チャンネルへ通知できません。Bot の View Channel / Send Messages 権限を確認してください。",
                    ephemeral=True,
                )
                return

        bot.store.upsert_status_notification_config(updated)
        bot.store.commit()
        await interaction.response.send_message(
            build_status_notify_config_message(
                updated,
                notice="通知設定を保存しました。",
            ),
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

    @bot.tree.command(name="status_bulk_set", description="複数メンバーにステータスロールを付与します")
    @app_commands.describe(
        targets="対象一覧を添付した UTF-8 テキストファイル",
        stage="対象段階 (1〜10)",
        reason="理由",
    )
    async def status_bulk_set(
        interaction: discord.Interaction,
        targets: discord.Attachment,
        stage: app_commands.Range[int, 1, MAX_STAGE_COUNT],
        reason: Optional[str] = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        if not has_manage_roles(interaction):
            await interaction.response.send_message("Manage Roles 権限が必要です。", ephemeral=True)
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
        await interaction.response.defer(ephemeral=True)
        try:
            members, skipped_lines, input_count = await _resolve_bulk_targets(interaction, targets, bot)
        except ValueError as e:
            await interaction.edit_original_response(content=str(e), view=None)
            return

        if input_count == 0:
            await interaction.edit_original_response(content="対象がありません。", view=None)
            return

        if not members:
            await interaction.edit_original_response(
                content=build_bulk_operation_message(
                    f"ステータス一括付与結果 ({stage_display_name(current_stage)})",
                    BulkOperationResult(
                        processed_count=0,
                        success_count=0,
                        failure_count=0,
                        detail_lines=[],
                    ),
                    skipped_count=len(skipped_lines),
                    skipped_lines=skipped_lines,
                )
            )
            return

        result = await bot.service.bulk_assign_status(
            interaction.guild.id,
            members,
            stage,
            reason,
            interaction.user,
        )
        await interaction.edit_original_response(
            content=build_bulk_operation_message(
                f"ステータス一括付与結果 ({stage_display_name(current_stage)})",
                result,
                skipped_count=len(skipped_lines),
                skipped_lines=skipped_lines,
            ),
            view=None,
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

    @bot.tree.command(name="status_bulk_clear", description="複数メンバーのステータスロールを解除します")
    @app_commands.describe(targets="対象一覧を添付した UTF-8 テキストファイル")
    async def status_bulk_clear(
        interaction: discord.Interaction,
        targets: discord.Attachment,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        if not has_manage_roles(interaction):
            await interaction.response.send_message("Manage Roles 権限が必要です。", ephemeral=True)
            return

        if bot.store.get_status_config(interaction.guild.id) is None:
            await interaction.response.send_message(
                f"このサーバーのステータス設定が未完了です。\n先に {SETUP_GUIDANCE}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            members, skipped_lines, input_count = await _resolve_bulk_targets(interaction, targets, bot)
        except ValueError as e:
            await interaction.edit_original_response(content=str(e), view=None)
            return

        if input_count == 0:
            await interaction.edit_original_response(content="対象がありません。", view=None)
            return

        if not members:
            await interaction.edit_original_response(
                content=build_bulk_operation_message(
                    "ステータス一括解除結果",
                    BulkOperationResult(
                        processed_count=0,
                        success_count=0,
                        failure_count=0,
                        detail_lines=[],
                    ),
                    skipped_count=len(skipped_lines),
                    skipped_lines=skipped_lines,
                )
            )
            return

        result = await bot.service.bulk_clear_status(
            interaction.guild.id,
            members,
            interaction.user,
        )
        await interaction.edit_original_response(
            content=build_bulk_operation_message(
                "ステータス一括解除結果",
                result,
                skipped_count=len(skipped_lines),
                skipped_lines=skipped_lines,
            ),
            view=None,
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

    @bot.tree.command(name="status_list", description="現在のステータス一覧を確認します")
    async def status_list(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            entries = await bot.service.list_guild_status_records(interaction.guild)
        except RuntimeError as e:
            await interaction.edit_original_response(content=str(e), view=None)
            return

        if not entries:
            await interaction.edit_original_response(
                content="このサーバーに有効なステータス状態はありません。",
                view=None,
            )
            return

        view = StatusListView(interaction.user.id, entries)
        await interaction.edit_original_response(content=view.render_content(), view=view)
        await view.bind_message(interaction)

    @bot.tree.command(name="status_history", description="対象メンバーのステータス履歴を確認します")
    @app_commands.describe(member="対象メンバー")
    async def status_history(interaction: discord.Interaction, member: discord.Member) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        entries = await bot.service.list_member_status_history(interaction.guild, member.id)
        if not entries:
            await interaction.edit_original_response(
                content=f"{member.mention} のステータス履歴はありません。",
                view=None,
            )
            return

        view = StatusHistoryView(interaction.user.id, member.mention, entries)
        await interaction.edit_original_response(content=view.render_content(), view=view)
        await view.bind_message(interaction)

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
