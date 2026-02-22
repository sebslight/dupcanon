from __future__ import annotations

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from dupcanon.llm_retry import retry_with_backoff, should_retry_http_status, validate_max_attempts


class OpenAIEmbeddingError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _should_retry(status_code: int | None) -> bool:
    return should_retry_http_status(status_code)


class OpenAIEmbeddingsClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "text-embedding-3-large",
        output_dimensionality: int = 3072,
        max_attempts: int = 5,
    ) -> None:
        if output_dimensionality <= 0:
            msg = "output_dimensionality must be > 0"
            raise ValueError(msg)
        validate_max_attempts(max_attempts)

        self.client = OpenAI(api_key=api_key)
        self.model = model.strip()
        self.output_dimensionality = output_dimensionality
        self.max_attempts = max_attempts

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        def _attempt() -> list[list[float]]:
            response = self.client.embeddings.create(
                model=self.model,
                input=texts,
                dimensions=self.output_dimensionality,
            )
            return self._parse_embeddings(response=response, expected_count=len(texts))

        def _map_error(exc: Exception) -> tuple[bool, OpenAIEmbeddingError]:
            if isinstance(exc, OpenAIEmbeddingError):
                return True, exc
            if isinstance(exc, RateLimitError):
                return True, OpenAIEmbeddingError(str(exc), status_code=429)
            if isinstance(exc, APIStatusError):
                status_code = _status_code(exc)
                err = OpenAIEmbeddingError(str(exc), status_code=status_code)
                return _should_retry(status_code), err
            if isinstance(exc, (APIConnectionError, APITimeoutError)):
                return True, OpenAIEmbeddingError(str(exc))
            return True, OpenAIEmbeddingError(str(exc))

        return retry_with_backoff(
            max_attempts=self.max_attempts,
            attempt=_attempt,
            on_error=_map_error,
        )

    def _parse_embeddings(self, *, response: object, expected_count: int) -> list[list[float]]:
        data = getattr(response, "data", None)
        if not isinstance(data, list) or len(data) != expected_count:
            got_count = len(data) if isinstance(data, list) else "n/a"
            msg = f"embedding response size mismatch: expected {expected_count}, got {got_count}"
            raise OpenAIEmbeddingError(msg)

        vectors: list[list[float]] = []
        for entry in data:
            embedding = getattr(entry, "embedding", None)
            if not isinstance(embedding, list):
                msg = "embedding values missing"
                raise OpenAIEmbeddingError(msg)

            vector = [float(value) for value in embedding]
            if len(vector) != self.output_dimensionality:
                msg = (
                    "embedding dimension mismatch: "
                    f"expected {self.output_dimensionality}, got {len(vector)}"
                )
                raise OpenAIEmbeddingError(msg)

            vectors.append(vector)

        return vectors


def _status_code(exc: APIStatusError) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    return None
