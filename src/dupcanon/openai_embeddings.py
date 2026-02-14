from __future__ import annotations

import time

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from dupcanon.llm_retry import retry_delay_seconds, should_retry_http_status, validate_max_attempts


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
        output_dimensionality: int = 768,
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

        last_error: OpenAIEmbeddingError | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client.embeddings.create(
                    model=self.model,
                    input=texts,
                    dimensions=self.output_dimensionality,
                )
                return self._parse_embeddings(response=response, expected_count=len(texts))
            except RateLimitError as exc:
                err = OpenAIEmbeddingError(str(exc), status_code=429)
                last_error = err
                if attempt >= self.max_attempts:
                    raise err from exc
            except APIStatusError as exc:
                status_code = _status_code(exc)
                err = OpenAIEmbeddingError(str(exc), status_code=status_code)
                last_error = err
                if attempt >= self.max_attempts or not _should_retry(status_code):
                    raise err from exc
            except (APIConnectionError, APITimeoutError) as exc:
                err = OpenAIEmbeddingError(str(exc))
                last_error = err
                if attempt >= self.max_attempts:
                    raise err from exc
            except Exception as exc:  # noqa: BLE001
                err = OpenAIEmbeddingError(str(exc))
                last_error = err
                if attempt >= self.max_attempts:
                    raise err from exc

            time.sleep(retry_delay_seconds(attempt))

        if last_error is not None:
            raise last_error
        raise OpenAIEmbeddingError("unreachable embedding retry state")

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
