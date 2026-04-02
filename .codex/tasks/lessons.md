# Lessons

- `/config_roles` のように設定変更直後に既存データへ再適用する処理では、更新後設定だけでなく更新前のロール ID も移行対象として保持する
- Discord slash command でメンバー走査や API 呼び出しを含む処理を追加するときは、初回応答 3 秒制限を前提に `defer` の要否を先に確認する
- View や Modal を使う設定 UI では、初回 slash command 時だけでなく各 callback / submit ごとに権限を再確認する
- Discord の `Role` オブジェクトを UI 状態として保持する場合でも、保存直前に `guild.get_role(id)` で再解決して削除済みロール ID を永続化しない
