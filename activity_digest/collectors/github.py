import logging
import os
from collections import Counter
from datetime import datetime
from typing import Any, cast

import httpx

from activity_digest.collectors import SourceDocument

GITHUB_CONTEXT = """このソースは、本人の GitHub アカウントで発生した操作の履歴である。
リポジトリごとに、対象期間内の操作を日付と種類ごとの件数で並べている。
操作には閲覧やスターなど作業を伴わないものも含まれるため、開発活動を推測する手がかりとして扱う。"""
GITHUB_EVENTS_PER_PAGE = 100
# GitHub の events API は直近300件までしか返さず、それ以降のページは 422 になる
GITHUB_EVENTS_MAX_PAGES = 3
logger = logging.getLogger("activity-digest")


def collect(
    config: dict[str, Any],
    start_dt: datetime,
    end_dt: datetime,
    client: httpx.Client | None = None,
) -> list[SourceDocument]:
    cfg = cast(dict[str, Any], config.get("github", {}))
    username = cfg.get("username")
    allowlist = {a.strip().lower() for a in cfg.get("allowlist", [])}
    blacklist = {b.strip().lower() for b in cfg.get("blacklist", [])}
    if not username:
        return []
    token = os.getenv("GH_ACTIVITY_TOKEN") or os.getenv("GITHUB_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "activity-digest",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    tz = start_dt.tzinfo
    c = client or httpx.Client(timeout=15.0)
    activities: dict[str, Counter[tuple[str, str]]] = {}
    try:
        for item in _fetch_github_events(c, str(username), headers, start_dt):
            if not item.get("created_at"):
                continue
            dt = datetime.fromisoformat(
                item["created_at"].replace("Z", "+00:00")
            ).astimezone(tz)
            repo_value = item.get("repo", {})
            repo_info = (
                cast(dict[str, Any], repo_value) if isinstance(repo_value, dict) else {}
            )
            repo = str(repo_info.get("name", ""))
            repo_key = repo.strip().lower()
            if (
                not (start_dt <= dt <= end_dt)
                or (allowlist and repo_key not in allowlist)
                or repo_key in blacklist
            ):
                continue
            event_payload_value = item.get("payload", {})
            event_payload = (
                cast(dict[str, Any], event_payload_value)
                if isinstance(event_payload_value, dict)
                else {}
            )
            etype = str(item.get("type", "Activity"))
            action = str(event_payload.get("action", ""))
            activities.setdefault(repo, Counter())[
                (f"{dt:%Y-%m-%d}", f"{etype} {action}".strip())
            ] += 1
    finally:
        if client is None:
            c.close()
    if not activities:
        return []
    records = [
        "\n".join(
            [f"### {repo}", ""]
            + [
                f"- {day}: {kind} ×{count}"
                for (day, kind), count in sorted(counts.items())
            ]
        )
        for repo, counts in sorted(activities.items())
    ]
    return [SourceDocument("GitHub", GITHUB_CONTEXT, records)]


def _fetch_github_events(
    client: httpx.Client,
    username: str,
    headers: dict[str, str],
    start_dt: datetime,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(1, GITHUB_EVENTS_MAX_PAGES + 1):
        resp = client.get(
            f"https://api.github.com/users/{username}/events",
            params={"per_page": GITHUB_EVENTS_PER_PAGE, "page": page},
            headers=headers,
        )
        resp.raise_for_status()
        raw_payload: Any = resp.json()
        page_items = (
            cast(list[dict[str, Any]], raw_payload)
            if isinstance(raw_payload, list)
            else []
        )
        items.extend(page_items)
        # イベントは新しい順に返るため、期間開始より古い時刻に届いたら以降のページは不要
        oldest = page_items[-1].get("created_at") if page_items else None
        if len(page_items) < GITHUB_EVENTS_PER_PAGE or (
            oldest
            and datetime.fromisoformat(str(oldest).replace("Z", "+00:00")) < start_dt
        ):
            break
    else:
        logger.warning(
            "GitHub の取得上限 %d 件に達しても期間の開始まで遡れなかった。"
            "それより古い対象期間のイベントは収集できていない",
            GITHUB_EVENTS_PER_PAGE * GITHUB_EVENTS_MAX_PAGES,
        )
    return items
