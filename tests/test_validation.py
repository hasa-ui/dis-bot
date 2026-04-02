import unittest

from status_bot.config import ACTION_CLEAR, ACTION_HOLD
from status_bot.models import GuildStatusConfig, StatusStageConfig
from status_bot.validation import days_to_seconds, parse_stage_count, stage_path_is_ready


class ValidationTests(unittest.TestCase):
    def test_stage_path_ready_for_self_contained_hold_stage(self) -> None:
        config = GuildStatusConfig(
            guild_id=1,
            stage_count=4,
            stages=[
                StatusStageConfig(1, "", None, days_to_seconds(1), ACTION_CLEAR),
                StatusStageConfig(2, "", None, days_to_seconds(2), ACTION_CLEAR),
                StatusStageConfig(3, "", None, days_to_seconds(3), ACTION_CLEAR),
                StatusStageConfig(4, "", 44, days_to_seconds(4), ACTION_HOLD),
            ],
        )

        self.assertTrue(stage_path_is_ready(config, 4))
        self.assertFalse(stage_path_is_ready(config, 3))

    def test_parse_stage_count_rejects_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            parse_stage_count("11")


if __name__ == "__main__":
    unittest.main()
