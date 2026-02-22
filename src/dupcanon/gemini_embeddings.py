from __future__ import annotations

import json
from urllib import error, request

from dupcanon.llm_retry import retry_with_backoff, should_retry_http_status, validate_max_attempts


class GeminiEmbeddingError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _should_retry(status_code: int | None) -> bool:
    return should_retry_http_status(status_code)


class GeminiEmbeddingsClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-embedding-001",
        output_dimensionality: int = 3072,
        max_attempts: int = 5,
        timeout_seconds: float = 60.0,
    ) -> None:
        if output_dimensionality <= 0:
            msg = "output_dimensionality must be > 0"
            raise ValueError(msg)
        validate_max_attempts(max_attempts)
        if timeout_seconds <= 0:
            msg = "timeout_seconds must be > 0"
            raise ValueError(msg)

        self.api_key = api_key
        self.model = model.strip().removeprefix("models/")
        self.output_dimensionality = output_dimensionality
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        request_body = {
            "requests": [
                {
                    "model": f"models/{self.model}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": "RETRIEVAL_DOCUMENT",
                    "outputDimensionality": self.output_dimensionality,
                }
                for text in texts
            ]
        }

        payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        url = (
            "https://generativelanguage.googleapis.com/v1beta/"
            f"models/{self.model}:batchEmbedContents?key={self.api_key}"
        )

        def _attempt() -> list[list[float]]:
            req = request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read()
            return self._parse_embeddings(raw=raw, expected_count=len(texts))

        def _map_error(exc: Exception) -> tuple[bool, GeminiEmbeddingError]:
            if isinstance(exc, GeminiEmbeddingError):
                return False, exc
            if isinstance(exc, error.HTTPError):
                body = exc.read().decode("utf-8", errors="replace")
                err = GeminiEmbeddingError(body or str(exc), status_code=exc.code)
                return _should_retry(exc.code), err
            if isinstance(exc, (error.URLError, TimeoutError)):
                return True, GeminiEmbeddingError(str(exc))
            return True, GeminiEmbeddingError(str(exc))

        return retry_with_backoff(
            max_attempts=self.max_attempts,
            attempt=_attempt,
            on_error=_map_error,
        )

    def _parse_embeddings(self, *, raw: bytes, expected_count: int) -> list[list[float]]:
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise GeminiEmbeddingError("invalid JSON in embedding response") from exc

        embeddings_raw = data.get("embeddings")
        if not isinstance(embeddings_raw, list) or len(embeddings_raw) != expected_count:
            got_count = len(embeddings_raw) if isinstance(embeddings_raw, list) else "n/a"
            msg = f"embedding response size mismatch: expected {expected_count}, got {got_count}"
            raise GeminiEmbeddingError(msg)

        vectors: list[list[float]] = []
        for embedding in embeddings_raw:
            if not isinstance(embedding, dict):
                raise GeminiEmbeddingError("embedding entry is not an object")

            values = embedding.get("values")
            if not isinstance(values, list):
                raise GeminiEmbeddingError("embedding values missing")

            vector = [float(value) for value in values]
            if len(vector) != self.output_dimensionality:
                msg = (
                    "embedding dimension mismatch: "
                    f"expected {self.output_dimensionality}, got {len(vector)}"
                )
                raise GeminiEmbeddingError(msg)

            vectors.append(vector)

        return vectors
