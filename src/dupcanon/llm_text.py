from __future__ import annotations

from typing import Any


def extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()

    if not isinstance(content, list):
        return ""

    chunks: list[str] = []
    for part in content:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text.strip():
            chunks.append(text)
            continue

        if isinstance(part, dict):
            value = part.get("text")
            if isinstance(value, str) and value.strip():
                chunks.append(value)

    return "".join(chunks).strip()
