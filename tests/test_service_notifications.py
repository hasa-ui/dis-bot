import os
import tempfile
import unittest
from types import SimpleNamespace

from status_bot.config import HISTORY_EVENT_AUTO_CLEAR, HISTORY_EVENT_MANUAL_SET
from status_bot.models import GuildStatusNotificationConfig
from status_bot.service_common import ServiceContext
from status_bot.service_notifications import notification_enabled, send_status_notification
from status_bot.store import StatusStore


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.messages: list[str] = []

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=True, send_messages=True)

    async def send(self, content: str) -> None:
        self.messages.append(content)


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id
        self.me = SimpleNamespace(id=999)
        self.channel = FakeChannel(900)

    def get_channel(self, channel_id: int):
        return self.channel if channel_id == 900 else None

    def get_member(self, user_id: int):
        return SimpleNamespace(mention=f"<@{user_id}>")


class FakeBot:
    def __init__(self, guild: FakeGuild) -> None:
        self.guild = guild

    def get_guild(self, guild_id: int):
        return self.guild if self.guild.id == guild_id else None


class ServiceNotificationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.path = path
        self.store = StatusStore(path)
        self.guild = FakeGuild(1)
        self.context = ServiceContext(bot=FakeBot(self.guild), store=self.store)

    async def asyncTearDown(self) -> None:
        self.store.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    async def test_notification_enabled_maps_auto_clear_to_auto_transition_toggle(self) -> None:
        config = GuildStatusNotificationConfig(
            guild_id=1,
            channel_id=900,
            notify_manual_set=False,
            notify_manual_clear=False,
            notify_auto_transition=True,
            notify_auto_hold=False,
            notify_config_change=False,
        )

        self.assertTrue(notification_enabled(config, HISTORY_EVENT_AUTO_CLEAR))

    async def test_send_status_notification_truncates_to_discord_limit(self) -> None:
        self.store.upsert_status_notification_config(
            GuildStatusNotificationConfig(
                guild_id=1,
                channel_id=900,
                notify_manual_set=True,
                notify_manual_clear=False,
                notify_auto_transition=False,
                notify_auto_hold=False,
                notify_config_change=False,
            )
        )
        self.store.commit()

        await send_status_notification(
            self.context,
            1,
            event_type=HISTORY_EVENT_MANUAL_SET,
            user_id=10,
            actor=SimpleNamespace(id=20),
            to_stage_name="段階1",
            next_change_text="1日後に 解除",
            reason="理" * 2500,
        )

        self.assertEqual(len(self.guild.channel.messages), 1)
        self.assertLessEqual(len(self.guild.channel.messages[0]), 2000)


if __name__ == "__main__":
    unittest.main()
