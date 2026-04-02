# AGENTS.md 更新案

`AGENTS.md` 自体は未変更。現行のレポジトリ実態に合わせて更新する場合の差分サマリと文案だけをまとめる。

## 差分サマリ

### 1. リポジトリ目的の説明が旧モデル前提
- 現行 `AGENTS.md` は「違反ロール管理 Bot」「重度→中度→軽度→解除」の固定 3 段前提で説明している。
- 実装は `status_bot/` に移行済みで、可変段階の `ステータス` モデル、`next` / `clear` / `hold`、legacy migration を前提にしている。
- 更新案では「現在の運用モデル」と「旧違反モデルは移行対象」という関係を明示する。

### 2. 主要ファイル欄が実装分割を反映していない
- `bot.py` は本体ではなく薄い起動エントリポイントになっている。
- 実装本体は `status_bot/` に分割され、`tests/` も追加済み。
- `supervisor.sh` は現行運用上の重要ファイルだが、主要ファイル欄に入っていない。

### 3. Discord Bot 固有ルールが固定 3 段前提
- 「`heavy -> medium -> light -> clear` を崩さない」は現在の可変段階モデルと食い違う。
- 現行コードでは guild ごとに 1〜10 段階、段階ごとにロール ID・期間・満了時動作を持つ。
- 旧 3 段違反モデルは migration のみで扱う。

### 4. Verification が現行の検証実態より古い
- 現在は `status_bot/*.py` と `tests/*.py` も構文確認対象。
- Python テストは `pytest` ではなく `unittest discover` ベースで追加済み。
- 起動スクリプトは `runbot.sh` と `supervisor.sh` の両方を `sh -n` で見るべき状態。

### 5. Review 観点が新構造を十分に表していない
- 現在は `command / view / service / store` の責務分離を前提に見る必要がある。
- DB も `guild_status_settings` / `guild_status_stages` / `status_records` と legacy migration の整合性を見る必要がある。

### 6. Termux / Android 欄に supervisor 運用の実態が足りない
- 現在は `runbot.sh` だけでなく `supervisor.sh` が監視・再起動フローの中核。
- `BOT_ENTRYPOINT=bot.py` の共通前提と、known-good checkout を保つ安全ロジックを崩さない観点を追記したほうがよい。

## 節ごとの更新文案

以下は、そのまま `AGENTS.md` に転記できる粒度の置き換え案。

### 1. `このリポジトリの目的` の更新案

```md
## このリポジトリの目的
Discord のステータス段階管理 Bot を保守する。
主な用途は以下。
- サーバーごとのステータス段階設定
- 段階ごとのロール / 期間 / 満了時動作 (`next` / `clear` / `hold`) の管理
- 自動段階遷移と再参加時のロール再適用
- Android Termux 上での運用
- SQLite による状態保存
- 旧 3 段違反モデルからの自動移行互換の維持
```

### 2. `主要ファイル` の更新案

```md
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
```

### 3. `Verification` の更新案

```md
## Verification
- 少なくとも以下を優先して検証する
  - `python -m py_compile bot.py status_bot/*.py tests/*.py`
  - `python -m unittest discover -s tests`
  - Shell 変更時は `sh -n runbot.sh supervisor.sh`
  - slash command 追加・変更時は、想定される入出力を説明する
  - DB 変更時は、既存 DB と legacy migration で何が起きるかを説明する
- 実行できない検証は「未実施」と明記し、理由を書く
- 高リスク変更では、差分意図と失敗時の影響を要約する
```

### 4. `Discord Bot 固有ルール` の更新案

```md
## Discord Bot 固有ルール
- サーバーごとの設定は `guild_id` 単位で扱う
- ロールは名前ではなく ID を基準に扱う
- 権限不足時のエラーメッセージは残す
- 段階数は guild ごとに可変であり、固定 3 段前提で実装しない
- 各段階はロール ID・期間・満了時動作 (`next` / `clear` / `hold`) を持つ前提で扱う
- 旧 `light` / `medium` / `heavy` モデルは legacy migration としてのみ扱う
- 既存動作を変える場合は、変更前後の挙動差を説明する
- 設定変更が既存 active record へ与える影響を見落とさない
```

### 5. `Review guidelines` の更新案

```md
## Review guidelines
- 実トークンや秘密情報が差分に含まれていないかを最優先で確認する
- `setenv.sh`、`.db`、`logs/` がコミット対象に入っていないか確認する
- 無関係ファイルの変更が混ざっていないか確認する
- `commands` / `views` / `service` / `store` の責務分離を壊していないか確認する
- 権限ロジック、ロール階層前提、DB 更新条件に破綻がないか確認する
- `guild_status_settings` / `guild_status_stages` / `status_records` と legacy migration の整合性を確認する
- 変更が要求範囲を超えていないか確認する
- 例外処理で障害時に黙って壊れないか確認する
```

### 6. `Termux / Android 固有ルール` の更新案

```md
## Termux / Android 固有ルール
- パスは Termux 環境で壊れないことを優先する
- `sh` 互換を意識し、不要に bash 専用構文を増やさない
- 自動起動スクリプトは二重起動を避ける
- `runbot.sh` と `supervisor.sh` の両方が運用入口になりうる前提で扱う
- `BOT_ENTRYPOINT=bot.py` の共通前提を崩さない
- supervisor の known-good checkout / 自動再起動安全性を壊さない
- `tmux` セッション名やログ出力先を勝手に変更しない
- Android のバックグラウンド制約を前提に、常時稼働前提の断定をしない
```

## 更新しなくてよい節

現時点では、以下は大きな食い違いがなく、必須更新対象ではない。

- `Workflow`
- `Task Management`
- `Editing Rules`
- `Core Principles`
- `Secrets / Safety`
- `Communication`

## 補足

- `violations.db` という DB ファイル名は現行実装でも使われているため、名称自体は必須変更ではない。
- `setenv.sh` は repo 内テンプレートではなく運用側の秘密ファイルなので、主要ファイル欄には残しつつ秘密情報ルールも維持する。
- 旧 `guild_settings` / `sanctions` は削除済みではなく、`status_bot/store.py` の migration 対象として残っている前提で文案化している。
