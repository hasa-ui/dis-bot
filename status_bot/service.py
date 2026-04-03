from typing import Optional

import discord

from .models import StatusStageConfig
from .service_actions import (
    bulk_assign_status,
    bulk_clear_status,
    apply_status_role,
    assign_status,
    clear_status,
    handle_member_join,
    process_due_records,
    reconcile_record,
    refresh_guild_status_roles,
    save_stage_count_settings,
    save_stage_settings,
)
from .service_common import ServiceContext, fetch_member_if_needed
from .service_notifications import send_status_notification
from .service_queries import (
    list_guild_status_records,
    list_member_status_history,
    preview_stage_count_settings,
    preview_stage_settings,
)
from .store import StatusStore


class StatusService:
    def __init__(self, bot: discord.Client, store: StatusStore) -> None:
        self.bot = bot
        self.store = store
        self.context = ServiceContext(bot=bot, store=store)

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
        await send_status_notification(
            self.context,
            guild_id,
            event_type=event_type,
            user_id=user_id,
            actor=actor,
            from_stage_name=from_stage_name,
            to_stage_name=to_stage_name,
            next_change_text=next_change_text,
            reason=reason,
            detail=detail,
            refreshed=refreshed,
            failed=failed,
        )

    def preview_stage_count_settings(
        self,
        guild: discord.Guild,
        stage_count: int,
    ):
        return preview_stage_count_settings(self.context, guild, stage_count)

    def preview_stage_settings(
        self,
        guild: discord.Guild,
        stage: StatusStageConfig,
    ):
        return preview_stage_settings(self.context, guild, stage)

    async def fetch_member_if_needed(self, guild_id: int, user_id: int) -> Optional[discord.Member]:
        return await fetch_member_if_needed(self.context, guild_id, user_id)

    async def list_guild_status_records(self, guild: discord.Guild):
        return await list_guild_status_records(self.context, guild)

    async def list_member_status_history(self, guild: discord.Guild, user_id: int):
        return await list_member_status_history(self.context, guild, user_id)

    async def apply_status_role(
        self,
        guild_id: int,
        user_id: int,
        stage_index: Optional[int],
        *,
        reason: str,
        remove_role_ids: Optional[set[int]] = None,
    ) -> None:
        await apply_status_role(
            self.context,
            guild_id,
            user_id,
            stage_index,
            reason=reason,
            remove_role_ids=remove_role_ids,
        )

    async def reconcile_record(self, row) -> None:
        await reconcile_record(self.context, row)

    async def refresh_guild_status_roles(
        self,
        guild_id: int,
        *,
        remove_role_ids: Optional[set[int]] = None,
        actor: Optional[object] = None,
    ) -> tuple[int, int]:
        return await refresh_guild_status_roles(
            self.context,
            guild_id,
            remove_role_ids=remove_role_ids,
            actor=actor,
        )

    async def process_due_records(self) -> None:
        await process_due_records(self.context)

    async def handle_member_join(self, member: discord.Member) -> None:
        await handle_member_join(self.context, member)

    async def save_stage_count_settings(
        self,
        guild_id: int,
        stage_count: int,
        actor: Optional[object] = None,
    ) -> tuple[int, int]:
        return await save_stage_count_settings(self.context, guild_id, stage_count, actor)

    async def save_stage_settings(
        self,
        guild_id: int,
        stage: StatusStageConfig,
        actor: Optional[object] = None,
    ) -> tuple[int, int]:
        return await save_stage_settings(self.context, guild_id, stage, actor)

    async def assign_status(
        self,
        guild_id: int,
        member: discord.Member,
        stage_index: int,
        reason: str,
        actor: object,
    ):
        return await assign_status(self.context, guild_id, member, stage_index, reason, actor)

    async def clear_status(
        self,
        guild_id: int,
        member: discord.Member,
        actor: object,
    ) -> None:
        await clear_status(self.context, guild_id, member, actor)

    async def bulk_assign_status(
        self,
        guild_id: int,
        members: list[discord.Member],
        stage_index: int,
        reason: str,
        actor: object,
    ):
        return await bulk_assign_status(self.context, guild_id, members, stage_index, reason, actor)

    async def bulk_clear_status(
        self,
        guild_id: int,
        members: list[discord.Member],
        actor: object,
    ):
        return await bulk_clear_status(self.context, guild_id, members, actor)
