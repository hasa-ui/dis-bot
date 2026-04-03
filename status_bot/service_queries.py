import json
from dataclasses import asdict
from typing import Optional

import discord

from .config import ACTION_CLEAR, ACTION_HOLD, MAX_STAGE_COUNT, SETUP_GUIDANCE
from .formatters import (
    build_status_config_import_diff_lines,
    describe_record_next_change,
    stage_display_name,
)
from .models import (
    GuildStatusConfig,
    SetupPreviewSummary,
    StatusConfigExportPayload,
    StatusConfigExportStage,
    StatusConfigImportPreview,
    StatusHistoryEntry,
    StatusListEntry,
    StatusStageConfig,
)
from .service_common import ServiceContext, resolve_actor_display, resolve_history_stage_name
from .validation import (
    default_stage_config,
    config_complete,
    get_stage,
    is_stage_ready,
    normalize_label,
    now_ts,
    validate_stage_configuration,
)

STATUS_CONFIG_EXPORT_SCHEMA_VERSION = 1


def predict_reconciled_record(
    config: GuildStatusConfig,
    row,
    *,
    current_ts: Optional[int] = None,
    projected_stage_index: Optional[int] = None,
    projected_expires_at: Optional[int] = None,
) -> Optional[dict[str, object]]:
    stage_index = projected_stage_index if projected_stage_index is not None else row["stage_index"]
    expires_at = projected_expires_at if projected_expires_at is not None else row["expires_at"]
    reason = row["reason"]

    if expires_at is None:
        return {
            "stage_index": stage_index,
            "expires_at": None,
            "reason": reason,
        }

    now_value = now_ts() if current_ts is None else current_ts
    while expires_at is not None and expires_at <= now_value:
        current_stage = get_stage(config, stage_index)
        if not is_stage_ready(current_stage):
            return {
                "stage_index": stage_index,
                "expires_at": expires_at,
                "reason": reason,
            }

        if current_stage.on_expire_action == ACTION_CLEAR:
            return None

        if current_stage.on_expire_action == ACTION_HOLD:
            return {
                "stage_index": stage_index,
                "expires_at": None,
                "reason": reason,
            }

        next_stage = get_stage(config, stage_index - 1)
        if not is_stage_ready(next_stage):
            return {
                "stage_index": stage_index,
                "expires_at": expires_at,
                "reason": reason,
            }

        stage_index -= 1
        expires_at = expires_at + next_stage.duration_seconds

    return {
        "stage_index": stage_index,
        "expires_at": expires_at,
        "reason": reason,
    }


def _count_projected_reapply_records(
    context: ServiceContext,
    guild_id: int,
    config: GuildStatusConfig,
    *,
    clamp_stage_index: Optional[int] = None,
) -> int:
    current_ts = now_ts()
    count = 0
    target_stage = get_stage(config, clamp_stage_index) if clamp_stage_index is not None else None
    target_expires_at = None
    if target_stage is not None and target_stage.duration_seconds > 0:
        target_expires_at = current_ts + target_stage.duration_seconds

    for row in context.store.get_active_records_by_guild(guild_id):
        projected_stage_index = row["stage_index"]
        projected_expires_at = row["expires_at"]
        if clamp_stage_index is not None and projected_stage_index > clamp_stage_index:
            projected_stage_index = clamp_stage_index
            if projected_expires_at is not None and target_expires_at is not None:
                projected_expires_at = target_expires_at

        projected = predict_reconciled_record(
            config,
            row,
            current_ts=current_ts,
            projected_stage_index=projected_stage_index,
            projected_expires_at=projected_expires_at,
        )
        if projected is not None:
            count += 1
    return count


def _count_missing_roles(guild: discord.Guild, config: GuildStatusConfig) -> int:
    return sum(
        1
        for stage in config.stages
        if stage.role_id is not None and guild.get_role(stage.role_id) is None
    )


def _build_stage_count_preview_config(
    guild_id: int,
    previous: Optional[GuildStatusConfig],
    stage_count: int,
) -> GuildStatusConfig:
    stages = [
        get_stage(previous, idx) if previous is not None else None
        for idx in range(1, stage_count + 1)
    ]
    return GuildStatusConfig(
        guild_id=guild_id,
        stage_count=stage_count,
        stages=[stage or default_stage_config(idx) for idx, stage in enumerate(stages, start=1)],
    )


def _build_stage_preview_config(
    config: GuildStatusConfig,
    replacement: StatusStageConfig,
) -> GuildStatusConfig:
    return GuildStatusConfig(
        guild_id=config.guild_id,
        stage_count=config.stage_count,
        stages=[
            replacement if stage.stage_index == replacement.stage_index else stage
            for stage in config.stages
        ],
    )


def export_status_config(
    context: ServiceContext,
    guild: discord.Guild,
) -> StatusConfigExportPayload:
    config = context.store.get_status_config(guild.id)
    if config is None:
        raise RuntimeError(f"このサーバーのステータス設定が未完了です。\n先に {SETUP_GUIDANCE}")
    if not config_complete(config):
        raise RuntimeError(
            "このサーバーのステータス設定は未完了です。"
            "\n先に /setup を完了してから export してください。"
        )

    return StatusConfigExportPayload(
        schema_version=STATUS_CONFIG_EXPORT_SCHEMA_VERSION,
        source_guild_id=guild.id,
        exported_at=now_ts(),
        stage_count=config.stage_count,
        stages=[
            StatusConfigExportStage(
                stage_index=stage.stage_index,
                label=stage.label,
                role_id=stage.role_id,
                duration_seconds=stage.duration_seconds,
                on_expire_action=stage.on_expire_action,
            )
            for stage in config.stages
        ],
    )


def serialize_status_config_export_payload(payload: StatusConfigExportPayload) -> str:
    return json.dumps(asdict(payload), ensure_ascii=False, indent=2, sort_keys=True)


def parse_status_config_export_payload(raw_text: str) -> StatusConfigExportPayload:
    try:
        data = json.loads(raw_text.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise ValueError("ステータス設定 JSON の解析に失敗しました。") from exc

    if not isinstance(data, dict):
        raise ValueError("ステータス設定 JSON の形式が正しくありません。")

    schema_version = data.get("schema_version")
    if schema_version != STATUS_CONFIG_EXPORT_SCHEMA_VERSION:
        raise ValueError("対応していないステータス設定 JSON です。")

    source_guild_id = data.get("source_guild_id")
    exported_at = data.get("exported_at")
    stage_count = data.get("stage_count")
    stages_data = data.get("stages")

    if not isinstance(source_guild_id, int):
        raise ValueError("source_guild_id が不正です。")
    if not isinstance(exported_at, int):
        raise ValueError("exported_at が不正です。")
    if not isinstance(stage_count, int):
        raise ValueError("stage_count が不正です。")
    if not 1 <= stage_count <= MAX_STAGE_COUNT:
        raise ValueError(f"stage_count は 1〜{MAX_STAGE_COUNT} の範囲である必要があります。")
    if not isinstance(stages_data, list):
        raise ValueError("stages が不正です。")
    if len(stages_data) != stage_count:
        raise ValueError("stage_count と stages の件数が一致しません。")

    stages: list[StatusConfigExportStage] = []
    seen_indices: set[int] = set()
    for item in stages_data:
        if not isinstance(item, dict):
            raise ValueError("stages の各要素が不正です。")

        stage_index = item.get("stage_index")
        label = item.get("label")
        role_id = item.get("role_id")
        duration_seconds = item.get("duration_seconds")
        on_expire_action = item.get("on_expire_action")

        if not isinstance(stage_index, int):
            raise ValueError("stage_index が不正です。")
        if not 1 <= stage_index <= stage_count:
            raise ValueError("stage_index が範囲外です。")
        if stage_index in seen_indices:
            raise ValueError("stage_index が重複しています。")
        if not isinstance(label, str):
            raise ValueError("label が不正です。")
        if role_id is not None and not isinstance(role_id, int):
            raise ValueError("role_id が不正です。")
        if not isinstance(duration_seconds, int):
            raise ValueError("duration_seconds が不正です。")
        if not isinstance(on_expire_action, str):
            raise ValueError("on_expire_action が不正です。")

        stages.append(
            StatusConfigExportStage(
                stage_index=stage_index,
                label=normalize_label(label),
                role_id=role_id,
                duration_seconds=duration_seconds,
                on_expire_action=on_expire_action,
            )
        )
        seen_indices.add(stage_index)

    stages.sort(key=lambda stage: stage.stage_index)
    return StatusConfigExportPayload(
        schema_version=schema_version,
        source_guild_id=source_guild_id,
        exported_at=exported_at,
        stage_count=stage_count,
        stages=stages,
    )


def build_status_config_from_export_payload(
    guild: discord.Guild,
    payload: StatusConfigExportPayload,
) -> GuildStatusConfig:
    if payload.stage_count != len(payload.stages):
        raise ValueError("stage_count と stages の件数が一致しません。")

    stages = []
    for item in payload.stages:
        if item.role_id is None:
            raise ValueError(f"{item.stage_index} 段階のロールが未設定です。")
        if guild.get_role(item.role_id) is None:
            raise ValueError(f"段階{item.stage_index} のロールが見つかりません。")
        stage = StatusStageConfig(
            stage_index=item.stage_index,
            label=item.label,
            role_id=item.role_id,
            duration_seconds=item.duration_seconds,
            on_expire_action=item.on_expire_action,
        )
        config = GuildStatusConfig(
            guild_id=guild.id,
            stage_count=payload.stage_count,
            stages=stages + [stage],
        )
        validate_stage_configuration(config, stage)
        stages.append(stage)

    return GuildStatusConfig(
        guild_id=guild.id,
        stage_count=payload.stage_count,
        stages=stages,
    )


def preview_status_config_import(
    context: ServiceContext,
    guild: discord.Guild,
    payload: StatusConfigExportPayload,
) -> StatusConfigImportPreview:
    current = context.store.get_status_config(guild.id)
    imported = build_status_config_from_export_payload(guild, payload)

    clamp_count = 0
    clamp_stage_index: Optional[int] = None
    if current is not None and imported.stage_count < current.stage_count:
        clamp_count = context.store.count_records_above_stage(guild.id, imported.stage_count)
        clamp_stage_index = imported.stage_count

    missing_role_count = sum(
        1 for stage in imported.stages if stage.role_id is not None and guild.get_role(stage.role_id) is None
    )
    warning_lines = []
    if current is not None and imported.stage_count < current.stage_count:
        warning_lines.append(
            f"- 段階数を {current.stage_count} から {imported.stage_count} に減らすため、"
            f"{clamp_count}件の既存レコードを段階{imported.stage_count}へ丸めます。"
        )

    return StatusConfigImportPreview(
        source_guild_id=payload.source_guild_id,
        exported_at=payload.exported_at,
        current_stage_count=current.stage_count if current is not None else None,
        imported_config=imported,
        reapply_count=_count_projected_reapply_records(
            context,
            guild.id,
            imported,
            clamp_stage_index=clamp_stage_index,
        ),
        clamp_count=clamp_count,
        missing_role_count=missing_role_count,
        diff_lines=build_status_config_import_diff_lines(guild, current, imported),
        warning_lines=warning_lines,
    )


def preview_stage_count_settings(
    context: ServiceContext,
    guild: discord.Guild,
    stage_count: int,
) -> SetupPreviewSummary:
    previous = context.store.get_status_config(guild.id)
    clamp_count = 0
    if previous is not None and stage_count < previous.stage_count:
        clamp_count = context.store.count_records_above_stage(guild.id, stage_count)
        if clamp_count > 0:
            target_stage = get_stage(previous, stage_count)
            if not is_stage_ready(target_stage):
                raise ValueError(
                    f"段階数を {stage_count} に減らす前に 段階{stage_count} を設定してください。"
                )

    projected = _build_stage_count_preview_config(guild.id, previous, stage_count)
    return SetupPreviewSummary(
        reapply_count=_count_projected_reapply_records(
            context,
            guild.id,
            projected,
            clamp_stage_index=stage_count if previous is not None and stage_count < previous.stage_count else None,
        ),
        clamp_count=clamp_count,
        missing_role_count=_count_missing_roles(guild, projected),
    )


def preview_stage_settings(
    context: ServiceContext,
    guild: discord.Guild,
    stage: StatusStageConfig,
) -> SetupPreviewSummary:
    config = context.store.get_status_config(guild.id)
    if config is None:
        raise ValueError("先に段階数を設定してください。")
    if not 1 <= stage.stage_index <= config.stage_count:
        raise ValueError("存在しない段階です。")

    validate_stage_configuration(config, stage)
    if stage.role_id is not None and guild.get_role(stage.role_id) is None:
        raise ValueError(
            f"{stage_display_name(stage)} のロールが見つかりません。設定を見直してください。"
        )

    projected = _build_stage_preview_config(config, stage)
    return SetupPreviewSummary(
        reapply_count=_count_projected_reapply_records(context, guild.id, projected),
        clamp_count=0,
        missing_role_count=_count_missing_roles(guild, projected),
    )


async def list_guild_status_records(
    context: ServiceContext,
    guild: discord.Guild,
) -> list[StatusListEntry]:
    config = context.store.get_status_config(guild.id)
    if config is None:
        raise RuntimeError(f"このサーバーのステータス設定が未完了です。\n先に {SETUP_GUIDANCE}")

    current_ts = now_ts()
    entries: list[StatusListEntry] = []
    for row in context.store.get_active_records_by_guild(guild.id):
        projected = predict_reconciled_record(config, row, current_ts=current_ts)
        if projected is None:
            continue

        current_stage = get_stage(config, projected["stage_index"])
        if current_stage is None:
            raise RuntimeError("段階設定の取得に失敗しました。")

        member = guild.get_member(row["user_id"])
        entries.append(
            StatusListEntry(
                user_id=row["user_id"],
                member_display=member.mention if member is not None else f"<@{row['user_id']}>",
                stage_index=projected["stage_index"],
                stage_name=stage_display_name(current_stage),
                next_change_text=describe_record_next_change(config, projected),
                reason=str(projected["reason"] or ""),
                expires_at=projected["expires_at"],
            )
        )

    entries.sort(
        key=lambda entry: (
            entry.expires_at is None,
            entry.expires_at if entry.expires_at is not None else 0,
            -entry.stage_index,
            entry.user_id,
        )
    )
    return entries


async def list_member_status_history(
    context: ServiceContext,
    guild: discord.Guild,
    user_id: int,
) -> list[StatusHistoryEntry]:
    config = context.store.get_status_config(guild.id)
    return [
        StatusHistoryEntry(
            created_at=row["created_at"],
            event_type=row["event_type"],
            actor_display=resolve_actor_display(guild, row["actor_user_id"]),
            from_stage_name=(
                row["from_stage_name"]
                or resolve_history_stage_name(config, row["from_stage_index"])
            ),
            to_stage_name=(
                row["to_stage_name"]
                or resolve_history_stage_name(config, row["to_stage_index"])
            ),
            reason=row["reason"] or "",
            detail=row["detail"] or "",
        )
        for row in context.store.get_status_history_for_member(guild.id, user_id)
    ]
