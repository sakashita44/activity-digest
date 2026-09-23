import os
from datetime import datetime
from typing import Any, cast

import httpx

from activity_digest.collectors import SourceDocument

NOTION_CONTEXT = """このソースは、本人が Notion の許可されたデータベースへ対象期間内に作成したページの一覧である。
メモ、作業記録、後で読むために保存した記事などが混在する。
URL を持つページは保存した情報である場合があり、実施した活動とは限らない。"""


def collect(
    config: dict[str, Any],
    start_dt: datetime,
    end_dt: datetime,
    client: httpx.Client | None = None,
) -> list[SourceDocument]:
    notion_config = cast(dict[str, Any], config.get("notion", {}))
    target_ids = [str(value) for value in notion_config.get("data_source_ids", [])]
    api_key = os.getenv("NOTION_API_KEY")
    if not target_ids or not api_key:
        return []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": "2026-03-11",
        "Content-Type": "application/json",
    }
    tz = start_dt.tzinfo
    c = client or httpx.Client(timeout=15.0)
    pages: list[tuple[datetime, str]] = []
    try:
        for ds_id in target_ids:
            for item in _query_notion_data_source(c, ds_id, headers, start_dt, end_dt):
                ts = item.get("created_time") or item.get("last_edited_time")
                if not ts:
                    continue
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
                if not (start_dt <= dt <= end_dt):
                    continue
                pages.append((dt, _format_page(item, dt)))
    finally:
        if client is None:
            c.close()
    if not pages:
        return []
    return [
        SourceDocument("Notion", NOTION_CONTEXT, [text for _, text in sorted(pages)])
    ]


def _format_page(item: dict[str, Any], created: datetime) -> str:
    title = "Notion item"
    # ページ自体の url は常に存在する非公開の notion.so URL のため使わない
    url: str | None = None
    properties_value = item.get("properties", {})
    properties = (
        cast(dict[str, dict[str, Any]], properties_value)
        if isinstance(properties_value, dict)
        else {}
    )
    for p in properties.values():
        if p.get("type") == "title" and p.get("title"):
            title_items = cast(list[dict[str, Any]], p["title"])
            title = "".join(str(t.get("plain_text", "")) for t in title_items)
        elif p.get("type") == "url" and p.get("url"):
            url = str(p["url"])
    lines = [f"### {title}", "", f"- Created: {created:%Y-%m-%d}"]
    if url:
        lines.append(f"- URL: {url}")
    return "\n".join(lines)


def _query_notion_data_source(
    client: httpx.Client,
    ds_id: str,
    headers: dict[str, str],
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict[str, Any]]:
    body: dict[str, Any] = {
        "filter": {
            "and": [
                {
                    "timestamp": "created_time",
                    "created_time": {"on_or_after": start_dt.isoformat()},
                },
                {
                    "timestamp": "created_time",
                    "created_time": {"on_or_before": end_dt.isoformat()},
                },
            ]
        },
        "page_size": 100,
    }
    results: list[dict[str, Any]] = []
    while True:
        resp = client.post(
            f"https://api.notion.com/v1/data_sources/{ds_id}/query",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        raw_payload: Any = resp.json()
        payload = (
            cast(dict[str, Any], raw_payload) if isinstance(raw_payload, dict) else {}
        )
        results_value = payload.get("results", [])
        if isinstance(results_value, list):
            results.extend(cast(list[dict[str, Any]], results_value))
        next_cursor = payload.get("next_cursor")
        if not payload.get("has_more") or not next_cursor:
            return results
        body["start_cursor"] = next_cursor
