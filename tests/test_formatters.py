import unittest
from types import SimpleNamespace

from status_bot.config import ACTION_CLEAR, ACTION_NEXT
from status_bot.formatters import build_status_config_message
from status_bot.models import GuildStatusConfig, StatusStageConfig
from status_bot.validation import days_to_seconds


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id
        self.mention = f"<@&{role_id}>"


class FakeGuild:
    def __init__(self) -> None:
        self._roles = {
            11: FakeRole(11),
            22: FakeRole(22),
        }

    def get_role(self, role_id: int):
        return self._roles.get(role_id)


class FormatterTests(unittest.TestCase):
    def test_build_status_config_message_contains_stage_summary(self) -> None:
        guild = FakeGuild()
        config = GuildStatusConfig(
            guild_id=1,
            stage_count=2,
            stages=[
                StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR),
                StatusStageConfig(2, "警告", 22, days_to_seconds(2), ACTION_NEXT),
            ],
        )

        message = build_status_config_message(guild, config)
        self.assertIn("現在のステータス設定", message)
        self.assertIn("段階2（警告）", message)
        self.assertIn("<@&22>", message)


if __name__ == "__main__":
    unittest.main()
