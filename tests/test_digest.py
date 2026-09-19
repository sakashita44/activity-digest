import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import httpx

from activity_digest.ai import (
    render_prompt,
    run_stage1_inspector,
    run_stage2_writer,
    run_stage3_editor,
)
from activity_digest.app import (
    FIXED_DISCLOSURE,
    format_article_markdown,
    get_target_range,
    load_config,
    publish_wordpress_draft,
    resolve_model,
    run_pipeline,
)
from activity_digest.sources import (
    COORD_KEYS,
    collect_github_events,
    collect_notion_events,
    ensure_no_coordinates,
    sanitize_and_order_events,
)

TOKYO_TZ = ZoneInfo("Asia/Tokyo")


def config_text(output_dir: str = "artifacts") -> str:
    return f'''timezone = "Asia/Tokyo"
output_dir = "{output_dir.replace("\\", "/")}"
disclosure = ""
local_files = []
default_location_label = "外出先"

[period]
days = 7
end_offset_days = 1

[gemini]
model = "gemini-3.7-flash"

[github]
username = "tester"
blacklist = ["secret-org/private-repo", "my-org/core-secret"]

[notion]
data_source_ids = ["ds-1", "ds-2"]

[wordpress]
slug_prefix = "activity-digest"

[wordpress.post_defaults]
categories = [3]
comment_status = "closed"

[[locations]]
name = "オフィス"
label = "都内オフィス街"
lat = 35.6812
lon = 139.7671
radius_meters = 1000
'''


class TestDigestPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "timezone": "Asia/Tokyo",
            "period": {"days": 7, "end_offset_days": 1},
            "gemini": {"model": "gemini-3.7-flash"},
            "github": {
                "username": "tester",
                "blacklist": ["secret-org/private-repo", "my-org/core-secret"],
            },
            "notion": {"data_source_ids": ["ds-1", "ds-2"]},
            "locations": [
                {
                    "name": "オフィス",
                    "label": "都内オフィス街",
                    "lat": 35.6812,
                    "lon": 139.7671,
                    "radius_meters": 1000,
                }
            ],
            "default_location_label": "外出先",
            "wordpress": {"slug_prefix": "activity-digest"},
        }

    def test_toml_config_and_period_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(config_text(), encoding="utf-8")
            self.assertEqual(load_config(path)["period"]["days"], 7)

        reference = datetime(2026, 9, 7, 12, tzinfo=TOKYO_TZ)
        start, end, suffix = get_target_range(self.config, ref_date=reference)
        self.assertEqual(start.date().isoformat(), "2026-08-31")
        self.assertEqual(end.date().isoformat(), "2026-09-06")
        self.assertEqual(suffix, "2026-w36")

        start, end, suffix = get_target_range(
            self.config,
            target_week="2026-W01",
            from_date="2026-08-02",
            to_date="2026-08-04",
            ref_date=reference,
        )
        self.assertEqual(
            (start.date().isoformat(), end.date().isoformat()),
            ("2026-08-02", "2026-08-04"),
        )
        self.assertEqual(suffix, "20260802-20260804")
        with self.assertRaises(ValueError):
            get_target_range(self.config, from_date="2026-08-02")

    def test_collection_filters(self) -> None:
        github_data = [
            {
                "id": "1",
                "type": "PushEvent",
                "created_at": "2026-09-02T10:00:00Z",
                "repo": {"name": "my-org/public-project"},
                "payload": {"action": "push"},
            },
            {
                "id": "2",
                "type": "PushEvent",
                "created_at": "2026-09-02T11:00:00Z",
                "repo": {"name": "secret-org/private-repo"},
                "payload": {"action": "push"},
            },
            {
                "id": "3",
                "type": "PushEvent",
                "created_at": "2026-09-02T12:00:00Z",
                "repo": {"name": "my-org/CORE-SECRET"},
                "payload": {"action": "push"},
            },
        ]
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=github_data)
            )
        )
        start = datetime(2026, 9, 1, tzinfo=TOKYO_TZ)
        end = datetime(2026, 9, 7, 23, 59, tzinfo=TOKYO_TZ)
        events = collect_github_events(self.config, start, end, client=client)
        self.assertEqual([event["id"] for event in events], ["gh-1"])

        queried: list[str] = []

        def notion_handler(request: httpx.Request) -> httpx.Response:
            queried.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "p1",
                            "created_time": "2026-09-02T15:00:00Z",
                            "properties": {
                                "Name": {
                                    "type": "title",
                                    "title": [{"plain_text": "Book"}],
                                },
                                "Link": {"type": "url", "url": "https://example.com"},
                            },
                        }
                    ]
                },
            )

        notion_client = httpx.Client(transport=httpx.MockTransport(notion_handler))
        with patch.dict(os.environ, {"NOTION_API_KEY": "fake-key"}):
            notion_events = collect_notion_events(
                self.config,
                start,
                end,
                client=notion_client,
                requested_ids=["ds-1", "ds-unauthorized"],
            )
        self.assertEqual(len(notion_events), 1)
        self.assertTrue(any("ds-1" in url for url in queried))
        self.assertFalse(any("ds-unauthorized" in url for url in queried))

    def test_coordinate_redaction(self) -> None:
        raw_events = [
            {
                "id": "loc-1",
                "timestamp": "2026-09-02T12:00:00+09:00",
                "location": {"lat": 35.6813, "lon": 139.7670},
                "title": "出社",
                "details": "座標 35.6813, 139.7670 付近",
            },
            {
                "id": "loc-2",
                "timestamp": "2026-09-03T18:00:00+09:00",
                "location": {"lat": 34.6937, "lon": 135.5023},
                "title": "出張",
                "details": "移動",
            },
        ]
        sanitized = sanitize_and_order_events(raw_events, self.config)
        self.assertEqual(sanitized[0]["location_label"], "都内オフィス街")
        self.assertEqual(sanitized[1]["location_label"], "外出先")
        self.assertIn("[座標マスク]", sanitized[0]["details"])
        self.assertTrue(
            all(key not in event for event in sanitized for key in COORD_KEYS)
        )
        ensure_no_coordinates(sanitized)

    def test_coordinates_in_non_string_values_are_redacted(self) -> None:
        raw_events = [
            {
                "id": "loc-3",
                "timestamp": "2026-09-02T12:00:00+09:00",
                "title": ["散歩", 35.681234],
                "details": {"lat": 35.681234, "lon": 139.767123},
            }
        ]
        sanitized = sanitize_and_order_events(raw_events, self.config)
        self.assertNotIn("35.681234", json.dumps(sanitized))
        self.assertNotIn("139.767123", json.dumps(sanitized))
        ensure_no_coordinates(sanitized)
        with self.assertRaises(ValueError):
            ensure_no_coordinates([{"id": "x", "details": {"note": 35.681234}}])

    def test_event_id_boundaries(self) -> None:
        client = MagicMock()
        response = MagicMock()
        client.models.generate_content.return_value = response
        events = [
            {"id": "ev-1", "title": "Task", "timestamp": "2026-09-02T10:00:00+09:00"}
        ]

        response.parsed = {"events": [{"id": "ev-fake", "title": "Fake"}]}
        with self.assertRaises(ValueError):
            run_stage1_inspector(client, "model", events)
        response.parsed = {
            "title": "T",
            "summary": "S",
            "sections": [{"heading": "H", "content": "C", "event_ids": ["ev-fake"]}],
        }
        with self.assertRaises(ValueError):
            run_stage2_writer(client, "model", events, "2026-w36")
        draft = {
            "title": "T",
            "summary": "S",
            "sections": [{"heading": "H", "content": "C", "event_ids": ["ev-1"]}],
        }
        response.parsed = {
            "title": "T2",
            "summary": "S2",
            "sections": [
                {"heading": "H", "content": "C", "event_ids": ["ev-1", "ev-added"]}
            ],
        }
        with self.assertRaises(ValueError):
            run_stage3_editor(client, "model", draft)

    def test_pipeline_calls_three_agents_and_requires_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text(
                config_text(str(root / "artifacts")), encoding="utf-8"
            )
            github_item = [
                {
                    "id": "10",
                    "type": "PushEvent",
                    "created_at": "2026-09-02T10:00:00Z",
                    "repo": {"name": "my-org/public-project"},
                    "payload": {"action": "push"},
                }
            ]
            http_client = httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json=github_item
                        if "api.github.com" in str(request.url)
                        else {"results": []},
                    )
                )
            )
            ai_client = MagicMock()
            responses = [MagicMock(), MagicMock(), MagicMock()]
            responses[0].parsed = {
                "events": [{"id": "gh-10", "title": "Push", "source": "github"}]
            }
            responses[1].parsed = {
                "title": "草案",
                "summary": "概要",
                "sections": [
                    {"heading": "開発", "content": "作業内容", "event_ids": ["gh-10"]}
                ],
            }
            responses[2].parsed = {
                "title": "推敲後草案",
                "summary": "改善概要",
                "sections": [
                    {
                        "heading": "開発",
                        "content": "洗練された作業内容",
                        "event_ids": ["gh-10"],
                    }
                ],
            }
            ai_client.models.generate_content.side_effect = responses

            result = run_pipeline(
                config_path=str(config_path),
                target_week="2026-W36",
                genai_client=ai_client,
                http_client=http_client,
            )
            self.assertEqual(ai_client.models.generate_content.call_count, 3)
            self.assertIn(FIXED_DISCLOSURE, result or "")
            self.assertTrue(
                (root / "artifacts" / "activity-digest-2026-w36.md").is_file()
            )

    def test_empty_period_skips_gemini(self) -> None:
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))
        )
        ai_client = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(config_text(), encoding="utf-8")
            result = run_pipeline(
                str(config_path), "2026-W36", genai_client=ai_client, http_client=client
            )
        self.assertIsNone(result)
        ai_client.models.generate_content.assert_not_called()

    def test_wordpress_defaults_are_allowlisted_and_draft_is_fixed(self) -> None:
        created: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=[])
            data = json.loads(request.content.decode("utf-8"))
            created.append(data)
            return httpx.Response(201, json={"id": 100, **data})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = publish_wordpress_draft(
            "https://example.com",
            "u",
            "p",
            "slug-new",
            "T1",
            "## C1",
            {
                "categories": [3],
                "status": "publish",
                "slug": "wrong",
                "unknown": "ignored",
            },
            client=client,
        )
        self.assertEqual(result["id"], 100)
        self.assertEqual(created[0]["status"], "draft")
        self.assertEqual(created[0]["slug"], "slug-new")
        self.assertEqual(created[0]["categories"], [3])
        self.assertNotIn("unknown", created[0])

    def test_wordpress_refuses_to_overwrite_published_post(self) -> None:
        posted: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 7, "status": "publish"}])
            posted.append(request)
            return httpx.Response(200, json={"id": 7})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(RuntimeError):
            publish_wordpress_draft(
                "https://example.com/wp-json/",
                "u",
                "p",
                "slug-published",
                "T",
                "C",
                client=client,
            )
        self.assertEqual(posted, [])

    def test_prompt_model_and_disclosure_configuration(self) -> None:
        rendered = render_prompt("{{week}} {{events}}", {"week": "W", "events": "[]"})
        self.assertEqual(rendered, "W []")
        config = {"gemini": {"model": "global", "stage1_model": "inspector"}}
        self.assertEqual(resolve_model(config, "stage1_model"), "inspector")
        self.assertEqual(resolve_model(config, "stage2_model"), "global")
        markdown_text = format_article_markdown(
            {"title": "T", "summary": "S", "sections": []}, ""
        )
        self.assertIn(FIXED_DISCLOSURE, markdown_text)


if __name__ == "__main__":
    unittest.main()
