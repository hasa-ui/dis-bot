# Lessons

- `/config_roles` のように設定変更直後に既存データへ再適用する処理では、更新後設定だけでなく更新前のロール ID も移行対象として保持する
- Discord slash command でメンバー走査や API 呼び出しを含む処理を追加するときは、初回応答 3 秒制限を前提に `defer` の要否を先に確認する
- View や Modal を使う設定 UI では、初回 slash command 時だけでなく各 callback / submit ごとに権限を再確認する
- Discord の `Role` オブジェクトを UI 状態として保持する場合でも、保存直前に `guild.get_role(id)` で再解決して削除済みロール ID を永続化しない
- 起動経路を増やすときは、既存エントリポイントの自己更新や自動起動導線を壊していないかを同時に確認する
- 更新監視からの再デプロイでは、新版の deploy 成功を確認するまで稼働中プロセスを止めない
- `runbot.sh` のような直接起動経路で更新を必須にする場合は、`set -e` か各コマンドの明示終了で update 失敗後の stale 起動を防ぐ
- `sh` では `if func; then` の条件式内で `set -e` 依存にしない。関数内の失敗は `|| return 1` などで明示的に伝播させる
- ただし `git checkout` は tracked 変更で失敗しても `git reset --hard origin/main` が自己回復できるため、checkout だけを即 fatal にしない
- その自己回復は「現在ブランチが main の場合」に限定する。非 `main` ブランチで checkout failure を無視すると、`reset --hard origin/main` が別ブランチの作業を破壊する
- supervisor の初回起動パスも更新ループと同じ失敗処理に揃える。`set -e` 下の top-level deploy failure を未処理のまま置くと、監視自体が止まる
