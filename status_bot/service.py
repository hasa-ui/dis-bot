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
from .formatters import (
    build_auto_clear_notification,
    build_auto_hold_notification,
    build_auto_transition_notification,
    build_config_change_notification,
    build_manual_clear_notification,
    build_manual_set_notification,
    describe_record_next_change,
    stage_display_name,
)
from .models import (
    GuildStatusConfig,
    GuildStatusNotificationConfig,
    SetupPreviewSummary,
    StatusHistoryEntry,
    StatusListEntry,
    StatusStageConfig,
)
from .store import StatusStore
from .validation import (
    configured_role_ids,
    default_stage_config,
    get_stage,
    is_stage_ready,
    now_ts,
    stage_path_is_ready,
    validate_stage_configuration,
)


class StatusService:
    def __init__(self, bot: discord.Client, store: StatusStore) -> None:
        self.bot = bot
        self.store = store

    def _actor_user_id(self, actor: object) -> Optional[int]:
        actor_id = getattr(actor, "id", None)
        return actor_id if isinstance(actor_id, int) else None

    def _record_history(
        self,
        guild_id: int,
        *,
        user_id: Optional[int],
        actor: Optional[object],
        event_type: str,
        from_stage_index: Optional[int],
        to_stage_index: Optional[int],
        reason: str = "",
        detail: str = "",
        config: Optional[GuildStatusConfig] = None,
        from_stage_name: Optional[str] = None,
        to_stage_name: Optional[str] = None,
    ) -> None:
        self.store.append_status_history(
            guild_id,
            user_id=user_id,
            actor_user_id=self._actor_user_id(actor) if actor is not None else None,
            event_type=event_type,
            from_stage_index=from_stage_index,
            from_stage_name=(
                from_stage_name
                if from_stage_name is not None or from_stage_index is None
                else self._resolve_history_stage_name(config, from_stage_index)
            ),
            to_stage_index=to_stage_index,
            to_stage_name=(
                to_stage_name
                if to_stage_name is not None or to_stage_index is None
                else self._resolve_history_stage_name(config, to_stage_index)
            ),
            reason=reason,
            detail=detail,
        )

    def _resolve_history_stage_name(
        self,
        config: Optional[GuildStatusConfig],
        stage_index: Optional[int],
    ) -> Optional[str]:
        if stage_index is None:
            return None
        stage = get_stage(config, stage_index) if config is not None else None
        return stage_display_name(stage or default_stage_config(stage_index))

    def _resolve_actor_display(self, guild: discord.Guild, actor_user_id: Optional[int]) -> str:
        if actor_user_id is None:
            return "システム"

        member = guild.get_member(actor_user_id)
        return member.mention if member is not None else f"<@{actor_user_id}>"

    def _resolve_member_display(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        return member.mention if member is not None else f"<@{user_id}>"

    def _notification_enabled(
        self,
        config: GuildStatusNotificationConfig,
        event_type: str,
    ) -> bool:
        if event_type == HISTORY_EVENT_MANUAL_SET:
            return config.notify_manual_set
        if event_type == HISTORY_EVENT_MANUAL_CLEAR:
            return config.notify_manual_clear
        if event_type in {HISTORY_EVENT_AUTO_TRANSITION, HISTORY_EVENT_AUTO_CLEAR}:
            return config.notify_auto_transition
        if event_type == HISTORY_EVENT_AUTO_HOLD:
            return config.notify_auto_hold
        if event_type in {HISTORY_EVENT_CONFIG_STAGE_COUNT_SAVED, HISTORY_EVENT_CONFIG_STAGE_SAVED}:
            return config.notify_config_change
        return False

    async def _send_notification_content(self, guild: discord.Guild, channel_id: int, content: str) -> None:
        channel = guild.get_channel(channel_id)
        if channel is None:
            logger.warning("Notification channel not found in cache: guild=%s channel=%s", guild.id, channel_id)
            return

        me = guild.me
        if me is None:
            logger.warning("Bot member not found for guild=%s when sending notification", guild.id)
            return

        perms = channel.permissions_for(me)
        if not perms.view_channel or not perms.send_messages:
            logger.warning(
                "Missing permission for notification channel: guild=%s channel=%s",
                guild.id,
                channel_id,
            )
            return

        try:
            await channel.send(content)
        except discord.HTTPException:
            logger.exception(
                "Failed to send notification: guild=%s channel=%s",
                guild.id,
                channel_id,
            )

    async def send_status_notification(
        self,
        guild_id: int,
        *,
        event_type: str,
        user_id: Optional[int] = None,
        actor: Optional[object] = None,
        from_stage_name: Optional[str] = None,
        to_stage_name: Optional[str] = None,
        next_change_text: Optional[str] = None,
        reason: str = "",
        detail: str = "",
        refreshed: Optional[int] = None,
        failed: Optional[int] = None,
    ) -> None:
        notification_config = self.store.get_status_notification_config(guild_id)
        if notification_config.channel_id is None:
            return
        if not self._notification_enabled(notification_config, event_type):
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning("Guild not found for notification: %s", guild_id)
            return

        actor_display = self._resolve_actor_display(guild, self._actor_user_id(actor))
        member_display = self._resolve_member_display(guild, user_id) if user_id is not None else ""

        if event_type == HISTORY_EVENT_MANUAL_SET:
            content = build_manual_set_notification(
                member_display,
                to_stage_name or "不明",
                next_change_text or "不明",
                reason=reason,
                actor_display=actor_display,
            )
        elif event_type == HISTORY_EVENT_MANUAL_CLEAR:
            content = build_manual_clear_notification(
                member_display,
                from_stage_name,
                reason=reason,
                actor_display=actor_display,
            )
        elif event_type == HISTORY_EVENT_AUTO_TRANSITION:
            content = build_auto_transition_notification(
                member_display,
                from_stage_name,
                to_stage_name,
                next_change_text or "不明",
                reason=reason,
            )
        elif event_type == HISTORY_EVENT_AUTO_HOLD:
            content = build_auto_hold_notification(
                member_display,
                to_stage_name,
                reason=reason,
            )
        elif event_type == HISTORY_EVENT_AUTO_CLEAR:
            content = build_auto_clear_notification(
                member_display,
                from_stage_name,
                reason=reason,
            )
        elif event_type in {HISTORY_EVENT_CONFIG_STAGE_COUNT_SAVED, HISTORY_EVENT_CONFIG_STAGE_SAVED}:
            content = build_config_change_notification(
                detail,
                actor_display=actor_display,
                refreshed=0 if refreshed is None else refreshed,
                failed=0 if failed is None else failed,
            )
        else:
            return

        await self._send_notification_content(guild, notification_config.channel_id, content)

    def _infer_stage_from_member_roles(
        self,
        config: Optional[GuildStatusConfig],
        member: object,
    ) -> Optional[int]:
        if config is None:
            return None

        roles = getattr(member, "roles", None)
        if roles is None:
            return None

        role_ids = {getattr(role, "id", None) for role in roles}
        for stage in reversed(config.stages):
            if stage.role_id is not None and stage.role_id in role_ids:
                return stage.stage_index
        return None

    def _predict_reconciled_record(
        self,
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
        self,
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

        for row in self.store.get_active_records_by_guild(guild_id):
            projected_stage_index = row["stage_index"]
            projected_expires_at = row["expires_at"]
            if clamp_stage_index is not None and projected_stage_index > clamp_stage_index:
                projected_stage_index = clamp_stage_index
                if projected_expires_at is not None and target_expires_at is not None:
                    projected_expires_at = target_expires_at

            projected = self._predict_reconciled_record(
                config,
                row,
                current_ts=current_ts,
                projected_stage_index=projected_stage_index,
                projected_expires_at=projected_expires_at,
            )
            if projected is not None:
                count += 1
        return count

    def _count_missing_roles(self, guild: discord.Guild, config: GuildStatusConfig) -> int:
        return sum(
            1
            for stage in config.stages
            if stage.role_id is not None and guild.get_role(stage.role_id) is None
        )

    def _build_stage_count_preview_config(
        self,
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
        self,
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
        self,
        guild: discord.Guild,
        stage_count: int,
    ) -> SetupPreviewSummary:
        previous = self.store.get_status_config(guild.id)
        clamp_count = 0
        if previous is not None and stage_count < previous.stage_count:
            clamp_count = self.store.count_records_above_stage(guild.id, stage_count)
            if clamp_count > 0:
                target_stage = get_stage(previous, stage_count)
                if not is_stage_ready(target_stage):
                    raise ValueError(
                        f"段階数を {stage_count} に減らす前に 段階{stage_count} を設定してください。"
                    )

        projected = self._build_stage_count_preview_config(guild.id, previous, stage_count)
        return SetupPreviewSummary(
            reapply_count=self._count_projected_reapply_records(
                guild.id,
                projected,
                clamp_stage_index=stage_count if previous is not None and stage_count < previous.stage_count else None,
            ),
            clamp_count=clamp_count,
            missing_role_count=self._count_missing_roles(guild, projected),
        )

    def preview_stage_settings(
        self,
        guild: discord.Guild,
        stage: StatusStageConfig,
    ) -> SetupPreviewSummary:
        config = self.store.get_status_config(guild.id)
        if config is None:
            raise ValueError("先に段階数を設定してください。")
        if not 1 <= stage.stage_index <= config.stage_count:
            raise ValueError("存在しない段階です。")

        validate_stage_configuration(config, stage)
        if stage.role_id is not None and guild.get_role(stage.role_id) is None:
            raise ValueError(
                f"{stage_display_name(stage)} のロールが見つかりません。設定を見直してください。"
            )

        projected = self._build_stage_preview_config(config, stage)
        return SetupPreviewSummary(
            reapply_count=self._count_projected_reapply_records(guild.id, projected),
            clamp_count=0,
            missing_role_count=self._count_missing_roles(guild, projected),
        )

    async def fetch_member_if_needed(self, guild_id: int, user_id: int) -> Optional[discord.Member]:
        guild = self.bot.get_guild(guild_id)
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

    async def list_guild_status_records(self, guild: discord.Guild) -> list[StatusListEntry]:
        config = self.store.get_status_config(guild.id)
        if config is None:
            raise RuntimeError(f"このサーバーのステータス設定が未完了です。\n先に {SETUP_GUIDANCE}")

        current_ts = now_ts()
        entries: list[StatusListEntry] = []
        for row in self.store.get_active_records_by_guild(guild.id):
            projected = self._predict_reconciled_record(config, row, current_ts=current_ts)
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
        self,
        guild: discord.Guild,
        user_id: int,
    ) -> list[StatusHistoryEntry]:
        config = self.store.get_status_config(guild.id)
        return [
            StatusHistoryEntry(
                created_at=row["created_at"],
                event_type=row["event_type"],
                actor_display=self._resolve_actor_display(guild, row["actor_user_id"]),
                from_stage_name=(
                    row["from_stage_name"]
                    or self._resolve_history_stage_name(config, row["from_stage_index"])
                ),
                to_stage_name=(
                    row["to_stage_name"]
                    or self._resolve_history_stage_name(config, row["to_stage_index"])
                ),
                reason=row["reason"] or "",
                detail=row["detail"] or "",
            )
            for row in self.store.get_status_history_for_member(guild.id, user_id)
        ]

    async def apply_status_role(
        self,
        guild_id: int,
        user_id: int,
        stage_index: Optional[int],
        *,
        reason: str,
        remove_role_ids: Optional[set[int]] = None,
    ) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning("Guild not found: %s", guild_id)
            return

        member = await self.fetch_member_if_needed(guild_id, user_id)
        if member is None:
            logger.info("Member %s is not currently in guild %s", user_id, guild_id)
            return

        config = self.store.get_status_config(guild_id)
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
                    f"段階{stage_index} の設定が未完了です。{SETUP_GUIDANCE}"
                )

            role = guild.get_role(stage.role_id)
            if role is None:
                raise RuntimeError(
                    f"{stage_display_name(stage)} のロールが見つかりません。設定を見直してください。"
                )
            new_roles.append(role)

        await member.edit(roles=new_roles, reason=reason)

    async def reconcile_record(self, row) -> None:
        guild_id = row["guild_id"]
        user_id = row["user_id"]
        stage_index = row["stage_index"]
        original_stage_index = stage_index
        expires_at = row["expires_at"]
        reason = row["reason"]

        config = self.store.get_status_config(guild_id)
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
                self.store.delete_status_record(guild_id, user_id)
                from_stage_name = self._resolve_history_stage_name(config, stage_index)
                self._record_history(
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
                self.store.commit()
                await self.send_status_notification(
                    guild_id,
                    event_type=HISTORY_EVENT_AUTO_CLEAR,
                    user_id=user_id,
                    from_stage_name=from_stage_name,
                    reason=reason,
                )
                try:
                    await self.apply_status_role(guild_id, user_id, None, reason="Status expired -> cleared")
                except discord.Forbidden:
                    logger.exception("Failed to clear status roles for user %s", user_id)
                except RuntimeError:
                    logger.exception("Failed to clear status roles for user %s", user_id)
                return

            if current_stage.on_expire_action == ACTION_HOLD:
                self.store.upsert_status_record(guild_id, user_id, stage_index, None, reason)
                stage_name = self._resolve_history_stage_name(config, stage_index)
                self._record_history(
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
                self.store.commit()
                await self.send_status_notification(
                    guild_id,
                    event_type=HISTORY_EVENT_AUTO_HOLD,
                    user_id=user_id,
                    to_stage_name=stage_name,
                    reason=reason,
                )
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

        self.store.upsert_status_record(guild_id, user_id, stage_index, expires_at, reason)
        from_stage_name = self._resolve_history_stage_name(config, original_stage_index)
        to_stage_name = self._resolve_history_stage_name(config, stage_index)
        self._record_history(
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
        self.store.commit()
        await self.send_status_notification(
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
        actor: Optional[object] = None,
    ) -> tuple[int, int]:
        total = 0
        failed = 0
        for row in self.store.get_active_records_by_guild(guild_id):
            await self.reconcile_record(row)
            current = self.store.get_status_record(guild_id, row["user_id"])
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

    async def process_due_records(self) -> None:
        for row in self.store.get_due_records(now_ts()):
            await self.reconcile_record(row)

    async def handle_member_join(self, member: discord.Member) -> None:
        row = self.store.get_status_record(member.guild.id, member.id)
        if row is None:
            return

        await self.reconcile_record(row)
        row = self.store.get_status_record(member.guild.id, member.id)
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

    async def save_stage_count_settings(
        self,
        guild_id: int,
        stage_count: int,
        actor: Optional[object] = None,
    ) -> tuple[int, int]:
        previous = self.store.get_status_config(guild_id)
        if previous is not None and stage_count < previous.stage_count:
            if self.store.count_records_above_stage(guild_id, stage_count) > 0:
                target_stage = get_stage(previous, stage_count)
                if not is_stage_ready(target_stage):
                    raise ValueError(
                        f"段階数を {stage_count} に減らす前に 段階{stage_count} を設定してください。"
                    )

        previous_role_ids = configured_role_ids(previous)
        self.store.set_stage_count_value(guild_id, stage_count)
        self.store.ensure_stage_rows(guild_id, stage_count)

        if previous is not None and previous.stage_count > stage_count:
            current = self.store.get_status_config(guild_id)
            target_stage = get_stage(current, stage_count) if current is not None else None
            target_expires_at = None
            if target_stage is not None and target_stage.duration_seconds > 0:
                target_expires_at = now_ts() + target_stage.duration_seconds
            self.store.clamp_records_to_stage(guild_id, stage_count, target_expires_at)
            self.store.delete_stages_above(guild_id, stage_count)

        previous_count = previous.stage_count if previous is not None else None
        self._record_history(
            guild_id,
            user_id=None,
            actor=actor,
            event_type=HISTORY_EVENT_CONFIG_STAGE_COUNT_SAVED,
            from_stage_index=None,
            to_stage_index=None,
            detail=(
                f"段階数を {'未設定' if previous_count is None else previous_count} から "
                f"{stage_count} に変更"
            ),
        )
        self.store.commit()
        detail = (
            f"段階数を {'未設定' if previous_count is None else previous_count} から "
            f"{stage_count} に変更"
        )
        refreshed, failed = await self.refresh_guild_status_roles(
            guild_id,
            remove_role_ids=previous_role_ids,
            actor=actor,
        )
        await self.send_status_notification(
            guild_id,
            event_type=HISTORY_EVENT_CONFIG_STAGE_COUNT_SAVED,
            actor=actor,
            detail=detail,
            refreshed=refreshed,
            failed=failed,
        )
        return refreshed, failed

    async def save_stage_settings(
        self,
        guild_id: int,
        stage: StatusStageConfig,
        actor: Optional[object] = None,
    ) -> tuple[int, int]:
        config = self.store.get_status_config(guild_id)
        if config is None:
            raise ValueError("先に段階数を設定してください。")
        if not 1 <= stage.stage_index <= config.stage_count:
            raise ValueError("存在しない段階です。")

        validate_stage_configuration(config, stage)
        previous_role_ids = configured_role_ids(config)
        previous_stage = get_stage(config, stage.stage_index)
        self.store.upsert_status_stage(guild_id, stage)
        detail = (
            f"{stage_display_name(stage)} を保存 "
            f"(ロール {previous_stage.role_id if previous_stage is not None else '未設定'} -> {stage.role_id}, "
            f"期間 {previous_stage.duration_seconds if previous_stage is not None else '未設定'} -> {stage.duration_seconds}, "
            f"満了時 {previous_stage.on_expire_action if previous_stage is not None else '未設定'} -> {stage.on_expire_action})"
        )
        self._record_history(
            guild_id,
            user_id=None,
            actor=actor,
            event_type=HISTORY_EVENT_CONFIG_STAGE_SAVED,
            from_stage_index=previous_stage.stage_index if previous_stage is not None else None,
            to_stage_index=stage.stage_index,
            detail=detail,
            config=config,
        )
        self.store.commit()
        refreshed, failed = await self.refresh_guild_status_roles(
            guild_id,
            remove_role_ids=previous_role_ids,
            actor=actor,
        )
        await self.send_status_notification(
            guild_id,
            event_type=HISTORY_EVENT_CONFIG_STAGE_SAVED,
            actor=actor,
            detail=detail,
            refreshed=refreshed,
            failed=failed,
        )
        return refreshed, failed

    async def assign_status(
        self,
        guild_id: int,
        member: discord.Member,
        stage_index: int,
        reason: str,
        actor: object,
    ):
        config = self.store.get_status_config(guild_id)
        if config is None:
            raise RuntimeError(f"このサーバーのステータス設定が未完了です。\n先に {SETUP_GUIDANCE}")
        current_stage = get_stage(config, stage_index)
        if current_stage is None or not stage_path_is_ready(config, stage_index):
            raise RuntimeError(
                f"段階{stage_index} から到達するステータス設定が未完了です。\n先に {SETUP_GUIDANCE}"
            )

        previous = self.store.get_status_record(guild_id, member.id)
        expires_at = now_ts() + current_stage.duration_seconds
        self.store.upsert_status_record(guild_id, member.id, stage_index, expires_at, reason)
        self._record_history(
            guild_id,
            user_id=member.id,
            actor=actor,
            event_type=HISTORY_EVENT_MANUAL_SET,
            from_stage_index=previous["stage_index"] if previous is not None else None,
            to_stage_index=stage_index,
            reason=reason,
            config=config,
        )
        self.store.commit()
        await self.send_status_notification(
            guild_id,
            event_type=HISTORY_EVENT_MANUAL_SET,
            user_id=member.id,
            actor=actor,
            to_stage_name=self._resolve_history_stage_name(config, stage_index),
            next_change_text=describe_record_next_change(
                config,
                {"stage_index": stage_index, "expires_at": expires_at, "reason": reason},
            ),
            reason=reason,
        )
        try:
            await self.apply_status_role(
                guild_id,
                member.id,
                stage_index,
                reason=f"Manual status set by {actor}",
            )
        except (discord.Forbidden, RuntimeError):
            raise
        return self.store.get_status_record(guild_id, member.id)

    async def clear_status(
        self,
        guild_id: int,
        member: discord.Member,
        actor: object,
    ) -> None:
        config = self.store.get_status_config(guild_id)
        previous = self.store.get_status_record(guild_id, member.id)
        stale_stage_index = self._infer_stage_from_member_roles(config, member) if previous is None else None
        if previous is not None or stale_stage_index is not None:
            self.store.delete_status_record(guild_id, member.id)
            from_stage_index = previous["stage_index"] if previous is not None else stale_stage_index
            reason = previous["reason"] if previous is not None else ""
            self._record_history(
                guild_id,
                user_id=member.id,
                actor=actor,
                event_type=HISTORY_EVENT_MANUAL_CLEAR,
                from_stage_index=from_stage_index,
                to_stage_index=None,
                reason=reason,
                config=config,
            )
            self.store.commit()
            await self.send_status_notification(
                guild_id,
                event_type=HISTORY_EVENT_MANUAL_CLEAR,
                user_id=member.id,
                actor=actor,
                from_stage_name=self._resolve_history_stage_name(config, from_stage_index),
                reason=reason,
            )
        try:
            await self.apply_status_role(
                guild_id,
                member.id,
                None,
                reason=f"Manual status clear by {actor}",
            )
        except (discord.Forbidden, RuntimeError):
            raise
