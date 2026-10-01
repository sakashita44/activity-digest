import argparse
import logging
import os
import re
import sys
import tomllib
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import markdown
from google import genai

from activity_digest.ai import (
    run_stage1_inspector,
    run_stage2_writer,
    run_stage3_editor,
)
from activity_digest.collectors import SourceDocument, github, notion

TOKYO_TZ = ZoneInfo("Asia/Tokyo")
CONTENT_PLACEHOLDER = "{{ content }}"
WORDPRESS_DEFAULT_FIELDS = {
    "categories",
    "tags",
    "author",
    "comment_status",
    "ping_status",
    "format",
}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("activity-digest")
# httpx は INFO でリクエスト URL を記録し、非公開リポジトリ名やデータソース ID が公開ログに残るため抑止する
for _noisy_logger in ("httpx", "httpcore"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)


class PipelineStageError(Exception):
    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


@contextmanager
def pipeline_stage(stage: str) -> Generator[None]:
    try:
        yield
    except Exception as error:
        raise PipelineStageError(stage) from error


def load_env(env_path: Path | None = None) -> None:
    path = env_path or Path(".env")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"設定ファイルが見つからない: {path}")
    with path.open("rb") as config_file:
        return tomllib.load(config_file)


def load_config(config_dir: str | Path = "config") -> dict[str, Any]:
    directory = Path(config_dir)
    config = _load_toml(directory / "global.toml")
    for path in sorted((directory / "sources").glob("*.toml")):
        if path.stem in config:
            raise ValueError(
                f"ソース設定 {path.name} の名前が Global 設定のキーと重複する"
            )
        config[path.stem] = _load_toml(path)
    return config


def _range_result(
    start_day: date, end_day: date, zone: ZoneInfo
) -> tuple[datetime, datetime, str]:
    if start_day > end_day:
        raise ValueError("開始日は終了日以前でなければならない")
    if end_day - start_day == timedelta(days=6) and start_day.weekday() == 0:
        year, week, _ = start_day.isocalendar()
        suffix = f"{year}-w{week:02d}"
    else:
        suffix = f"{start_day:%Y%m%d}-{end_day:%Y%m%d}"
    return (
        datetime.combine(start_day, time.min, tzinfo=zone),
        datetime.combine(end_day, time.max, tzinfo=zone),
        suffix,
    )


def get_target_range(
    config: dict[str, Any],
    target_week: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    ref_date: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> tuple[datetime, datetime, str]:
    zone = tz or TOKYO_TZ
    now = (ref_date or datetime.now(zone)).astimezone(zone)

    if bool(from_date) != bool(to_date):
        raise ValueError("--from と --to は同時に指定する")
    if from_date and to_date:
        try:
            return _range_result(
                date.fromisoformat(from_date), date.fromisoformat(to_date), zone
            )
        except ValueError as error:
            raise ValueError("--from と --to は YYYY-MM-DD 形式で指定する") from error

    if target_week:
        if target_week == "current":
            monday = now.date() - timedelta(days=now.weekday())
        elif target_week == "last":
            monday = now.date() - timedelta(days=now.weekday() + 7)
        elif re.fullmatch(r"\d{4}-W\d{2}", target_week):
            year, week = map(int, target_week.split("-W"))
            try:
                monday = date.fromisocalendar(year, week, 1)
            except ValueError as error:
                raise ValueError("存在しない ISO 週が指定された") from error
        else:
            raise ValueError(
                "--target-week は last、current、YYYY-Www のいずれかで指定する"
            )
        return _range_result(monday, monday + timedelta(days=6), zone)

    period = config.get("period", {})
    window_days = int(period.get("window_days", 7))
    delay_days = int(period.get("delay_days", 0))
    if window_days < 1 or delay_days < 0:
        raise ValueError(
            "period.window_days は1以上、period.delay_days は0以上で指定する"
        )
    end_day = now.date() - timedelta(days=delay_days + 1)
    return _range_result(end_day - timedelta(days=window_days - 1), end_day, zone)


def get_target_week_range(
    target_week: str | None = None,
    ref_date: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> tuple[datetime, datetime, str]:
    return get_target_range({}, target_week or "last", ref_date=ref_date, tz=tz)


def resolve_model(config: dict[str, Any], stage_key: str | None = None) -> str:
    gemini_config = config.get("gemini", {})
    global_model = os.getenv("GEMINI_MODEL") or gemini_config.get(
        "model", "gemini-3.7-flash"
    )
    return (
        str(gemini_config.get(stage_key, global_model))
        if stage_key
        else str(global_model)
    )


def format_article_title(start_dt: datetime, end_dt: datetime) -> str:
    return f"{start_dt:%Y-%m-%d}〜{end_dt:%Y-%m-%d}の活動記録"


def load_article_template(template_path: str | Path) -> str:
    path = Path(template_path)
    if not path.is_file():
        raise FileNotFoundError(f"記事テンプレートが見つからない: {path}")
    template = path.read_text(encoding="utf-8")
    if CONTENT_PLACEHOLDER not in template:
        raise ValueError(f"記事テンプレートに {CONTENT_PLACEHOLDER} がない: {path}")
    return template


def render_article_body(template: str, content: str) -> str:
    return template.replace(CONTENT_PLACEHOLDER, content.strip())


def format_article_markdown(title: str, body: str) -> str:
    # WordPress はタイトルを別フィールドで表示するため、タイトル見出しはローカル出力にだけ付ける
    return f"# {title}\n\n{body}"


def publish_wordpress_draft(
    wp_url: str,
    username: str,
    app_password: str,
    slug: str,
    title: str,
    content: str,
    post_defaults: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    base = wp_url.rstrip("/")
    if base.endswith("/wp-json"):
        base = f"{base}/wp/v2"
    elif not base.endswith("/wp/v2"):
        base = f"{base}/wp-json/wp/v2"
    http_client = client or httpx.Client(timeout=20.0)
    try:
        response = http_client.get(
            f"{base}/posts?slug={slug}&status=any", auth=(username, app_password)
        )
        response.raise_for_status()
        result: Any = response.json()
        posts = cast(list[dict[str, Any]], result) if isinstance(result, list) else []
        # 人が公開・予約した記事を下書きへ戻して編集内容を失わないよう、上書きせず止める
        if posts and posts[0].get("status") != "draft":
            raise RuntimeError(
                f"slug {slug} の投稿は既に {posts[0].get('status')} 状態のため更新しない"
            )
        payload = {
            key: value
            for key, value in (post_defaults or {}).items()
            if key in WORDPRESS_DEFAULT_FIELDS
        }
        payload.update(
            {
                "title": title,
                "content": markdown.markdown(content),
                "status": "draft",
            }
        )
        url = f"{base}/posts/{posts[0]['id']}" if posts else f"{base}/posts"
        if not posts:
            payload["slug"] = slug
        response = http_client.post(url, auth=(username, app_password), json=payload)
        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"WordPress API error {response.status_code}: {response.text}"
            )
        return response.json()
    finally:
        if client is None:
            http_client.close()


def dump_intermediate(dump_dir: str | None, name: str, text: str) -> None:
    if dump_dir is None:
        return
    path = Path(dump_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_pipeline(
    config_dir: str = "config",
    target_week: str | None = None,
    publish: bool = False,
    output_override: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    dump_dir: str | None = None,
    ref_date: datetime | None = None,
    genai_client: Any | None = None,
    http_client: httpx.Client | None = None,
) -> str | None:
    with pipeline_stage("setup"):
        load_env()
        config = load_config(config_dir)
        try:
            timezone = ZoneInfo(str(config.get("timezone", "Asia/Tokyo")))
        except ZoneInfoNotFoundError:
            logger.warning("未知のタイムゾーン。Asia/Tokyo を使用する")
            timezone = TOKYO_TZ

        start_dt, end_dt, slug_suffix = get_target_range(
            config, target_week, from_date, to_date, ref_date, timezone
        )
        wordpress_config = config.get("wordpress", {})
        weekly_slug = (
            f"{wordpress_config.get('slug_prefix', 'activity-digest')}-{slug_suffix}"
        )
        title = format_article_title(start_dt, end_dt)
        article_template = load_article_template(
            config.get("article", {}).get("template", "templates/article.md")
        )

    with pipeline_stage("collect"):
        documents: list[SourceDocument] = []
        for collector in (github, notion):
            documents.extend(
                collector.collect(config, start_dt, end_dt, client=http_client)
            )
    if not documents:
        logger.info("No activity records found for %s. Skipping.", slug_suffix)
        return None
    for document in documents:
        logger.info(
            "Source %s: %d records collected", document.name, len(document.records)
        )
    dump_intermediate(
        dump_dir,
        "sources.md",
        "\n\n".join(document.to_markdown() for document in documents),
    )

    prompt_config = config.get("prompts", {})
    with pipeline_stage("stage1-inspector"):
        ai_client = genai_client or genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        material = run_stage1_inspector(
            ai_client,
            resolve_model(config, "stage1_model"),
            documents,
            prompt_path=prompt_config.get("inspector", "prompts/inspector.md"),
        )
    dump_intermediate(dump_dir, "stage1.md", material)
    if not material:
        logger.info("Stage 1 kept no publishable material. Skipping.")
        return None
    logger.info("Stage 1 completed")
    with pipeline_stage("stage2-writer"):
        draft = run_stage2_writer(
            ai_client,
            resolve_model(config, "stage2_model"),
            material,
            f"{start_dt:%Y-%m-%d}〜{end_dt:%Y-%m-%d}",
            prompt_path=prompt_config.get("writer", "prompts/writer.md"),
        )
    dump_intermediate(dump_dir, "stage2.md", draft)
    logger.info("Stage 2 completed")
    with pipeline_stage("stage3-editor"):
        content = run_stage3_editor(
            ai_client,
            resolve_model(config, "stage3_model"),
            draft,
            prompt_path=prompt_config.get("editor", "prompts/editor.md"),
        )
    dump_intermediate(dump_dir, "stage3.md", content)
    logger.info("Stage 3 completed")
    body = render_article_body(article_template, content)
    final_markdown = format_article_markdown(title, body)

    with pipeline_stage("write-output"):
        output_path = Path(
            output_override
            or (Path(config.get("output_dir", "artifacts")) / f"{weekly_slug}.md")
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(final_markdown, encoding="utf-8")

    if publish:
        with pipeline_stage("publish"):
            wp_url = os.getenv("WORDPRESS_URL")
            wp_user = os.getenv("WORDPRESS_USERNAME")
            wp_password = os.getenv("WORDPRESS_APP_PASSWORD")
            if not (wp_url and wp_user and wp_password):
                raise ValueError("WordPress credentials not configured")
            publish_wordpress_draft(
                wp_url,
                wp_user,
                wp_password,
                weekly_slug,
                title,
                body,
                wordpress_config.get("post_defaults", {}),
                client=http_client,
            )
        logger.info("WordPress draft saved")
    return final_markdown


def report_failure(error: Exception, detailed: bool) -> None:
    stage = error.stage if isinstance(error, PipelineStageError) else "unknown"
    cause = error.__cause__ if isinstance(error, PipelineStageError) else error
    if detailed:
        logger.error("Pipeline failed at %s", stage, exc_info=cause)
        return
    # 公開される Actions のログには API の応答本文や活動内容を含み得る例外メッセージを出さない
    logger.error(
        "Pipeline failed at %s: %s", stage, type(cause).__name__ if cause else "-"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Activity Digest Pipeline")
    parser.add_argument(
        "--config-dir",
        default="config",
        help="global.toml と sources/*.toml を置くディレクトリ",
    )
    parser.add_argument("--target-week", default=None)
    parser.add_argument("--from", dest="from_date", default=None)
    parser.add_argument("--to", dest="to_date", default=None)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--output", default=None)
    parser.add_argument(
        "--dump-dir",
        default=None,
        help="Source document と各段階の出力を書き出すディレクトリ。ローカル確認用",
    )
    args = parser.parse_args()
    try:
        run_pipeline(
            args.config_dir,
            args.target_week,
            args.publish,
            args.output,
            args.from_date,
            args.to_date,
            dump_dir=args.dump_dir,
        )
    except Exception as error:
        report_failure(error, detailed=os.getenv("GITHUB_ACTIONS") != "true")
        sys.exit(1)


if __name__ == "__main__":
    main()
