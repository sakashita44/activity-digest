# activity-digest

GitHub、Notion、ローカルイベントの活動記録から、LLM を活用して週次の活動日記を生成し、WordPress へ保存する個人用パイプライン。

## 特徴

- 複数データソースの収集: GitHub のユーザーイベント、Notion の指定データソース、任意のローカル JSON イベントに対応
- プライバシー保護: 位置情報の粗粒度ラベル化（座標マスキング）や除外リストによる機微情報の保護
- 段階的な AI 生成: 情報検査、草案執筆、文体推敲の 3 段階処理（各段階でモデル設定が可能）
- 安全な WordPress 連携: 下書き保存による事前確認、同一週スラッグによる更新、公開済み記事の誤上書き防止に対応
- 自動運用: GitHub Actions による週次定期実行と、手動実行・期間指定実行に対応

## セットアップ

### 前提条件

- Python 3.12
- [uv](https://docs.astral.sh/uv/)

### 手順

依存関係のインストールと設定ファイルの準備を行う。

```bash
uv sync --dev
cp config.example.toml config.toml
cp .env.example .env
```

`config.toml` と `.env` は Git の管理対象外である。認証情報は `.env` または実行環境の環境変数に設定する。

### 環境変数

| 変数名                   | 必須 | 説明                                                   |
| :----------------------- | :--- | :----------------------------------------------------- |
| `GEMINI_API_KEY`         | 必須 | Google AI API キー                                     |
| `GH_ACTIVITY_TOKEN`      | 任意 | GitHub トークン（未設定時は `GITHUB_TOKEN` を使用）    |
| `NOTION_API_KEY`         | 任意 | Notion インテグレーションシークレット（Notion 連携時） |
| `WORDPRESS_URL`          | 任意 | WordPress サイトの URL（WordPress 連携時）             |
| `WORDPRESS_USERNAME`     | 任意 | WordPress ユーザー名                                   |
| `WORDPRESS_APP_PASSWORD` | 任意 | WordPress アプリケーションパスワード                   |
| `GEMINI_MODEL`           | 任意 | TOML の共通モデルを一時的に上書きするモデル名          |

### 設定ファイル

設定項目の詳細は、コメント付きの [config.example.toml](config.example.toml) を参照のこと。セットアップで複製した `config.toml` を必要に応じて編集する。

- 基本設定: タイムゾーン、出力先、開示文、追加イベント JSON
- 収集期間: 日数（`period.days`）と終了オフセット日数（`period.end_offset_days`）
- Gemini: 共通モデル、3 段階それぞれのモデル、各プロンプトファイル（[prompts/](prompts/)）
- GitHub: 対象ユーザー、除外リポジトリリスト
- Notion: 収集を許可するデータソース ID
- 位置情報: 登録地点の座標、判定半径、公開用ラベル、既定ラベル
- WordPress: スラッグ接頭辞、カテゴリー ID、タグ ID、投稿者 ID など

## 使い方

### CLI での実行

通常実行では、`config.toml` の設定に基づき実行前日までの 7 日間を収集して記事を生成する。

```bash
uv run python digest.py
```

WordPress へ保存する場合は `--publish` を指定する（既定では下書きとして保存）。

```bash
uv run python digest.py --publish
```

### 収集期間の指定

CLI 引数により収集期間を上書きできる。優先順位は次のとおりである。

1. `--from` と `--to`（特定の日付範囲を指定。両方を必ず同時に指定すること）
1. `--target-week`（週単位で指定。例: `2026-W36` や `current`）
1. `config.toml` の `[period]` 設定

```bash
# 週を指定して実行
uv run python digest.py --target-week 2026-W36

# 今週（実行日を含む週）を指定して実行
uv run python digest.py --target-week current

# 日付範囲を指定して実行
uv run python digest.py --from 2026-08-01 --to 2026-08-10
```

> [!NOTE]
> 対象期間内に活動イベントが存在しない場合、AI モデルの呼び出しや記事生成は行わずに終了する。

## 定期実行（GitHub Actions）

[.github/workflows/digest.yml](.github/workflows/digest.yml) により、毎週月曜 9:00（JST）に自動実行し、WordPress へ下書きを保存する。GitHub の Actions タブから手動実行（workflow_dispatch）も可能である。

### Repository Secrets の設定

GitHub リポジトリの Secrets に次の項目を登録する。

- `DIGEST_CONFIG_TOML`: `config.toml` の内容全体
- `GEMINI_API_KEY`
- `GH_ACTIVITY_TOKEN`
- `NOTION_API_KEY`
- `WORDPRESS_URL`
- `WORDPRESS_USERNAME`
- `WORDPRESS_APP_PASSWORD`

### 60 日間非アクティブ時の再有効化

GitHub の仕様により、public リポジトリで 60 日間コミットなどの活動がない場合、スケジュール実行（cron）が自動的に無効化される。無効化された場合は、Actions タブから再有効化するか、GitHub CLI で次のコマンドを実行する。

```bash
gh workflow enable digest.yml
```

## 開発と品質検査

`project-standards` の一般層と Python 層に合わせ、pre-commit、Ruff、Pyright、pytest を使用する。初回セットアップ時にフックを設定する。

```bash
bash scripts/setup.sh
uv run pre-commit run --all-files
uv run pre-commit run --all-files --hook-stage pre-push
```

Pull Request では [.github/workflows/ci.yml](.github/workflows/ci.yml) が共有の Python CI を呼ぶ。

## 関連ドキュメント

システムの設計方針、アーキテクチャ、データ境界、コンポーネント責務の詳細は [docs/concept.md](docs/concept.md) を参照のこと。
