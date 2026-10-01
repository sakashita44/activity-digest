# activity-digest

GitHubとNotionに残る活動の痕跡を集め、Geminiで週次の活動記録を生成してWordPressへ下書き保存する個人用パイプラインである。記事にはAI生成であることと、事実と異なる可能性があることを示す開示文を必ず付ける。

## 処理の流れ

1. サービスごとのCollectorが、対象期間の記録をSource documentとして生成する。Source documentは、ソース名、その記録が何を意味するかをAIへ伝える前提（context）、記録の一覧からなるMarkdownである。GitHubの対象リポジトリの絞り込みとNotionのデータソースの許可は、Collectorがコードで適用する。
1. Geminiを独立したコンテキストで3回呼ぶ。情報検査では各ソースの前提に沿って記録を解釈し、個人名や秘密情報を除いた材料を作る。続いて記事草案の執筆、文体の推敲を順に行う。
1. 推敲後の本文を記事テンプレートへ差し込み、対象期間から生成したタイトルを付けてMarkdownへ保存する。開示文は記事テンプレートの固定部分に含め、AIには生成させない。
1. 指定時だけWordPress REST APIへ下書きとして登録する。同じスラッグの下書きがあれば更新する。同じスラッグの投稿が公開済みなど下書き以外の状態なら、人が確認・編集した記事を守るため更新せずエラーで終了する。

対象期間に活動がなければGeminiを呼ばず、記事も作らない。

## セットアップ

Python 3.12と[uv](https://docs.astral.sh/uv/)を使用する。

```bash
uv sync --dev
cp -r config.example config
cp .env.example .env
```

`config/` と `.env` はGitで管理しない。認証情報は `.env` または実行環境へ設定する。

- `GEMINI_API_KEY`: Google AI APIキー
- `GH_ACTIVITY_TOKEN`: GitHubトークン。未設定時は `GITHUB_TOKEN` を使う
- `NOTION_API_KEY`: Notionインテグレーションシークレット
- `WORDPRESS_URL`: WordPressサイトURL
- `WORDPRESS_USERNAME`: WordPressユーザー名
- `WORDPRESS_APP_PASSWORD`: WordPressアプリケーションパスワード
- `GEMINI_MODEL`: TOMLの共通モデルを一時的に上書きする任意値

## 設定できる範囲

コメント付きの [config.example/](config.example/) を `config/` へ複製して設定する。設定は、全体の動作を決めるGlobal設定と、サービスごとの収集対象を決めるソース設定に分かれる。

- [config.example/global.toml](config.example/global.toml): Global設定
    - 基本設定: タイムゾーン、出力先
    - 収集期間: `period.window_days` と `period.delay_days`
    - 記事テンプレート: `article.template`
    - Gemini: 共通モデル、3段階それぞれのモデル、各プロンプトのファイルパス
    - WordPress: スラッグ接頭辞、カテゴリーID、タグID、投稿者ID、コメント、ピンバック、投稿フォーマット
- [config.example/sources/github.toml](config.example/sources/github.toml): 対象ユーザー、対象リポジトリと除外リポジトリの完全一致リスト
- [config.example/sources/notion.toml](config.example/sources/notion.toml): 収集を許可するデータソースID

`config/sources/` に置いたファイルだけが収集対象になる。収集しないサービスの設定ファイルは置かない。

WordPressの `status` は常に `draft` とする。設定に `status`、`slug`、`title`、`content` を指定しても無視する。

記事の本文は [templates/article.md](templates/article.md) から組み立てる。AIが生成した本文を `{{ content }}` の位置へ差し込み、開示文などの固定部分はテンプレートにそのまま残す。テンプレートに `{{ content }}` がなければ、Geminiを呼ぶ前にエラーで終了する。

プロンプト本文は [prompts/inspector.md](prompts/inspector.md)、[prompts/writer.md](prompts/writer.md)、[prompts/editor.md](prompts/editor.md) にある。スクリプトを変更せず、役割ごとの指示を編集できる。

## 対象期間

通常実行では、実行前日から `period.delay_days` 日さかのぼった日を末尾とし、そこから `period.window_days` 日間を収集する。`period.delay_days` は、直近の予定や位置などをすぐ記事に含めないための猶予日数として設定する。

- `window_days = 7`、`delay_days = 0`（既定値）: 実行前日までの7日間。月曜実行なら前週の月曜から日曜まで
- `window_days = 7`、`delay_days = 7`: その1週間前の7日間。月曜実行なら前々週の月曜から日曜まで

CLIでは次の順に設定を上書きする。

1. `--from` と `--to` の組み合わせ
1. `--target-week`
1. Global設定の `[period]`

`--from` と `--to` は必ず同時に指定する。CLIで明示した期間には `period.delay_days` を適用しない。

GitHubのイベントAPIは直近300件までしか返さないため、対象期間に300件を超える活動があると期間の前半のイベントを収集できない。この場合は警告ログを出す。

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

生成結果をプロンプトの調整などで確認する場合は、`--dump-dir` を加える。指定したディレクトリへ、AIへ渡したSource document（`sources.md`）と各段階の出力（`stage1.md`、`stage2.md`、`stage3.md`）を書き出す。これらは活動内容をそのまま含むため、Git管理外の `tmp/` 配下に実行ごとのディレクトリを指定し、Actionsでは使わない。

```bash
uv run python digest.py --dump-dir tmp/2026-09-23/dump --output tmp/2026-09-23/article.md
```

## GitHub Actions

[.github/workflows/digest.yml](.github/workflows/digest.yml) は毎週月曜9時（日本時間）に実行し、WordPressへ下書きを保存する。手動実行では対象週と送信の有無を選べる。Repository Secretsへ次を登録する。

- `CONFIG_GLOBAL`: `config/global.toml` の内容全体。登録は必須
- `CONFIG_SOURCE_GITHUB`: `config/sources/github.toml` の内容全体。未登録時はGitHubから収集しない
- `CONFIG_SOURCE_NOTION`: `config/sources/notion.toml` の内容全体。未登録時はNotionから収集しない
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
