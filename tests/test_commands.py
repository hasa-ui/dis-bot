import unittest
from types import SimpleNamespace
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
