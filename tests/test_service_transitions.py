import os
import tempfile
import unittest
from types import SimpleNamespace

from status_bot.config import ACTION_CLEAR, ACTION_HOLD, ACTION_NEXT
from status_bot.models import StatusStageConfig
from status_bot.service import StatusService
from status_bot.store import StatusStore
from status_bot.validation import days_to_seconds, get_stage, now_ts


class FakeBot:
    def get_guild(self, guild_id: int):
        return None


class ServiceTransitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.path = path
        self.store = StatusStore(path)
        self.service = StatusService(FakeBot(), self.store)

    async def asyncTearDown(self) -> None:
        self.store.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    async def test_hold_transition_sets_expires_at_to_none(self) -> None:
        self.store.set_stage_count_value(1, 1)
        self.store.ensure_stage_rows(1, 1)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_HOLD))
        self.store.upsert_status_record(1, 100, 1, now_ts() - 1, "hold")
        self.store.commit()

        await self.service.reconcile_record(self.store.get_status_record(1, 100))
        row = self.store.get_status_record(1, 100)
        self.assertIsNotNone(row)
        self.assertEqual(row["stage_index"], 1)
        self.assertIsNone(row["expires_at"])

    async def test_stage_count_shrink_retimes_clamped_records(self) -> None:
        self.store.set_stage_count_value(1, 5)
        self.store.ensure_stage_rows(1, 5)
        for idx, role_id in ((1, 11), (2, 22), (3, 33), (4, 44), (5, 55)):
            action = ACTION_CLEAR if idx == 1 else ACTION_NEXT
            self.store.upsert_status_stage(1, StatusStageConfig(idx, "", role_id, days_to_seconds(idx), action))
        self.store.upsert_status_record(1, 200, 5, now_ts() + days_to_seconds(9), "shrink")
        self.store.commit()

        await self.service.save_stage_count_settings(1, 3)
        row = self.store.get_status_record(1, 200)
        self.assertIsNotNone(row)
        self.assertEqual(row["stage_index"], 3)

        config = self.store.get_status_config(1)
        target_stage = get_stage(config, 3)
        self.assertIsNotNone(target_stage)
        expected = now_ts() + target_stage.duration_seconds
        self.assertIsNotNone(row["expires_at"])
        self.assertLessEqual(abs(row["expires_at"] - expected), 2)

    async def test_assign_status_allows_self_contained_stage(self) -> None:
        self.store.set_stage_count_value(1, 4)
        self.store.ensure_stage_rows(1, 4)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", None, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", None, days_to_seconds(2), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(3, "", None, days_to_seconds(3), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(4, "", 44, days_to_seconds(4), ACTION_HOLD))
        self.store.commit()

        member = SimpleNamespace(id=300)
        row = await self.service.assign_status(1, member, 4, "reason", "tester")
        self.assertIsNotNone(row)
        self.assertEqual(row["stage_index"], 4)


if __name__ == "__main__":
    unittest.main()
