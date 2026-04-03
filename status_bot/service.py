from typing import Optional

import discord

from .config import ACTION_CLEAR, ACTION_HOLD, SETUP_GUIDANCE, logger
from .formatters import describe_record_next_change, stage_display_name
from .models import GuildStatusConfig, SetupPreviewSummary, StatusListEntry, StatusStageConfig
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

        entries: list[StatusListEntry] = []
        for row in self.store.get_active_records_by_guild(guild.id):
            await self.reconcile_record(row)
            current = self.store.get_status_record(guild.id, row["user_id"])
            if current is None:
                continue

            current_stage = get_stage(config, current["stage_index"])
            if current_stage is None:
                raise RuntimeError("段階設定の取得に失敗しました。")

            member = guild.get_member(current["user_id"])
            entries.append(
                StatusListEntry(
                    user_id=current["user_id"],
                    member_display=member.mention if member is not None else f"<@{current['user_id']}>",
                    stage_index=current["stage_index"],
                    stage_name=stage_display_name(current_stage),
                    next_change_text=describe_record_next_change(config, current),
                    reason=current["reason"] or "",
                    expires_at=current["expires_at"],
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
                self.store.commit()
                try:
                    await self.apply_status_role(guild_id, user_id, None, reason="Status expired -> cleared")
                except discord.Forbidden:
                    logger.exception("Failed to clear status roles for user %s", user_id)
                except RuntimeError:
                    logger.exception("Failed to clear status roles for user %s", user_id)
                return

            if current_stage.on_expire_action == ACTION_HOLD:
                self.store.upsert_status_record(guild_id, user_id, stage_index, None, reason)
                self.store.commit()
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
        self.store.commit()
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

    async def save_stage_count_settings(self, guild_id: int, stage_count: int) -> tuple[int, int]:
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

        self.store.commit()
        return await self.refresh_guild_status_roles(
            guild_id,
            remove_role_ids=previous_role_ids,
        )

    async def save_stage_settings(self, guild_id: int, stage: StatusStageConfig) -> tuple[int, int]:
        config = self.store.get_status_config(guild_id)
        if config is None:
            raise ValueError("先に段階数を設定してください。")
        if not 1 <= stage.stage_index <= config.stage_count:
            raise ValueError("存在しない段階です。")

        validate_stage_configuration(config, stage)
        previous_role_ids = configured_role_ids(config)
        self.store.upsert_status_stage(guild_id, stage)
        self.store.commit()
        return await self.refresh_guild_status_roles(
            guild_id,
            remove_role_ids=previous_role_ids,
        )

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

        expires_at = now_ts() + current_stage.duration_seconds
        self.store.upsert_status_record(guild_id, member.id, stage_index, expires_at, reason)
        self.store.commit()
        await self.apply_status_role(
            guild_id,
            member.id,
            stage_index,
            reason=f"Manual status set by {actor}",
        )
        return self.store.get_status_record(guild_id, member.id)

    async def clear_status(
        self,
        guild_id: int,
        member: discord.Member,
        actor: object,
    ) -> None:
        self.store.delete_status_record(guild_id, member.id)
        self.store.commit()
        await self.apply_status_role(
            guild_id,
            member.id,
            None,
            reason=f"Manual status clear by {actor}",
        )
