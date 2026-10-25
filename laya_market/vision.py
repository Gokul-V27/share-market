"""Screenshot analysis: send chart images to Claude with the Laya prompt, then re-check its numbers.

Claude reads the screenshots (levels, MAs, countdown) and writes the report plus a JSON
appendix of what it read. The rule engine then recomputes scores, R:R and ranking from
that JSON, so arithmetic slips in the model's prose get caught.

Needs `pip install anthropic` and credentials (ANTHROPIC_API_KEY or `ant auth login`).
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Optional

MODEL = "claude-opus-5-5"
PROMPT_PATH = Path(__file__).parent / "prompts" / "laya_system.md"
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".gif": "image/gif"}


class VisionError(RuntimeError):
    pass


def system_prompt() -> str:
    return PROMPT_PATH.read_text()


def image_block(path: str) -> dict:
    p = Path(path)
    media = MEDIA_TYPES.get(p.suffix.lower())
    if media is None:
        raise VisionError(f"{path}: unsupported image type (use png, jpg, webp or gif)")
    data = base64.standard_b64encode(p.read_bytes()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


def extract_json(text: str) -> Optional[dict]:
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    for raw in reversed(blocks):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    return None


def analyse_screenshots(paths: list[str], note: str = "", model: str = MODEL) -> tuple[str, Optional[dict]]:
    """Return (Claude's report text, parsed JSON appendix or None)."""
    try:
        import anthropic
    except ImportError as e:
        raise VisionError("screenshot analysis needs the Anthropic SDK: pip install anthropic") from e

    content = []
    for p in paths:
        content.append({"type": "text", "text": f"Screenshot: {Path(p).name}"})
        content.append(image_block(p))
    ask = "Analyse these chart screenshots."
    if note:
        ask += f"\nTrader's note: {note}"
    content.append({"type": "text", "text": ask})

    client = anthropic.Anthropic()
    try:
        # Server-side fallback re-runs a safety-declined request on a recommended model.
        with client.beta.messages.stream(
            model=model,
            max_tokens=64000,
            system=system_prompt(),
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": content}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise VisionError("Anthropic credentials missing or invalid (set ANTHROPIC_API_KEY)") from e
    except anthropic.RateLimitError as e:
        raise VisionError(f"rate limited; retry after {e.response.headers.get('retry-after', '60')}s") from e
    except anthropic.APIStatusError as e:
        raise VisionError(f"API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise VisionError("could not reach the Anthropic API") from e

    if message.stop_reason == "refusal":
        raise VisionError("the request was declined")
    text = "".join(b.text for b in message.content if b.type == "text")
    if message.stop_reason == "max_tokens":
        text += "\n[report truncated at max_tokens]"
    return text, extract_json(text)
