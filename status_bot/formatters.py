import sqlite3
from typing import Optional

import discord

from .config import (
    ACTION_HOLD,
    ACTION_LABELS,
    ACTION_CLEAR,
    HISTORY_EVENT_AUTO_CLEAR,
    HISTORY_EVENT_AUTO_HOLD,
    HISTORY_EVENT_AUTO_TRANSITION,
    HISTORY_EVENT_CONFIG_STAGE_COUNT_SAVED,
    HISTORY_EVENT_CONFIG_STAGE_SAVED,
    HISTORY_EVENT_CONFIG_IMPORTED,
    HISTORY_EVENT_CONFIG_TEMPLATE_APPLIED,
    HISTORY_EVENT_MANUAL_CLEAR,
    HISTORY_EVENT_MANUAL_SET,
    SETUP_GUIDANCE,
    VALID_EXPIRE_ACTIONS,
)
from .models import (
    GuildStatusConfig,
    GuildStatusNotificationConfig,
    BulkOperationResult,
    StatusConfigExportPayload,
    StatusConfigImportPreview,
    StatusTemplateApplyPreview,
    SetupPreviewSummary,
    StatusHistoryEntry,
    StatusListEntry,
    StatusStageConfig,
)
from .validation import (
    config_complete,
    default_stage_name,
    days_to_seconds,
    get_stage,
    normalize_label,
    now_ts,
)

STATUS_LIST_MESSAGE_LIMIT = 1900
STATUS_HISTORY_MESSAGE_LIMIT = 1900
STATUS_BULK_MESSAGE_LIMIT = 1900
NOTIFICATION_MESSAGE_LIMIT = 2000
HISTORY_EVENT_LABELS = {
    HISTORY_EVENT_MANUAL_SET: "手動付与",
    HISTORY_EVENT_MANUAL_CLEAR: "手動解除",
    HISTORY_EVENT_AUTO_TRANSITION: "自動遷移",
    HISTORY_EVENT_AUTO_HOLD: "自動維持",
    HISTORY_EVENT_AUTO_CLEAR: "自動解除",
    HISTORY_EVENT_CONFIG_STAGE_COUNT_SAVED: "段階数設定変更",
    HISTORY_EVENT_CONFIG_STAGE_SAVED: "段階設定変更",
    HISTORY_EVENT_CONFIG_IMPORTED: "設定インポート",
    HISTORY_EVENT_CONFIG_TEMPLATE_APPLIED: "テンプレート適用",
}


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


def build_bulk_operation_message(
    title: str,
    result: BulkOperationResult,
    *,
    skipped_count: int = 0,
    skipped_lines: Optional[list[str]] = None,
    max_detail_lines: int = 8,
) -> str:
    lines = [
        title,
        f"- 対象件数: {result.processed_count + skipped_count}件",
        f"- 成功: {result.success_count}件",
        f"- 失敗: {result.failure_count}件",
        f"- 除外: {skipped_count}件",
    ]

    all_detail_lines = []
    if skipped_lines:
        all_detail_lines.extend(skipped_lines)
    all_detail_lines.extend(result.detail_lines)

    if all_detail_lines:
        lines.append("- 詳細:")
        shown_lines = all_detail_lines[:max_detail_lines]
        lines.extend(shown_lines)
        if len(all_detail_lines) > max_detail_lines:
            lines.append(f"- ... 他{len(all_detail_lines) - max_detail_lines}件")

    return truncate_notification_message("\n".join(lines), STATUS_BULK_MESSAGE_LIMIT)


def format_notification_channel(channel_id: Optional[int]) -> str:
    if channel_id is None:
        return "未設定"
    return f"<#{channel_id}>"


def format_notification_toggle(enabled: bool) -> str:
    return "ON" if enabled else "OFF"


def build_status_notify_config_message(
    config: GuildStatusNotificationConfig,
    *,
    notice: Optional[str] = None,
) -> str:
    lines = ["現在の通知設定"]
    if notice:
        lines.append(notice)
        lines.append("")

    lines.append(f"- 通知先チャンネル: {format_notification_channel(config.channel_id)}")
    lines.append(f"- 手動付与: {format_notification_toggle(config.notify_manual_set)}")
    lines.append(f"- 手動解除: {format_notification_toggle(config.notify_manual_clear)}")
    lines.append(f"- 自動遷移・自動解除: {format_notification_toggle(config.notify_auto_transition)}")
    lines.append(f"- 自動維持: {format_notification_toggle(config.notify_auto_hold)}")
    lines.append(f"- 設定変更: {format_notification_toggle(config.notify_config_change)}")
    return "\n".join(lines)


def build_status_config_export_message(payload: StatusConfigExportPayload) -> str:
    lines = ["ステータス設定エクスポート"]
    lines.append(f"- 元サーバーID: {payload.source_guild_id}")
    lines.append(f"- 出力時刻: <t:{payload.exported_at}:f>")
    lines.append(f"- 段階数: {payload.stage_count}")
    lines.append("- 含まれる情報: 段階設定のみ")
    lines.append("- 含まれない情報: ステータス保持者、履歴、通知設定")
    lines.append("この JSON ファイルをバックアップや移行に使用できます。")
    return "\n".join(lines)


def build_status_config_diff_lines(
    guild: discord.Guild,
    current: Optional[GuildStatusConfig],
    projected: GuildStatusConfig,
) -> list[str]:
    lines = []
    current_count = "未設定" if current is None else f"{current.stage_count}段階"
    lines.append(f"- 段階数: {current_count} -> {projected.stage_count}段階")

    for stage in projected.stages:
        current_stage = get_stage(current, stage.stage_index) if current is not None else None
        current_display = stage_display_name(current_stage) if current_stage is not None else "未設定"
        lines.append(
            f"- {current_display} -> {stage_display_name(stage)}: "
            f"ロール {format_role_setting(guild, current_stage.role_id if current_stage is not None else None)} -> "
            f"{format_role_setting(guild, stage.role_id)} / "
            f"期間 {format_duration_setting(current_stage.duration_seconds if current_stage is not None else None)} -> "
            f"{format_duration_setting(stage.duration_seconds)} / "
            f"満了時 {describe_stage_expire_action(current_stage, current) if current_stage is not None else '未設定'} -> "
            f"{describe_stage_expire_action(stage, projected)}"
        )
    return lines


def build_status_config_import_diff_lines(
    guild: discord.Guild,
    current: Optional[GuildStatusConfig],
    imported: GuildStatusConfig,
) -> list[str]:
    return build_status_config_diff_lines(guild, current, imported)


def build_status_config_import_preview_message(
    guild: discord.Guild,
    preview: StatusConfigImportPreview,
) -> str:
    lines = ["ステータス設定インポートプレビュー"]
    lines.append(f"- 現在: {'未設定' if preview.current_stage_count is None else f'{preview.current_stage_count}段階'}")
    lines.append(f"- インポート後: {preview.imported_config.stage_count}段階")
    if preview.source_guild_id is not None:
        lines.append(f"- 出力元サーバーID: {preview.source_guild_id}")
    lines.append(f"- 出力時刻: <t:{preview.exported_at}:f>")
    lines.extend(build_preview_summary_lines(
        SetupPreviewSummary(
            reapply_count=preview.reapply_count,
            clamp_count=preview.clamp_count,
            missing_role_count=preview.missing_role_count,
        )
    ))
    if preview.warning_lines:
        lines.append("- 注意:")
        lines.extend(preview.warning_lines)
    lines.append("- 変更予定:")
    lines.extend(preview.diff_lines)
    lines.append("この内容でインポートする場合は確認ボタンを押してください。")
    return "\n".join(lines)


def build_status_config_import_result_message(
    current_stage_count: Optional[int],
    imported: GuildStatusConfig,
    refreshed: int,
    failed: int,
) -> str:
    lines = ["ステータス設定をインポートしました。"]
    lines.append(f"- 段階数: {'未設定' if current_stage_count is None else current_stage_count} -> {imported.stage_count}")
    lines.append(f"- 既存ステータス保持者への再適用: {refreshed}件中 {failed}件失敗")
    return "\n".join(lines)


def build_status_template_apply_preview_message(
    guild: discord.Guild,
    preview: StatusTemplateApplyPreview,
) -> str:
    lines = [f"ステータステンプレート適用プレビュー ({preview.template_name})"]
    lines.append(f"- 現在: {'未設定' if preview.current_stage_count is None else f'{preview.current_stage_count}段階'}")
    lines.append(f"- 適用後: {preview.projected_config.stage_count}段階")
    lines.extend(build_preview_summary_lines(
        SetupPreviewSummary(
            reapply_count=preview.reapply_count,
            clamp_count=preview.clamp_count,
            missing_role_count=preview.missing_role_count,
        )
    ))
    if preview.warning_lines:
        lines.append("- 注意:")
        lines.extend(preview.warning_lines)
    lines.append("- 変更予定:")
    lines.extend(preview.diff_lines)
    lines.append("この内容で適用する場合は確認ボタンを押してください。")
    return "\n".join(lines)


def build_status_template_apply_result_message(
    template_name: str,
    current_stage_count: Optional[int],
    projected: GuildStatusConfig,
    refreshed: int,
    failed: int,
) -> str:
    lines = [f"ステータステンプレートを適用しました。 ({template_name})"]
    lines.append(
        f"- 段階数: {'未設定' if current_stage_count is None else current_stage_count} -> {projected.stage_count}"
    )
    lines.append(f"- 既存ステータス保持者への再適用: {refreshed}件中 {failed}件失敗")
    return "\n".join(lines)


def truncate_notification_message(content: str, limit: int = NOTIFICATION_MESSAGE_LIMIT) -> str:
    if len(content) <= limit:
        return content
    if limit <= 3:
        return content[:limit]
    return content[: limit - 3] + "..."


def shorten_reason(reason: str, limit: int = 80) -> str:
    if len(reason) <= limit:
        return reason
    return reason[: limit - 3] + "..."


def build_status_list_entry_line(entry: StatusListEntry) -> str:
    return (
        f"- {entry.member_display}: {entry.stage_name} / "
        f"次回変更 {entry.next_change_text} / "
        f"理由 {shorten_reason(entry.reason) if entry.reason else '（なし）'}"
    )


def describe_status_history_change(entry: StatusHistoryEntry) -> str:
    if entry.from_stage_name and entry.to_stage_name:
        return f"{entry.from_stage_name} -> {entry.to_stage_name}"
    if entry.from_stage_name and entry.to_stage_name is None:
        return f"{entry.from_stage_name} -> 解除"
    if entry.from_stage_name is None and entry.to_stage_name:
        return f"なし -> {entry.to_stage_name}"
    return "変更情報なし"


def build_status_history_entry_line(entry: StatusHistoryEntry) -> str:
    line = (
        f"- <t:{entry.created_at}:f>: "
        f"{HISTORY_EVENT_LABELS.get(entry.event_type, entry.event_type)} / "
        f"実行者 {entry.actor_display} / "
        f"変更 {describe_status_history_change(entry)} / "
        f"理由 {entry.reason or '（なし）'}"
    )
    if entry.detail:
        line += f" / 詳細 {entry.detail}"
    return line


def _build_status_list_message_from_lines(
    entry_lines: list[str],
    *,
    page_index: int,
    page_count: int,
    total_count: int,
) -> str:
    lines = [
        "現在のステータス一覧",
        f"- ページ: {page_index + 1}/{page_count}",
        f"- 全件数: {total_count}件",
    ]
    lines.extend(entry_lines)
    return "\n".join(lines)


def _build_status_history_message_from_lines(
    member_display: str,
    entry_lines: list[str],
    *,
    page_index: int,
    page_count: int,
    total_count: int,
) -> str:
    lines = [
        f"{member_display} のステータス履歴",
        f"- ページ: {page_index + 1}/{page_count}",
        f"- 全件数: {total_count}件",
    ]
    lines.extend(entry_lines)
    return "\n".join(lines)


def build_status_list_message(
    entries: list[StatusListEntry],
    *,
    page_index: int,
    page_count: int,
    total_count: int,
) -> str:
    return _build_status_list_message_from_lines(
        [build_status_list_entry_line(entry) for entry in entries],
        page_index=page_index,
        page_count=page_count,
        total_count=total_count,
    )


def build_status_history_message(
    member_display: str,
    entries: list[StatusHistoryEntry],
    *,
    page_index: int,
    page_count: int,
    total_count: int,
) -> str:
    return _build_status_history_message_from_lines(
        member_display,
        [build_status_history_entry_line(entry) for entry in entries],
        page_index=page_index,
        page_count=page_count,
        total_count=total_count,
    )


def paginate_status_list_messages(
    entries: list[StatusListEntry],
    *,
    max_length: int = STATUS_LIST_MESSAGE_LIMIT,
) -> list[str]:
    total_count = len(entries)
    if total_count == 0:
        return [
            _build_status_list_message_from_lines(
                [],
                page_index=0,
                page_count=1,
                total_count=0,
            )
        ]

    max_page_count = total_count
    max_header_length = len(
        _build_status_list_message_from_lines(
            [],
            page_index=max_page_count - 1,
            page_count=max_page_count,
            total_count=total_count,
        )
    )
    if max_header_length >= max_length:
        raise ValueError("ステータス一覧ヘッダーが長すぎます。")

    pages: list[list[str]] = []
    current_page_lines: list[str] = []
    current_length = max_header_length

    for entry in entries:
        entry_line = build_status_list_entry_line(entry)
        added_length = len(entry_line) + 1

        if current_page_lines and current_length + added_length > max_length:
            pages.append(current_page_lines)
            current_page_lines = []
            current_length = max_header_length

        if current_length + added_length > max_length:
            raise ValueError("ステータス一覧の 1 行が長すぎます。")

        current_page_lines.append(entry_line)
        current_length += added_length

    if current_page_lines:
        pages.append(current_page_lines)

    return [
        _build_status_list_message_from_lines(
            page_lines,
            page_index=page_index,
            page_count=len(pages),
            total_count=total_count,
        )
        for page_index, page_lines in enumerate(pages)
    ]


def paginate_status_history_messages(
    member_display: str,
    entries: list[StatusHistoryEntry],
    *,
    max_length: int = STATUS_HISTORY_MESSAGE_LIMIT,
) -> list[str]:
    total_count = len(entries)
    if total_count == 0:
        return [
            _build_status_history_message_from_lines(
                member_display,
                [],
                page_index=0,
                page_count=1,
                total_count=0,
            )
        ]

    max_page_count = total_count
    max_header_length = len(
        _build_status_history_message_from_lines(
            member_display,
            [],
            page_index=max_page_count - 1,
            page_count=max_page_count,
            total_count=total_count,
        )
    )
    if max_header_length >= max_length:
        raise ValueError("ステータス履歴ヘッダーが長すぎます。")

    pages: list[list[str]] = []
    current_page_lines: list[str] = []
    current_length = max_header_length

    for entry in entries:
        entry_line = build_status_history_entry_line(entry)
        added_length = len(entry_line) + 1

        if current_page_lines and current_length + added_length > max_length:
            pages.append(current_page_lines)
            current_page_lines = []
            current_length = max_header_length

        if current_length + added_length > max_length:
            raise ValueError("ステータス履歴の 1 行が長すぎます。")

        current_page_lines.append(entry_line)
        current_length += added_length

    if current_page_lines:
        pages.append(current_page_lines)

    return [
        _build_status_history_message_from_lines(
            member_display,
            page_lines,
            page_index=page_index,
            page_count=len(pages),
            total_count=total_count,
        )
        for page_index, page_lines in enumerate(pages)
    ]


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


def build_manual_set_notification(
    member_display: str,
    stage_name: str,
    next_change_text: str,
    *,
    reason: str,
    actor_display: str,
) -> str:
    return (
        "ステータス通知: 手動付与\n"
        f"- 対象: {member_display}\n"
        f"- 現在: {stage_name}\n"
        f"- 次回変更: {next_change_text}\n"
        f"- 理由: {reason or '（なし）'}\n"
        f"- 実行者: {actor_display}"
    )


def build_manual_clear_notification(
    member_display: str,
    from_stage_name: Optional[str],
    *,
    reason: str,
    actor_display: str,
) -> str:
    return (
        "ステータス通知: 手動解除\n"
        f"- 対象: {member_display}\n"
        f"- 解除前: {from_stage_name or '不明'}\n"
        f"- 理由: {reason or '（なし）'}\n"
        f"- 実行者: {actor_display}"
    )


def build_auto_transition_notification(
    member_display: str,
    from_stage_name: Optional[str],
    to_stage_name: Optional[str],
    next_change_text: str,
    *,
    reason: str,
) -> str:
    return (
        "ステータス通知: 自動遷移\n"
        f"- 対象: {member_display}\n"
        f"- 変更: {from_stage_name or '不明'} -> {to_stage_name or '不明'}\n"
        f"- 次回変更: {next_change_text}\n"
        f"- 理由: {reason or '（なし）'}"
    )


def build_auto_hold_notification(
    member_display: str,
    stage_name: Optional[str],
    *,
    reason: str,
) -> str:
    return (
        "ステータス通知: 自動維持\n"
        f"- 対象: {member_display}\n"
        f"- 現在: {stage_name or '不明'}\n"
        f"- 次回変更: なし（現在の段階を維持中）\n"
        f"- 理由: {reason or '（なし）'}"
    )


def build_auto_clear_notification(
    member_display: str,
    from_stage_name: Optional[str],
    *,
    reason: str,
) -> str:
    return (
        "ステータス通知: 自動解除\n"
        f"- 対象: {member_display}\n"
        f"- 解除前: {from_stage_name or '不明'}\n"
        f"- 理由: {reason or '（なし）'}"
    )


def build_config_change_notification(
    detail: str,
    *,
    actor_display: str,
    refreshed: int,
    failed: int,
) -> str:
    return (
        "ステータス通知: 設定変更\n"
        f"- 内容: {detail}\n"
        f"- 既存ステータス保持者への再適用: {refreshed}件中 {failed}件失敗\n"
        f"- 実行者: {actor_display}"
    )


def build_preview_summary_lines(summary: SetupPreviewSummary) -> list[str]:
    return [
        f"- 再適用対象: {summary.reapply_count}件",
        f"- 丸め対象: {summary.clamp_count}件",
        f"- 見つからないロール: {summary.missing_role_count}件",
    ]


def build_stage_count_preview_message(
    current_count: Optional[int],
    next_count: int,
    summary: SetupPreviewSummary,
) -> str:
    lines = [
        "段階数変更プレビュー",
        f"- 現在: {'未設定' if current_count is None else f'{current_count}段階'}",
        f"- 保存後: {next_count}段階",
    ]
    lines.extend(build_preview_summary_lines(summary))
    lines.append("この内容で保存する場合は確認ボタンを押してください。")
    return "\n".join(lines)


def build_stage_save_preview_message(
    guild: discord.Guild,
    persisted_stage: StatusStageConfig,
    draft_stage: StatusStageConfig,
    config: GuildStatusConfig,
    summary: SetupPreviewSummary,
) -> str:
    lines = [f"{stage_display_name(draft_stage)} の保存前プレビュー"]
    lines.append(f"- 現在のロール: {format_role_setting(guild, persisted_stage.role_id)}")
    lines.append(f"- 保存後のロール: {format_role_setting(guild, draft_stage.role_id)}")
    lines.append(f"- 保存後の期間: {format_duration_setting(draft_stage.duration_seconds)}")
    lines.append(f"- 保存後の満了時: {describe_stage_expire_action(draft_stage, config)}")
    lines.extend(build_preview_summary_lines(summary))
    lines.append("この内容で保存する場合は確認ボタンを押してください。")
    return "\n".join(lines)


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
    lines.append(f"- ロール: {format_role_setting(guild, stage.role_id)}")
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
