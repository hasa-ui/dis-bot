import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        self.messages.append((content, ephemeral))

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


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_list_requires_manage_roles(self) -> None:
        bot = FakeBot()
        register_commands(bot)
        command = bot.tree.commands["status_list"]

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=10),
            response=FakeResponse(),
        )

        with patch("status_bot.commands.has_manage_roles", return_value=False):
            await command(interaction)

        self.assertEqual(
            interaction.response.messages,
            [("Manage Roles 権限が必要です。", True)],
        )
        self.assertFalse(interaction.response.deferred)

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


if __name__ == "__main__":
    unittest.main()
