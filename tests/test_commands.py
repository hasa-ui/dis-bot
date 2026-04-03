import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from status_bot.config import ACTION_HOLD
from status_bot.models import (
    BulkOperationResult,
    GuildStatusConfig,
    GuildStatusNotificationConfig,
    StatusConfigExportPayload,
    StatusConfigExportStage,
    StatusConfigImportPreview,
    StatusHistoryEntry,
    StatusStageConfig,
)

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
    def __init__(self, guild=None) -> None:
        self.tree = FakeTree()
        self.service = SimpleNamespace()
        self.store = SimpleNamespace()
        self._guild = guild

    def get_guild(self, guild_id: int):
        if self._guild is not None and self._guild.id == guild_id:
            return self._guild
        return None


class FakeResponse:
    def __init__(self) -> None:
        self.messages = []
        self.files = []
        self.deferred = False

    async def send_message(self, content: str, ephemeral: bool = False, view=None, file=None) -> None:
        self.messages.append((content, ephemeral, view))
        self.files.append(file)

    async def defer(self, ephemeral: bool = False) -> None:
        self.deferred = True


class FakeInteraction:
    def __init__(self, *, guild, user) -> None:
        self.guild = guild
        self.user = user
        self.response = FakeResponse()
        self.edits = []
        self._original_response = object()

    async def edit_original_response(self, *, content: str, view=None) -> None:
        self.edits.append((content, view))

    async def original_response(self):
        return self._original_response


class FakeChannel:
    def __init__(self, channel_id: int, *, can_send: bool = True) -> None:
        self.id = channel_id
        self._can_send = can_send

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=self._can_send, send_messages=self._can_send)


class FakeAttachment:
    def __init__(self, content: str) -> None:
        self._content = content

    async def read(self) -> bytes:
        return self._content.encode("utf-8")


class FakeBomAttachment:
    def __init__(self, content: str) -> None:
        self._content = content

    async def read(self) -> bytes:
        return ("\ufeff" + self._content).encode("utf-8")


class FakeGuild:
    def __init__(self, guild_id: int, *, me=None, role_ids=()) -> None:
        self.id = guild_id
        self.me = me if me is not None else object()
        self._roles = {role_id: SimpleNamespace(id=role_id, mention=f"<@&{role_id}>") for role_id in role_ids}

    def get_role(self, role_id: int):
        return self._roles.get(role_id)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_list_defers_without_manage_roles(self) -> None:
        bot = FakeBot()
        bot.service.list_guild_status_records = AsyncMock(return_value=[])
        register_commands(bot)
        command = bot.tree.commands["status_list"]

        interaction = FakeInteraction(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=10),
        )

        await command(interaction)

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.edits, [("このサーバーに有効なステータス状態はありません。", None)])

    async def test_status_history_returns_empty_message_when_no_history(self) -> None:
        bot = FakeBot()
        bot.service.list_member_status_history = AsyncMock(return_value=[])
        register_commands(bot)
        command = bot.tree.commands["status_history"]

        member = SimpleNamespace(id=20, mention="<@20>")
        interaction = FakeInteraction(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=10),
        )

        await command(interaction, member)

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.edits, [("<@20> のステータス履歴はありません。", None)])

    async def test_status_history_renders_view_when_entries_exist(self) -> None:
        bot = FakeBot()
        bot.service.list_member_status_history = AsyncMock(
            return_value=[
                StatusHistoryEntry(
                    created_at=12345,
                    event_type="manual_set",
                    actor_display="<@99>",
                    from_stage_name=None,
                    to_stage_name="段階2",
                    reason="確認",
                    detail="",
                )
            ]
        )
        register_commands(bot)
        command = bot.tree.commands["status_history"]

        member = SimpleNamespace(id=20, mention="<@20>")
        interaction = FakeInteraction(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=10),
        )

        await command(interaction, member)

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(len(interaction.edits), 1)
        self.assertIn("<@20> のステータス履歴", interaction.edits[0][0])
        self.assertIsNotNone(interaction.edits[0][1])

    async def test_status_notify_config_returns_current_settings_without_updates(self) -> None:
        bot = FakeBot()
        bot.store.get_status_notification_config = Mock(
            return_value=GuildStatusNotificationConfig(1, 123, True, False, True, False, True)
        )
        register_commands(bot)
        command = bot.tree.commands["status_notify_config"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10),
        )

        with patch("status_bot.commands.has_manage_guild", return_value=True):
            await command(interaction)

        self.assertEqual(len(interaction.response.messages), 1)
        self.assertIn("現在の通知設定", interaction.response.messages[0][0])
        self.assertIn("<#123>", interaction.response.messages[0][0])

    async def test_status_notify_config_rejects_without_manage_guild(self) -> None:
        bot = FakeBot()
        bot.store.get_status_notification_config = Mock(
            return_value=GuildStatusNotificationConfig(1, None, False, False, False, False, False)
        )
        register_commands(bot)
        command = bot.tree.commands["status_notify_config"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10),
        )

        with patch("status_bot.commands.has_manage_guild", return_value=False):
            await command(interaction)

        self.assertEqual(
            interaction.response.messages,
            [("Manage Server 権限か管理者権限が必要です。", True, None)],
        )

    async def test_status_notify_config_rejects_enabling_without_channel(self) -> None:
        bot = FakeBot()
        bot.store.get_status_notification_config = Mock(
            return_value=GuildStatusNotificationConfig(1, None, False, False, False, False, False)
        )
        bot.store.upsert_status_notification_config = Mock()
        bot.store.commit = Mock()
        register_commands(bot)
        command = bot.tree.commands["status_notify_config"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10),
        )

        with patch("status_bot.commands.has_manage_guild", return_value=True):
            await command(interaction, manual_set=True)

        self.assertIn("通知先テキストチャンネルを指定してください。", interaction.response.messages[0][0])
        bot.store.upsert_status_notification_config.assert_not_called()

    async def test_status_notify_config_saves_partial_update(self) -> None:
        bot = FakeBot()
        bot.store.get_status_notification_config = Mock(
            return_value=GuildStatusNotificationConfig(1, 123, False, False, False, False, False)
        )
        bot.store.upsert_status_notification_config = Mock()
        bot.store.commit = Mock()
        register_commands(bot)
        command = bot.tree.commands["status_notify_config"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10),
        )

        with patch("status_bot.commands.has_manage_guild", return_value=True):
            await command(interaction, manual_set=True)

        saved = bot.store.upsert_status_notification_config.call_args.args[0]
        self.assertEqual(saved.channel_id, 123)
        self.assertTrue(saved.notify_manual_set)
        self.assertFalse(saved.notify_manual_clear)
        bot.store.commit.assert_called_once()
        self.assertIn("通知設定を保存しました。", interaction.response.messages[0][0])

    async def test_status_export_returns_json_attachment(self) -> None:
        bot = FakeBot()
        bot.service.export_status_config = Mock(
            return_value=StatusConfigExportPayload(
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
        register_commands(bot)
        command = bot.tree.commands["status_export"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10),
        )

        with patch("status_bot.commands.has_manage_guild", return_value=True):
            await command(interaction)

        self.assertEqual(len(interaction.response.messages), 1)
        self.assertIn("ステータス設定エクスポート", interaction.response.messages[0][0])
        self.assertEqual(len(interaction.response.files), 1)
        file = interaction.response.files[0]
        self.assertIsNotNone(file)
        file.fp.seek(0)
        content = file.fp.read().decode("utf-8")
        self.assertIn('"schema_version": 1', content)
        self.assertIn('"stage_count": 1', content)
        self.assertIn('"label": "警告"', content)

    async def test_status_export_rejects_incomplete_settings(self) -> None:
        bot = FakeBot()
        bot.service.export_status_config = Mock(side_effect=RuntimeError("未完了"))
        register_commands(bot)
        command = bot.tree.commands["status_export"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10),
        )

        with patch("status_bot.commands.has_manage_guild", return_value=True):
            await command(interaction)

        self.assertEqual(interaction.response.messages, [("未完了", True, None)])
        self.assertEqual(interaction.response.files, [None])

    async def test_status_import_renders_preview(self) -> None:
        guild = FakeGuild(1, role_ids=(11,))
        bot = FakeBot(guild)
        payload = StatusConfigExportPayload(
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
        preview = StatusConfigImportPreview(
            source_guild_id=1,
            exported_at=123,
            current_stage_count=1,
            imported_config=GuildStatusConfig(
                guild_id=1,
                stage_count=1,
                stages=[StatusStageConfig(1, "警告", 11, 86400, ACTION_HOLD)],
            ),
            reapply_count=0,
            clamp_count=0,
            missing_role_count=0,
            diff_lines=["- 段階数: 1段階 -> 1段階"],
            warning_lines=[],
        )
        bot.service.parse_status_config_export_payload = Mock(return_value=payload)
        bot.service.preview_status_config_import = Mock(return_value=preview)
        register_commands(bot)
        command = bot.tree.commands["status_import"]

        interaction = FakeInteraction(
            guild=guild,
            user=SimpleNamespace(id=10),
        )

        with patch("status_bot.commands.has_manage_guild", return_value=True):
            await command(interaction, FakeAttachment("{}"))

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(len(interaction.edits), 1)
        self.assertIn("ステータス設定インポートプレビュー", interaction.edits[0][0])
        self.assertIsNotNone(interaction.edits[0][1])

    async def test_status_bulk_set_parses_attachment_and_skips_invalid_targets(self) -> None:
        bot = FakeBot()
        bot.store.get_status_config = Mock(
            return_value=SimpleNamespace(
                guild_id=1,
                stage_count=1,
                stages=[StatusStageConfig(1, "", 11, 86400, ACTION_HOLD)],
            )
        )
        bot.service.fetch_member_if_needed = AsyncMock(
            side_effect=[
                SimpleNamespace(id=100, mention="<@100>"),
                SimpleNamespace(id=200, mention="<@200>"),
            ]
        )
        bot.service.bulk_assign_status = AsyncMock(
            return_value=BulkOperationResult(
                processed_count=1,
                success_count=1,
                failure_count=0,
                detail_lines=["- <@100>: 成功"],
            )
        )
        register_commands(bot)
        command = bot.tree.commands["status_bulk_set"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10, guild_permissions=SimpleNamespace(manage_roles=True)),
        )

        with patch("status_bot.commands.has_manage_roles", return_value=True):
            with patch(
                "status_bot.commands.can_manage_target",
                side_effect=[
                    (True, ""),
                    (False, "Botより上位または同位のロールを持つ相手には変更できません。"),
                ],
            ):
                await command(
                    interaction,
                    FakeAttachment("100\n100\nabc\n<@200>\n"),
                    1,
                    "bulk reason",
                )

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(len(interaction.edits), 1)
        content = interaction.edits[0][0]
        self.assertIn("ステータス一括付与結果", content)
        self.assertIn("対象件数: 4件", content)
        self.assertIn("成功: 1件", content)
        self.assertIn("除外: 3件", content)
        bot.service.bulk_assign_status.assert_called_once()
        called_members = bot.service.bulk_assign_status.call_args.args[1]
        self.assertEqual([member.id for member in called_members], [100])

    async def test_status_bulk_clear_parses_attachment_and_reports_skips(self) -> None:
        bot = FakeBot()
        bot.store.get_status_config = Mock(return_value=SimpleNamespace())
        bot.service.fetch_member_if_needed = AsyncMock(
            return_value=SimpleNamespace(id=100, mention="<@100>")
        )
        bot.service.bulk_clear_status = AsyncMock(
            return_value=BulkOperationResult(
                processed_count=1,
                success_count=1,
                failure_count=0,
                detail_lines=["- <@100>: 成功"],
            )
        )
        register_commands(bot)
        command = bot.tree.commands["status_bulk_clear"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10, guild_permissions=SimpleNamespace(manage_roles=True)),
        )

        with patch("status_bot.commands.has_manage_roles", return_value=True):
            with patch("status_bot.commands.can_manage_target", return_value=(True, "")):
                await command(
                    interaction,
                    FakeAttachment("100\n100\nabc\n"),
                )

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(len(interaction.edits), 1)
        content = interaction.edits[0][0]
        self.assertIn("ステータス一括解除結果", content)
        self.assertIn("成功: 1件", content)
        self.assertIn("除外: 2件", content)
        bot.service.bulk_clear_status.assert_called_once()
        called_members = bot.service.bulk_clear_status.call_args.args[1]
        self.assertEqual([member.id for member in called_members], [100])

    async def test_status_bulk_set_accepts_utf8_bom(self) -> None:
        bot = FakeBot()
        bot.store.get_status_config = Mock(
            return_value=SimpleNamespace(
                guild_id=1,
                stage_count=1,
                stages=[StatusStageConfig(1, "", 11, 86400, ACTION_HOLD)],
            )
        )
        bot.service.fetch_member_if_needed = AsyncMock(
            return_value=SimpleNamespace(id=100, mention="<@100>")
        )
        bot.service.bulk_assign_status = AsyncMock(
            return_value=BulkOperationResult(
                processed_count=1,
                success_count=1,
                failure_count=0,
                detail_lines=[],
            )
        )
        register_commands(bot)
        command = bot.tree.commands["status_bulk_set"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10, guild_permissions=SimpleNamespace(manage_roles=True)),
        )

        with patch("status_bot.commands.has_manage_roles", return_value=True):
            with patch("status_bot.commands.can_manage_target", return_value=(True, "")):
                await command(interaction, FakeBomAttachment("100\n"), 1, "reason")

        bot.service.bulk_assign_status.assert_called_once()
        called_members = bot.service.bulk_assign_status.call_args.args[1]
        self.assertEqual([member.id for member in called_members], [100])

    async def test_status_bulk_set_skips_lookup_failure_per_line(self) -> None:
        bot = FakeBot()
        bot.store.get_status_config = Mock(
            return_value=SimpleNamespace(
                guild_id=1,
                stage_count=1,
                stages=[StatusStageConfig(1, "", 11, 86400, ACTION_HOLD)],
            )
        )
        bot.service.fetch_member_if_needed = AsyncMock(
            side_effect=[
                discord.DiscordException("lookup failed"),
                SimpleNamespace(id=200, mention="<@200>"),
            ]
        )
        bot.service.bulk_assign_status = AsyncMock(
            return_value=BulkOperationResult(
                processed_count=1,
                success_count=1,
                failure_count=0,
                detail_lines=[],
            )
        )
        register_commands(bot)
        command = bot.tree.commands["status_bulk_set"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10, guild_permissions=SimpleNamespace(manage_roles=True)),
        )

        with patch("status_bot.commands.has_manage_roles", return_value=True):
            with patch("status_bot.commands.can_manage_target", return_value=(True, "")):
                await command(interaction, FakeAttachment("100\n200\n"), 1, "reason")

        content = interaction.edits[0][0]
        self.assertIn("取得に失敗しました", content)
        bot.service.bulk_assign_status.assert_called_once()
        called_members = bot.service.bulk_assign_status.call_args.args[1]
        self.assertEqual([member.id for member in called_members], [200])

    async def test_status_notify_config_disable_all_rejects_other_updates(self) -> None:
        bot = FakeBot()
        bot.store.get_status_notification_config = Mock(
            return_value=GuildStatusNotificationConfig(1, 123, True, True, True, True, True)
        )
        register_commands(bot)
        command = bot.tree.commands["status_notify_config"]

        interaction = FakeInteraction(
            guild=FakeGuild(1),
            user=SimpleNamespace(id=10),
        )

        with patch("status_bot.commands.has_manage_guild", return_value=True):
            await command(interaction, channel=FakeChannel(123), disable_all=True)

        self.assertIn("`disable_all`", interaction.response.messages[0][0])

if __name__ == "__main__":
    unittest.main()
