"""Command-line entry point for sol.

Supports the full-screen TUI, approval-based task execution, model selection,
session continuation, and local branch switching.
"""

import argparse
import getpass
import json
import os
import sys

from . import (cacheprobe, cassette as tape, client, codesearch, context,
               credentials, git, github, goal as goal_mod,
               interact as interact_mod, lsp, model, recall,
               session as session_mod, spec as spec_mod, tracker, vcs,
               webfetch, websearch, worktree)
from .hooks import ShellHooks
from .guards import (CompletionGate, FinalReportGate, RepetitionGuard,
                     VerificationGate, WebSearchGate)
from .loop import Agent, DEFAULT_MAX_ROUNDS, Episode
from .permissions import ALLOW, resolve as resolve_permissions
from .prefix import StablePrefix
from .progress import LiveCLI
from .processors import Chain, SecretRedactor, ToolAvoidanceGuard
from .session import Session
from .subagent import delegate_tool, spawn_tool
from .tools import Registry
from .workspace import Workspace, WorkspaceFactory
from . import mcpclient

BASE_SYSTEM = """당신은 한국어로 일하는 코딩 에이전트입니다.

실행 원칙:
1. 파일 내용은 도구로 직접 확인하고, 기억이나 추측에 의존하지 않습니다.
2. 수정하기 전에 반드시 해당 파일을 읽습니다.
3. 요청받은 범위만 변경하고, 무관한 정리나 리팩터링은 하지 않습니다.
4. 구현 요청은 제안에서 멈추지 않고 구현, 검사, 검증, 최종 보고까지
   자율적으로 이어갑니다. 도구 결과 하나가 나왔다는 이유로 멈추지 않습니다.
5. 정보가 부족하면 먼저 저장소와 실행 환경에서 찾고, 되돌릴 수 있는 선택은
   기존 관례에 맞춰 진행합니다. 되돌리기 어렵고 결과를 크게 바꾸는 정보만
   질문하며, 단순한 불확실성을 중단 사유로 사용하지 않습니다.

완료 계약:
6. 세 단계 이상인 작업은 시작할 때 plan_tasks로 구체적인 작업 목록을 만들고,
   각 항목을 시작할 때 doing, 실제 완료 후 done으로 갱신합니다.
7. open 또는 doing 항목이 하나라도 남아 있으면 최종 답변을 내지 않습니다.
   작업을 계속하거나, 외부 권한·입력처럼 스스로 해결할 수 없는 경우에만
   blocked로 바꾸고 구체적인 사유를 기록합니다.
8. 최종 답변 전에는 원래 요청과 작업 목록을 다시 대조하고, 생성된 결과를
   직접 읽어 누락·오류·사용자 변경 훼손이 없는지 확인합니다.
9. 코드나 설정을 바꿨다면 마지막 변경 이후 가능한 테스트·빌드·정적 검사를
   실행합니다. 테스트가 없는 산출물은 구문 검사나 로컬 렌더 확인처럼 가장
   작은 실행 확인을 하고, 실패하면 원인을 해결하고 재실행합니다. 실행할 수
   없는 검증은 생략하지 말고 미검증 사실과 이유를 최종 정리에 기록합니다.
   ls, cat 같은 조회 명령은 검증으로 치지 않습니다.
10. 라운드 상한, 출력 예산, 정책 차단, 외부 권한 부재만 정당한 강제 중단
    사유입니다. 이 경우 완료했다고 표현하지 않고 한 일과 남은 일을 나눕니다.
11. 모든 실행은 최종 정리로 끝냅니다. 변경한 동작과 파일 경로, 실제 검증
    결과, 남은 작업 또는 차단 사유를 간결하게 보고합니다.
12. 수행하지 않은 작업이나 관찰하지 않은 테스트를 완료했다고 주장하지
    않습니다.

대용량 파일과 검증:
13. 웹 페이지처럼 큰 산출물은 한 파일에 몰아넣지 말고 HTML, CSS, JS를
    별도 파일로 나눠 생성합니다. 파일 하나도 write_file 상한을 넘기면
    조각으로 나눠 append=true로 이어 씁니다.
14. 도구 인자 스트림이 절단되어 실패하면 같은 내용을 처음부터 다시
    만들지 않고, 이미 쓰인 부분을 확인한 뒤 나머지 조각만 이어 씁니다.
15. JavaScript 구문 검증은 node --check 하나로 고정합니다. Python 파서,
    임의 문자열 검사, 설치되지 않은 패키지로 JS를 검사하지 않습니다.
    HTML과 CSS 검증도 각각 한 가지 방법으로 1회 실행하고, 같은 검사를
    도구만 바꿔 반복하지 않습니다.
16. 이미 read_file로 읽은 파일을 통째로 다시 읽지 않습니다. 일부 확인은
    start_line/end_line 범위 조회나 grep을 사용합니다.

웹 검색·페치 무결성:
- 사용자가 레퍼런스, 조사, 출처 확인을 요구한 작업은 web_search에서 유효한
  HTTP(S) 출처를 확보하기 전 파일 쓰기나 셸 실행으로 넘어가지 않습니다.
- 발견은 web_search, 페이지 직접 확인은 web_fetch가 담당합니다. 검색
  스니펫만 보고 "공식 사이트를 확인했다"고 쓰지 않습니다.
- web_fetch 결과에 content_warning이 있으면 그 페이지는 근거로 쓰지
  않습니다.
- 근거 기반 종합은 (1) 질의를 2~3개로 나눠 검색하고 (2) 도메인 중복 없이
  5개 이내로 선별하고 (3) web_fetch로 읽고 (4) 출처별 근거 카드를 모은 뒤
  (5) [n] 인용과 출처 목록으로 종합합니다. 출처 목록의 각 항목에는
  verified_fetch(직접 읽음) 또는 search_only(검색 결과만) 등급을 표기합니다.
- provider 오류, 차단, 파싱 실패, 빈 결과는 검색 성공이 아닙니다. 기억으로
  출처를 대신하지 말고 실패 사유와 완료하지 못한 범위를 최종 답변에
  명시합니다."""

TRUTH_SYSTEM = """

생성 콘텐츠 사실성:
- 산출물에 넣는 수치, 고객 후기, 인증, 수상, 등록 정보, 지급률처럼 사실로
  보이는 내용은 사용자가 제공했거나 출처를 확인한 경우에만 씁니다. 확인하지
  못한 사실은 지어내지 않습니다.
- 디자인 검토용 값이 필요하면 "예시", "예상", "조건에 따라 달라짐"을 같은
  화면에 붙여 실제 데이터처럼 보이지 않게 합니다.
- 가상의 인물 이름, 직업, 별점, 절감액을 후기로 만들지 않습니다. 후기
  자리에는 확인 가능한 절차나 원칙을 씁니다."""

# Plan-mode instructions are deterministic. Keep tracker, question, and goal
# state out of the system prefix; expose it through tools, sessions, and results.
PLAN_SYSTEM = """

계획 모드 규칙:
1. 여러 요소가 갖춰져야 완성인 작업(페이지, 기능, 문서)은 set_goal을 가장
   먼저 호출해 완성 기준을 선언합니다.
2. 기준은 "만들었다"는 주장이 아니라 파일/답변의 기계 검사로 판정됩니다.
   주장만으로는 통과되지 않으므로, 사용자가 당연히 기대하는 요소까지
   빠짐없이 선언하십시오.
3. 웹 페이지, 특히 보험 랜딩 페이지라면 다음을 기준에 포함하는지 반드시
   검토합니다: 실제 이미지/시각 자산, CTA와 상담 신청 모달, 각 서비스가
   무엇과 어떻게 연결되는지의 설명, 반응형과 접근성, 법적 고지와 신뢰
   콘텐츠, 그리고 실제 검증(파일 검사 또는 실행).
4. 되돌리기 비싼 결정은 ask_user로 묻습니다. 추측으로 밀어붙이지 않습니다.
5. 웹 검색은 web_search, 페이지 직접 확인은 web_fetch를 사용합니다. 도구가
   오류 봉투를 돌려주면(provider 장애·차단) 확인한 것으로 주장하지 말고,
   검색이 필요한 기준은 미충족으로 남긴 뒤 그 사유를 답변에 명시하십시오.
6. 이미지 검색/생성 도구는 아직 없습니다. 이미지 자산 기준은 실제 로컬
   파일로 충족하거나, 충족할 수 없으면 사유를 명시하십시오."""


def build_agent(args) -> tuple[Agent, StablePrefix, Session | None]:
    selected_model = getattr(args, "model", None)
    if selected_model:
        model.select(selected_model)
    root = os.path.abspath(args.root)
    policy = args.permission_policy
    workspace = Workspace(root, allow_write=policy.allow_write,
                          allow_shell=policy.allow_shell)

    registry = Registry()
    skills = context.load_skills(root)
    task_tracker = None
    goal_holder = None
    ask_log = None
    for tool in workspace.tools():
        registry.add(tool)
    if skills:
        registry.add(context.skill_tool(root, skills))
    # AST outline/symbol search is dependency-free and always available.
    for tool in codesearch.tools(root):
        registry.add(tool)
    # LSP tools are registered only when a server binary exists at startup, so
    # the tool schema stays fixed for the whole run (prefix cache contract).
    lsp_pool = None
    if not getattr(args, "no_lsp", False):
        servers = lsp.resolve_servers(root)
        if servers:
            lsp_pool = lsp.Pool(root, servers)
            for tool in lsp.tools(lsp_pool):
                registry.add(tool)
    if not getattr(args, "no_websearch", False):
        # TinyFish when keyed, keyless providers otherwise. A provider
        # failure must stay visible as an error envelope, never a fake result.
        registry.add(websearch.web_search_tool())
    if not getattr(args, "no_webfetch", False):
        # Keyless direct page read; failures surface as error envelopes.
        registry.add(webfetch.web_fetch_tool())
    if getattr(args, "plan", False):
        # Plan mode adds goal and question tools. Share their state with the agent
        # and the outer completion loop.
        goal_holder = {}
        ask_log = interact_mod.AskLog()
        registry.add(goal_mod.set_goal_tool(goal_holder, root))
        registry.add(interact_mod.ask_user_tool(
            ask_log, interactive=not args.yes and sys.stdin.isatty()))
    if not args.no_recall:
        registry.add(recall.recall_tool(session_mod.default_root(), root))

    session: Session | None = None
    continue_session = getattr(args, "continue_session", None)
    if continue_session:
        session = Session.find(continue_session, cwd=root)
        if session is None:
            raise ValueError(f"세션을 찾을 수 없습니다: {continue_session}")
    if session is None and not args.no_session:
        session = Session.create(cwd=root)

    if not args.no_tracker:
        # Keep plans within the same session boundary as the conversation. When
        # sessions are disabled, use a one-off tracker without prior project state.
        task_tracker = tracker.Tracker(root, session=session,
                                       isolated=session is None)
        for tool in tracker.tools(task_tracker):
            registry.add(tool)

    shared_processors: dict = {}
    if not args.no_subagent:
        # Implementers use an isolated worktree. Non-repositories fall back to
        # the current workspace when a worktree cannot be created.
        factory = None
        if policy.allow_write and worktree.is_repo(root):
            worktree.ensure_ignored(root)
            factory = WorkspaceFactory(root, allow_shell=policy.allow_shell)
        # The processors chain is built after registration, so pass a shared
        # box that spawn/delegate resolve when actually invoked.
        registry.add(spawn_tool(registry, effort=args.effort,
                                processors_ref=shared_processors))
        registry.add(delegate_tool(registry, effort=args.effort,
                                   workspace_factory=factory,
                                   processors_ref=shared_processors))

    mcp_manager = None
    if not getattr(args, "no_mcp", False):
        # MCP tools are registered once at startup. Failed servers are omitted
        # entirely for this run so the schema stays fixed for the episode.
        mcp_manager = mcpclient.register_mcp_tools(registry, root, enabled=True)

    base = (BASE_SYSTEM + TRUTH_SYSTEM
            + (PLAN_SYSTEM if getattr(args, "plan", False) else ""))
    system, system_blocks = context.build_system(
        base, root, skills=skills,
        environment=context.describe_environment(
            root, policy.allow_write, policy.allow_shell),
        return_blocks=True)
    prefix = StablePrefix(system, registry.specs(), system_blocks=system_blocks)

    # In write mode, provide checkpoint records so the gate can detect no-op writes.
    verification = VerificationGate(checkpoints=workspace.log)
    # Block implementation tools after a required search fails, while allowing
    # tracker tools to record a blocked state.
    search_integrity = WebSearchGate({
        "write_file", "edit_file", "shell", "spawn_agent", "delegate",
    })
    processors = Chain([search_integrity, SecretRedactor(),
                        ToolAvoidanceGuard(), RepetitionGuard(), verification])
    if task_tracker is not None:
        processors.add(CompletionGate(task_tracker))
    if goal_holder is not None:
        # Run search integrity, verification, completion, and goal gates in order.
        # The chain stops at the first retry request.
        processors.add(goal_mod.GoalGate(goal_holder, root))
    if args.hooks:
        hooks = ShellHooks(root)
        if hooks.active:
            processors.add(hooks)
    final_report = FinalReportGate(verification)
    processors.add(final_report)
    # Delegated children resolve the finished chain at call time (ChildChain
    # keeps evidence processors, drops parent-scoped gates and resets).
    shared_processors["processors"] = processors

    send = None
    if args.cassette:
        # Record or replay traffic for debugging and regression checks.
        recorder = tape.Cassette(args.cassette,
                                 tape.REPLAY if args.replay else tape.AUTO)
        send = tape.wrap(client.complete, recorder)

    agent = Agent(prefix, registry, effort=args.effort,
                  max_rounds=getattr(args, "max_rounds", DEFAULT_MAX_ROUNDS),
                  processors=processors, session=session,
                  approve=_approver(args), complete=send)
    agent.verification = verification
    agent.final_report = final_report
    agent.tracker = task_tracker
    agent.goal_holder = goal_holder
    agent.ask_log = ask_log
    agent.permission_policy = policy
    agent.lsp_pool = lsp_pool
    agent.mcp_manager = mcp_manager
    return agent, prefix, session


def specify(args):
    """Resolve an optional task specification and return the task plus draft.

    Ask about ambiguities interactively, record assumptions in non-interactive
    mode, and fall back to the original task when drafting fails.
    """
    try:
        draft = spec_mod.draft(args.task)
    except (spec_mod.SpecError, client.SolarError) as exc:
        print(f"명세 생성 실패: {exc}", file=sys.stderr)
        print("명세 없이 원래 지시로 진행합니다.", file=sys.stderr)
        return args.task, None

    interactive = not args.yes and sys.stdin.isatty()
    if draft.ambiguities and interactive:
        for item in draft.ambiguities:
            question = item.get("question", "")
            assumed = item.get("assumed", "")
            answer = input(f"\n[확인] {question}\n"
                           f"(비우면 가정대로: {assumed}) > ").strip()
            if answer:
                item["assumed"] = answer

    print("\n--- 작업 명세 ---", file=sys.stderr)
    print(draft.render_block(), file=sys.stderr)
    if interactive:
        if input("\n이 명세로 진행할까요? [Y/n] ").strip().lower() in ("n", "no"):
            print("명세 없이 원래 지시로 진행합니다.", file=sys.stderr)
            return args.task, None

    return spec_mod.compose_task(draft, args.task), draft


def _approver(args):
    """Convert a normalized permission policy into a CLI approval callback."""
    policy = args.permission_policy
    if policy.auto_all:
        return None

    def ask(tool, tool_args: dict) -> bool:
        if policy.decision(tool.name) == ALLOW:
            return True
        if not sys.stdin.isatty():
            return False
        preview = json.dumps(tool_args, ensure_ascii=False)[:160]
        answer = input(f"\n[승인 필요] {tool.name} {preview}\n실행할까요? [y/N] ")
        return answer.strip().lower() in ("y", "yes")

    return ask


def _cli_permissions(yolo: bool):
    """CLI has one safe default and one explicit automatic mode."""
    return resolve_permissions("yolo" if yolo else "ask")


def do_commit(args) -> int:
    """Create a commit message from the staged diff and commit after approval."""
    root = os.path.abspath(args.root)
    if args.stage:
        outcome = git.stage(root)
        if "error" in outcome:
            print(outcome["error"], file=sys.stderr)
            return 1

    result = vcs.write_commit_message(root)
    if "error" in result:
        print(result["error"], file=sys.stderr)
        return 1

    print(result["message"])
    print(f"\n--- 대상 파일 {len(result['files'])}개 · "
          f"관례: {result['convention']}", file=sys.stderr)

    if args.dry_run:
        return 0
    if not args.yes and sys.stdin.isatty():
        if input("\n이 메시지로 커밋할까요? [y/N] ").strip().lower() not in ("y", "yes"):
            print("취소했습니다", file=sys.stderr)
            return 1

    outcome = git.commit(root, result["message"])
    if "error" in outcome:
        print(outcome["error"], file=sys.stderr)
        return 1
    print(f"커밋 {outcome['sha']}", file=sys.stderr)
    return 0


def do_pr(args) -> int:
    root = os.path.abspath(args.root)
    result = vcs.write_pr(root, base=args.base or "")
    if "error" in result:
        print(result["error"], file=sys.stderr)
        return 1

    print(result["title"])
    print()
    print(result["body"])
    print(f"\n--- 기준 {result['base']} · 커밋 {len(result['commits'])}개",
          file=sys.stderr)

    if args.dry_run:
        return 0
    ok, reason = github.available(root)
    if not ok:
        print(f"\n{reason}", file=sys.stderr)
        print("위 내용을 직접 사용하시거나 인증 후 다시 실행하십시오.", file=sys.stderr)
        return 1
    if not args.yes and sys.stdin.isatty():
        if input("\nPR을 생성할까요? [y/N] ").strip().lower() not in ("y", "yes"):
            print("취소했습니다", file=sys.stderr)
            return 1

    outcome = github.create_pr(root, result["title"], result["body"],
                               base=result["base"], draft=not args.ready)
    if "error" in outcome:
        print(outcome["error"], file=sys.stderr)
        return 1
    print(outcome["url"], file=sys.stderr)
    return 0


def do_issue(args) -> int:
    root = os.path.abspath(args.root)
    result = vcs.write_issue(args.issue)
    if "error" in result:
        print(result["error"], file=sys.stderr)
        return 1

    print(result["title"])
    print()
    print(result["body"])

    if args.dry_run:
        return 0
    ok, reason = github.available(root)
    if not ok:
        print(f"\n{reason}", file=sys.stderr)
        return 1
    if not args.yes and sys.stdin.isatty():
        if input("\n이슈를 등록할까요? [y/N] ").strip().lower() not in ("y", "yes"):
            print("취소했습니다", file=sys.stderr)
            return 1

    outcome = github.create_issue(root, result["title"], result["body"])
    if "error" in outcome:
        print(outcome["error"], file=sys.stderr)
        return 1
    print(outcome["url"], file=sys.stderr)
    return 0


def check() -> int:
    """Check whether the configured model contract is still valid."""
    failures = 0

    turn = client.complete([{"role": "user", "content": "2+2는? 숫자만."}],
                           effort=model.Effort.OFF, max_tokens=64)
    ok = turn.reasoning_tokens == 0 and turn.answered
    print(f"  effort=off  -> reasoning={turn.reasoning_tokens} "
          f"content={turn.content.strip()[:20]!r} {'OK' if ok else 'FAIL'}")
    failures += 0 if ok else 1

    turn = client.complete([{"role": "user", "content": "2+2는? 숫자만."}],
                           effort=model.Effort.ON)
    ok = turn.reasoning_tokens > 0 and turn.answered
    print(f"  effort=on   -> reasoning={turn.reasoning_tokens} "
          f"content={turn.content.strip()[:20]!r} {'OK' if ok else 'FAIL'}")
    failures += 0 if ok else 1

    # Reject values above the configured limit so stale limits fail loudly.
    try:
        client.complete([{"role": "user", "content": "hi"}],
                        max_tokens=model.MAX_OUTPUT_TOKENS + 1, retries=0)
        print(f"  max_tokens  -> {model.MAX_OUTPUT_TOKENS} 초과가 통과함 FAIL")
        failures += 1
    except client.SolarError:
        print(f"  max_tokens  -> 상한 {model.MAX_OUTPUT_TOKENS} 유효 OK")

    print("계약 유효" if not failures else f"{failures}개 항목이 어긋남")
    return 1 if failures else 0


def _consume_live(stream, renderer: LiveCLI):
    """Render generator events and return its ``StopIteration.value``."""
    while True:
        try:
            event = next(stream)
        except StopIteration as done:
            return done.value
        renderer.on_event(event)


def do_cache_probe(args) -> int:
    """Measure cache hits across repeated requests with one prefix.

    Hit-rate values come from cached token counts after the configured warm-up.
    """
    try:
        if args.task:
            # With a task, measure the actual system and tool prefix from
            # build_agent instead of a padded prompt. Avoid creating a session so
            # the probe does not change workspace state.
            args.no_session = True
            agent, prefix, _ = build_agent(args)

            def complete(messages, tools=None):
                return client.complete(messages, tools=tools,
                                       effort=model.Effort.OFF, max_tokens=64)

            result = cacheprobe.run_prefix_probe(
                complete, prefix, args.task, requests=args.probe_requests)
        else:
            def complete(messages):
                return client.complete(messages, effort=model.Effort.OFF,
                                       max_tokens=64)

            result = cacheprobe.run_probe(complete, requests=args.probe_requests)
    except client.SolarError as exc:
        print(f"cache probe failed: {exc}", file=sys.stderr)
        return 1
    print(cacheprobe.render(result))
    if not result["eligible"]:
        print(f"주의: 프리픽스가 임계({model.CACHE_MIN_PREFIX_TOKENS}) "
              "미만이라 캐시가 붙지 않는 구간입니다.", file=sys.stderr)
    if result["measured_requests"] == 0:
        print("측정 구간(4회차 이후)이 없습니다. "
              "--probe-requests를 늘리십시오.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sol",
        add_help=False,
        description="Solar coding agent. 옵션 없이 실행하면 TUI를 엽니다.",
        epilog=("예시:\n"
                "  sol -p \"이 저장소를 설명해줘\"\n"
                "  sol -p \"버그를 고치고 테스트해줘\" --yolo\n"
                "  sol -p \"복잡한 설계를 검토해줘\" --think on\n"
                "  sol -p \"빠른 검토\"\n"
                "  sol --set-api-key\n"
                "  sol --continue 20260803-120000-a1b2c3d4\n"
                "  sol --branch feature/command-cleanup"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--help", action="help", help="전체 사용법을 보여준다")
    parser.add_argument("-p", dest="task", metavar="PROMPT",
                        help="CLI로 실행할 요청. 생략하면 TUI를 연다")
    parser.add_argument("--yolo", action="store_true",
                        help="파일 수정과 셸 실행을 묻지 않고 승인한다")
    parser.add_argument("--think", choices=(model.Effort.ON, model.Effort.OFF),
                        dest="effort", default=model.Effort.OFF,
                        help="추론 사용 여부: on 또는 off (기본 off)")
    # 계획 모드는 숨김 플래그다. 도움말에는 노출하지 않고, TUI에서는 /plan과
    # /goal 명령이 같은 기능을 보이는 진입점으로 제공한다 (--goal은 호환 별칭).
    parser.add_argument("--plan", action="store_true", dest="plan",
                        help=argparse.SUPPRESS)
    parser.add_argument("--goal", action="store_true", dest="plan",
                        help=argparse.SUPPRESS)
    parser.add_argument("--model", choices=model.SUPPORTED_MODELS,
                        dest="model", metavar="MODEL", default=None,
                        help=("사용할 모델: "
                              + ", ".join(model.SUPPORTED_MODELS)
                              + f" (기본 {model.MODEL})"))
    parser.add_argument("--continue", dest="continue_session",
                        metavar="SESSION_ID",
                        help="지정한 세션을 이어서 실행한다")
    parser.add_argument("--branch", metavar="BRANCH_NAME",
                        help="Git 브랜치가 있으면 이동하고 없으면 생성한다")
    secret_group = parser.add_mutually_exclusive_group()
    secret_group.add_argument(
        "--set-api-key", action="store_true",
        help="입력값을 표시하지 않고 전역 $UPSTAGE_API_KEY로 저장한다")
    secret_group.add_argument(
        "--set-tinyfish-api-key", action="store_true",
        help="입력값을 표시하지 않고 전역 $TINYFISH_API_KEY로 저장한다")
    # Keep agent configuration details as stable defaults rather than commands.
    parser.set_defaults(
        root=".", yes=False, no_session=False, no_subagent=False,
        no_tracker=False, no_recall=False, cassette=None, replay=False,
        hooks=False, plan=False, no_websearch=False, no_webfetch=False,
        no_lsp=False, no_mcp=False, max_rounds=DEFAULT_MAX_ROUNDS)
    args = parser.parse_args(argv)
    secret_name = ("UPSTAGE_API_KEY" if args.set_api_key else
                   "TINYFISH_API_KEY" if args.set_tinyfish_api_key else "")
    if secret_name:
        if sys.stdin.isatty():
            value = getpass.getpass(
                f"${secret_name} 입력 (화면과 기록에 표시되지 않음): ")
        else:
            value = sys.stdin.read().strip()
        try:
            marker = credentials.save_global(secret_name, value)
        except credentials.CredentialError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"전역 {marker} 저장 완료 (macOS Keychain)", file=sys.stderr)
        return 0
    if args.model:
        model.select(args.model)
    args.permission_policy = _cli_permissions(args.yolo)

    if args.branch:
        outcome = git.switch_branch(os.path.abspath(args.root), args.branch)
        if "error" in outcome:
            print(outcome["error"], file=sys.stderr)
            return 1
        verb = "생성" if outcome["action"] == "created" else "이동"
        args.branch_notice = f"브랜치 {verb}: {outcome['branch']}"
        print(args.branch_notice, file=sys.stderr)

    # Only the presence of -p selects the mode; other options configure that mode.
    if not args.task:
        if sys.stdin.isatty():
            from . import tui as tui_mod
            try:
                return tui_mod.run(build_agent, args)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        if not args.task:
            parser.error("비대화형 실행에는 -p PROMPT가 필요합니다")

    try:
        agent, prefix, session = build_agent(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    live = LiveCLI(effort=args.effort,
                   yolo=args.permission_policy.explicit_yolo)
    agent.on_delta = live.on_delta
    agent.on_tool_output = live.on_tool_output
    agent.approve = live.wrap_approver(getattr(agent, "approve", None))
    episode = Episode()
    episodes: list[Episode] = []
    live.start()
    try:
        if getattr(agent, "goal_holder", None) is not None:
            # 계획 모드는 ralph 바깥 루프가 미충족 기준을 후속 회차로 넘긴다.
            stream = goal_mod.ralph_stream(agent, args.task, agent.goal_holder,
                                           os.path.abspath(args.root),
                                           episodes=episodes)
            goal = None
            while True:
                try:
                    event = next(stream)
                except StopIteration as done:
                    episode, goal = done.value
                    break
                live.on_event(event)
            _report_plan(agent, goal)
        else:
            for event in agent.stream(args.task, episode=episode):
                live.on_event(event)
    finally:
        live.close()
        if getattr(agent, "lsp_pool", None) is not None:
            agent.lsp_pool.close()
        if getattr(agent, "mcp_manager", None) is not None:
            agent.mcp_manager.close()

    return 1 if episode.incomplete else 0


def _report_plan(agent, goal) -> None:
    """Write goal and question-log summaries to stderr."""
    if goal is not None:
        print(goal.render(), file=sys.stderr)
        unmet = goal.unmet()
        if unmet:
            print(f"미충족 기준 {len(unmet)}건이 남아 있습니다 "
                  f"(회차 상한 {goal_mod.MAX_PASSES}).", file=sys.stderr)
    if agent.ask_log is not None:
        rendered = interact_mod.render_log(agent.ask_log)
        if rendered:
            print(rendered, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
