# TODO

- [x] 現状の違反設定反映フローを確認する
- [x] guild単位の有効違反レコード取得処理を追加する
- [x] `/config_roles` 後に既存違反者へロール再適用する
- [x] `/config_durations` 応答文を既存違反者への反映方針に合わせて明確化する
- [x] 構文検証を実施し、結果を記録する

## Notes

- `.codex/tasks` ディレクトリが存在しなかったため新規作成した

## Changes

- `bot.py` に guild 単位で有効な違反レコードを取得する `get_active_records_by_guild()` を追加
- `bot.py` に `/config_roles` 後の既存違反者向けロール再適用処理 `refresh_guild_violation_roles()` を追加
- `/config_roles` の応答に既存違反者への再適用件数と失敗件数を追加
- `/config_durations` の応答に「現在の期限は変わらず、次回降格以降に新期間が反映される」旨を追加

## Verification

- 実施: `python -m py_compile bot.py` -> 成功
- 未実施: Discord 上での slash command 動作確認
- 未実施理由: この環境では実サーバー接続とロール変更を伴う E2E 検証ができないため
