"""Register tools and execute calls through one result envelope.

Tool specifications are part of the stable prefix, so registration order and
serialization must remain deterministic while an episode is running.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[dict], object]
    # Read-only tools run without approval; write and execution tools go through gates.
    read_only: bool = True
    # Tools that can emit output while running. The final return value must use
    # the same result envelope as run().
    stream: Callable[[dict, Callable[[str], None]], object] | None = None

    def spec(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name,
                             "description": self.description,
                             "parameters": self.parameters}}


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[dict]:
        """Return name-sorted specifications independent of registration order."""
        return [self._tools[name].spec() for name in sorted(self._tools)]


@dataclass
class Invocation:
    """Record one tool invocation."""

    tool: str
    ok: bool
    error: str | None = None
    # 인자 지문. 같은 도구를 같은 인자로 반복 호출하는 정체를 감지하는 데 쓴다
    # (loop.StallDetector). 인자 원본을 담지 않는 것은 비밀값이 진단 경로로
    # 새지 않게 하기 위해서다.
    signature: str = ""


def _signature(raw: object) -> str:
    """도구 인자를 짧은 해시로 만든다. 내용은 남기지 않는다."""
    if isinstance(raw, str):
        blob = raw
    else:
        try:
            blob = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            blob = str(raw)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _truncated_arguments(raw: str, exc: json.JSONDecodeError) -> bool:
    """인자 JSON이 문법 오류가 아니라 절단으로 깨졌는지 추정한다.

    스트림이 중간에 끊기면 문자열이 닫히지 않거나 오류 위치가 입력의 끝에
    걸린다. 이 구분이 있어야 모델이 전체 재생성 대신 이어쓰기를 택한다.
    """
    if "Unterminated string" in exc.msg:
        return True
    return exc.pos >= len(raw.rstrip()) - 1


_TRUNCATED_GUIDANCE = (
    "arguments 스트림이 중간에 절단되었습니다. 같은 내용을 처음부터 다시 "
    "만들지 마십시오. 파일 쓰기였다면 내용을 더 작은 조각으로 나눠 "
    "write_file로 첫 조각을 쓰고 append=true로 이어 쓰십시오.")


def execute(registry: Registry, call: dict,
            approve: Callable[[Tool, dict], bool] | None = None,
            emit: Callable[[str], None] | None = None
            ) -> tuple[dict, Invocation]:
    """Execute one tool call and return its tool message.

    Always return exactly one tool message so a failed call cannot break the next
    request's message pairing.
    """
    name = call.get("function", {}).get("name", "")
    call_id = call.get("id", "")
    raw = call.get("function", {}).get("arguments") or "{}"
    fingerprint = _signature(raw)

    def message(payload: object) -> dict:
        return {"role": "tool", "tool_call_id": call_id, "name": name,
                "content": json.dumps(payload, ensure_ascii=False)}

    tool = registry.get(name)
    if tool is None:
        return message({"error": f"unknown tool: {name}"}), \
            Invocation(name, False, "unknown tool", fingerprint)

    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        if isinstance(raw, str) and _truncated_arguments(raw, exc):
            return message({"error": _TRUNCATED_GUIDANCE}), \
                Invocation(name, False,
                           f"truncated arguments: {exc}", fingerprint)
        return message({"error": "arguments were not valid JSON"}), \
            Invocation(name, False, f"unparsable arguments: {exc}", fingerprint)

    if approve and not tool.read_only and not approve(tool, args):
        return message({"error": "denied by policy"}), \
            Invocation(name, False, "denied", fingerprint)

    try:
        if emit is not None and tool.stream is not None:
            result = tool.stream(args, emit)
        else:
            result = tool.run(args)
        # Tool bodies return an {"error": ...} envelope instead of raising.
        # Treat that envelope as a failure so the UI and episode diagnostics do
        # not mistake provider or permission errors for successful calls.
        if isinstance(result, dict) and "error" in result:
            detail = str(result.get("error") or "tool returned an error envelope")
            return message(result), Invocation(name, False, detail, fingerprint)
        # A shell-style envelope reports failure through exit_code. Count it as
        # a failed invocation so stall detection sees broken verification loops,
        # but return the payload unchanged to keep the diagnostic output.
        if isinstance(result, dict) and result.get("exit_code") not in (None, 0):
            detail = f"exit_code {result['exit_code']}"
            return message(result), Invocation(name, False, detail, fingerprint)
        return message(result), Invocation(name, True, None, fingerprint)
    except Exception as exc:  # noqa: BLE001 - tool bodies are user code
        return message({"error": str(exc)}), \
            Invocation(name, False, str(exc), fingerprint)
