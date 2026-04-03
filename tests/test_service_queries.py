import os
import tempfile
import unittest

from status_bot.config import ACTION_CLEAR, ACTION_HOLD
from status_bot.models import StatusConfigExportPayload, StatusConfigExportStage, StatusStageConfig
from status_bot.service_common import ServiceContext
from status_bot.service_queries import (
    parse_status_config_export_payload,
    predict_reconciled_record,
    serialize_status_config_export_payload,
)
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

    def test_parse_status_config_export_payload_accepts_bom(self) -> None:
        raw = serialize_status_config_export_payload(
            StatusConfigExportPayload(
                schema_version=1,
                source_guild_id=1,
                exported_at=123,
                stage_count=1,
                stages=[
                    StatusConfigExportStage(
                        stage_index=1,
                        label="警告",
                        role_id=11,
                        duration_seconds=86400,
                        on_expire_action=ACTION_HOLD,
                    )
                ],
            )
        )

        parsed = parse_status_config_export_payload("\ufeff" + raw)

        self.assertEqual(parsed.schema_version, 1)
        self.assertEqual(parsed.stage_count, 1)
        self.assertEqual(parsed.stages[0].label, "警告")


if __name__ == "__main__":
    unittest.main()
