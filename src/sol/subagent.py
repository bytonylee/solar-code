"""Run focused child agents without polluting the parent context.

Children return concise results, can use isolated worktrees for implementation,
and support bounded parallel delegation for independent tasks.
"""

import concurrent.futures
import json
import os
import threading

from . import model, worktree
from .loop import Agent
from .prefix import StablePrefix
from .processors import ChildChain
from .tools import Registry, Tool

# Keep a child from outliving its parent. Allow enough turns for investigation,
# verification, and a final summary without permitting unbounded delegation.
CHILD_MAX_ROUNDS = 12

# Concurrent child limit. Token cost grows linearly with concurrency, so keep the
# default conservative.
MAX_PARALLEL = 4


NOTES_DIR = ".sol/notes"
_notes_lock = threading.Lock()
_notes_seq: dict[str, int] = {}


def notes_root(root: str | None = None) -> str:
    base = root or os.getcwd()
    path = os.path.join(base, NOTES_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def next_notes_path(role: str, root: str | None = None,
                    prefix: str = "", suffix: str = ".md") -> str:
    """Return a deterministic role-sequence notes path under .sol/notes/.

    Sequence numbers are process-local and monotonic per role (or prefix+role).
    Files are not deleted; the user cleans them up.
    """
    directory = notes_root(root)
    key = f"{prefix}:{role}:{suffix}"
    with _notes_lock:
        n = _notes_seq.get(key, 0) + 1
        _notes_seq[key] = n
    safe_role = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in role)
    if prefix:
        safe_prefix = "".join(
            ch if ch.isalnum() or ch in "-_" else "-" for ch in prefix)
        name = f"{safe_prefix}-{safe_role}-{n}{suffix}"
    else:
        name = f"{safe_role}-{n}{suffix}"
    return os.path.join(directory, name)


def write_notes(content: str, role: str, root: str | None = None,
                prefix: str = "", suffix: str = ".md") -> str:
    """Write full content to a notes file and return its path."""
    if not suffix.startswith("."):
        suffix = "." + suffix
    path = next_notes_path(role, root=root, prefix=prefix, suffix=suffix)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content if content is not None else "")
    return path


SUBAGENT_SYSTEM = """당신은 상위 에이전트의 요청을 받아 한 가지 조사를 수행하는 보조 에이전트입니다.

원칙:
1. 요청받은 것만 조사하고, 범위를 넓히지 않습니다.
2. 결과는 상위 에이전트가 그대로 쓸 수 있도록 간결한 결론으로 정리합니다.
3. 찾은 근거는 파일 경로와 줄 번호로 제시합니다.
4. 필요한 조사와 확인이 모두 끝나기 전에 결론을 내리지 않습니다.
5. 끝날 때 수행한 확인, 결론, 남은 불확실성을 구분해 보고합니다."""


ROLES = {
    "explorer": (
        "당신은 코드베이스 조사 담당입니다. 요청된 정보를 찾아 파일 경로와 "
        "줄 번호로 근거를 제시합니다. 추측하지 않고 실제로 읽은 것만 보고합니다. "
        "파일을 수정하지 않습니다."),
    "reviewer": (
        "당신은 코드 리뷰 담당입니다. 버그, 경계 조건, 회귀 위험을 심각도 순으로 "
        "지적하고 각 지적에 파일 경로와 줄 번호를 답니다. 칭찬이나 요약보다 "
        "문제를 먼저 씁니다. 파일을 수정하지 않습니다."),
    "tester": (
        "당신은 테스트 담당입니다. 관련 테스트를 실행하고 실패의 원인을 "
        "진단합니다. 실패 로그의 핵심 줄을 인용하고 원인을 한 문장으로 "
        "정리합니다."),
    "implementer": (
        "당신은 구현 담당입니다. 요청된 변경만 수행하고 무관한 정리는 하지 "
        "않습니다. 수정 전에 반드시 파일을 읽고, 변경한 파일 경로를 보고합니다."),
    "researcher": (
        "당신은 웹 출처 조사 담당입니다. 요청받은 URL과 질의를 web_search와 "
        "web_fetch로 직접 확인하고, URL별로 뒷받침되는 주장과 접근 실패를 "
        "구분해 보고합니다. 검색 스니펫만으로 확인했다고 주장하지 않으며, "
        "content_warning이 붙은 페이지는 근거에서 제외합니다. 근거는 파일 "
        "경로 대신 확인한 URL로 제시합니다. 파일을 수정하지 않습니다."),
}

# Read-only roles do not receive write tools.
READ_ONLY_ROLES = ("explorer", "reviewer", "researcher")


def spawn(task: str, registry: Registry, effort: str = model.Effort.OFF,
          processors=None, complete=None, role: str = "explorer",
          workspace_factory=None) -> dict:
    """Run one child agent and return a concise outcome.

    When provided, ``workspace_factory`` gives implementers an isolated worktree.
    """
    read_only = role not in ("implementer", "tester")
    isolated = None
    cleanup = None

    if not read_only and workspace_factory is not None:
        try:
            isolated = worktree.create(workspace_factory.root, role)
            registry, cleanup = workspace_factory(isolated.path)
        except worktree.WorktreeError:
            # Non-repositories run without isolation rather than failing the task.
            isolated = None

    child_registry = Registry()
    for spec in registry.specs():
        name = spec["function"]["name"]
        tool = registry.get(name)
        if tool is None or name in ("spawn_agent", "delegate"):
            continue
        # Investigation and review roles remain read-only so the worktree cannot
        # change without the parent knowing.
        if read_only and not tool.read_only:
            continue
        child_registry.add(tool)

    role_prompt = ROLES.get(role, ROLES["explorer"])
    system = f"{SUBAGENT_SYSTEM}\n\n역할 지침:\n{role_prompt}"
    try:
        # Keep worktree paths out of the prefix so each child shares the same cache key.
        child = Agent(StablePrefix(system, child_registry.specs()),
                      child_registry, effort=effort,
                      max_rounds=CHILD_MAX_ROUNDS,
                      processors=ChildChain(processors), complete=complete)
        episode = child.run(task)
        # Keep result as the concise summary. Persist the full answer externally so
        # the parent can restore details without polluting its context window.
        answer = episode.answer or ""
        notes_path = write_notes(answer, role=role)
        outcome = {"role": role,
                   "result": answer,
                   "notes_path": notes_path,
                   "tools_used": [t.tool for t in episode.trace],
                   "starved": episode.starved,
                   "incomplete": episode.incomplete,
                   "status": episode.status,
                   "rounds_exhausted": episode.rounds_exhausted,
                   "stalled": episode.stalled,
                   "incomplete_reasons": episode.incomplete_reasons,
                   "reasoning_tokens": episode.reasoning_tokens}
        if isolated is not None:
            # The parent reviews changes before applying them; do not merge automatically.
            outcome["worktree"] = isolated.path
            outcome["changed_files"] = isolated.changed_files()
            # This summary is for the model. A truncated diff cannot be applied, so
            # direct application through the worktree path instead.
            patch = isolated.diff()
            outcome["diff"] = patch[:8000]
            outcome["diff_truncated"] = len(patch) > 8000
        return outcome
    finally:
        if cleanup is not None:
            cleanup()


def delegate(tasks: list[dict], registry: Registry,
             effort: str = model.Effort.OFF, processors=None,
             complete=None, parallel: int = MAX_PARALLEL,
             workspace_factory=None) -> list[dict]:
    """Run independent child roles sequentially or in parallel."""
    if not tasks:
        return []

    def one(item: dict) -> dict:
        try:
            return spawn(item["task"], registry, effort=effort,
                         processors=processors, complete=complete,
                         role=item.get("role", "explorer"),
                         workspace_factory=workspace_factory)
        except Exception as exc:  # noqa: BLE001 - one child must not stop the batch.
            return {"role": item.get("role", "explorer"), "result": "",
                    "notes_path": "",
                    "error": str(exc), "tools_used": [], "starved": False,
                    "incomplete": True, "status": "error",
                    "rounds_exhausted": False,
                    "stalled": False,
                    "incomplete_reasons": [str(exc)],
                    "reasoning_tokens": 0}

    if len(tasks) == 1 or parallel <= 1:
        return [one(item) for item in tasks]
    with concurrent.futures.ThreadPoolExecutor(min(parallel, len(tasks))) as pool:
        return list(pool.map(one, tasks))


def spawn_tool(registry: Registry, effort: str = model.Effort.OFF,
               processors=None, complete=None,
               processors_ref: dict | None = None) -> Tool:
    """Build the parent's ``spawn_agent`` delegation tool."""
    def run(args: dict) -> dict:
        resolved = processors if processors is not None else \
            (processors_ref or {}).get("processors")
        return spawn(args["task"], registry, effort=effort,
                     processors=resolved, complete=complete,
                     role=args.get("role", "explorer"))

    return Tool(
        name="spawn_agent",
        description=(
            "보조 에이전트에게 조사를 위임하고 결론만 받는다. "
            "여러 파일을 뒤져야 하는 넓은 탐색에만 쓴다. "
            "파일 한두 개를 확인하는 정도라면 read_file을 직접 쓰는 것이 빠르고 싸다."),
        parameters={"type": "object",
                    "properties": {"task": {"type": "string",
                                            "description": "조사 내용. 구체적일수록 좋다"},
                                   "role": {"type": "string",
                                            "enum": sorted(ROLES),
                                            "description": "보조 에이전트의 역할"}},
                    "required": ["task"]},
        run=run)


def delegate_tool(registry: Registry, effort: str = model.Effort.OFF,
                  processors=None, complete=None, workspace_factory=None,
                  processors_ref: dict | None = None) -> Tool:
    """Build the parent's parallel ``delegate`` tool."""
    def run(args: dict) -> dict:
        resolved = processors if processors is not None else \
            (processors_ref or {}).get("processors")
        results = delegate(args["tasks"], registry, effort=effort,
                           processors=resolved, complete=complete,
                           workspace_factory=workspace_factory)
        return {"results": results,
                "succeeded": sum(1 for r in results
                                 if not r.get("error")
                                 and not r.get("incomplete")),
                "total": len(results)}

    return Tool(
        name="delegate",
        description=(
            "독립적인 작업 여러 개를 역할별 보조 에이전트에게 동시에 맡긴다. "
            "explorer/reviewer는 읽기 전용, implementer/tester는 작업을 수행한다. "
            "implementer는 격리된 git worktree에서 일하므로 서로 충돌하지 않으며, "
            "변경 내용은 diff로 돌려받아 검토한 뒤 적용한다. "
            "서로 의존하는 작업에는 쓰지 않는다. 통합과 최종 검증은 직접 한다."),
        parameters={"type": "object",
                    "properties": {
                        "tasks": {"type": "array",
                                  "description": f"최대 {MAX_PARALLEL}개 권장",
                                  "items": {"type": "object",
                                            "properties": {
                                                "role": {"type": "string",
                                                         "enum": sorted(ROLES)},
                                                "task": {"type": "string"}},
                                            "required": ["role", "task"]}}},
                    "required": ["tasks"]},
        run=run)
