from __future__ import annotations

import json

import pytest

from dupcanon.gemini_embeddings import GeminiEmbeddingError, GeminiEmbeddingsClient


def test_parse_embeddings_success() -> None:
    client = GeminiEmbeddingsClient(api_key="key", output_dimensionality=3)
    raw = json.dumps(
        {
            "embeddings": [
                {"values": [0.1, 0.2, 0.3]},
                {"values": [0.4, 0.5, 0.6]},
            ]
        }
    ).encode("utf-8")

    vectors = client._parse_embeddings(raw=raw, expected_count=2)

    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


def test_parse_embeddings_count_mismatch_raises() -> None:
    client = GeminiEmbeddingsClient(api_key="key", output_dimensionality=3)
    raw = json.dumps({"embeddings": [{"values": [0.1, 0.2, 0.3]}]}).encode("utf-8")

    with pytest.raises(GeminiEmbeddingError):
        client._parse_embeddings(raw=raw, expected_count=2)


def test_parse_embeddings_dim_mismatch_raises() -> None:
    client = GeminiEmbeddingsClient(api_key="key", output_dimensionality=3)
    raw = json.dumps({"embeddings": [{"values": [0.1, 0.2]}]}).encode("utf-8")

    with pytest.raises(GeminiEmbeddingError):
        client._parse_embeddings(raw=raw, expected_count=1)
