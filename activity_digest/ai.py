import re
from pathlib import Path
from typing import Any

from activity_digest.collectors import SourceDocument


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


def strip_code_fence(text: str) -> str:
    cleaned = re.sub(r"^```(?:markdown|md)?\s*\n", "", text.strip(), flags=re.I)
    return re.sub(r"\n?```$", "", cleaned).strip()


def call_gemini(client: Any, model: str, prompt: str) -> str:
    resp = client.models.generate_content(model=model, contents=prompt)
    return strip_code_fence(str(resp.text or ""))


def run_stage1_inspector(
    client: Any,
    model: str,
    documents: list[SourceDocument],
    prompt_path: str | Path = "prompts/inspector.md",
) -> str:
    sources = "\n\n".join(document.to_markdown() for document in documents)
    prompt = render_prompt(load_prompt(prompt_path), {"sources": sources})
    return call_gemini(client, model, prompt)


def run_stage2_writer(
    client: Any,
    model: str,
    material: str,
    period: str,
    prompt_path: str | Path = "prompts/writer.md",
) -> str:
    prompt = render_prompt(
        load_prompt(prompt_path), {"material": material, "period": period}
    )
    draft = call_gemini(client, model, prompt)
    if not draft:
        raise ValueError("Stage 2 returned an empty draft")
    return draft


def run_stage3_editor(
    client: Any,
    model: str,
    draft: str,
    prompt_path: str | Path = "prompts/editor.md",
) -> str:
    prompt = render_prompt(load_prompt(prompt_path), {"draft": draft})
    content = call_gemini(client, model, prompt)
    if not content:
        raise ValueError("Stage 3 returned empty content")
    return content
