from typing import Optional

import discord

from .config import ACTION_CLEAR, ACTION_HOLD, SETUP_GUIDANCE
from .formatters import describe_record_next_change, stage_display_name
from .models import GuildStatusConfig, SetupPreviewSummary, StatusHistoryEntry, StatusListEntry, StatusStageConfig
from .service_common import ServiceContext, resolve_actor_display, resolve_history_stage_name
from .validation import (
    default_stage_config,
    get_stage,
    is_stage_ready,
    now_ts,
    validate_stage_configuration,
)


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
