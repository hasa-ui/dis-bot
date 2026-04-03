import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from status_bot.config import ACTION_CLEAR, ACTION_HOLD, ACTION_NEXT
from status_bot.models import GuildStatusNotificationConfig, StatusListEntry, StatusStageConfig
from status_bot.service import StatusService
from status_bot.store import StatusStore
from status_bot.validation import days_to_seconds, get_stage, now_ts


class FakeBot:
    def __init__(self, guild=None) -> None:
        self._guild = guild

    def get_guild(self, guild_id: int):
        if self._guild is not None and self._guild.id == guild_id:
            return self._guild
        return None


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id
        self.mention = f"<@&{role_id}>"


class FakeMember:
    def __init__(self, user_id: int, roles: tuple[FakeRole, ...] = ()) -> None:
        self.id = user_id
        self.roles = list(roles)
        self.mention = f"<@{user_id}>"

    async def edit(self, *, roles, reason: str) -> None:
        self.roles = list(roles)


class FakeChannel:
    def __init__(self, channel_id: int, *, can_send: bool = True) -> None:
        self.id = channel_id
        self._can_send = can_send
        self.messages: list[str] = []

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=self._can_send, send_messages=self._can_send)

    async def send(self, content: str) -> None:
        self.messages.append(content)


class FakeGuild:
    def __init__(
        self,
        guild_id: int,
        role_ids: tuple[int, ...],
        *,
        channel_ids: tuple[int, ...] = (),
        member_ids: tuple[int, ...] = (),
    ) -> None:
        self.id = guild_id
        self.me = SimpleNamespace(id=999)
        self._roles = {role_id: FakeRole(role_id) for role_id in role_ids}
        self._channels = {channel_id: FakeChannel(channel_id) for channel_id in channel_ids}
        self._members = {member_id: FakeMember(member_id) for member_id in member_ids}

    def get_role(self, role_id: int):
        return self._roles.get(role_id)

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)

    def get_member(self, user_id: int):
        return self._members.get(user_id)


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

    def _configure_notifications(
        self,
        guild: FakeGuild,
        *,
        notify_manual_set: bool = False,
        notify_manual_clear: bool = False,
        notify_auto_transition: bool = False,
        notify_auto_hold: bool = False,
        notify_config_change: bool = False,
    ) -> FakeChannel:
        channel = guild.get_channel(900)
        self.assertIsNotNone(channel)
        self.service = StatusService(FakeBot(guild), self.store)
        self.service.apply_status_role = AsyncMock()
        self.store.upsert_status_notification_config(
            GuildStatusNotificationConfig(
                guild_id=guild.id,
                channel_id=900,
                notify_manual_set=notify_manual_set,
                notify_manual_clear=notify_manual_clear,
                notify_auto_transition=notify_auto_transition,
                notify_auto_hold=notify_auto_hold,
                notify_config_change=notify_config_change,
            )
        )
        self.store.commit()
        return channel

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
        history = self.store.get_status_history_for_member(1, 100)
        self.assertEqual(history[0]["event_type"], "auto_hold")

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
        history = self.store.get_status_history_for_member(1, 300)
        self.assertEqual(history[0]["event_type"], "manual_set")
        self.assertEqual(history[0]["reason"], "reason")

    async def test_assign_status_sends_manual_set_notification_when_enabled(self) -> None:
        self.store.set_stage_count_value(1, 1)
        self.store.ensure_stage_rows(1, 1)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "警告", 11, days_to_seconds(1), ACTION_HOLD))
        self.store.commit()

        guild = FakeGuild(1, (11,), channel_ids=(900,), member_ids=(300,))
        channel = self._configure_notifications(guild, notify_manual_set=True)

        await self.service.assign_status(1, SimpleNamespace(id=300), 1, "reason", SimpleNamespace(id=77))

        self.assertEqual(len(channel.messages), 1)
        self.assertIn("手動付与", channel.messages[0])
        self.assertIn("<@300>", channel.messages[0])
        self.assertIn("<@77>", channel.messages[0])

    async def test_clear_status_records_manual_clear_history(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.upsert_status_record(1, 301, 2, now_ts() + 60, "manual-clear")
        self.store.commit()

        member = SimpleNamespace(id=301)
        await self.service.clear_status(1, member, "tester")

        history = self.store.get_status_history_for_member(1, 301)
        self.assertEqual(history[0]["event_type"], "manual_clear")
        self.assertEqual(history[0]["from_stage_index"], 2)
        self.assertEqual(history[0]["from_stage_name"], "段階2")
        self.assertIsNone(history[0]["to_stage_index"])

    async def test_clear_status_without_target_does_not_send_notification(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.commit()

        guild = FakeGuild(1, (11, 22), channel_ids=(900,), member_ids=(401,))
        channel = self._configure_notifications(guild, notify_manual_clear=True)

        await self.service.clear_status(1, FakeMember(401), "tester")

        self.assertEqual(channel.messages, [])

    async def test_reconcile_transition_records_auto_transition_history(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.upsert_status_record(1, 302, 2, now_ts() - 1, "expired")
        self.store.commit()

        await self.service.reconcile_record(self.store.get_status_record(1, 302))

        history = self.store.get_status_history_for_member(1, 302)
        self.assertEqual(history[0]["event_type"], "auto_transition")
        self.assertEqual(history[0]["from_stage_index"], 2)
        self.assertEqual(history[0]["from_stage_name"], "段階2")
        self.assertEqual(history[0]["to_stage_index"], 1)
        self.assertEqual(history[0]["to_stage_name"], "段階1")

    async def test_reconcile_transition_sends_auto_transition_notification(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.upsert_status_record(1, 302, 2, now_ts() - 1, "expired")
        self.store.commit()

        guild = FakeGuild(1, (11, 22), channel_ids=(900,), member_ids=(302,))
        channel = self._configure_notifications(guild, notify_auto_transition=True)

        await self.service.reconcile_record(self.store.get_status_record(1, 302))

        self.assertEqual(len(channel.messages), 1)
        self.assertIn("自動遷移", channel.messages[0])
        self.assertIn("段階2 -> 段階1", channel.messages[0])

    async def test_hold_transition_sends_auto_hold_notification(self) -> None:
        self.store.set_stage_count_value(1, 1)
        self.store.ensure_stage_rows(1, 1)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "維持", 11, days_to_seconds(1), ACTION_HOLD))
        self.store.upsert_status_record(1, 100, 1, now_ts() - 1, "hold")
        self.store.commit()

        guild = FakeGuild(1, (11,), channel_ids=(900,), member_ids=(100,))
        channel = self._configure_notifications(guild, notify_auto_hold=True)

        await self.service.reconcile_record(self.store.get_status_record(1, 100))

        self.assertEqual(len(channel.messages), 1)
        self.assertIn("自動維持", channel.messages[0])
        self.assertIn("段階1（維持）", channel.messages[0])

    async def test_auto_clear_uses_auto_transition_toggle_for_notification(self) -> None:
        self.store.set_stage_count_value(1, 1)
        self.store.ensure_stage_rows(1, 1)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_record(1, 303, 1, now_ts() - 1, "expired")
        self.store.commit()

        guild = FakeGuild(1, (11,), channel_ids=(900,), member_ids=(303,))
        channel = self._configure_notifications(guild, notify_auto_transition=True)

        await self.service.reconcile_record(self.store.get_status_record(1, 303))

        self.assertEqual(len(channel.messages), 1)
        self.assertIn("自動解除", channel.messages[0])
        self.assertIn("<@303>", channel.messages[0])

    async def test_status_history_uses_snapshot_stage_names_after_stage_rename(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "旧名", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.upsert_status_record(1, 302, 2, now_ts() - 1, "expired")
        self.store.commit()

        guild = FakeGuild(1, (11, 22))
        guild.get_member = lambda user_id: SimpleNamespace(mention=f"<@{user_id}>")
        await self.service.reconcile_record(self.store.get_status_record(1, 302))

        self.store.upsert_status_stage(1, StatusStageConfig(1, "更新後", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "新名", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.commit()

        entries = await self.service.list_member_status_history(guild, 302)
        self.assertEqual(entries[0].from_stage_name, "段階2（旧名）")
        self.assertEqual(entries[0].to_stage_name, "段階1")

    async def test_clear_status_without_active_record_skips_manual_clear_history(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.commit()

        member = FakeMember(401)
        await self.service.clear_status(1, member, "tester")

        self.assertEqual(self.store.get_status_history_for_member(1, 401), [])

    async def test_clear_status_records_manual_clear_history_for_stale_role_cleanup(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "警告", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.commit()

        member = FakeMember(402, (FakeRole(22),))
        await self.service.clear_status(1, member, "tester")

        history = self.store.get_status_history_for_member(1, 402)
        self.assertEqual(history[0]["event_type"], "manual_clear")
        self.assertEqual(history[0]["from_stage_index"], 2)
        self.assertEqual(history[0]["from_stage_name"], "段階2（警告）")

    async def test_save_stage_settings_records_config_history(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.commit()

        actor = SimpleNamespace(id=77)
        await self.service.save_stage_settings(1, StatusStageConfig(2, "更新", 22, days_to_seconds(3), ACTION_NEXT), actor)

        row = self.store.db.execute(
            """
            SELECT event_type, user_id, actor_user_id
            FROM status_history_records
            WHERE guild_id = ? AND event_type = 'config_stage_saved'
            ORDER BY id DESC
            LIMIT 1
            """,
            (1,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["user_id"])
        self.assertEqual(row["actor_user_id"], 77)

    async def test_save_stage_settings_sends_config_change_notification(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.commit()

        guild = FakeGuild(1, (11, 22), channel_ids=(900,), member_ids=(77,))
        channel = self._configure_notifications(guild, notify_config_change=True)

        await self.service.save_stage_settings(
            1,
            StatusStageConfig(2, "更新", 22, days_to_seconds(3), ACTION_NEXT),
            SimpleNamespace(id=77),
        )

        self.assertEqual(len(channel.messages), 1)
        self.assertIn("設定変更", channel.messages[0])
        self.assertIn("0件中 0件失敗", channel.messages[0])
        self.assertIn("<@77>", channel.messages[0])

    async def test_preview_stage_count_settings_reports_reapply_and_clamp_counts(self) -> None:
        self.store.set_stage_count_value(1, 4)
        self.store.ensure_stage_rows(1, 4)
        for idx, role_id in ((1, 11), (2, 22), (3, 33), (4, 44)):
            action = ACTION_CLEAR if idx == 1 else ACTION_NEXT
            self.store.upsert_status_stage(1, StatusStageConfig(idx, "", role_id, days_to_seconds(idx), action))
        self.store.upsert_status_record(1, 10, 4, now_ts() + 10, "a")
        self.store.upsert_status_record(1, 20, 2, now_ts() + 10, "b")
        self.store.commit()

        summary = self.service.preview_stage_count_settings(FakeGuild(1, (11, 22)), 3)
        self.assertEqual(summary.reapply_count, 2)
        self.assertEqual(summary.clamp_count, 1)
        self.assertEqual(summary.missing_role_count, 1)

    async def test_preview_stage_count_settings_rejects_incomplete_clamp_target(self) -> None:
        self.store.set_stage_count_value(1, 4)
        self.store.ensure_stage_rows(1, 4)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.upsert_status_stage(1, StatusStageConfig(3, "", None, days_to_seconds(3), ACTION_NEXT))
        self.store.upsert_status_stage(1, StatusStageConfig(4, "", 44, days_to_seconds(4), ACTION_NEXT))
        self.store.upsert_status_record(1, 10, 4, now_ts() + 10, "a")
        self.store.commit()

        with self.assertRaises(ValueError):
            self.service.preview_stage_count_settings(FakeGuild(1, (11, 22, 44)), 3)

    async def test_preview_stage_settings_counts_other_missing_roles(self) -> None:
        self.store.set_stage_count_value(1, 3)
        self.store.ensure_stage_rows(1, 3)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.upsert_status_stage(1, StatusStageConfig(3, "", 33, days_to_seconds(3), ACTION_NEXT))
        self.store.upsert_status_record(1, 10, 2, now_ts() + 10, "a")
        self.store.commit()

        summary = self.service.preview_stage_settings(
            FakeGuild(1, (11, 22)),
            StatusStageConfig(2, "更新", 22, days_to_seconds(5), ACTION_NEXT),
        )
        self.assertEqual(summary.reapply_count, 1)
        self.assertEqual(summary.clamp_count, 0)
        self.assertEqual(summary.missing_role_count, 1)

    async def test_preview_stage_settings_rejects_missing_selected_role(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.commit()

        with self.assertRaises(ValueError):
            self.service.preview_stage_settings(
                FakeGuild(1, (11,)),
                StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT),
            )

    async def test_preview_stage_count_settings_excludes_records_cleared_by_reconcile(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.upsert_status_record(1, 10, 1, now_ts() - 5, "expired")
        self.store.upsert_status_record(1, 20, 2, now_ts() + 60, "active")
        self.store.commit()

        summary = self.service.preview_stage_count_settings(FakeGuild(1, (11, 22)), 2)
        self.assertEqual(summary.reapply_count, 1)

    async def test_preview_stage_settings_excludes_records_cleared_by_reconcile(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.upsert_status_record(1, 10, 1, now_ts() - 5, "expired")
        self.store.upsert_status_record(1, 20, 2, now_ts() + 60, "active")
        self.store.commit()

        summary = self.service.preview_stage_settings(
            FakeGuild(1, (11, 22)),
            StatusStageConfig(2, "更新", 22, days_to_seconds(3), ACTION_NEXT),
        )
        self.assertEqual(summary.reapply_count, 1)

    async def test_list_guild_status_records_sorts_expiring_before_hold(self) -> None:
        self.store.set_stage_count_value(1, 3)
        self.store.ensure_stage_rows(1, 3)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "警告", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.upsert_status_stage(1, StatusStageConfig(3, "", 33, days_to_seconds(3), ACTION_HOLD))
        self.store.upsert_status_record(1, 10, 2, now_ts() + 60, "soon")
        self.store.upsert_status_record(1, 20, 3, None, "hold")
        self.store.upsert_status_record(1, 30, 2, now_ts() + 600, "")
        self.store.commit()

        guild = FakeGuild(1, (11, 22, 33))
        guild.get_member = lambda user_id: SimpleNamespace(mention=f"<@{user_id}>")
        entries = await self.service.list_guild_status_records(guild)

        self.assertEqual([entry.user_id for entry in entries], [10, 30, 20])
        self.assertEqual(entries[0].stage_name, "段階2（警告）")
        self.assertEqual(entries[1].reason, "")
        self.assertEqual(entries[2].next_change_text, "なし（現在の段階を維持中）")

    async def test_list_guild_status_records_excludes_rows_removed_by_reconcile(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.upsert_status_record(1, 10, 1, now_ts() - 5, "expired")
        self.store.upsert_status_record(1, 20, 2, now_ts() + 60, "active")
        self.store.commit()

        guild = FakeGuild(1, (11, 22))
        guild.get_member = lambda user_id: None
        entries = await self.service.list_guild_status_records(guild)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].member_display, "<@20>")

    async def test_list_guild_status_records_does_not_mutate_overdue_rows(self) -> None:
        self.store.set_stage_count_value(1, 2)
        self.store.ensure_stage_rows(1, 2)
        self.store.upsert_status_stage(1, StatusStageConfig(1, "", 11, days_to_seconds(1), ACTION_CLEAR))
        self.store.upsert_status_stage(1, StatusStageConfig(2, "", 22, days_to_seconds(2), ACTION_NEXT))
        self.store.upsert_status_record(1, 10, 1, now_ts() - 5, "expired")
        self.store.commit()

        guild = FakeGuild(1, (11, 22))
        guild.get_member = lambda user_id: None
        entries = await self.service.list_guild_status_records(guild)

        self.assertEqual(entries, [])
        self.assertIsNotNone(self.store.get_status_record(1, 10))
        self.assertEqual(self.store.get_status_history_for_member(1, 10), [])


if __name__ == "__main__":
    unittest.main()
