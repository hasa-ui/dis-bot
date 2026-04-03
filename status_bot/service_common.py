from dataclasses import dataclass
from typing import Optional

import discord

from .config import logger
from .formatters import stage_display_name
from .models import GuildStatusConfig
from .store import StatusStore
from .validation import default_stage_config, get_stage


@dataclass(frozen=True)
class ServiceContext:
    bot: discord.Client
    store: StatusStore


def actor_user_id(actor: object) -> Optional[int]:
    actor_id = getattr(actor, "id", None)
    return actor_id if isinstance(actor_id, int) else None


def resolve_history_stage_name(
    config: Optional[GuildStatusConfig],
    stage_index: Optional[int],
) -> Optional[str]:
    if stage_index is None:
        return None
    stage = get_stage(config, stage_index) if config is not None else None
    return stage_display_name(stage or default_stage_config(stage_index))


def record_history(
    context: ServiceContext,
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
    context.store.append_status_history(
        guild_id,
        user_id=user_id,
        actor_user_id=actor_user_id(actor) if actor is not None else None,
        event_type=event_type,
        from_stage_index=from_stage_index,
        from_stage_name=(
            from_stage_name
            if from_stage_name is not None or from_stage_index is None
            else resolve_history_stage_name(config, from_stage_index)
        ),
        to_stage_index=to_stage_index,
        to_stage_name=(
            to_stage_name
            if to_stage_name is not None or to_stage_index is None
            else resolve_history_stage_name(config, to_stage_index)
        ),
        reason=reason,
        detail=detail,
    )


def resolve_actor_display(guild: discord.Guild, actor_id: Optional[int]) -> str:
    if actor_id is None:
        return "システム"

    member = guild.get_member(actor_id)
    return member.mention if member is not None else f"<@{actor_id}>"


def resolve_member_display(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.mention if member is not None else f"<@{user_id}>"


def infer_stage_from_member_roles(
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


async def fetch_member_if_needed(
    context: ServiceContext,
    guild_id: int,
    user_id: int,
) -> Optional[discord.Member]:
    guild = context.bot.get_guild(guild_id)
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
