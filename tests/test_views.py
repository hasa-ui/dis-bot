import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from status_bot.models import StatusListEntry, StatusStageConfig
from status_bot.store import StatusStore
from status_bot.validation import days_to_seconds
from status_bot.views import StageSetupView, StatusListView


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id

    def get_role(self, role_id: int):
        return None

    def get_member(self, user_id: int):
        return SimpleNamespace(mention=f"<@{user_id}>")


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

    async def test_status_list_view_updates_button_state_by_page(self) -> None:
        entries = [
            StatusListEntry(i, f"<@{i}>", 1, "段階1（" + ("警告" * 10) + "）", "1日後に 解除", "確認メモ" * 10, i)
            for i in range(3)
        ]
        view = StatusListView(1, entries, max_length=220)

        self.assertGreater(view.page_count, 1)
        self.assertTrue(view.previous_page.disabled)
        self.assertFalse(view.next_page.disabled)
        self.assertIn(f"ページ: 1/{view.page_count}", view.render_content())

        view.page_index = 1
        view._sync_buttons()
        self.assertFalse(view.previous_page.disabled)
        if view.page_count == 2:
            self.assertTrue(view.next_page.disabled)
        else:
            self.assertFalse(view.next_page.disabled)
        self.assertIn(f"ページ: 2/{view.page_count}", view.render_content())

    async def test_status_list_view_rejects_other_user(self) -> None:
        entries = [StatusListEntry(1, "<@1>", 1, "段階1", "1日後に 解除", "", 1)]
        view = StatusListView(1, entries)

        class FakeResponse:
            def __init__(self) -> None:
                self.called = False

            async def send_message(self, content: str, ephemeral: bool = False) -> None:
                self.called = True

        interaction = SimpleNamespace(user=SimpleNamespace(id=2), response=FakeResponse())
        allowed = await view.interaction_check(interaction)

        self.assertFalse(allowed)
        self.assertTrue(interaction.response.called)

    async def test_status_list_view_rechecks_manage_roles(self) -> None:
        entries = [StatusListEntry(1, "<@1>", 1, "段階1", "1日後に 解除", "", 1)]
        view = StatusListView(1, entries)

        class FakeResponse:
            def __init__(self) -> None:
                self.messages: list[tuple[str, bool]] = []

            async def send_message(self, content: str, ephemeral: bool = False) -> None:
                self.messages.append((content, ephemeral))

        interaction = SimpleNamespace(user=SimpleNamespace(id=1), response=FakeResponse(), guild=object())
        with patch("status_bot.views.has_manage_roles", return_value=False):
            allowed = await view.interaction_check(interaction)

        self.assertFalse(allowed)
        self.assertEqual(interaction.response.messages, [("Manage Roles 権限が必要です。", True)])


if __name__ == "__main__":
    unittest.main()
