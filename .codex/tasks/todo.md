# TODO

- [x] `runbot.sh` が自己更新失敗時に即終了するようにする
- [x] `supervisor.sh` の `deploy_main` が途中失敗を成功扱いしないようにする
- [x] 起動スクリプトの検証結果を追記する
- [x] `runbot.sh` の自己更新挙動を維持する
- [x] `supervisor.sh` で deploy 失敗時に既存 bot を止めないようにする
- [x] `supervisor.sh` に実行権限を付ける
- [x] 起動スクリプトの検証結果を追記する
- [x] `/setup` の follow-up 操作でも権限再確認する
- [x] `/setup` のロール保存前に選択ロールを再解決する
- [x] 構文検証を実施し、結果を追記する
- [x] `/setup` コマンドと対話 UI を追加する
- [x] `/setup` からロール設定と期間設定を保存できるようにする
- [x] 既存の設定案内文を `/setup` 優先に更新する
- [x] 構文検証を実施し、結果を追記する
- [x] `/config_roles` の旧違反ロール移行バグを修正する
- [x] `/config_roles` の再適用前に interaction を defer する
- [x] 構文検証を実施し、結果を追記する
- [x] 現状の違反設定反映フローを確認する
- [x] guild単位の有効違反レコード取得処理を追加する
- [x] `/config_roles` 後に既存違反者へロール再適用する
- [x] `/config_durations` 応答文を既存違反者への反映方針に合わせて明確化する
- [x] 構文検証を実施し、結果を記録する

## Notes

- `.codex/tasks` ディレクトリが存在しなかったため新規作成した

## Changes

- `runbot.sh` に `set -eu` を追加し、自己更新の `git` コマンドが失敗したら stale な bot を起動せず即終了するようにした
- `supervisor.sh` の `deploy_main()` で各 `git` コマンドを `|| return 1` 付きにし、`if deploy_main; then ...` でも途中失敗を成功扱いしないようにした
- `runbot.sh` に `git fetch` / `checkout main` / `reset --hard origin/main` を戻し、既存の直接起動フローでも自己更新されるようにした
- `supervisor.sh` の `deploy_main()` から二重の `git fetch` を外し、更新検知後は deploy 成功時のみ bot を stop/start するようにした
- `supervisor.sh` に実行権限を付け、直接起動できるようにした
- `OwnerOnlyView.interaction_check()` で setup 実行者の一致に加えて `Manage Server` 権限も毎回再確認するようにした
- `DurationSetupModal.on_submit()` でも保存前に `Manage Server` 権限を再確認するようにした
- `RoleSetupView.save_roles()` で保存直前に選択ロールを `guild.get_role(id)` で再解決し、削除済みロールは保存せず選び直しを促すようにした
- `bot.py` に `/setup` を追加し、現在設定表示と `ロール設定` / `期間設定` / `再表示` ボタンを持つ `SetupHomeView` を実装した
- `bot.py` に `RoleSetupView` と `DurationSetupModal` を追加し、RoleSelect 3 個と日数入力モーダルから設定保存できるようにした
- `bot.py` にロール設定・期間設定の共通保存 helper と setup 表示用 helper を追加し、既存 `/config_roles` と `/config_durations` でも再利用するようにした
- `config_show` と未設定エラー文言を `/setup` 優先の案内へ更新した
- `bot.py` に `role_ids_from_settings()` を追加し、現設定と旧設定の両方の違反ロール ID を除去対象へ渡せるようにした
- `apply_violation_role()` と `refresh_guild_violation_roles()` に追加除去ロール ID の引数を追加し、設定変更時に旧違反ロールを外せるようにした
- `/config_roles` で更新前の違反ロール ID を保持してから interaction を defer し、その後に保存と既存違反者への再適用を行うようにした
- `bot.py` に guild 単位で有効な違反レコードを取得する `get_active_records_by_guild()` を追加
- `bot.py` に `/config_roles` 後の既存違反者向けロール再適用処理 `refresh_guild_violation_roles()` を追加
- `/config_roles` の応答に既存違反者への再適用件数と失敗件数を追加
- `/config_durations` の応答に「現在の期限は変わらず、次回降格以降に新期間が反映される」旨を追加

## Verification

- 実施: `sh -n runbot.sh supervisor.sh` -> 成功
- 実施: `runbot.sh` が `set -eu` で始まり、更新失敗時に python 実行へ進まないことをコード上で確認
- 実施: `deploy_main()` の各 `git` コマンドが `|| return 1` 付きになり、`if deploy_main` でも checkout 失敗を成功扱いしないことをコード上で確認
- 実施: `stat -c '%A %n' supervisor.sh` -> `-rwxr-xr-x supervisor.sh`
- 実施: `git diff --summary -- runbot.sh supervisor.sh` -> `supervisor.sh` が `100755` になったことを確認
- 実施: `python -m py_compile bot.py` -> 成功
- 実施: setup View/Modal の権限判定とロール再解決が保存前に走ることをコード上で確認
- 実施: `DISCORD_TOKEN=dummy DB_PATH=/tmp/dis-bot-setup-test.db python - <<'PY' ... PY` -> `SetupHomeView` / `RoleSetupView` / `DurationSetupModal` の生成成功
- 実施: `/config_roles` の処理順を確認し、`defer -> 保存 -> 再適用 -> followup` になっていることをコード上で確認
- 実施: 旧設定ロール ID を `refresh_guild_violation_roles(..., remove_role_ids=previous_role_ids)` 経由で再適用時に除去することをコード上で確認
- 実施: `/setup` から既存保存 helper を呼ぶ構成になっていること、`config_show` / 未設定エラーが `/setup` 優先文言になっていることをコード上で確認
- 未実施: Discord 上での slash command 動作確認
- 未実施理由: この環境では実サーバー接続とロール変更を伴う E2E 検証ができないため
