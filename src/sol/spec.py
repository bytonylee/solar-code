"""Turn ambiguous task requests into executable specifications.

Keep scope, forbidden work, completion criteria, effort routing, steps, and
assumptions in one deterministic block attached to the user task.
"""

import json
import re
from dataclasses import dataclass, field

from . import client, model

# Heuristic inputs for deciding whether a task needs a specification.
_PATH_RE = re.compile(r"[\w./-]+\.[A-Za-z]{1,4}\b")   # calc.py, src/a.ts
_VAGUE_VERBS = ("정리", "개선", "손봐", "다듬", "알아서", "적당히", "깔끔하게",
                "리팩터링", "리팩토링", "최적화")
_CONCRETE_VERBS = ("고쳐", "수정", "추가", "삭제", "바꿔", "작성", "만들어",
                   "구현", "실행", "돌려", "읽어", "요약", "설명")


def ambiguity(task: str) -> str:
    """Estimate whether a task is low or high ambiguity conservatively."""
    has_path = bool(_PATH_RE.search(task))
    vague = any(v in task for v in _VAGUE_VERBS)
    concrete = any(v in task for v in _CONCRETE_VERBS)
    if vague:
        return "high"
    if has_path and concrete:
        return "low"
    if len(task) < 15:
        # A short request may not identify its target.
        return "high"
    return "low" if concrete else "high"


@dataclass
class Spec:
    """Serializable task specification with scope and completion criteria."""

    goal: str
    scope_files: list[str] = field(default_factory=list)
    scope_forbidden: list[str] = field(default_factory=list)
    done_criteria: list[dict] = field(default_factory=list)
    effort_route: str = model.Effort.OFF
    steps: list[str] = field(default_factory=list)
    ambiguities: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"goal": self.goal, "scope_files": self.scope_files,
                "scope_forbidden": self.scope_forbidden,
                "done_criteria": self.done_criteria,
                "effort_route": self.effort_route, "steps": self.steps,
                "ambiguities": self.ambiguities}

    @classmethod
    def from_dict(cls, data: dict) -> "Spec":
        route = data.get("effort_route", model.Effort.OFF)
        if route not in model.Effort.CHOICES:
            route = model.Effort.OFF
        return cls(goal=str(data.get("goal") or ""),
                   scope_files=[str(p) for p in data.get("scope_files") or []],
                   scope_forbidden=[str(p) for p in data.get("scope_forbidden") or []],
                   done_criteria=[c for c in data.get("done_criteria") or []
                                  if isinstance(c, dict)],
                   effort_route=route,
                   steps=[str(s) for s in data.get("steps") or []],
                   ambiguities=[a for a in data.get("ambiguities") or []
                                if isinstance(a, dict)])

    def render_block(self) -> str:
        """Render a deterministic specification block before the user task."""
        lines = ["[작업 명세]", f"목표: {self.goal}"]
        if self.scope_files:
            lines.append("대상: " + ", ".join(self.scope_files))
        if self.scope_forbidden:
            lines.append("금지: " + ", ".join(self.scope_forbidden))
        if self.steps:
            lines.append("단계: " + " -> ".join(self.steps))
        for criterion in self.done_criteria:
            if criterion.get("kind") == "dynamic":
                lines.append(f"완료 기준(실행): {criterion.get('command', '')}")
            else:
                lines.append(f"완료 기준(확인): {criterion.get('check', '')}")
        for item in self.ambiguities:
            lines.append(f"가정: {item.get('question', '')}"
                         f" -> {item.get('assumed', '')}")
        return "\n".join(lines)


SPEC_SYSTEM = """당신은 코딩 작업 지시를 명세로 변환합니다.

JSON 하나만 출력합니다. 다른 텍스트를 붙이지 않습니다. 키:
  goal            한 문장 목표
  scope_files     대상 파일/디렉터리 경로 목록. 지시에 없으면 빈 배열
  scope_forbidden 하지 말아야 할 것 목록 (예: "무관한 리팩터링")
  done_criteria   완료 기준 목록. {"kind":"dynamic","command":"..."} 또는
                  {"kind":"static","check":"..."}
  effort_route    "off" 또는 "on". 형식이 엄격하거나 단순한 작업은 off,
                  정밀한 계산 검증이 필요한 작업만 on
  steps           2~6개의 실행 단계
  ambiguities     지시만으로 정할 수 없는 것. {"question":"...","assumed":"..."}
                  assumed는 가장 보수적인 가정. 없으면 빈 배열

지시에 없는 것을 지어내지 않습니다. 범위가 불명확하면 ambiguities에 씁니다."""


class SpecError(RuntimeError):
    pass


def draft(task: str, complete=None) -> Spec:
    """Draft a specification with one low-effort model call."""
    send = complete or client.complete
    turn = send([{"role": "system", "content": SPEC_SYSTEM},
                 {"role": "user", "content": task}],
                effort=model.Effort.OFF)
    if turn.starved or not turn.content.strip():
        raise SpecError("명세 생성에서 모델이 답을 내지 못했습니다")
    text = turn.content.strip()
    fence = re.match(r"^```[a-zA-Z]*\n(.*?)\n?```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecError(f"명세 JSON 파싱 실패: {exc}") from exc
    if not isinstance(data, dict) or not str(data.get("goal") or "").strip():
        raise SpecError("명세에 goal이 없습니다")
    return Spec.from_dict(data)


def compose_task(spec: Spec, task: str) -> str:
    """Prefix the original task with its specification block."""
    return f"{spec.render_block()}\n\n[원래 지시]\n{task}"
