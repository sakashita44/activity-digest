import json
import logging
import math
import os
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx

COORD_REGEX = re.compile(r"-?\b\d{1,3}\.\d{4,}\b")
COORD_KEYS = (
    "location",
    "lat",
    "lon",
    "latitude",
    "longitude",
    "coords",
    "coordinates",
)
EVENT_FIELDS = (
    "id",
    "source",
    "timestamp",
    "category",
    "title",
    "details",
    "url",
    "location_label",
)
GITHUB_EVENTS_PER_PAGE = 100
# GitHub の events API は直近300件までしか返さず、それ以降のページは 422 になる
GITHUB_EVENTS_MAX_PAGES = 3
logger = logging.getLogger("activity-digest")


def get_timezone(tz_input: Any = None) -> timezone | ZoneInfo:
    if isinstance(tz_input, (timezone, ZoneInfo)):
        return tz_input
    name = str(tz_input) if tz_input else "Asia/Tokyo"
    try:
        return ZoneInfo(name)
    except Exception:
        return (
            timezone(timedelta(hours=9), name="Asia/Tokyo")
            if name in ("Asia/Tokyo", "JST")
            else UTC
        )


TOKYO_TZ = get_timezone("Asia/Tokyo")


def haversine_distance_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    p1, p2, dp, dl = (
        math.radians(lat1),
        math.radians(lat2),
        math.radians(lat2 - lat1),
        math.radians(lon2 - lon1),
    )
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * 6371000.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def sanitize_and_order_events(
    events: list[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    locs = cast(list[dict[str, Any]], config.get("locations", []))
    default_label = str(config.get("default_location_label", "外出先"))
    sanitized: list[dict[str, Any]] = []
    for ev in events:
        item = dict(ev)
        location_value = item.get("location")
        loc = (
            cast(dict[str, Any], location_value)
            if isinstance(location_value, dict)
            else None
        )
        lat = (
            loc.get("lat") or loc.get("latitude")
            if loc is not None
            else (item.get("lat") or item.get("latitude"))
        )
        lon = (
            loc.get("lon") or loc.get("longitude")
            if loc is not None
            else (item.get("lon") or item.get("longitude"))
        )
        if lat is not None and lon is not None:
            label = default_label
            try:
                f_lat, f_lon = float(lat), float(lon)
                for reg in locs:
                    if haversine_distance_meters(
                        f_lat, f_lon, float(reg["lat"]), float(reg["lon"])
                    ) <= float(reg.get("radius_meters", 1000)):
                        label = reg.get("label", default_label)
                        break
            except (ValueError, TypeError):
                pass
            item["location_label"] = label
        kept: dict[str, Any] = {}
        for k, v in item.items():
            if k not in EVENT_FIELDS or v is None:
                continue
            # 数値や入れ子の値に含まれる座標もマスクできるよう、文字列化してから検査する
            text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            kept[k] = COORD_REGEX.sub("[座標マスク]", text)
        sanitized.append(kept)
    sanitized.sort(key=_event_sort_key)
    return sanitized


def _event_sort_key(event: dict[str, Any]) -> tuple[bool, datetime, str]:
    # 出典ごとに UTC オフセットが異なり得るため、文字列ではなく時刻として比較する
    try:
        moment = datetime.fromisoformat(str(event["timestamp"]))
    except (KeyError, ValueError):
        return (True, datetime.min.replace(tzinfo=UTC), str(event.get("id", "")))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (False, moment, str(event.get("id", "")))


def _contains_coordinates(value: Any) -> bool:
    if isinstance(value, str):
        return bool(COORD_REGEX.search(value))
    if isinstance(value, dict):
        mapping = cast(dict[Any, Any], value)
        return any(k in COORD_KEYS for k in mapping) or any(
            _contains_coordinates(v) for v in mapping.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_coordinates(v) for v in cast(list[Any], value))
    if isinstance(value, float):
        return bool(COORD_REGEX.search(repr(value)))
    return False


def ensure_no_coordinates(events: list[dict[str, Any]]) -> None:
    for ev in events:
        if any(k in ev for k in COORD_KEYS):
            raise ValueError("Raw coordinate key detected")
        if any(_contains_coordinates(v) for v in ev.values()):
            raise ValueError("Raw coordinate pattern detected in text")


def collect_github_events(
    config: dict[str, Any],
    start_dt: datetime,
    end_dt: datetime,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    cfg = cast(dict[str, Any], config.get("github", {}))
    username, blacklist = (
        cfg.get("username"),
        {b.strip().lower() for b in cfg.get("blacklist", [])},
    )
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
    tz = start_dt.tzinfo or get_timezone(config.get("timezone", "Asia/Tokyo"))
    c = client or httpx.Client(timeout=15.0)
    events: list[dict[str, Any]] = []
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
            if not (start_dt <= dt <= end_dt) or repo.strip().lower() in blacklist:
                continue
            event_payload_value = item.get("payload", {})
            event_payload = (
                cast(dict[str, Any], event_payload_value)
                if isinstance(event_payload_value, dict)
                else {}
            )
            etype = str(item.get("type", "Activity"))
            action = str(event_payload.get("action", ""))
            events.append(
                {
                    "id": f"gh-{item.get('id', len(events))}",
                    "source": "github",
                    "timestamp": dt.isoformat(timespec="seconds"),
                    "category": "development",
                    "title": f"{etype}: {repo}",
                    "details": f"{etype} {action}".strip(),
                    "url": f"https://github.com/{repo}",
                }
            )
    finally:
        if client is None:
            c.close()
    return events


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
    return items


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


def collect_notion_events(
    config: dict[str, Any],
    start_dt: datetime,
    end_dt: datetime,
    client: httpx.Client | None = None,
    requested_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    notion_config = cast(dict[str, Any], config.get("notion", {}))
    allowed = {str(value) for value in notion_config.get("data_source_ids", [])}
    target_ids = [
        d for d in (allowed if requested_ids is None else requested_ids) if d in allowed
    ]
    api_key = os.getenv("NOTION_API_KEY")
    if not target_ids or not api_key:
        return []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": "2026-03-11",
        "Content-Type": "application/json",
    }
    tz = start_dt.tzinfo or get_timezone(config.get("timezone", "Asia/Tokyo"))
    c = client or httpx.Client(timeout=15.0)
    events: list[dict[str, Any]] = []
    try:
        for ds_id in target_ids:
            for item in _query_notion_data_source(c, ds_id, headers, start_dt, end_dt):
                ts = item.get("created_time") or item.get("last_edited_time")
                if not ts:
                    continue
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
                if not (start_dt <= dt <= end_dt):
                    continue
                title = "Notion item"
                # ページ自体の url は常に存在する非公開の notion.so URL のため使わない
                url: Any = None
                properties_value = item.get("properties", {})
                properties = (
                    cast(dict[str, dict[str, Any]], properties_value)
                    if isinstance(properties_value, dict)
                    else {}
                )
                for p in properties.values():
                    if p.get("type") == "title" and p.get("title"):
                        title_items = cast(list[dict[str, Any]], p["title"])
                        title = "".join(
                            str(t.get("plain_text", "")) for t in title_items
                        )
                    elif p.get("type") == "url" and p.get("url"):
                        url = p.get("url")
                events.append(
                    {
                        "id": f"notion-{item.get('id', '')}",
                        "source": "notion",
                        "timestamp": dt.isoformat(timespec="seconds"),
                        "category": "reading" if url else "note",
                        "title": title,
                        "details": "",
                        "url": url,
                    }
                )
    finally:
        if client is None:
            c.close()
    return events


def load_local_events(
    file_paths: list[str], start_dt: datetime, end_dt: datetime, tz: Any = None
) -> list[dict[str, Any]]:
    zone = get_timezone(tz) if tz else (start_dt.tzinfo or TOKYO_TZ)
    events: list[dict[str, Any]] = []
    for fp in file_paths:
        p = Path(fp)
        if not p.is_file():
            continue
        try:
            raw_items: Any = json.loads(p.read_text(encoding="utf-8"))
            items = (
                cast(list[dict[str, Any]], raw_items)
                if isinstance(raw_items, list)
                else []
            )
            for idx, item in enumerate(items):
                ts = item.get("timestamp")
                normalized: dict[str, Any] = {}
                if ts:
                    moment = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    # オフセットなしの時刻は実行環境 (Actions では UTC) ではなく設定のタイムゾーンで解釈する
                    if moment.tzinfo is None:
                        moment = moment.replace(tzinfo=zone)
                    moment = moment.astimezone(zone)
                    if not (start_dt <= moment <= end_dt):
                        continue
                    normalized["timestamp"] = moment.isoformat(timespec="seconds")
                events.append(
                    {
                        **item,
                        **normalized,
                        "id": item.get("id") or f"local-{idx}",
                        "source": item.get("source", "local"),
                        "category": item.get("category", "activity"),
                    }
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            logger.warning("Failed to load local activity file %s: %s", fp, error)
    return events
