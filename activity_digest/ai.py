import json
import re
from pathlib import Path
from typing import Any, cast

from google.genai import types

from activity_digest.sources import EVENT_FIELDS, ensure_no_coordinates

STAGE1_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "events": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {k: {"type": "STRING"} for k in EVENT_FIELDS},
                "required": ["id", "title"],
            },
        }
    },
    "required": ["events"],
}

DRAFT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "summary": {"type": "STRING"},
        "sections": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "heading": {"type": "STRING"},
                    "content": {"type": "STRING"},
                    "event_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
                "required": ["heading", "content", "event_ids"],
            },
        },
    },
    "required": ["title", "summary", "sections"],
}


def render_prompt(template: str, variables: dict[str, str]) -> str:
    result = template
    for k, v in variables.items():
        result = result.replace(f"{{{{{k}}}}}", v)
    return result


def load_prompt(prompt_path: str | Path) -> str:
    p = Path(prompt_path)
    if not p.is_file():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
    return p.read_text(encoding="utf-8")


def parse_json(raw_text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.IGNORECASE)
    data: Any = json.loads(re.sub(r"\s*```$", "", cleaned))
    if isinstance(data, dict):
        return cast(dict[str, Any], data)
    raise ValueError("Invalid JSON response")


def call_gemini(
    client: Any, model: str, prompt: str, schema: dict[str, Any]
) -> dict[str, Any]:
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=schema
        ),
    )
    parsed: Any = getattr(resp, "parsed", None)
    if isinstance(parsed, dict):
        return cast(dict[str, Any], parsed)
    return parse_json(str(resp.text or "{}"))


def run_stage1_inspector(
    client: Any,
    model: str,
    events: list[dict[str, Any]],
    prompt_path: str | Path = "prompts/inspector.md",
) -> list[dict[str, Any]]:
    prompt = render_prompt(
        load_prompt(prompt_path), {"events": json.dumps(events, ensure_ascii=False)}
    )
    approved_value = call_gemini(client, model, prompt, STAGE1_SCHEMA).get("events", [])
    approved = (
        cast(list[dict[str, Any]], approved_value)
        if isinstance(approved_value, list)
        else []
    )
    valid_ids = {e["id"] for e in events}
    if any(e.get("id") not in valid_ids for e in approved):
        raise ValueError("Invalid event IDs in Stage 1")
    ensure_no_coordinates(approved)
    return approved


def run_stage2_writer(
    client: Any,
    model: str,
    events: list[dict[str, Any]],
    slug_suffix: str,
    prompt_path: str | Path = "prompts/writer.md",
) -> dict[str, Any]:
    prompt = render_prompt(
        load_prompt(prompt_path),
        {"events": json.dumps(events, ensure_ascii=False), "week": slug_suffix},
    )
    draft = call_gemini(client, model, prompt, DRAFT_SCHEMA)
    valid_ids = {e["id"] for e in events}
    if any(
        eid not in valid_ids
        for sec in draft.get("sections", [])
        for eid in sec.get("event_ids", [])
    ):
        raise ValueError("Unknown event ID in Stage 2")
    return draft


def run_stage3_editor(
    client: Any,
    model: str,
    draft: dict[str, Any],
    prompt_path: str | Path = "prompts/editor.md",
) -> dict[str, Any]:
    prompt = render_prompt(
        load_prompt(prompt_path), {"draft": json.dumps(draft, ensure_ascii=False)}
    )
    improved = call_gemini(client, model, prompt, DRAFT_SCHEMA)
    allowed_ids = {
        eid for sec in draft.get("sections", []) for eid in sec.get("event_ids", [])
    }
    improved_ids = {
        eid for sec in improved.get("sections", []) for eid in sec.get("event_ids", [])
    }
    if improved_ids != allowed_ids:
        raise ValueError("Event IDs changed in Stage 3")
    return improved
