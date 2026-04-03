import unittest
from types import SimpleNamespace

from status_bot.config import ACTION_CLEAR, ACTION_NEXT
from status_bot.formatters import (
    build_stage_count_preview_message,
    build_stage_save_preview_message,
    build_status_config_message,
)
from status_bot.models import GuildStatusConfig, SetupPreviewSummary, StatusStageConfig
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

    def test_build_stage_count_preview_message_contains_summary(self) -> None:
        message = build_stage_count_preview_message(
            4,
            3,
            SetupPreviewSummary(reapply_count=5, clamp_count=2, missing_role_count=1),
        )

        self.assertIn("段階数変更プレビュー", message)
        self.assertIn("現在: 4段階", message)
        self.assertIn("保存後: 3段階", message)
        self.assertIn("再適用対象: 5件", message)
        self.assertIn("丸め対象: 2件", message)
        self.assertIn("見つからないロール: 1件", message)

    def test_build_stage_save_preview_message_contains_draft(self) -> None:
        guild = FakeGuild()
        config = GuildStatusConfig(
            guild_id=1,
            stage_count=2,
            stages=[
                StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR),
                StatusStageConfig(2, "警告", 22, days_to_seconds(2), ACTION_NEXT),
            ],
        )

        message = build_stage_save_preview_message(
            guild,
            config.stages[1],
            StatusStageConfig(2, "再警告", 22, days_to_seconds(3), ACTION_NEXT),
            config,
            SetupPreviewSummary(reapply_count=4, clamp_count=0, missing_role_count=0),
        )

        self.assertIn("段階2（再警告） の保存前プレビュー", message)
        self.assertIn("保存後のロール: <@&22>", message)
        self.assertIn("保存後の期間: 3日", message)
        self.assertIn("保存後の満了時: 段階1へ移行", message)
        self.assertIn("再適用対象: 4件", message)


if __name__ == "__main__":
    unittest.main()
