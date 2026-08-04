"""Boundary for Solar model calls.

Preserve response envelopes, reasoning, and usage so budget exhaustion can be
diagnosed without guessing from an empty answer.
"""

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import credentials, model


class SolarError(RuntimeError):
    pass


def load_key() -> str:
    package_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    key = credentials.load("UPSTAGE_API_KEY", package_root=package_root)
    if key:
        return key
    raise SolarError(
        "$UPSTAGE_API_KEY가 필요합니다. sol --set-api-key로 전역 저장하거나 "
        "환경변수 또는 현재 작업 디렉터리의 .env에 설정하세요.")


@dataclass
class Turn:
    """Flatten one request-response exchange for diagnostics."""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str = ""
    prompt_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    raw_message: dict = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        return bool(self.content.strip()) or bool(self.tool_calls)

    @property
    def starved(self) -> bool:
        """The model consumed the output budget before producing an answer."""
        return self.finish_reason == "length" and not self.answered

    def assistant_message(self) -> dict:
        """Return the assistant message to send on the next turn.

        Omit reasoning because it is not required for continuation and can vary
        between otherwise identical requests.
        """
        out: dict = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = self.tool_calls
        return out


def complete(messages: list[dict], tools: list[dict] | None = None,
             effort: str = model.Effort.OFF, max_tokens: int | None = None,
             tool_choice: str | None = None, retries: int = 2,
             timeout: int = 300, on_delta=None) -> Turn:
    """Perform one chat completion.

    Use the model output limit by default. When ``on_delta`` is provided, emit
    SSE content as it arrives and return a ``Turn`` with the same shape as the
    non-streaming path.
    """
    payload: dict = {
        "model": model.MODEL,
        "messages": messages,
        "max_tokens": max_tokens or model.MAX_OUTPUT_TOKENS,
        "reasoning_effort": model.Effort.to_api(effort),
        **model.sampling_params(),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    if on_delta is not None:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    emitted = False  # Do not retry after a streamed delta was already emitted.

    def tracking_delta(text: str, is_reasoning: bool) -> None:
        nonlocal emitted
        emitted = True
        on_delta(text, is_reasoning)

    for attempt in range(retries + 1):
        request = urllib.request.Request(
            model.API_URL, data=body,
            headers={"Authorization": f"Bearer {load_key()}",
                     "Content-Type": "application/json"},
            method="POST")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if on_delta is None:
                    data = json.loads(response.read().decode("utf-8"))
                    return _to_turn(data, int((time.monotonic() - started) * 1000))
                return _stream_to_turn(response, tracking_delta, started)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            last_error = SolarError(f"HTTP {exc.code}: {detail}")
            if exc.code < 500 and exc.code != 429:
                raise last_error from exc
        except Exception as exc:  # noqa: BLE001 - the network boundary is broad.
            last_error = exc
        if attempt < retries and not emitted:
            time.sleep(1.5 * (attempt + 1))
        elif emitted:
            break  # Retrying would duplicate a response that already reached the UI.

    raise SolarError(f"request failed after {retries + 1} attempts: {last_error}")


def _stream_to_turn(response, on_delta, started: float) -> Turn:
    """Reassemble an SSE stream into a ``Turn``.

    Read the compatible chunk format, emit content and reasoning deltas
    immediately, merge tool calls by index, and read usage from the final chunk.
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_slots: dict[int, dict] = {}
    finish_reason = ""
    usage: dict = {}

    def handle(line: bytes) -> None:
        nonlocal finish_reason, usage
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if payload == b"[DONE]":
            return
        try:
            chunk = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            if reasoning:
                reasoning_parts.append(reasoning)
                on_delta(reasoning, True)
            content = delta.get("content")
            if content:
                content_parts.append(content)
                on_delta(content, False)
            for call in delta.get("tool_calls") or []:
                slot = tool_slots.setdefault(
                    call.get("index", 0),
                    {"id": "", "type": "function",
                     "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    slot["id"] += call["id"]
                function = call.get("function") or {}
                if function.get("name"):
                    slot["function"]["name"] += function["name"]
                if function.get("arguments"):
                    slot["function"]["arguments"] += function["arguments"]

    buffer = b""
    while True:
        chunk = response.read(8192)
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            handle(line)
    if buffer:
        handle(buffer)

    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return Turn(
        content="".join(content_parts),
        reasoning="".join(reasoning_parts),
        tool_calls=[tool_slots[i] for i in sorted(tool_slots)],
        finish_reason=finish_reason,
        prompt_tokens=usage.get("prompt_tokens", 0),
        cached_tokens=prompt_details.get("cached_tokens", 0),
        reasoning_tokens=completion_details.get("reasoning_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        latency_ms=int((time.monotonic() - started) * 1000),
        raw_message={"role": "assistant",
                     "content": "".join(content_parts)},
    )


def _to_turn(data: dict, latency_ms: int) -> Turn:
    choice = data["choices"][0]
    message = choice.get("message") or {}
    usage = data.get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    # Accept the alternate field name so a server rename does not hide reasoning.
    reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
    return Turn(
        content=message.get("content") or "",
        reasoning=reasoning,
        tool_calls=list(message.get("tool_calls") or []),
        finish_reason=choice.get("finish_reason") or "",
        prompt_tokens=usage.get("prompt_tokens", 0),
        cached_tokens=prompt_details.get("cached_tokens", 0),
        reasoning_tokens=completion_details.get("reasoning_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        latency_ms=latency_ms,
        raw_message=message,
    )
