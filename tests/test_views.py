import os
import tempfile
import unittest

from status_bot.models import StatusStageConfig
from status_bot.store import StatusStore
from status_bot.validation import days_to_seconds
from status_bot.views import StageSetupView


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id

    def get_role(self, role_id: int):
        return None


class FakeBot:
    def __init__(self, store: StatusStore, guild: FakeGuild) -> None:
        self.store = store
        self._guild = guild

    def get_guild(self, guild_id: int):
        if guild_id == self._guild.id:
            return self._guild
        return None


class ViewRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.path = path
        self.store = StatusStore(path)
        self.guild = FakeGuild(1)
        self.bot = FakeBot(self.store, self.guild)

    async def asyncTearDown(self) -> None:
        self.store.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    async def test_stage_setup_view_discards_stale_draft_when_stage_count_shrinks(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), "clear"))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "現行", 22, days_to_seconds(2), "next"))
        self.store.commit()

        stale_draft = StatusStageConfig(3, "旧下書き", 33, days_to_seconds(3), "hold")
        view = StageSetupView(self.bot, 1, self.guild, 3, draft_stage=stale_draft)

        current = view.current_stage_config()
        self.assertEqual(view.stage_index, 2)
        self.assertEqual(current.stage_index, 2)
        self.assertEqual(current.label, "現行")
        self.assertEqual(current.role_id, 22)
        self.assertIn("下書きは復元せず", view.notice or "")


if __name__ == "__main__":
    unittest.main()
