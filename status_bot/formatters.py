import sqlite3
from typing import Optional

import discord

from .config import ACTION_HOLD, ACTION_LABELS, ACTION_CLEAR, SETUP_GUIDANCE, VALID_EXPIRE_ACTIONS
from .models import GuildStatusConfig, StatusStageConfig
from .validation import (
    config_complete,
    default_stage_name,
    days_to_seconds,
    get_stage,
    normalize_label,
    now_ts,
)


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


def stage_display_name(stage: StatusStageConfig) -> str:
    custom = normalize_label(stage.label)
    base = default_stage_name(stage.stage_index)
    if not custom:
        return base
    return f"{base}（{custom}）"


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
        if stage.stage_index == 1 and stage.on_expire_action == "next":
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


def build_setup_home_message(
    guild: discord.Guild,
    config: Optional[GuildStatusConfig],
    *,
    notice: Optional[str] = None,
) -> str:
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


def build_status_config_message(guild: discord.Guild, config: Optional[GuildStatusConfig]) -> str:
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


def describe_record_next_change(config: GuildStatusConfig, row: sqlite3.Row) -> str:
    current_stage = get_stage(config, row["stage_index"])
    if current_stage is None:
        return "不明"

    if row["expires_at"] is None:
        return "なし（現在の段階を維持中）"

    remaining = max(0, row["expires_at"] - now_ts())
    return f"{format_remaining(remaining)}後に {describe_stage_expire_action(current_stage, config)}"
