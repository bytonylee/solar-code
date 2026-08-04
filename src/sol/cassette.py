"""Record and replay model traffic.

Use recordings for debugging, regression checks, and offline tests. A request
hash is the replay key. Reasoning text and tool-call identifiers are excluded
because they can vary between otherwise equivalent requests.
"""

import json
import os

from .client import Turn
from .determinism import stable_hash

RECORD, REPLAY, AUTO = "record", "replay", "auto"


class CassetteMiss(RuntimeError):
    """A replay request has no matching recorded exchange."""


def request_key(messages: list[dict], tools: list[dict] | None,
                effort: str) -> str:
    """Return the request identity used for replay.

    Exclude volatile tool-call identifiers but retain tool names and arguments,
    which determine the meaning of the conversation.
    """
    scrubbed = []
    for message in messages:
        item = {"role": message.get("role"),
                "content": message.get("content") or ""}
        calls = message.get("tool_calls")
        if calls:
            item["calls"] = [{"name": c["function"]["name"],
                              "args": c["function"].get("arguments", "")}
                             for c in calls]
        if message.get("role") == "tool":
            item["name"] = message.get("name")
        scrubbed.append(item)
    names = [t["function"]["name"] for t in (tools or [])]
    return stable_hash({"messages": scrubbed, "tools": names,
                        "effort": effort}, 16)


class Cassette:
    """Store request and response exchanges.

    Repeated keys return responses in recording order so multi-turn loops can
    replay a repeated request.
    """

    def __init__(self, path: str, mode: str = AUTO) -> None:
        self.path = path
        self.mode = mode
        self.entries: dict[str, list[dict]] = {}
        self.cursor: dict[str, int] = {}
        self.hits = 0
        self.misses = 0
        self.recorded = 0
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        self.entries = data.get("entries", {})

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "entries": self.entries}, fh,
                      ensure_ascii=False, indent=2, sort_keys=True)

    @property
    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def take(self, key: str) -> dict | None:
        bucket = self.entries.get(key)
        if not bucket:
            return None
        index = self.cursor.get(key, 0)
        # Reuse the last response when replay requests exceed the recording.
        # This keeps replay usable when the loop runs for more turns.
        chosen = bucket[min(index, len(bucket) - 1)]
        self.cursor[key] = index + 1
        return chosen

    def put(self, key: str, payload: dict) -> None:
        self.entries.setdefault(key, []).append(payload)
        self.recorded += 1


def _to_payload(turn: Turn) -> dict:
    """Keep only fields needed for replay and omit reasoning text.

    This keeps recordings small while retaining token counts for cost analysis.
    """
    return {"content": turn.content, "tool_calls": turn.tool_calls,
            "finish_reason": turn.finish_reason,
            "prompt_tokens": turn.prompt_tokens,
            "cached_tokens": turn.cached_tokens,
            "reasoning_tokens": turn.reasoning_tokens,
            "completion_tokens": turn.completion_tokens}


def _to_turn(payload: dict) -> Turn:
    return Turn(content=payload.get("content", ""),
                tool_calls=list(payload.get("tool_calls") or []),
                finish_reason=payload.get("finish_reason", ""),
                prompt_tokens=payload.get("prompt_tokens", 0),
                cached_tokens=payload.get("cached_tokens", 0),
                reasoning_tokens=payload.get("reasoning_tokens", 0),
                completion_tokens=payload.get("completion_tokens", 0))


def wrap(complete, cassette: Cassette):
    """Wrap a completion function with recording or replay behavior."""
    cassette.load()
    replaying = cassette.mode == REPLAY or (
        cassette.mode == AUTO and cassette.exists)

    def send(messages, tools=None, effort="off", tool_choice=None, **kwargs):
        key = request_key(messages, tools, effort)

        if replaying:
            payload = cassette.take(key)
            if payload is None:
                cassette.misses += 1
                raise CassetteMiss(
                    f"기록에 없는 요청입니다 (key={key}). "
                    "대화가 달라졌거나 카세트를 다시 기록해야 합니다.")
            cassette.hits += 1
            return _to_turn(payload)

        turn = complete(messages, tools=tools, effort=effort,
                        tool_choice=tool_choice, **kwargs)
        cassette.put(key, _to_payload(turn))
        cassette.save()
        return turn

    return send
