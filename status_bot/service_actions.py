from typing import Optional

import discord

from .config import (
    ACTION_CLEAR,
    ACTION_HOLD,
    HISTORY_EVENT_AUTO_CLEAR,
    HISTORY_EVENT_AUTO_HOLD,
    HISTORY_EVENT_AUTO_TRANSITION,
    HISTORY_EVENT_CONFIG_STAGE_COUNT_SAVED,
    HISTORY_EVENT_CONFIG_STAGE_SAVED,
    HISTORY_EVENT_MANUAL_CLEAR,
    HISTORY_EVENT_MANUAL_SET,
    SETUP_GUIDANCE,
    logger,
)
from .formatters import describe_record_next_change, shorten_reason, stage_display_name
from .models import BulkOperationResult, StatusStageConfig
from .service_common import (
    ServiceContext,
    fetch_member_if_needed,
    infer_stage_from_member_roles,
    record_history,
    resolve_history_stage_name,
)
from .service_notifications import send_status_notification
from .validation import (
    configured_role_ids,
    get_stage,
    is_stage_ready,
    now_ts,
    stage_path_is_ready,
    validate_stage_configuration,
)


async def apply_status_role(
    context: ServiceContext,
    guild_id: int,
    user_id: int,
    stage_index: Optional[int],
    *,
    reason: str,
    remove_role_ids: Optional[set[int]] = None,
) -> None:
    guild = context.bot.get_guild(guild_id)
    if guild is None:
        logger.warning("Guild not found: %s", guild_id)
        return

    member = await fetch_member_if_needed(context, guild_id, user_id)
    if member is None:
        logger.info("Member %s is not currently in guild %s", user_id, guild_id)
        return

    config = context.store.get_status_config(guild_id)
    configured_ids = configured_role_ids(config)
    if remove_role_ids is not None:
        configured_ids.update(remove_role_ids)

    new_roles = [role for role in member.roles if role.id not in configured_ids]
    if stage_index is not None:
        if config is None:
            raise RuntimeError(f"このサーバーのステータス設定が未完了です。{SETUP_GUIDANCE}")

        stage = get_stage(config, stage_index)
        if not is_stage_ready(stage):
            raise RuntimeError(f"段階{stage_index} の設定が未完了です。{SETUP_GUIDANCE}")

        role = guild.get_role(stage.role_id)
        if role is None:
            raise RuntimeError(
                f"{stage_display_name(stage)} のロールが見つかりません。設定を見直してください。"
            )
        new_roles.append(role)

    await member.edit(roles=new_roles, reason=reason)


async def reconcile_record(context: ServiceContext, row) -> None:
    guild_id = row["guild_id"]
    user_id = row["user_id"]
    stage_index = row["stage_index"]
    original_stage_index = stage_index
    expires_at = row["expires_at"]
    reason = row["reason"]

    config = context.store.get_status_config(guild_id)
    if config is None:
        logger.warning("Guild %s has no status config; skipping reconcile for user %s", guild_id, user_id)
        return
    if expires_at is None:
        return

    current_ts = now_ts()
    changed = False
    while expires_at is not None and expires_at <= current_ts:
        current_stage = get_stage(config, stage_index)
        if not is_stage_ready(current_stage):
            logger.warning(
                "Guild %s stage %s is incomplete; skipping reconcile for user %s",
                guild_id,
                stage_index,
                user_id,
            )
            return

        if current_stage.on_expire_action == ACTION_CLEAR:
            context.store.delete_status_record(guild_id, user_id)
            from_stage_name = resolve_history_stage_name(config, stage_index)
            record_history(
                context,
                guild_id,
                user_id=user_id,
                actor=None,
                event_type=HISTORY_EVENT_AUTO_CLEAR,
                from_stage_index=stage_index,
                to_stage_index=None,
                reason=reason,
                detail="期限満了により解除",
                config=config,
            )
            context.store.commit()
            try:
                await apply_status_role(context, guild_id, user_id, None, reason="Status expired -> cleared")
            except discord.Forbidden:
                logger.exception("Failed to clear status roles for user %s", user_id)
            except RuntimeError:
                logger.exception("Failed to clear status roles for user %s", user_id)
            else:
                await send_status_notification(
                    context,
                    guild_id,
                    event_type=HISTORY_EVENT_AUTO_CLEAR,
                    user_id=user_id,
                    from_stage_name=from_stage_name,
                    reason=reason,
                )
            return

        if current_stage.on_expire_action == ACTION_HOLD:
            context.store.upsert_status_record(guild_id, user_id, stage_index, None, reason)
            stage_name = resolve_history_stage_name(config, stage_index)
            record_history(
                context,
                guild_id,
                user_id=user_id,
                actor=None,
                event_type=HISTORY_EVENT_AUTO_HOLD,
                from_stage_index=stage_index,
                to_stage_index=stage_index,
                reason=reason,
                detail="期限満了により同じ段階を維持",
                config=config,
            )
            context.store.commit()
            try:
                await apply_status_role(
                    context,
                    guild_id,
                    user_id,
                    stage_index,
                    reason=f"Status expiry -> hold stage {stage_index}",
                )
            except discord.Forbidden:
                logger.exception("Failed to hold status roles for user %s", user_id)
            except RuntimeError:
                logger.exception("Failed to hold status roles for user %s", user_id)
            else:
                await send_status_notification(
                    context,
                    guild_id,
                    event_type=HISTORY_EVENT_AUTO_HOLD,
                    user_id=user_id,
                    to_stage_name=stage_name,
                    reason=reason,
                )
            return

        next_stage = get_stage(config, stage_index - 1)
        if not is_stage_ready(next_stage):
            logger.warning(
                "Guild %s next stage %s is incomplete; skipping reconcile for user %s",
                guild_id,
                stage_index - 1,
                user_id,
            )
            return

        stage_index -= 1
        expires_at = expires_at + next_stage.duration_seconds
        changed = True

    if not changed:
        return

    context.store.upsert_status_record(guild_id, user_id, stage_index, expires_at, reason)
    from_stage_name = resolve_history_stage_name(config, original_stage_index)
    to_stage_name = resolve_history_stage_name(config, stage_index)
    record_history(
        context,
        guild_id,
        user_id=user_id,
        actor=None,
        event_type=HISTORY_EVENT_AUTO_TRANSITION,
        from_stage_index=original_stage_index,
        to_stage_index=stage_index,
        reason=reason,
        detail="期限満了により自動遷移",
        config=config,
    )
    context.store.commit()
    try:
        await apply_status_role(
            context,
            guild_id,
            user_id,
            stage_index,
            reason=f"Status auto-transitioned -> stage {stage_index}",
        )
    except discord.Forbidden:
        logger.exception("Failed to update status roles for user %s", user_id)
    except RuntimeError:
        logger.exception("Failed to update status roles for user %s", user_id)
    else:
        await send_status_notification(
            context,
            guild_id,
            event_type=HISTORY_EVENT_AUTO_TRANSITION,
            user_id=user_id,
            from_stage_name=from_stage_name,
            to_stage_name=to_stage_name,
            next_change_text=describe_record_next_change(
                config,
                {"stage_index": stage_index, "expires_at": expires_at, "reason": reason},
            ),
            reason=reason,
        )


async def refresh_guild_status_roles(
    context: ServiceContext,
    guild_id: int,
    *,
    remove_role_ids: Optional[set[int]] = None,
    actor: Optional[object] = None,
) -> tuple[int, int]:
    total = 0
    failed = 0
    for row in context.store.get_active_records_by_guild(guild_id):
        await reconcile_record(context, row)
        current = context.store.get_status_record(guild_id, row["user_id"])
        if current is None:
            continue

        total += 1
        try:
            await apply_status_role(
                context,
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


async def process_due_records(context: ServiceContext) -> None:
    for row in context.store.get_due_records(now_ts()):
        await reconcile_record(context, row)


async def handle_member_join(context: ServiceContext, member: discord.Member) -> None:
    row = context.store.get_status_record(member.guild.id, member.id)
    if row is None:
        return

    await reconcile_record(context, row)
    row = context.store.get_status_record(member.guild.id, member.id)
    if row is None:
        return

    try:
        await apply_status_role(
            context,
            member.guild.id,
            member.id,
            row["stage_index"],
            reason="Re-applied active status on rejoin",
        )
    except discord.Forbidden:
        logger.exception("Failed to re-apply status roles on rejoin for user %s", member.id)
    except RuntimeError:
        logger.exception("Failed to re-apply status roles on rejoin for user %s", member.id)


async def save_stage_count_settings(
    context: ServiceContext,
    guild_id: int,
    stage_count: int,
    actor: Optional[object] = None,
) -> tuple[int, int]:
    previous = context.store.get_status_config(guild_id)
    if previous is not None and stage_count < previous.stage_count:
        if context.store.count_records_above_stage(guild_id, stage_count) > 0:
            target_stage = get_stage(previous, stage_count)
            if not is_stage_ready(target_stage):
                raise ValueError(
                    f"段階数を {stage_count} に減らす前に 段階{stage_count} を設定してください。"
                )

    previous_role_ids = configured_role_ids(previous)
    context.store.set_stage_count_value(guild_id, stage_count)
    context.store.ensure_stage_rows(guild_id, stage_count)

    if previous is not None and previous.stage_count > stage_count:
        current = context.store.get_status_config(guild_id)
        target_stage = get_stage(current, stage_count) if current is not None else None
        target_expires_at = None
        if target_stage is not None and target_stage.duration_seconds > 0:
            target_expires_at = now_ts() + target_stage.duration_seconds
        context.store.clamp_records_to_stage(guild_id, stage_count, target_expires_at)
        context.store.delete_stages_above(guild_id, stage_count)

    previous_count = previous.stage_count if previous is not None else None
    detail = (
        f"段階数を {'未設定' if previous_count is None else previous_count} から "
        f"{stage_count} に変更"
    )
    record_history(
        context,
        guild_id,
        user_id=None,
        actor=actor,
        event_type=HISTORY_EVENT_CONFIG_STAGE_COUNT_SAVED,
        from_stage_index=None,
        to_stage_index=None,
        detail=detail,
    )
    context.store.commit()
    refreshed, failed = await refresh_guild_status_roles(
        context,
        guild_id,
        remove_role_ids=previous_role_ids,
        actor=actor,
    )
    await send_status_notification(
        context,
        guild_id,
        event_type=HISTORY_EVENT_CONFIG_STAGE_COUNT_SAVED,
        actor=actor,
        detail=detail,
        refreshed=refreshed,
        failed=failed,
    )
    return refreshed, failed


async def save_stage_settings(
    context: ServiceContext,
    guild_id: int,
    stage: StatusStageConfig,
    actor: Optional[object] = None,
) -> tuple[int, int]:
    config = context.store.get_status_config(guild_id)
    if config is None:
        raise ValueError("先に段階数を設定してください。")
    if not 1 <= stage.stage_index <= config.stage_count:
        raise ValueError("存在しない段階です。")

    validate_stage_configuration(config, stage)
    previous_role_ids = configured_role_ids(config)
    previous_stage = get_stage(config, stage.stage_index)
    context.store.upsert_status_stage(guild_id, stage)
    detail = (
        f"{stage_display_name(stage)} を保存 "
        f"(ロール {previous_stage.role_id if previous_stage is not None else '未設定'} -> {stage.role_id}, "
        f"期間 {previous_stage.duration_seconds if previous_stage is not None else '未設定'} -> {stage.duration_seconds}, "
        f"満了時 {previous_stage.on_expire_action if previous_stage is not None else '未設定'} -> {stage.on_expire_action})"
    )
    record_history(
        context,
        guild_id,
        user_id=None,
        actor=actor,
        event_type=HISTORY_EVENT_CONFIG_STAGE_SAVED,
        from_stage_index=previous_stage.stage_index if previous_stage is not None else None,
        to_stage_index=stage.stage_index,
        detail=detail,
        config=config,
    )
    context.store.commit()
    refreshed, failed = await refresh_guild_status_roles(
        context,
        guild_id,
        remove_role_ids=previous_role_ids,
        actor=actor,
    )
    await send_status_notification(
        context,
        guild_id,
        event_type=HISTORY_EVENT_CONFIG_STAGE_SAVED,
        actor=actor,
        detail=detail,
        refreshed=refreshed,
        failed=failed,
    )
    return refreshed, failed


async def assign_status(
    context: ServiceContext,
    guild_id: int,
    member: discord.Member,
    stage_index: int,
    reason: str,
    actor: object,
):
    config = context.store.get_status_config(guild_id)
    if config is None:
        raise RuntimeError(f"このサーバーのステータス設定が未完了です。\n先に {SETUP_GUIDANCE}")
    current_stage = get_stage(config, stage_index)
    if current_stage is None or not stage_path_is_ready(config, stage_index):
        raise RuntimeError(
            f"段階{stage_index} から到達するステータス設定が未完了です。\n先に {SETUP_GUIDANCE}"
        )

    previous = context.store.get_status_record(guild_id, member.id)
    expires_at = now_ts() + current_stage.duration_seconds
    await apply_status_role(
        context,
        guild_id,
        member.id,
        stage_index,
        reason=f"Manual status set by {actor}",
    )
    context.store.upsert_status_record(guild_id, member.id, stage_index, expires_at, reason)
    record_history(
        context,
        guild_id,
        user_id=member.id,
        actor=actor,
        event_type=HISTORY_EVENT_MANUAL_SET,
        from_stage_index=previous["stage_index"] if previous is not None else None,
        to_stage_index=stage_index,
        reason=reason,
        config=config,
    )
    context.store.commit()
    await send_status_notification(
        context,
        guild_id,
        event_type=HISTORY_EVENT_MANUAL_SET,
        user_id=member.id,
        actor=actor,
        to_stage_name=resolve_history_stage_name(config, stage_index),
        next_change_text=describe_record_next_change(
            config,
            {"stage_index": stage_index, "expires_at": expires_at, "reason": reason},
        ),
        reason=reason,
    )
    return context.store.get_status_record(guild_id, member.id)


async def clear_status(
    context: ServiceContext,
    guild_id: int,
    member: discord.Member,
    actor: object,
) -> None:
    config = context.store.get_status_config(guild_id)
    previous = context.store.get_status_record(guild_id, member.id)
    stale_stage_index = infer_stage_from_member_roles(config, member) if previous is None else None
    await apply_status_role(
        context,
        guild_id,
        member.id,
        None,
        reason=f"Manual status clear by {actor}",
    )
    if previous is not None or stale_stage_index is not None:
        context.store.delete_status_record(guild_id, member.id)
        from_stage_index = previous["stage_index"] if previous is not None else stale_stage_index
        reason = previous["reason"] if previous is not None else ""
        record_history(
            context,
            guild_id,
            user_id=member.id,
            actor=actor,
            event_type=HISTORY_EVENT_MANUAL_CLEAR,
            from_stage_index=from_stage_index,
            to_stage_index=None,
            reason=reason,
            config=config,
        )
        context.store.commit()
        await send_status_notification(
            context,
            guild_id,
            event_type=HISTORY_EVENT_MANUAL_CLEAR,
            user_id=member.id,
            actor=actor,
            from_stage_name=resolve_history_stage_name(
                config,
                previous["stage_index"] if previous is not None else stale_stage_index,
            ),
            reason=previous["reason"] if previous is not None else "",
        )


async def bulk_assign_status(
    context: ServiceContext,
    guild_id: int,
    members: list[discord.Member],
    stage_index: int,
    reason: str,
    actor: object,
) -> BulkOperationResult:
    success_count = 0
    failure_count = 0
    detail_lines: list[str] = []

    for member in members:
        try:
            await assign_status(context, guild_id, member, stage_index, reason, actor)
        except discord.Forbidden:
            failure_count += 1
            detail_lines.append(f"- {member.mention}: 失敗 (権限不足)")
            logger.warning("Failed to bulk-assign status for user %s", member.id)
        except discord.HTTPException as exc:
            failure_count += 1
            detail_lines.append(
                f"- {member.mention}: 失敗 ({shorten_reason(str(exc) or exc.__class__.__name__, 80)})"
            )
            logger.warning("Failed to bulk-assign status for user %s", member.id)
        except RuntimeError as exc:
            failure_count += 1
            detail_lines.append(f"- {member.mention}: 失敗 ({shorten_reason(str(exc), 80)})")
            logger.warning("Failed to bulk-assign status for user %s", member.id)
        else:
            success_count += 1

    return BulkOperationResult(
        processed_count=len(members),
        success_count=success_count,
        failure_count=failure_count,
        detail_lines=detail_lines,
    )


async def bulk_clear_status(
    context: ServiceContext,
    guild_id: int,
    members: list[discord.Member],
    actor: object,
) -> BulkOperationResult:
    config = context.store.get_status_config(guild_id)
    success_count = 0
    failure_count = 0
    detail_lines: list[str] = []

    for member in members:
        row = context.store.get_status_record(guild_id, member.id)
        stale_stage_index = infer_stage_from_member_roles(config, member) if row is None else None
        if row is None and stale_stage_index is None:
            detail_lines.append(f"- {member.mention}: 除外 (解除対象なし)")
            continue

        try:
            await clear_status(context, guild_id, member, actor)
        except discord.Forbidden:
            failure_count += 1
            detail_lines.append(f"- {member.mention}: 失敗 (権限不足)")
            logger.warning("Failed to bulk-clear status for user %s", member.id)
        except discord.HTTPException as exc:
            failure_count += 1
            detail_lines.append(
                f"- {member.mention}: 失敗 ({shorten_reason(str(exc) or exc.__class__.__name__, 80)})"
            )
            logger.warning("Failed to bulk-clear status for user %s", member.id)
        except RuntimeError as exc:
            failure_count += 1
            detail_lines.append(f"- {member.mention}: 失敗 ({shorten_reason(str(exc), 80)})")
            logger.warning("Failed to bulk-clear status for user %s", member.id)
        else:
            success_count += 1

    return BulkOperationResult(
        processed_count=len(members),
        success_count=success_count,
        failure_count=failure_count,
        detail_lines=detail_lines,
    )
