import os
import tempfile
import unittest

from status_bot.config import ACTION_CLEAR, ACTION_HOLD
from status_bot.models import StatusStageConfig
from status_bot.service_common import ServiceContext
from status_bot.service_queries import predict_reconciled_record
from status_bot.store import StatusStore
from status_bot.validation import days_to_seconds, now_ts


class FakeBot:
    def get_guild(self, guild_id: int):
        return None


class ServiceQueriesTests(unittest.TestCase):
    def setUp(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.path = path
        self.store = StatusStore(path)
        self.context = ServiceContext(bot=FakeBot(), store=self.store)

    def tearDown(self) -> None:
        self.store.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_predict_reconciled_record_returns_none_for_clear(self) -> None:
        self.store.set_stage_count_value(1, 1)
        self.store.ensure_stage_rows(1, 1)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_record(1, 10, 1, now_ts() - 1, "expired")
        self.store.commit()

        config = self.store.get_status_config(1)
        row = self.store.get_status_record(1, 10)

        projected = predict_reconciled_record(config, row, current_ts=now_ts())

        self.assertIsNone(projected)

    def test_predict_reconciled_record_converts_hold_to_no_expiry(self) -> None:
        self.store.set_stage_count_value(1, 1)
        self.store.ensure_stage_rows(1, 1)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_HOLD))
        self.store.upsert_status_record(1, 10, 1, now_ts() - 1, "expired")
        self.store.commit()

        config = self.store.get_status_config(1)
        row = self.store.get_status_record(1, 10)

        projected = predict_reconciled_record(config, row, current_ts=now_ts())

        self.assertIsNotNone(projected)
        self.assertEqual(projected["stage_index"], 1)
        self.assertIsNone(projected["expires_at"])


if __name__ == "__main__":
    unittest.main()
