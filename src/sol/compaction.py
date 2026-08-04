"""Compact context without breaking tool-message pairs.

An assistant tool call must remain paired with its tool results. Compaction may
shorten tool results or replace an older section with a summary, but it must not
leave orphaned messages or remove the newest working context.
"""

import json
from dataclasses import dataclass

from . import model

# Context usage thresholds. Apply the least destructive step first.
SNIP_RATIO = 0.60      # Shorten older tool results.
PRUNE_RATIO = 0.80     # Replace older tool results with placeholders.
SUMMARY_RATIO = 0.90   # Summarize if the context is still too large.

# Keep the newest turns intact so the model retains active task evidence.
RECENT_TURNS_KEPT = 8

SNIP_MAX_CHARS = 2_000
# Restorable placeholder. Keep the tool name and key arguments so the model can
# re-fetch the omitted result instead of losing the reference entirely.
PRUNED_PLACEHOLDER = "[생략: {tool} {args}]"
PLACEHOLDER_ARG_MAX = 80

# Summary must preserve these six items in a fixed order. The list is the
# contract for any summarizer callback (recall-first, then precision).
SUMMARY_PROMPT = (
    "다음 대화를 요약하십시오. 아래 6항목을 이 순서의 고정 형식으로 남기십시오.\n"
    "1. 결정: 아키텍처·전략 결정\n"
    "2. 목표·진행: 현재 목표와 진행 상황\n"
    "3. 미해결: 미해결 버그·질문\n"
    "4. 구현 세부·파일 경로: 중요한 구현 세부와 변경·조회 파일 경로\n"
    "5. 제약: 제약과 의존성\n"
    "6. 실패 기록: 실패한 시도와 그 이유\n"
    "중복 논의, 오래된 원시 도구 출력, 반복 설명은 버리십시오."
)


@dataclass
class CompactionResult:
    messages: list[dict]
    stage: str
    before_tokens: int
    after_tokens: int

    @property
    def saved(self) -> int:
        return self.before_tokens - self.after_tokens

    def render(self) -> str:
        return (f"compaction[{self.stage}] {self.before_tokens} -> "
                f"{self.after_tokens} tokens (saved {self.saved})")


def measure(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += model.estimate_tokens(str(message.get("content") or ""))
        for call in message.get("tool_calls") or []:
            total += model.estimate_tokens(
                str(call.get("function", {}).get("arguments") or ""))
    return total


def tool_call_ids(message: dict) -> set[str]:
    return {c.get("id") for c in message.get("tool_calls") or [] if c.get("id")}


def pair_boundaries(messages: list[dict]) -> list[int]:
    """Return only indices that are safe compaction boundaries.

    A boundary may follow an assistant tool call only after all matching tool
    results have passed. Cutting between them would create orphaned messages.
    """
    safe: list[int] = []
    pending: set[str] = set()
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "tool":
            pending.discard(message.get("tool_call_id"))
        elif role == "assistant":
            pending |= tool_call_ids(message)
        if not pending and role in ("assistant", "user"):
            safe.append(index + 1)
    return safe


def validate(messages: list[dict]) -> list[str]:
    """Validate tool-message pairing before sending a request."""
    problems: list[str] = []
    open_calls: dict[str, int] = {}
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            for call_id in tool_call_ids(message):
                open_calls[call_id] = index
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in open_calls:
                problems.append(f"#{index}: orphan tool message ({call_id})")
            else:
                del open_calls[call_id]
    for call_id, index in open_calls.items():
        problems.append(f"#{index}: tool_call {call_id} has no tool response")
    return problems


def _split_tail(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split the recent section at a safe message boundary."""
    boundaries = pair_boundaries(messages)
    if not boundaries:
        return [], list(messages)
    turns = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(turns) <= RECENT_TURNS_KEPT:
        return [], list(messages)
    wanted = turns[-RECENT_TURNS_KEPT]
    cut = max([b for b in boundaries if b <= wanted] or [0])
    return messages[:cut], messages[cut:]


def _is_placeholder(content: str) -> bool:
    text = content.strip()
    return text.startswith("[생략:") or text == "[생략된 도구 결과]"


def _should_preserve(content: str) -> bool:
    """Keep failure evidence so the model does not repeat known mistakes.

    Subagent envelopes carry incomplete/starved keys without always saying
    "error", so preserve those keys as well as explicit error text.
    """
    lowered = content.lower()
    if "error" in lowered or "오류" in content:
        return True
    try:
        payload = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("error") not in (None, "", False):
        return True
    if payload.get("starved") is True:
        return True
    if payload.get("incomplete") is True:
        return True
    reasons = payload.get("incomplete_reasons")
    if isinstance(reasons, list) and reasons:
        return True
    results = payload.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            if item.get("starved") is True or item.get("incomplete") is True:
                return True
            nested = item.get("incomplete_reasons")
            if isinstance(nested, list) and nested:
                return True
    return False


def _summarize_args(raw: object) -> str:
    """Extract path/pattern style arguments for a restorable placeholder."""
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    text = " ".join(text.split())
    if not text or text in ("{}", "null"):
        return ""
    try:
        data = json.loads(text) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        preferred = []
        for key in ("path", "paths", "pattern", "query", "file", "files",
                    "command", "name", "task", "role", "glob", "cwd", "root"):
            if key in data and data[key] not in (None, "", [], {}):
                value = data[key]
                if isinstance(value, (list, dict)):
                    value = json.dumps(value, ensure_ascii=False,
                                       separators=(",", ":"))
                preferred.append(f"{key}={value}")
        if preferred:
            text = " ".join(preferred)
        else:
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"),
                              sort_keys=True)
    if len(text) > PLACEHOLDER_ARG_MAX:
        text = text[: PLACEHOLDER_ARG_MAX - 1] + "…"
    return text


def _tool_meta(messages: list[dict]) -> dict[str, tuple[str, str]]:
    """Map tool_call_id -> (tool name, argument summary) from assistant calls."""
    meta: dict[str, tuple[str, str]] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            call_id = call.get("id")
            if not call_id:
                continue
            function = call.get("function") or {}
            name = function.get("name") or "tool"
            args = _summarize_args(function.get("arguments"))
            meta[call_id] = (name, args)
    return meta


def _placeholder_for(message: dict, meta: dict[str, tuple[str, str]]) -> str:
    call_id = message.get("tool_call_id")
    name, args = meta.get(call_id, ("", ""))
    if not name:
        name = message.get("name") or "tool"
    return PRUNED_PLACEHOLDER.format(tool=name, args=args).rstrip()


def _shrink(messages: list[dict], placeholder: bool) -> list[dict]:
    """Shorten tool results without removing messages.

    Preserve message count so pairs remain valid, and keep error results so the
    model retains evidence of failed actions.
    """
    meta = _tool_meta(messages)
    out: list[dict] = []
    for message in messages:
        if message.get("role") != "tool":
            out.append(message)
            continue
        content = str(message.get("content") or "")
        if _is_placeholder(content):
            out.append(message)
            continue
        if _should_preserve(content):
            out.append(message)
            continue
        if placeholder:
            shrunk = _placeholder_for(message, meta)
        elif len(content) > SNIP_MAX_CHARS:
            head = content[: SNIP_MAX_CHARS // 2]
            tail = content[-SNIP_MAX_CHARS // 2:]
            label = _placeholder_for(message, meta)
            shrunk = f"{head}\n...[중략: {label}]...\n{tail}"
        else:
            out.append(message)
            continue
        out.append({**message, "content": shrunk})
    return out


def compact(messages: list[dict], budget_tokens: int,
            summarize=None) -> CompactionResult:
    """Compact messages in stages according to context pressure.

    The optional summarizer is the last resort, after shortening and pruning
    older tool results.
    """
    before = measure(messages)
    if not budget_tokens:
        return CompactionResult(list(messages), "disabled", before, before)

    ratio = before / budget_tokens
    if ratio < SNIP_RATIO:
        return CompactionResult(list(messages), "none", before, before)

    head, tail = _split_tail(messages)
    if not head:
        return CompactionResult(list(messages), "tail-only", before, before)

    stage = "snip"
    head = _shrink(head, placeholder=False)
    if measure(head + tail) / budget_tokens >= PRUNE_RATIO:
        stage = "prune"
        head = _shrink(head, placeholder=True)

    result = head + tail
    if summarize and measure(result) / budget_tokens >= SUMMARY_RATIO:
        stage = "summary"
        digest = summarize(head)
        # Replace the compacted section with one assistant summary turn.
        result = [{"role": "assistant", "content": f"[이전 대화 요약]\n{digest}"}] + tail

    return CompactionResult(result, stage, before, measure(result))
