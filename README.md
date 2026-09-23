# activity-digest

GitHub、Notion、任意のローカルイベントから活動記録を集め、Geminiで週次の日記を生成してWordPressへ下書き保存する個人用パイプラインである。記事にはAI生成であることと、事実と異なる可能性があることを示す開示文を必ず付ける。

## 処理の流れ

1. GitHubのユーザーイベント、許可したNotionデータソース、ローカルJSONを収集する。
1. GitHubのブラックリスト、Notionのホワイトリスト、座標のラベル化をコードで適用する。
1. Geminiを独立したコンテキストで3回呼び、情報検査、日記の執筆、文体の推敲を順に行う。
1. イベントIDと座標を各境界で検査し、開示文を付けてMarkdownへ保存する。
1. 指定時だけWordPress REST APIへ下書きとして登録する。同じスラッグの下書きがあれば更新する。同じスラッグの投稿が公開済みなど下書き以外の状態なら、人が確認・編集した記事を守るため更新せずエラーで終了する。

対象期間に活動がなければGeminiを呼ばず、記事も作らない。

## セットアップ

Python 3.12と[uv](https://docs.astral.sh/uv/)を使用する。

```bash
uv sync --dev
cp config.example.toml config.toml
cp .env.example .env
```

`config.toml` と `.env` はGit管理の対象外である。認証情報は `.env` または実行環境へ設定する。

- `GEMINI_API_KEY`: Google AI APIキー
- `GH_ACTIVITY_TOKEN`: GitHubトークン。未設定時は `GITHUB_TOKEN` を使う
- `NOTION_API_KEY`: Notionインテグレーションシークレット
- `WORDPRESS_URL`: WordPressサイトURL
- `WORDPRESS_USERNAME`: WordPressユーザー名
- `WORDPRESS_APP_PASSWORD`: WordPressアプリケーションパスワード
- `GEMINI_MODEL`: TOMLの共通モデルを一時的に上書きする任意値

## 設定できる範囲

コメント付きの [config.example.toml](config.example.toml) を複製して設定する。

- 基本設定: タイムゾーン、出力先、開示文、追加イベントJSON
- 収集期間: `period.days` と `period.end_offset_days`
- Gemini: 共通モデル、3段階それぞれのモデル、各プロンプトのファイルパス
- GitHub: 対象ユーザー、除外リポジトリの完全一致リスト
- Notion: 収集を許可するデータソースID
- 位置情報: 登録地点の座標、半径、公開用の曖昧なラベル、登録外の既定ラベル
- WordPress: スラッグ接頭辞、カテゴリーID、タグID、投稿者ID、コメント、ピンバック、投稿フォーマット

WordPressの `status` は常に `draft` である。設定に `status`、`slug`、`title`、`content` を加えても無視する。開示文は文面を変更できるが、空にすると組み込みの文面へ戻る。

プロンプト本文は [prompts/inspector.md](prompts/inspector.md)、[prompts/writer.md](prompts/writer.md)、[prompts/editor.md](prompts/editor.md) にある。スクリプトを変更せず、役割ごとの指示を編集できる。

## 対象期間

通常実行では、実行日から `period.end_offset_days` 日前を末尾とし、そこから `period.days` 日間を収集する。既定値は実行前日までの7日間である。月曜実行なら前週の月曜から日曜までになる。

CLIでは次の順に設定を上書きする。

1. `--from` と `--to` の組み合わせ
1. `--target-week`
1. TOMLの `[period]`

`--from` と `--to` は必ず同時に指定する。

ローカルJSONのイベントは `timestamp` で期間を判定し、`timestamp` のない項目は収集しない。GitHubのイベントAPIは直近300件までしか返さないため、対象期間に300件を超える活動があると期間の前半のイベントを収集できない。この場合は警告ログを出す。

```bash
uv run python digest.py
uv run python digest.py --target-week 2026-W36
uv run python digest.py --target-week current
uv run python digest.py --from 2026-08-01 --to 2026-08-10
```

WordPressへ下書きを送る場合は `--publish` を加える。

```bash
uv run python digest.py --publish
```

## GitHub Actions

[.github/workflows/digest.yml](.github/workflows/digest.yml) は毎週月曜9時（日本時間）に実行し、WordPressへ下書きを保存する。手動実行では対象週と送信の有無を選べる。Repository Secretsへ次を登録する。

- `DIGEST_CONFIG_TOML`: `config.toml` の内容全体
- `GEMINI_API_KEY`
- `GH_ACTIVITY_TOKEN`
- `NOTION_API_KEY`
- `WORDPRESS_URL`
- `WORDPRESS_USERNAME`
- `WORDPRESS_APP_PASSWORD`

publicリポジトリでは、実行ログとアーティファクトを第三者が閲覧できる。そのため、確認前の記事はWordPressの下書きにのみ保存し、アーティファクトにはアップロードしない。Actions上で失敗した場合は、ログに失敗した段階と例外の型名のみを出力する。例外メッセージやスタックトレースを含む詳細は、ローカル実行時にのみ出力する。

publicリポジトリでは、60日間活動がないとGitHubが定期実行を無効化する。定期実行自体は活動に含まれない。無効化のおよそ7日前に、ワークフローの作成者へ警告メールが届く。作成後に別の人がcronを変更した場合や、無効化後に再有効化した場合は、その人へ届く。有効なワークフローへAPIで再有効化を繰り返して延命する方法は公式に保証されておらず、この方法を提供していたActionはGitHubの利用規約違反で凍結された。そのため、自動で延命する仕組みは置かず、このメールで停止を検知する。無効化された場合は、Actionsタブの対象ワークフローで再有効化するか、次のコマンドを実行する。

```bash
gh workflow enable digest.yml
```

## 品質検査

`project-standards` の一般層とPython層に合わせ、pre-commit、Ruff、Pyright、pytestを使用する。初回だけフックを設定する。

```bash
bash scripts/setup.sh
uv run pre-commit run --all-files
uv run pre-commit run --all-files --hook-stage pre-push
```

Pull Requestでは [.github/workflows/ci.yml](.github/workflows/ci.yml) が共有のPython CIを呼ぶ。

詳しい責務とデータ境界は [docs/concept.md](docs/concept.md) に記載する。
