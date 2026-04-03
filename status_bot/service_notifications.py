from typing import Optional

import discord

from .config import (
    HISTORY_EVENT_AUTO_CLEAR,
    HISTORY_EVENT_AUTO_HOLD,
    HISTORY_EVENT_AUTO_TRANSITION,
    HISTORY_EVENT_CONFIG_STAGE_COUNT_SAVED,
    HISTORY_EVENT_CONFIG_STAGE_SAVED,
    HISTORY_EVENT_CONFIG_IMPORTED,
    HISTORY_EVENT_MANUAL_CLEAR,
    HISTORY_EVENT_MANUAL_SET,
    logger,
)
from .formatters import (
    build_auto_clear_notification,
    build_auto_hold_notification,
    build_auto_transition_notification,
    build_config_change_notification,
    build_manual_clear_notification,
    build_manual_set_notification,
    truncate_notification_message,
)
from .models import GuildStatusNotificationConfig
from .service_common import ServiceContext, actor_user_id, resolve_actor_display, resolve_member_display


def notification_enabled(
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
    if event_type in {
        HISTORY_EVENT_CONFIG_STAGE_COUNT_SAVED,
        HISTORY_EVENT_CONFIG_STAGE_SAVED,
        HISTORY_EVENT_CONFIG_IMPORTED,
    }:
        return config.notify_config_change
    return False


async def _send_notification_content(
    guild: discord.Guild,
    channel_id: int,
    content: str,
) -> None:
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
        await channel.send(truncate_notification_message(content))
    except discord.HTTPException:
        logger.exception(
            "Failed to send notification: guild=%s channel=%s",
            guild.id,
            channel_id,
        )


async def send_status_notification(
    context: ServiceContext,
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
    notification_config = context.store.get_status_notification_config(guild_id)
    if notification_config.channel_id is None:
        return
    if not notification_enabled(notification_config, event_type):
        return

    guild = context.bot.get_guild(guild_id)
    if guild is None:
        logger.warning("Guild not found for notification: %s", guild_id)
        return

    actor_display = resolve_actor_display(guild, actor_user_id(actor))
    member_display = resolve_member_display(guild, user_id) if user_id is not None else ""

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
    elif event_type in {
        HISTORY_EVENT_CONFIG_STAGE_COUNT_SAVED,
        HISTORY_EVENT_CONFIG_STAGE_SAVED,
        HISTORY_EVENT_CONFIG_IMPORTED,
    }:
        content = build_config_change_notification(
            detail,
            actor_display=actor_display,
            refreshed=0 if refreshed is None else refreshed,
            failed=0 if failed is None else failed,
        )
    else:
        return

    await _send_notification_content(guild, notification_config.channel_id, content)
