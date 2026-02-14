from __future__ import annotations

import json
import random
import re
import selectors
import subprocess
import time
import uuid
from collections.abc import Callable
from typing import Any, cast


class OpenAICodexJudgeError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def _should_retry(retryable: bool) -> bool:
    return retryable


class OpenAICodexJudgeClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "",
        max_attempts: int = 3,
        timeout_seconds: float = 120.0,
        pi_command: str = "pi",
        debug: bool = False,
        debug_sink: Callable[[str], None] | None = None,
    ) -> None:
        # Kept for parity with other judge clients; authentication is handled by `pi`.
        self.api_key = api_key
        self.model = model.strip()
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self.pi_command = pi_command
        self.debug = debug
        self.debug_sink = debug_sink

    def judge(self, *, system_prompt: str, user_prompt: str) -> str:
        last_error: OpenAICodexJudgeError | None = None
        prompt = _build_rpc_prompt(system_prompt=system_prompt, user_prompt=user_prompt)

        for attempt in range(1, self.max_attempts + 1):
            try:
                response_text = _invoke_pi_rpc(
                    pi_command=self.pi_command,
                    model=self.model,
                    prompt=prompt,
                    timeout_seconds=self.timeout_seconds,
                    debug=self.debug,
                    debug_sink=self.debug_sink,
                )
                json_text = _extract_json_text(response_text)
                if json_text:
                    return json_text
                msg = "judge model returned empty text"
                raise OpenAICodexJudgeError(msg)
            except OpenAICodexJudgeError as exc:
                last_error = exc
                if attempt >= self.max_attempts or not _should_retry(exc.retryable):
                    raise
            except Exception as exc:  # noqa: BLE001
                err = OpenAICodexJudgeError(str(exc))
                last_error = err
                if attempt >= self.max_attempts:
                    raise err from exc

            delay = min(30.0, float(2 ** (attempt - 1))) + random.uniform(0.0, 0.25)
            time.sleep(delay)

        if last_error is not None:
            raise last_error
        raise OpenAICodexJudgeError("unreachable judge retry state")


def _build_rpc_prompt(*, system_prompt: str, user_prompt: str) -> str:
    return (
        "You are a strict JSON API endpoint for duplicate-triage decisions. "
        "Do not call tools. Do not add markdown. "
        "Return exactly one JSON object.\n\n"
        "SYSTEM INSTRUCTIONS:\n"
        f"{system_prompt}\n\n"
        "USER INPUT:\n"
        f"{user_prompt}\n"
    )


def _emit_debug(*, enabled: bool, sink: Callable[[str], None] | None, message: str) -> None:
    if not enabled:
        return
    if sink is not None:
        sink(message)
        return
    print(message)


def _invoke_pi_rpc(
    *,
    pi_command: str,
    model: str,
    prompt: str,
    timeout_seconds: float,
    debug: bool,
    debug_sink: Callable[[str], None] | None,
) -> str:
    command = [pi_command, "--mode", "rpc", "--provider", "openai-codex", "--no-session"]
    model_value = model.strip()
    if model_value:
        command.extend(["--model", model_value])

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        msg = "pi CLI not found on PATH; required for --provider=openai-codex"
        raise OpenAICodexJudgeError(msg, retryable=False) from exc

    selector = selectors.DefaultSelector()
    try:
        stdin = process.stdin
        stdout = process.stdout
        stderr = process.stderr
        if stdin is None or stdout is None or stderr is None:
            msg = "failed to open pi rpc stdio pipes"
            raise OpenAICodexJudgeError(msg)

        selector.register(stdout, selectors.EVENT_READ)
        selector.register(stderr, selectors.EVENT_READ)

        _emit_debug(
            enabled=debug,
            sink=debug_sink,
            message=f"[codex-rpc] command={' '.join(command)}",
        )

        request_id = f"judge-{uuid.uuid4().hex}"
        request_payload = {
            "id": request_id,
            "type": "prompt",
            "message": prompt,
        }
        stdin.write(json.dumps(request_payload) + "\n")
        stdin.flush()
        _emit_debug(
            enabled=debug,
            sink=debug_sink,
            message=f"[codex-rpc] sent prompt id={request_id}",
        )

        deadline = time.monotonic() + timeout_seconds

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = f"pi rpc prompt timed out after {timeout_seconds:.1f}s"
                raise OpenAICodexJudgeError(msg)

            events = selector.select(timeout=remaining)
            if not events:
                msg = f"pi rpc prompt timed out after {timeout_seconds:.1f}s"
                raise OpenAICodexJudgeError(msg)

            for key, _ in events:
                stream = key.fileobj
                if not hasattr(stream, "readline"):
                    continue
                stream_reader = cast(Any, stream)
                line = stream_reader.readline()

                if stream is stderr:
                    if line == "":
                        try:
                            selector.unregister(stderr)
                        except Exception:  # noqa: BLE001
                            pass
                        continue
                    _emit_debug(
                        enabled=debug,
                        sink=debug_sink,
                        message=f"[codex-rpc][stderr] {line.rstrip()}",
                    )
                    continue

                if line == "":
                    exit_code = process.poll()
                    stderr_text = _stderr_text(process)
                    msg = f"pi rpc process exited unexpectedly (exit_code={exit_code})"
                    if stderr_text:
                        msg = f"{msg}: {stderr_text}"
                    raise OpenAICodexJudgeError(msg)

                _emit_debug(
                    enabled=debug,
                    sink=debug_sink,
                    message=f"[codex-rpc][stdout] {line.rstrip()}",
                )
                payloads = _parse_json_line(line)
                if not payloads:
                    continue

                for payload in payloads:
                    event_type = payload.get("type")
                    if event_type == "response" and payload.get("id") == request_id:
                        if payload.get("success"):
                            continue
                        error = payload.get("error") or "unknown rpc prompt failure"
                        raise OpenAICodexJudgeError(
                            f"pi rpc prompt failed: {error}",
                            retryable=False,
                        )

                    if event_type == "agent_end":
                        text = _extract_assistant_text_from_agent_end(payload)
                        if text:
                            return text
                        msg = "pi rpc completed without assistant text"
                        raise OpenAICodexJudgeError(msg)
    finally:
        selector.close()
        _terminate_process(process)


def _parse_json_line(line: str) -> list[dict[str, Any]]:
    stripped = line.strip()
    if not stripped:
        return []

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        return [payload]

    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    offset = 0
    while offset < len(stripped):
        start = stripped.find("{", offset)
        if start < 0:
            break

        try:
            parsed, consumed = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            offset = start + 1
            continue

        if isinstance(parsed, dict):
            found.append(parsed)

        next_offset = start + max(consumed, 1)
        if next_offset <= offset:
            next_offset = offset + 1
        offset = next_offset

    prioritized = [item for item in found if "type" in item]
    if prioritized:
        return prioritized
    return found


def _extract_assistant_text_from_agent_end(payload: dict[str, Any]) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""

    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue

        content = message.get("content")
        text = _extract_message_content_text(content)
        if text:
            return text

    return ""


def _extract_message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()

    if not isinstance(content, list):
        return ""

    chunks: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue

        part_type = part.get("type")
        if part_type == "text":
            text_value = part.get("text")
            if isinstance(text_value, str):
                chunks.append(text_value)
                continue

        fallback = part.get("text")
        if isinstance(fallback, str):
            chunks.append(fallback)

    return "".join(chunks).strip()


def _extract_json_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.IGNORECASE | re.DOTALL)
    if fenced:
        return fenced.group(1).strip()

    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1].strip()

    return stripped


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)


def _stderr_text(process: subprocess.Popen[str]) -> str:
    stderr = process.stderr
    if stderr is None:
        return ""

    try:
        return stderr.read().strip()
    except Exception:  # noqa: BLE001
        return ""
