import unittest
from types import SimpleNamespace

from status_bot.config import ACTION_CLEAR, ACTION_NEXT
from status_bot.formatters import (
    build_bulk_operation_message,
    build_stage_count_preview_message,
    build_stage_save_preview_message,
    build_status_history_message,
    build_status_list_message,
    build_status_config_message,
    paginate_status_history_messages,
    paginate_status_list_messages,
)
from status_bot.models import (
    BulkOperationResult,
    GuildStatusConfig,
    SetupPreviewSummary,
    StatusHistoryEntry,
    StatusListEntry,
    StatusStageConfig,
)
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

    def test_build_status_list_message_contains_page_and_reason(self) -> None:
        message = build_status_list_message(
            [
                StatusListEntry(
                    user_id=10,
                    member_display="<@10>",
                    stage_index=2,
                    stage_name="段階2（警告）",
                    next_change_text="1日後に 段階1へ移行",
                    reason="確認用の理由",
                    expires_at=12345,
                ),
                StatusListEntry(
                    user_id=20,
                    member_display="<@20>",
                    stage_index=1,
                    stage_name="段階1",
                    next_change_text="なし（現在の段階を維持中）",
                    reason="",
                    expires_at=None,
                ),
            ],
            page_index=0,
            page_count=2,
            total_count=11,
        )

        self.assertIn("現在のステータス一覧", message)
        self.assertIn("ページ: 1/2", message)
        self.assertIn("全件数: 11件", message)
        self.assertIn("<@10>: 段階2（警告） / 次回変更 1日後に 段階1へ移行 / 理由 確認用の理由", message)
        self.assertIn("<@20>: 段階1 / 次回変更 なし（現在の段階を維持中） / 理由 （なし）", message)

    def test_build_status_history_message_contains_actor_and_detail(self) -> None:
        message = build_status_history_message(
            "<@10>",
            [
                StatusHistoryEntry(
                    created_at=12345,
                    event_type="manual_set",
                    actor_display="<@99>",
                    from_stage_name="段階1",
                    to_stage_name="段階2（警告）",
                    reason="確認用の理由",
                    detail="手動更新",
                )
            ],
            page_index=0,
            page_count=1,
            total_count=1,
        )

        self.assertIn("<@10> のステータス履歴", message)
        self.assertIn("ページ: 1/1", message)
        self.assertIn("全件数: 1件", message)
        self.assertIn("手動付与 / 実行者 <@99> / 変更 段階1 -> 段階2（警告） / 理由 確認用の理由 / 詳細 手動更新", message)

    def test_build_bulk_operation_message_contains_summary_and_details(self) -> None:
        message = build_bulk_operation_message(
            "ステータス一括付与結果",
            BulkOperationResult(
                processed_count=3,
                success_count=2,
                failure_count=1,
                detail_lines=["- <@2>: 失敗 (権限不足)", "- <@3>: 成功"],
            ),
            skipped_count=1,
            skipped_lines=["- 4行目: 重複しているため除外しました。"],
        )

        self.assertIn("ステータス一括付与結果", message)
        self.assertIn("対象件数: 4件", message)
        self.assertIn("成功: 2件", message)
        self.assertIn("失敗: 1件", message)
        self.assertIn("除外: 1件", message)
        self.assertIn("詳細:", message)
        self.assertIn("重複しているため除外しました。", message)

    def test_paginate_status_list_messages_splits_before_discord_limit(self) -> None:
        entries = [
            StatusListEntry(
                user_id=index,
                member_display=f"<@{index}>",
                stage_index=2,
                stage_name="段階2（" + ("警告" * 20) + "）",
                next_change_text="<@&1> と <@&2> の確認後に 段階1へ移行",
                reason="確認メモ" * 20,
                expires_at=1000 + index,
            )
            for index in range(1, 8)
        ]

        pages = paginate_status_list_messages(entries, max_length=400)

        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 400 for page in pages))
        self.assertIn(f"ページ: 1/{len(pages)}", pages[0])
        self.assertIn(f"ページ: {len(pages)}/{len(pages)}", pages[-1])

    def test_paginate_status_history_messages_splits_before_discord_limit(self) -> None:
        entries = [
            StatusHistoryEntry(
                created_at=1000 + index,
                event_type="manual_set",
                actor_display=f"<@{index}>",
                from_stage_name="段階2（" + ("警告" * 10) + "）",
                to_stage_name="段階1",
                reason="確認メモ" * 10,
                detail="例外詳細" * 20,
            )
            for index in range(1, 6)
        ]

        pages = paginate_status_history_messages("<@10>", entries, max_length=500)

        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 500 for page in pages))
        self.assertIn(f"ページ: 1/{len(pages)}", pages[0])
        self.assertIn(f"ページ: {len(pages)}/{len(pages)}", pages[-1])


if __name__ == "__main__":
    unittest.main()
