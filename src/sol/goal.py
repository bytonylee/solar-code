"""Evaluate explicit completion criteria across repeated agent passes.

The goal loop separates planned work from observable result criteria. A goal gate
requests another pass when criteria remain unmet, while file, regex, and answer
checks report failures as unmet conditions instead of raising.
"""

import os
import re
from dataclasses import dataclass, field

from .loop import Episode, Event
from .processors import Retry

# Maximum outer-loop rounds. This prevents infinite retries and leaves room for
# an honest final report when the limit is reached. The value is a design choice.
MAX_PASSES = 4

VALID_KINDS = ("file_contains", "file_exists", "answered")
_FILE_KINDS = ("file_contains", "file_exists")


def _contained(root: str, path: str) -> bool:
    """Return whether path resolves inside root."""
    if not path:
        return False
    base = os.path.realpath(root)
    target = os.path.realpath(os.path.join(base, path))
    return target == base or target.startswith(base + os.sep)


@dataclass
class Criterion:
    """One observable completion criterion.

    Supported kinds check file contents, file existence, or answer content. A
    model claim never satisfies a criterion without the corresponding evidence.
    """

    kind: str
    label: str                  # Human-readable criterion used in follow-up tasks.
    path: str = ""
    pattern: str = ""
    met: bool = False
    error: str = ""

    def check(self, workspace_root: str, answer: str) -> bool:
        """Check the criterion again and treat failures as unmet."""
        self.error = ""
        if self.kind in _FILE_KINDS and not _contained(workspace_root,
                                                       self.path):
            self.met = False
            self.error = f"작업 디렉터리 밖 경로: {self.path}"
            return False
        if self.kind == "file_exists":
            # Directories do not satisfy this file criterion.
            self.met = os.path.isfile(os.path.join(workspace_root,
                                                   self.path))
        elif self.kind == "file_contains":
            self.met = self._file_contains(workspace_root)
        elif self.kind == "answered":
            self.met = self._answered(answer)
        else:
            self.met = False
            self.error = f"알 수 없는 kind: {self.kind}"
        return self.met

    def _match(self, pattern: str, text: str) -> bool:
        try:
            return bool(re.search(pattern, text,
                                  re.IGNORECASE | re.DOTALL))
        except re.error as exc:
            self.error = f"정규식 오류: {exc}"
            return False

    def _file_contains(self, root: str) -> bool:
        full = os.path.join(root, self.path)
        if os.path.isdir(full):
            self.error = f"디렉터리는 검사할 수 없습니다: {self.path}"
            return False
        try:
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            self.error = f"읽기 실패: {exc}"
            return False
        return self._match(self.pattern, text)

    def _answered(self, answer: str) -> bool:
        if not self.pattern:
            self.error = "pattern이 비어 있습니다"
            return False
        return self._match(self.pattern, answer or "")


@dataclass
class Goal:
    """Goal containing a list of completion criteria."""

    objective: str
    criteria: list[Criterion] = field(default_factory=list)
    passes: int = 0

    def unmet(self) -> list[Criterion]:
        return [c for c in self.criteria if not c.met]

    def evaluate(self, workspace_root: str, answer: str) -> list[Criterion]:
        """Recheck every criterion and return the unmet list."""
        for criterion in self.criteria:
            criterion.check(workspace_root, answer)
        return self.unmet()

    def followup_task(self) -> str:
        """Build the next-pass task from unmet criteria."""
        lines = [f"목표가 아직 완성되지 않았습니다: {self.objective}",
                 "", "다음 항목이 빠져 있습니다. 이번에는 이것만 채우십시오:"]
        for criterion in self.unmet():
            line = f"- {criterion.label}"
            if criterion.error:
                line += f" (판정 사유: {criterion.error})"
            lines.append(line)
        lines.append("")
        lines.append("이미 완료된 부분은 다시 만들지 말고 수정만 하십시오.")
        return "\n".join(lines)

    def render(self) -> str:
        marks = {True: "[x]", False: "[ ]"}
        lines = [f"목표: {self.objective} (회차 {self.passes})"]
        for criterion in self.criteria:
            line = f"{marks[criterion.met]} {criterion.label}"
            if criterion.error:
                line += f" -- {criterion.error}"
            lines.append(line)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"objective": self.objective, "passes": self.passes,
                "criteria": [{"kind": c.kind, "label": c.label, "met": c.met,
                              **({"error": c.error} if c.error else {})}
                             for c in self.criteria],
                "unmet": [c.label for c in self.unmet()]}


def set_goal_tool(holder: dict, workspace_root: str):
    """Build a read-only tool that declares observable completion criteria."""
    from .tools import Tool

    def run(args: dict) -> dict:
        objective = str(args.get("objective") or "").strip()
        if not objective:
            return {"error": "objective가 비어 있습니다"}
        items = args.get("criteria")
        if not isinstance(items, list) or not items:
            return {"error": "기준이 비어 있습니다"}
        criteria = []
        for item in items:
            if not isinstance(item, dict):
                return {"error": "각 기준은 객체여야 합니다"}
            kind = str(item.get("kind") or "")
            if kind not in VALID_KINDS:
                return {"error": f"kind는 {VALID_KINDS} 중 하나여야 합니다"}
            label = str(item.get("label") or "").strip()
            if not label:
                return {"error": "각 기준에 label이 필요합니다"}
            path = str(item.get("path") or "").strip()
            pattern = str(item.get("pattern") or "")
            if kind in _FILE_KINDS:
                if not path:
                    return {"error": f"{kind} 기준에 path가 필요합니다: "
                                     f"{label}"}
                if not _contained(workspace_root, path):
                    return {"error": "작업 디렉터리 밖 경로는 기준으로 쓸 수 "
                                     f"없습니다: {path}"}
            if kind in ("file_contains", "answered") and not pattern:
                return {"error": f"{kind} 기준에 pattern이 필요합니다: "
                                 f"{label}"}
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    return {"error": f"정규식이 유효하지 않습니다 "
                                     f"({label}): {exc}"}
            criteria.append(Criterion(kind=kind, label=label, path=path,
                                      pattern=pattern))
        holder["goal"] = Goal(objective=objective, criteria=criteria)
        return {"set": True, "criteria": len(criteria)}

    return Tool(
        name="set_goal",
        description=(
            "작업의 완성 기준을 선언한다. 여러 요소가 갖춰져야 완성인 "
            "작업(페이지, 기능, 문서)을 시작할 때 가장 먼저 호출한다. "
            "기준은 파일 내용/존재/답변 내용으로 기계 검사되므로, 사용자가 "
            "당연히 기대할 요소(이미지, 연결 설명, 반응형 등)를 빠짐없이 "
            "선언해야 한다."),
        parameters={
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "한 문장 목표"},
                "criteria": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "kind": {"type": "string", "enum": list(VALID_KINDS)},
                        "label": {"type": "string",
                                  "description": "기준 설명 (한국어)"},
                        "path": {"type": "string",
                                 "description": "검사할 파일 (file_* 전용)"},
                        "pattern": {"type": "string",
                                    "description": "정규식. 예: <img|이미지"}},
                        "required": ["kind", "label"]}}},
            "required": ["objective", "criteria"]},
        run=run, read_only=True)


class GoalGate:
    """Request another turn when declared criteria remain unmet."""

    def __init__(self, holder: dict, workspace_root: str) -> None:
        self.holder = holder
        self.root = workspace_root

    def after_turn(self, turn) -> Retry | None:
        if turn.tool_calls:
            return None
        content = (turn.content or "").strip()
        if not content:
            return None
        goal = self.holder.get("goal")
        if goal is None:
            return None
        unmet = goal.evaluate(self.root, content)
        if not unmet:
            return None
        labels = ", ".join(c.label for c in unmet[:3])
        more = f" 외 {len(unmet) - 3}건" if len(unmet) > 3 else ""
        needs_tool = any(item.kind in _FILE_KINDS for item in unmet)
        return Retry(
            f"선언한 완성 기준이 아직 충족되지 않았습니다: {labels}{more}. "
            "해당 요소를 실제로 만들거나, 만들 수 없다면 그 이유를 "
            "답변에 명시하십시오.",
            tool_choice="required" if needs_tool else None)

    def terminal_issues(self, answer: str = "") -> list[str]:
        goal = self.holder.get("goal")
        if goal is None:
            return []
        unmet = goal.evaluate(self.root, answer)
        if not unmet:
            return []
        labels = ", ".join(item.label for item in unmet[:5])
        more = f" 외 {len(unmet) - 5}건" if len(unmet) > 5 else ""
        return [f"선언한 완성 기준이 미충족입니다: {labels}{more}"]


def ralph_stream(agent, task: str, holder: dict, workspace_root: str,
                 max_passes: int = MAX_PASSES, episodes: list | None = None):
    """Stream repeated passes until all goal criteria are met or limited.

    Pass through agent events and add pass and goal boundary events. Return the
    final episode and goal through the generator's stop value.
    """
    goal = None
    episode = Episode()
    passes = 0
    next_task = task
    while True:
        passes += 1
        yield Event("pass_start", {"pass": passes,
                                   "max_passes": max_passes})
        episode = Episode()
        for event in agent.stream(next_task, episode=episode):
            yield event
        if episodes is not None:
            episodes.append(episode)
        goal = holder.get("goal")
        if goal is None:
            yield Event("pass_end", {"pass": passes, "goal": False})
            break
        goal.passes = passes
        unmet = goal.evaluate(workspace_root, episode.answer)
        yield Event("pass_end", {"pass": passes, "goal": True,
                                 "unmet": [c.label for c in unmet]})
        if not unmet:
            yield Event("goal_complete", {"passes": passes,
                                          "objective": goal.objective})
            break
        yield Event("goal_unmet", {"pass": passes,
                                   "unmet": [c.label for c in unmet]})
        if passes >= max_passes:
            yield Event("goal_limit", {
                "passes": passes, "unmet": [c.label for c in unmet],
                "note": "회차 상한에 도달했습니다. 남은 기준은 미충족으로 "
                        "보고합니다."})
            break
        next_task = goal.followup_task()
    return episode, goal


def ralph(agent, task: str, holder: dict, workspace_root: str,
          max_passes: int = MAX_PASSES, on_pass=None,
          episodes: list | None = None):
    """Run the goal loop and return the final episode and goal."""
    stream = ralph_stream(agent, task, holder, workspace_root,
                          max_passes=max_passes, episodes=episodes)
    seen_unmet: tuple | None = None
    while True:
        try:
            event = next(stream)
        except StopIteration as done:
            return done.value
        if on_pass is not None and event.kind == "goal_unmet":
            key = tuple(event.data.get("unmet") or ())
            if key != seen_unmet:
                seen_unmet = key
                goal = holder.get("goal")
                if goal is not None:
                    on_pass(goal)


def usage_summary(agent, goal: Goal | None, passes: int) -> dict:
    """Return usage and cache summary data for the completed passes."""
    ledger = agent.cache
    return {"requests": ledger.requests,
            "prompt_tokens": ledger.prompt_tokens,
            "cached_tokens": ledger.cached_tokens,
            "cache_hit_rate": round(ledger.hit_rate, 3),
            "passes": passes,
            "unmet": [c.label for c in goal.unmet()] if goal else [],
            "prefix_breaks": list(ledger.breaks)}
