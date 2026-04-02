# AGENTS.md

## このリポジトリの目的
Discord のステータス段階管理 Bot を保守する。
主な用途は以下。
- サーバーごとのステータス段階設定
- 段階ごとのロール / 期間 / 満了時動作 (`next` / `clear` / `hold`) の管理
- 自動段階遷移と再参加時のロール再適用
- Android Termux 上での運用
- SQLite による状態保存
- 旧 3 段違反モデルからの自動移行互換の維持

## 主要ファイル
- `bot.py`: 薄い起動エントリポイント。`status_bot` を組み立てて起動する
- `status_bot/app.py`: Bot 本体の組み立て、イベント、定期処理
- `status_bot/commands.py`: slash command 登録
- `status_bot/views.py`: `/setup` の View / Modal
- `status_bot/service.py`: 状態遷移、ロール再適用、設定保存ユースケース
- `status_bot/store.py`: SQLite 初期化、legacy migration、設定 / record CRUD
- `runbot.sh`: 自己更新付きの直接起動スクリプト
- `supervisor.sh`: 監視、再起動、更新反映を行う supervisor
- `setenv.sh`: ローカル実行用の環境変数読み込みファイル（実値はコミットしない）
- `setenv.example.sh`: 環境変数ファイルのテンプレート
- `tests/`: `unittest` ベースの回帰テスト
- `violations.db`: 実行時に生成される SQLite DB
- `logs/`: 実行ログ置き場
- `.codex/tasks/todo.md`: 作業計画・進捗・検証ログ
- `.codex/tasks/lessons.md`: 再発防止メモ

## Workflow
- 非自明タスクは Plan → Execute → Verify → Report の順で進行する
- 前提崩れ・設計崩れ・予期しない副作用が出たら、いったん停止して再計画する
- 完了報告前に、必ず実施した検証と結果を示す
- 変更前に依頼範囲を明確化し、無関係な改善を混ぜない
- 複数ファイルを触る場合は、なぜ必要かを説明できる状態で行う

## Task Management
- 着手時に `.codex/tasks/todo.md` へチェックリストを書く
- 実施した変更、確認内容、未解決事項を同ファイルに追記する
- ユーザー修正・手戻り・誤解が発生したら `.codex/tasks/lessons.md` に再発防止策を書く
- 長時間タスクでは途中の判断と中間結果を記録する

## Editing Rules
- 影響範囲は最小化する
- 無関係ファイルを変更しない
- 既存の命名・構成・責務分離に合わせる
- 一時しのぎの修正ではなく、原因に対応する
- 依存追加は原則しない。必要なら理由を明記する
- DB スキーマ変更は後方互換性または移行手順を必ず示す
- 既存の slash command 名を変える場合は、利用者影響を明記する

## Core Principles
- **シンプルさを第一に**: 変更は可能な限り単純にし、コードへの影響を最小限にする
- **根本原因を優先**: 表面的な回避策より、再発しない修正を選ぶ
- **安全性を優先**: 認証情報、権限、ロール操作、DB 破壊に注意する
- **再現性を重視**: 手順・検証・失敗条件を残す
- **実運用を意識**: Android Termux 上での起動、自動起動、DB 永続化、ロール階層を考慮する

## Secrets / Safety
- 実際のトークン、ID、個人情報をコミットしない
- `setenv.sh` に実値を入れた変更を提案・作成・コミットしない
- `violations.db`、`logs/`、キャッシュ類は変更対象にしない
- Bot の権限変更や危険な自動化を行う場合は、その理由と影響を明示する
- Discord のロール操作は、Bot のロール順と `Manage Roles` 前提を崩さない

## Verification
- 少なくとも以下を優先して検証する
  - `python -m py_compile bot.py status_bot/*.py tests/*.py`
  - `python -m unittest discover -s tests`
  - Shell 変更時は `sh -n runbot.sh supervisor.sh`
  - slash command 追加・変更時は、想定される入出力を説明する
  - DB 変更時は、既存 DB と legacy migration で何が起きるかを説明する
- 実行できない検証は「未実施」と明記し、理由を書く
- 高リスク変更では、差分意図と失敗時の影響を要約する

## Discord Bot 固有ルール
- サーバーごとの設定は `guild_id` 単位で扱う
- ロールは名前ではなく ID を基準に扱う
- 権限不足時のエラーメッセージは残す
- 段階数は guild ごとに可変であり、固定 3 段前提で実装しない
- 各段階はロール ID・期間・満了時動作 (`next` / `clear` / `hold`) を持つ前提で扱う
- 旧 `light` / `medium` / `heavy` モデルは legacy migration としてのみ扱う
- 既存動作を変える場合は、変更前後の挙動差を説明する
- 設定変更が既存 active record へ与える影響を見落とさない

## Termux / Android 固有ルール
- パスは Termux 環境で壊れないことを優先する
- `sh` 互換を意識し、不要に bash 専用構文を増やさない
- 自動起動スクリプトは二重起動を避ける
- `runbot.sh` と `supervisor.sh` の両方が運用入口になりうる前提で扱う
- `BOT_ENTRYPOINT=bot.py` の共通前提を崩さない
- supervisor の known-good checkout / 自動再起動安全性を壊さない
- `tmux` セッション名やログ出力先を勝手に変更しない
- Android のバックグラウンド制約を前提に、常時稼働前提の断定をしない
- 
## Review guidelines
- 実トークンや秘密情報が差分に含まれていないかを最優先で確認する
- `setenv.sh`、`.db`、`logs/` がコミット対象に入っていないか確認する
- 無関係ファイルの変更が混ざっていないか確認する
- `commands` / `views` / `service` / `store` の責務分離を壊していないか確認する
- 権限ロジック、ロール階層前提、DB 更新条件に破綻がないか確認する
- `guild_status_settings` / `guild_status_stages` / `status_records` と legacy migration の整合性を確認する
- 変更が要求範囲を超えていないか確認する
- 例外処理で障害時に黙って壊れないか確認する

## Communication
- 作業前に、短い方針と対象範囲を示す
- 不確実な点は推測で断定しない
- 実施できたこと / できなかったこと / 未確認事項を分けて報告する
- 代替案がある場合は、現案との差分を明示する
