"""Define intervention points around the agent loop.

Processors can edit or block requests, approve or alter tool arguments, redact
results, request retries, and report unresolved conditions. They return explicit
control objects instead of raising so message pairs and session state stay valid.
"""

from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass
class Block:
    """Stop processing and pass the reason to the model."""
    reason: str


@dataclass
class Retry:
    """Request another attempt for the current turn."""
    reason: str
    tool_choice: str | None = None


class Processor(Protocol):
    """Protocol whose lifecycle methods are all optional."""

    def start_episode(self, task: str) -> None: ...
    def before_request(self, messages: list[dict]) -> list[dict] | Block: ...
    def before_tool(self, name: str, args: dict) -> dict | Block: ...
    def after_tool(self, name: str, result: str) -> str: ...
    def stream_tool(self, name: str, chunk: str) -> str: ...
    def after_turn(self, turn) -> Retry | None: ...
    def terminal_issues(self, answer: str) -> list[str]: ...


class Chain:
    """Run registered processors in order.

    A block from an earlier processor stops later processors, so order defines
    precedence.
    """

    def __init__(self, processors: list[object] | None = None) -> None:
        self.processors = list(processors or [])

    def add(self, processor: object) -> "Chain":
        self.processors.append(processor)
        return self

    def start_episode(self, task: str) -> None:
        """Notify processors that a new user task has started."""
        for processor in self.processors:
            hook = getattr(processor, "start_episode", None)
            if hook is not None:
                hook(task)

    def before_request(self, messages: list[dict]) -> list[dict] | Block:
        for processor in self.processors:
            hook = getattr(processor, "before_request", None)
            if hook is None:
                continue
            outcome = hook(messages)
            if isinstance(outcome, Block):
                return outcome
            if outcome is not None:
                messages = outcome
        return messages

    def before_tool(self, name: str, args: dict) -> dict | Block:
        for processor in self.processors:
            hook = getattr(processor, "before_tool", None)
            if hook is None:
                continue
            outcome = hook(name, args)
            if isinstance(outcome, Block):
                return outcome
            if outcome is not None:
                args = outcome
        return args

    def after_tool(self, name: str, result: str) -> str:
        for processor in self.processors:
            hook = getattr(processor, "after_tool", None)
            if hook is None:
                continue
            outcome = hook(name, result)
            if outcome is not None:
                result = outcome
        return result

    def stream_tool(self, name: str, chunk: str) -> str:
        """Process only tool chunks that are being sent to the user interface."""
        for processor in self.processors:
            hook = getattr(processor, "stream_tool", None)
            if hook is None:
                continue
            outcome = hook(name, chunk)
            if outcome is not None:
                chunk = outcome
        return chunk

    def after_turn(self, turn) -> Retry | None:
        for processor in self.processors:
            hook = getattr(processor, "after_turn", None)
            if hook is None:
                continue
            outcome = hook(turn)
            if isinstance(outcome, Retry):
                return outcome
        return None

    def terminal_issues(self, answer: str = "") -> list[str]:
        """Collect unresolved conditions from processors in registration order."""
        issues: list[str] = []
        for processor in self.processors:
            hook = getattr(processor, "terminal_issues", None)
            if hook is None:
                continue
            for issue in hook(answer) or []:
                if issue and issue not in issues:
                    issues.append(issue)
        return issues


class ChildChain(Chain):
    """Processor chain for delegated child agents.

    Keeps only processors marked ``child_safe`` (evidence and integrity
    recording) and never forwards ``start_episode``: a child task must not
    reset the parent's episode state, and parent-scoped gates (completion,
    goal, final report, hooks) must not retry a child over parent duties.
    Processors with ``child_view()`` are replaced by that view so a shared
    gate can record a child's evidence without imposing the parent's
    episode contract on the child.
    """

    def __init__(self, parent: "Chain | None") -> None:
        safe = [] if parent is None else [
            p for p in parent.processors if getattr(p, "child_safe", False)]
        super().__init__([
            p.child_view() if hasattr(p, "child_view") else p
            for p in safe])

    def start_episode(self, task: str) -> None:
        return None


# --- Built-in processors --------------------------------------------------


class SecretRedactor:
    """Remove credentials from tool results before they reach the model."""

    # Evidence-redaction stays active inside delegated children.
    child_safe = True

    PATTERNS = ("UPSTAGE_API_KEY", "TINYFISH_API_KEY", "OPENAI_API_KEY",
                "AWS_SECRET", "PRIVATE KEY", "X-API-Key",
                "Authorization: Bearer", "Bearer ")

    def after_tool(self, name: str, result: str) -> str:
        for pattern in self.PATTERNS:
            if pattern not in result:
                continue
            trailing_newline = result.endswith("\n")
            out = []
            for line in result.splitlines():
                out.append("[비밀값 제거됨]" if pattern in line else line)
            result = "\n".join(out)
            if trailing_newline:
                result += "\n"
        return result

    def stream_tool(self, name: str, chunk: str) -> str:
        return self.after_tool(name, chunk)


class PathGuard:
    """Block access outside the workspace directory."""

    def __init__(self, root: str, writable: bool = False) -> None:
        import os
        self.root = os.path.abspath(root)
        self.writable = writable

    def before_tool(self, name: str, args: dict) -> dict | Block:
        import os
        path = args.get("path")
        if not path:
            return args
        target = os.path.abspath(os.path.join(self.root, path))
        if not target.startswith(self.root):
            return Block(f"작업 디렉터리 밖 경로는 허용되지 않습니다: {path}")
        return args


class ToolAvoidanceGuard:
    """Discourage unnecessary tool calls and repeated identical invocations."""

    # Repetition blocking is stateless enough to share with children.
    child_safe = True

    def __init__(self, limit: int = 2) -> None:
        self.limit = limit
        self._seen: dict[tuple, int] = {}

    def before_tool(self, name: str, args: dict) -> dict | Block:
        import json
        key = (name, json.dumps(args, ensure_ascii=False, sort_keys=True))
        self._seen[key] = self._seen.get(key, 0) + 1
        if self._seen[key] > self.limit:
            return Block(
                f"{name}을(를) 같은 인자로 {self.limit}회 이상 호출했습니다. "
                "이미 받은 결과를 사용하세요.")
        return args


class OutputGuard:
    """Prevent forbidden terms from reaching the final answer."""

    def __init__(self, banned: list[str], on_hit: Callable | None = None) -> None:
        self.banned = banned
        self.on_hit = on_hit

    def after_turn(self, turn) -> Retry | None:
        for word in self.banned:
            if word in (turn.content or ""):
                if self.on_hit:
                    self.on_hit(word)
                return Retry(f"금지된 표현({word})이 포함되어 다시 작성이 필요합니다.")
        return None
