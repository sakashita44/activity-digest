# Changelog

このプロジェクトの主な変更を記録する。形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に従い、バージョンは [Semantic Versioning](https://semver.org/lang/ja/) に従う。

## [Unreleased]

### Added

- GitHubとNotionの活動記録から週次の活動記録をGeminiで生成し、WordPressへ下書き保存するパイプラインを追加した。AIへは、ソースごとの前提を含むSource documentを渡す。記事タイトルは対象期間から生成する (#2, #5)
- GitHubの収集対象リポジトリを `github.allowlist` で限定できるようにした (#5)
- `--dump-dir` で、AIへ渡したSource documentと各段階の出力をローカルに書き出せるようにした (#5)

### Security

- Actionsで生成記事をアーティファクトとしてアップロードしないようにした。確認前の記事はWordPressの下書きにだけ保存する (#4)
- Actions上で失敗した場合、ログには失敗した段階と例外の型名だけを出力するようにした。APIの応答本文とリクエストURLはログに残さない (#4)
- ワークフローの `GITHUB_TOKEN` を読み取り権限に絞った (#4)
