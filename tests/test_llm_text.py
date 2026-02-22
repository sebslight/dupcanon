from __future__ import annotations

from dupcanon.llm_text import extract_text_from_content


class _Chunk:
    def __init__(self, text: str | None) -> None:
        self.text = text


def test_extract_text_from_string() -> None:
    assert extract_text_from_content("  hello ") == "hello"


def test_extract_text_from_list_prefers_text_attr() -> None:
    content = [_Chunk("hello "), _Chunk(""), {"text": "world"}]

    assert extract_text_from_content(content) == "hello world"


def test_extract_text_from_list_dicts_only() -> None:
    content = [{"text": "foo "}, {"text": ""}, {"text": "bar"}]

    assert extract_text_from_content(content) == "foo bar"


def test_extract_text_from_non_list_non_string() -> None:
    assert extract_text_from_content(123) == ""
