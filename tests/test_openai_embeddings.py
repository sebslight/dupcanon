from __future__ import annotations

import pytest

from dupcanon.openai_embeddings import OpenAIEmbeddingError, OpenAIEmbeddingsClient


class _FakeEmbeddingEntry:
    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _FakeEmbeddingResponse:
    def __init__(self, data: list[_FakeEmbeddingEntry]) -> None:
        self.data = data


def test_parse_embeddings_success() -> None:
    client = OpenAIEmbeddingsClient(api_key="key", output_dimensionality=3)
    response = _FakeEmbeddingResponse(
        data=[
            _FakeEmbeddingEntry([0.1, 0.2, 0.3]),
            _FakeEmbeddingEntry([0.4, 0.5, 0.6]),
        ]
    )

    vectors = client._parse_embeddings(response=response, expected_count=2)

    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


def test_parse_embeddings_count_mismatch_raises() -> None:
    client = OpenAIEmbeddingsClient(api_key="key", output_dimensionality=3)
    response = _FakeEmbeddingResponse(data=[_FakeEmbeddingEntry([0.1, 0.2, 0.3])])

    with pytest.raises(OpenAIEmbeddingError):
        client._parse_embeddings(response=response, expected_count=2)


def test_parse_embeddings_dim_mismatch_raises() -> None:
    client = OpenAIEmbeddingsClient(api_key="key", output_dimensionality=3)
    response = _FakeEmbeddingResponse(data=[_FakeEmbeddingEntry([0.1, 0.2])])

    with pytest.raises(OpenAIEmbeddingError):
        client._parse_embeddings(response=response, expected_count=1)
