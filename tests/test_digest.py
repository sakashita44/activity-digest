import json
import logging
import os
import tempfile
import unittest
from datetime import datetime
from logging.handlers import BufferingHandler
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import httpx

from activity_digest.ai import (
    render_prompt,
    run_stage1_inspector,
    run_stage3_editor,
    strip_code_fence,
)
from activity_digest.app import (
    FIXED_DISCLOSURE,
    PipelineStageError,
    format_article_markdown,
    format_article_title,
    get_target_range,
    load_config,
    publish_wordpress_draft,
    report_failure,
    resolve_model,
    run_pipeline,
)
from activity_digest.collectors import SourceDocument, github, notion

TOKYO_TZ = ZoneInfo("Asia/Tokyo")
PERIOD_START = datetime(2026, 9, 1, tzinfo=TOKYO_TZ)
PERIOD_END = datetime(2026, 9, 7, 23, 59, tzinfo=TOKYO_TZ)


def config_text(output_dir: str = "artifacts") -> str:
    return f'''timezone = "Asia/Tokyo"
output_dir = "{output_dir.replace("\\", "/")}"
disclosure = ""

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
'''


def github_event(event_id: str, repo: str, created_at: str) -> dict[str, object]:
    return {
        "id": event_id,
        "type": "PushEvent",
        "created_at": created_at,
        "repo": {"name": repo},
        "payload": {},
    }


def ai_responses(*texts: str) -> MagicMock:
    client = MagicMock()
    responses: list[MagicMock] = []
    for text in texts:
        response = MagicMock()
        response.text = text
        responses.append(response)
    client.models.generate_content.side_effect = responses
    return client


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
        self.assertEqual(
            format_article_title(start, end), "2026-08-31〜2026-09-06の活動記録"
        )

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

    def test_github_document_excludes_blacklisted_repositories(self) -> None:
        events = [
            github_event("1", "my-org/public-project", "2026-09-02T10:00:00Z"),
            github_event("2", "my-org/public-project", "2026-09-02T11:00:00Z"),
            github_event("3", "secret-org/private-repo", "2026-09-02T11:00:00Z"),
            github_event("4", "my-org/CORE-SECRET", "2026-09-02T12:00:00Z"),
        ]
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=events))
        )
        documents = github.collect(self.config, PERIOD_START, PERIOD_END, client)
        self.assertEqual(len(documents), 1)
        text = documents[0].to_markdown()
        self.assertIn("### my-org/public-project", text)
        self.assertIn("- 2026-09-02: PushEvent ×2", text)
        self.assertNotIn("private-repo", text)
        self.assertNotIn("CORE-SECRET", text)

    def test_github_allowlist_limits_repositories(self) -> None:
        events = [
            github_event("1", "me/Allowed", "2026-09-02T10:00:00Z"),
            github_event("2", "me/other", "2026-09-02T10:00:00Z"),
        ]
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=events))
        )
        config = {"github": {"username": "me", "allowlist": ["me/allowed"]}}
        documents = github.collect(config, PERIOD_START, PERIOD_END, client)
        text = documents[0].to_markdown()
        self.assertIn("me/Allowed", text)
        self.assertNotIn("me/other", text)

    def test_notion_document_uses_only_configured_data_sources(self) -> None:
        queried: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            queried.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "p1",
                            "url": "https://www.notion.so/private-page",
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

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {"NOTION_API_KEY": "fake-key"}):
            documents = notion.collect(self.config, PERIOD_START, PERIOD_END, client)
        self.assertEqual(len(queried), 2)
        self.assertTrue(all("ds-1" in u or "ds-2" in u for u in queried))
        text = documents[0].to_markdown()
        self.assertIn("### Book", text)
        self.assertIn("- URL: https://example.com", text)
        self.assertNotIn("notion.so", text)

    def test_source_document_markdown_has_name_context_and_records(self) -> None:
        document = SourceDocument("Work Log", "実作業ログである。", ["### A", "### B"])
        self.assertEqual(
            document.to_markdown(),
            "# Work Log\n\n実作業ログである。\n\n## Records\n\n### A\n\n### B",
        )

    def test_stage1_receives_every_source_with_its_context(self) -> None:
        client = ai_responses("- 作業した")
        documents = [
            SourceDocument("GitHub", "開発の履歴である。", ["### repo"]),
            SourceDocument("Inbox", "保存した情報である。", ["### article"]),
        ]
        result = run_stage1_inspector(client, "model", documents)
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        for expected in ("# GitHub", "開発の履歴である。", "# Inbox", "保存した情報"):
            self.assertIn(expected, prompt)
        self.assertEqual(result, "- 作業した")

    def test_empty_editor_output_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            run_stage3_editor(ai_responses("  "), "model", "草案")
        self.assertEqual(strip_code_fence("```markdown\n## 開発\n```"), "## 開発")

    def test_pipeline_runs_three_stages_and_writes_template_parts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text(
                config_text(str(root / "artifacts")), encoding="utf-8"
            )
            events = [
                github_event("10", "my-org/public-project", "2026-09-02T10:00:00Z")
            ]
            http_client = httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json=events
                        if "api.github.com" in str(request.url)
                        else {"results": []},
                    )
                )
            )
            ai_client = ai_responses("- 開発した", "## 開発\n\n草案", "## 開発\n\n本文")

            result = run_pipeline(
                config_path=str(config_path),
                target_week="2026-W36",
                dump_dir=str(root / "dump"),
                genai_client=ai_client,
                http_client=http_client,
            )
            self.assertEqual(ai_client.models.generate_content.call_count, 3)
            self.assertEqual(
                result,
                "# 2026-08-31〜2026-09-06の活動記録\n\n## 開発\n\n本文\n\n---\n\n"
                f"{FIXED_DISCLOSURE}\n",
            )
            self.assertTrue(
                (root / "artifacts" / "activity-digest-2026-w36.md").is_file()
            )
            self.assertEqual(
                sorted(path.name for path in (root / "dump").iterdir()),
                ["sources.md", "stage1.md", "stage2.md", "stage3.md"],
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

    def test_no_publishable_material_skips_writer(self) -> None:
        events = [github_event("1", "my-org/public-project", "2026-09-02T10:00:00Z")]
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=events))
        )
        ai_client = ai_responses("")
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(config_text(directory), encoding="utf-8")
            result = run_pipeline(
                str(config_path), "2026-W36", genai_client=ai_client, http_client=client
            )
        self.assertIsNone(result)
        self.assertEqual(ai_client.models.generate_content.call_count, 1)

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
        rendered = render_prompt(
            "{{period}} {{material}}", {"period": "P", "material": "M"}
        )
        self.assertEqual(rendered, "P M")
        config = {"gemini": {"model": "global", "stage1_model": "inspector"}}
        self.assertEqual(resolve_model(config, "stage1_model"), "inspector")
        self.assertEqual(resolve_model(config, "stage2_model"), "global")
        self.assertIn(FIXED_DISCLOSURE, format_article_markdown("T", "本文", ""))

    def test_http_request_urls_are_not_logged(self) -> None:
        handler = BufferingHandler(capacity=1000)
        root = logging.getLogger()
        original_level = root.level
        # pytest 環境では basicConfig が効かないため、本番と同じ INFO に揃える
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))
        )
        try:
            client.get("https://api.github.com/repos/secret-org/private-repo")
        finally:
            root.removeHandler(handler)
            root.setLevel(original_level)
        self.assertFalse(
            any("private-repo" in record.getMessage() for record in handler.buffer)
        )

    def test_failure_log_hides_error_details_unless_detailed(self) -> None:
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(500, text="secret-org/private-repo")
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(config_text(), encoding="utf-8")
            with (
                patch.dict(os.environ, {"GH_ACTIVITY_TOKEN": "token"}),
                self.assertRaises(PipelineStageError) as raised,
            ):
                run_pipeline(
                    str(config_path),
                    "2026-W36",
                    genai_client=MagicMock(),
                    http_client=client,
                )
        self.assertEqual(raised.exception.stage, "collect")

        with self.assertLogs("activity-digest", "ERROR") as logs:
            report_failure(raised.exception, detailed=False)
        self.assertEqual(
            logs.output,
            ["ERROR:activity-digest:Pipeline failed at collect: HTTPStatusError"],
        )

        with self.assertLogs("activity-digest", "ERROR") as logs:
            report_failure(raised.exception, detailed=True)
        self.assertIn("api.github.com", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
