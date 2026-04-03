import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from status_bot.models import GuildStatusNotificationConfig
from status_bot.models import StatusHistoryEntry

from status_bot.commands import register_commands


class FakeTree:
    def __init__(self) -> None:
        self.commands = {}
        self.error_handler = None

    def command(self, *, name: str, description: str):
        def decorator(func):
            self.commands[name] = func
            return func

        return decorator

    def error(self, func):
        self.error_handler = func
        return func


class FakeBot:
    def __init__(self) -> None:
        self.tree = FakeTree()
        self.service = SimpleNamespace()
        self.store = SimpleNamespace()


class FakeResponse:
    def __init__(self) -> None:
        self.messages = []
        self.deferred = False

    async def send_message(self, content: str, ephemeral: bool = False, view=None) -> None:
        self.messages.append((content, ephemeral, view))

    async def defer(self, ephemeral: bool = False) -> None:
        self.deferred = True


class FakeInteraction:
    def __init__(self, *, guild, user) -> None:
        self.guild = guild
        self.user = user
        self.response = FakeResponse()
        self.edits = []
        self._original_response = object()

    async def edit_original_response(self, *, content: str, view=None) -> None:
        self.edits.append((content, view))

    async def original_response(self):
        return self._original_response


class FakeChannel:
    def __init__(self, channel_id: int, *, can_send: bool = True) -> None:
        self.id = channel_id
        self._can_send = can_send

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=self._can_send, send_messages=self._can_send)


class FakeGuild:
    def __init__(self, guild_id: int, *, me=None) -> None:
        self.id = guild_id
        self.me = me if me is not None else object()


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_list_defers_without_manage_roles(self) -> None:
        bot = FakeBot()
        bot.service.list_guild_status_records = AsyncMock(return_value=[])
        register_commands(bot)
        command = bot.tree.commands["status_list"]

        interaction = FakeInteraction(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=10),
        )

        await command(interaction)

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.edits, [("このサーバーに有効なステータス状態はありません。", None)])

    async def test_status_history_returns_empty_message_when_no_history(self) -> None:
        bot = FakeBot()
        bot.service.list_member_status_history = AsyncMock(return_value=[])
        register_commands(bot)
        command = bot.tree.commands["status_history"]

        member = SimpleNamespace(id=20, mention="<@20>")
        interaction = FakeInteraction(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=10),
        )

        await command(interaction, member)

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.edits, [("<@20> のステータス履歴はありません。", None)])

    async def test_status_history_renders_view_when_entries_exist(self) -> None:
        bot = FakeBot()
        bot.service.list_member_status_history = AsyncMock(
            return_value=[
                StatusHistoryEntry(
                    created_at=12345,
                    event_type="manual_set",
                    actor_display="<@99>",
                    from_stage_name=None,
                    to_stage_name="段階2",
                    reason="確認",
                    detail="",
                )
            ]
        )
        register_commands(bot)
        command = bot.tree.commands["status_history"]

        member = SimpleNamespace(id=20, mention="<@20>")
        interaction = FakeInteraction(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=10),
        )

        await command(interaction, member)

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(len(interaction.edits), 1)
        self.assertIn("<@20> のステータス履歴", interaction.edits[0][0])
        self.assertIsNotNone(interaction.edits[0][1])

    async def test_status_notify_config_returns_current_settings_without_updates(self) -> None:
        bot = FakeBot()
        bot.store.get_status_notification_config = Mock(
            return_value=GuildStatusNotificationConfig(1, 123, True, False, True, False, True)
        )
        register_commands(bot)
        command = bot.tree.commands["status_notify_config"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10),
        )

        with patch("status_bot.commands.has_manage_guild", return_value=True):
            await command(interaction)

        self.assertEqual(len(interaction.response.messages), 1)
        self.assertIn("現在の通知設定", interaction.response.messages[0][0])
        self.assertIn("<#123>", interaction.response.messages[0][0])

    async def test_status_notify_config_rejects_without_manage_guild(self) -> None:
        bot = FakeBot()
        bot.store.get_status_notification_config = Mock(
            return_value=GuildStatusNotificationConfig(1, None, False, False, False, False, False)
        )
        register_commands(bot)
        command = bot.tree.commands["status_notify_config"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10),
        )

        with patch("status_bot.commands.has_manage_guild", return_value=False):
            await command(interaction)

        self.assertEqual(
            interaction.response.messages,
            [("Manage Server 権限か管理者権限が必要です。", True, None)],
        )

    async def test_status_notify_config_rejects_enabling_without_channel(self) -> None:
        bot = FakeBot()
        bot.store.get_status_notification_config = Mock(
            return_value=GuildStatusNotificationConfig(1, None, False, False, False, False, False)
        )
        bot.store.upsert_status_notification_config = Mock()
        bot.store.commit = Mock()
        register_commands(bot)
        command = bot.tree.commands["status_notify_config"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10),
        )

        with patch("status_bot.commands.has_manage_guild", return_value=True):
            await command(interaction, manual_set=True)

        self.assertIn("通知先テキストチャンネルを指定してください。", interaction.response.messages[0][0])
        bot.store.upsert_status_notification_config.assert_not_called()

    async def test_status_notify_config_saves_partial_update(self) -> None:
        bot = FakeBot()
        bot.store.get_status_notification_config = Mock(
            return_value=GuildStatusNotificationConfig(1, 123, False, False, False, False, False)
        )
        bot.store.upsert_status_notification_config = Mock()
        bot.store.commit = Mock()
        register_commands(bot)
        command = bot.tree.commands["status_notify_config"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10),
        )

        with patch("status_bot.commands.has_manage_guild", return_value=True):
            await command(interaction, manual_set=True)

        saved = bot.store.upsert_status_notification_config.call_args.args[0]
        self.assertEqual(saved.channel_id, 123)
        self.assertTrue(saved.notify_manual_set)
        self.assertFalse(saved.notify_manual_clear)
        bot.store.commit.assert_called_once()
        self.assertIn("通知設定を保存しました。", interaction.response.messages[0][0])

    async def test_status_notify_config_disable_all_rejects_other_updates(self) -> None:
        bot = FakeBot()
        bot.store.get_status_notification_config = Mock(
            return_value=GuildStatusNotificationConfig(1, 123, True, True, True, True, True)
        )
        register_commands(bot)
        command = bot.tree.commands["status_notify_config"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10),
        )

        with patch("status_bot.commands.has_manage_guild", return_value=True):
            await command(interaction, channel=FakeChannel(123), disable_all=True)

        self.assertIn("`disable_all`", interaction.response.messages[0][0])

if __name__ == "__main__":
    unittest.main()
