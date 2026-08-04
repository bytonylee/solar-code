"""Manage agent turns, tool calls, retries, and explicit completion states.

Prefix stability, context compaction, and tool execution remain separate
components; this module coordinates their lifecycle and always emits a final
status when execution stops.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterator

from . import client, compaction, model
from .processors import Block, Chain, Retry
from .prefix import AppendOnlyLog, CacheLedger, StablePrefix
from .tools import Invocation, Registry, execute


@dataclass
class Event:
    """Progress event emitted by the loop."""

    kind: str
    data: dict = field(default_factory=dict)


@dataclass
class Episode:
    """Result and diagnostics for one execution."""

    answer: str = ""
    rounds: int = 0
    requests: int = 0
    trace: list[Invocation] = field(default_factory=list)
    starved: bool = False
    nudged: bool = False
    rounds_exhausted: bool = False
    stalled: bool = False
    stall_reason: str = ""
    gate_exhausted: bool = False
    wrapped_up: bool = False
    wrap_up_reason: str = ""
    finalization_failed: bool = False
    blocked: list[str] = field(default_factory=list)
    incomplete_reasons: list[str] = field(default_factory=list)
    reasoning_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    # Cache promotion observation recorded at completion.
    cache_promotion: str = "steady"
    cache_max_lag: int = 0
    cache_promotion_misses: int = 0
    warm_prompt_tokens: int = 0
    warm_cached_tokens: int = 0
    compactions: list[str] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)

    @property
    def tool_failures(self) -> int:
        return sum(1 for t in self.trace if not t.ok)

    @property
    def cache_hit_rate(self) -> float:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0

    @property
    def warm_cache_hit_rate(self) -> float:
        """Return hit rate after excluding the first request."""
        if not self.warm_prompt_tokens:
            return 0.0
        return self.warm_cached_tokens / self.warm_prompt_tokens

    def mark_incomplete(self, reason: str) -> None:
        reason = reason.strip()
        if reason and reason not in self.incomplete_reasons:
            self.incomplete_reasons.append(reason)

    @property
    def incomplete(self) -> bool:
        return (self.starved or self.rounds_exhausted or self.gate_exhausted
                or self.stalled
                or self.finalization_failed or bool(self.blocked)
                or bool(self.incomplete_reasons) or not self.answer.strip())

    @property
    def status(self) -> str:
        if not self.incomplete:
            return "completed"
        if self.starved:
            return "starved"
        if self.blocked:
            return "blocked"
        if self.rounds_exhausted:
            return "rounds_exhausted"
        if self.stalled:
            return "stalled"
        if self.gate_exhausted:
            return "gate_exhausted"
        return "incomplete"


# Retry limit for processor interventions. Different gates may surface in sequence,
# so allow one correction for each before ending with a failure report.
MAX_RETRIES_PER_TURN = 4

# Default round limit. Complex multi-step work can exhaust a smaller budget while
# tool calls remain, so use a larger design limit. The wrap-up turn reports leftovers.
# 0 (또는 음수)은 "고정 상한 없음"을 뜻한다. 긴 작업이 라운드 수 때문에 잘리는
# 것을 막되, 무한 루프는 아래 StallDetector가 잡는다. 상한 대신 "진행이 있는가"를
# 종료 조건으로 쓴다.
DEFAULT_MAX_ROUNDS = 0

# 진행 없는 반복을 중단시키는 임계값. 라운드 수가 아니라 병리를 센다.
#
# 왜 라운드 상한이 아니라 이 방식인가: 2026-08-03 랜딩 실행에서 계획 작업을
# 전부 done으로 끝내고도 24라운드에 걸려 rounds_exhausted로 종료했다. 상한은
# "느린 작업"과 "멈춘 작업"을 구분하지 못한다. 아래 신호는 멈춘 작업만 잡는다.
#
# 같은 도구를 같은 인자로 반복 호출: 결과가 달라질 이유가 없으므로 진행이 아니다.
STALL_REPEAT_LIMIT = 5
# 연속으로 도구가 전부 실패: 복구 가능한 오류라면 그 전에 성공이 섞인다.
STALL_FAILURE_LIMIT = 8
# 진행 신호(성공한 도구, 계획 상태 변화, 새 인자) 없이 흘러간 라운드.
STALL_IDLE_LIMIT = 12
# 안전 그물. 위 신호가 모두 빠져나가는 병리를 대비한 절대 상한이다. 정상
# 작업이 여기 닿는 것은 관측되지 않았고, 닿으면 정리 턴으로 끝난다.
ABSOLUTE_ROUND_CAP = 500


class StallDetector:
    """진행 없는 반복을 감지한다 (라운드 상한의 대체물).

    고정 상한은 작업의 길이를 벌한다. 이 감지기는 길이가 아니라 정체를 벌한다.
    긴 작업이라도 매 라운드 새로운 일을 하면 계속 진행하고, 짧은 작업이라도
    같은 실패를 반복하면 즉시 멈춘다.
    """

    def __init__(self, repeat_limit: int = STALL_REPEAT_LIMIT,
                 failure_limit: int = STALL_FAILURE_LIMIT,
                 idle_limit: int = STALL_IDLE_LIMIT) -> None:
        self.repeat_limit = repeat_limit
        self.failure_limit = failure_limit
        self.idle_limit = idle_limit
        self.consecutive_failures = 0
        self.idle_rounds = 0
        self._signatures: dict[str, int] = {}
        self._seen: set[str] = set()

    def observe_round(self, records: list) -> str:
        """한 라운드의 도구 실행을 보고 중단 사유를 돌려준다. 정상이면 ""."""
        if not records:
            # 도구 없는 턴은 최종 답변으로 가는 정상 경로다. 루프가 이미
            # 그 경우를 종료로 처리하므로 여기서는 셈하지 않는다.
            return ""

        progressed = False
        for record in records:
            signature = f"{record.tool}:{record.signature}"
            count = self._signatures.get(signature, 0) + 1
            self._signatures[signature] = count
            if count >= self.repeat_limit:
                return (f"같은 도구 호출이 {count}회 반복됐습니다 "
                        f"({record.tool}). 결과가 달라지지 않아 진행이 "
                        "없다고 판단했습니다.")
            if signature not in self._seen:
                self._seen.add(signature)
                progressed = True
            if record.ok:
                progressed = True

        if all(not record.ok for record in records):
            self.consecutive_failures += len(records)
            if self.consecutive_failures >= self.failure_limit:
                return (f"도구 실행이 연속 {self.consecutive_failures}회 "
                        "실패했습니다. 자체 복구가 되지 않는 상태로 "
                        "판단했습니다.")
        else:
            self.consecutive_failures = 0

        if progressed:
            self.idle_rounds = 0
        else:
            self.idle_rounds += 1
            if self.idle_rounds >= self.idle_limit:
                return (f"{self.idle_rounds}개 라운드 동안 새로운 진행이 "
                        "관측되지 않았습니다.")
        return ""

# Wrap-up instruction that forces an honest status report instead of a silent stop.
WRAP_UP_ASK = {
    "rounds_exhausted": "도구 실행 라운드가 상한에 도달해 더 이상 도구를 "
                        "실행할 수 없습니다.",
    "stalled": "작업이 더 이상 진행되지 않는 상태로 판단되어 중단했습니다.",
    "empty_answer": "직전 응답이 비어 있어 사용자가 결과를 알 수 없습니다.",
    "gate_exhausted": "완료 조건을 만족시키지 못한 채 가드레일 재시도 상한에 "
                      "도달했습니다.",
    "starved": "추론이 출력 예산을 소진해 정상 응답을 만들지 못했습니다.",
}
WRAP_UP_TASK = ("지금까지 실제로 수행한 작업과 검증 결과, 완료하지 못한 "
                "작업과 그 이유, 사용자가 이어갈 다음 단계를 구분해 최종 "
                "정리를 작성하십시오. 하지 않은 작업을 했다고 쓰지 마십시오. "
                "이 턴에서는 도구를 호출하지 말고 최종 정리 문장만 작성하십시오.")


def _sanitize_tool_calls(message: dict) -> dict:
    """Replace malformed tool-call arguments before storing history."""
    calls = message.get("tool_calls")
    if not calls:
        return message
    out = dict(message)
    fixed = []
    for call in calls:
        function = call.get("function") or {}
        raw = function.get("arguments") or "{}"
        if isinstance(raw, str):
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                call = {**call, "function": {**function, "arguments": "{}"}}
        fixed.append(call)
    out["tool_calls"] = fixed
    return out


class Agent:
    def __init__(self, prefix: StablePrefix, registry: Registry | None = None,
                 effort: str = model.Effort.OFF,
                 max_rounds: int = DEFAULT_MAX_ROUNDS,
                 context_budget: int = model.VERIFIED_CONTEXT_TOKENS,
                 approve=None, complete=None, processors=None,
                 session=None, tracker=None) -> None:
        self.prefix = prefix
        self.registry = registry or Registry()
        self.effort = effort
        self.max_rounds = max_rounds
        self.context_budget = context_budget
        self.approve = approve
        self.processors = processors if isinstance(processors, Chain) else Chain(processors)
        self.session = session
        # Optional plan tracker. Used only to re-recite state after summary
        # compaction; never injected into the system prefix.
        self.tracker = tracker
        self.log = AppendOnlyLog()
        self.cache = CacheLedger()
        self._complete = complete or client.complete
        # Token streaming callback used by the TUI. When absent, return one result.
        self.on_delta = None
        # Callback for chunks emitted by running tools such as shell.
        self.on_tool_output = None
        if session is not None:
            # Resume from the saved branch.
            restored = session.branch()
            if restored:
                self.log.extend(restored)

    def _request(self, messages: list[dict], episode: Episode,
                 tool_choice: str | None = None, use_tools: bool = True,
                 effort: str | None = None,
                 stream_output: bool = True) -> client.Turn:
        problems = compaction.validate(messages)
        if problems:
            raise ValueError(f"message integrity broken: {problems[0]}")

        # Always send the same tools field. Omitting tools rewrites the prefix
        # from byte 0 and invalidates the entire prompt cache. Tool suppression
        # is done only through tool_choice and prompt instructions.
        specs = self.prefix.tools or (self.registry.specs() or None)
        if not use_tools and tool_choice is None:
            tool_choice = "none"
        kwargs = {}
        if self.on_delta is not None and stream_output:
            content_deltas: list[str] = []

            def buffer_delta(text: str, is_reasoning: bool) -> None:
                # Buffer content that a gate may reject. Show reasoning progress
                # immediately, but publish answer content only after approval.
                if is_reasoning:
                    self.on_delta(text, True)
                else:
                    content_deltas.append(text)

            kwargs["on_delta"] = buffer_delta
        else:
            content_deltas = []
        episode.requests += 1
        turn = self._complete(messages, tools=specs,
                              effort=effort or self.effort,
                              tool_choice=tool_choice, **kwargs)
        turn._sol_content_deltas = content_deltas

        # Record which prefix segment changed when the prefix is unstable.
        self.cache.observe(self.prefix)
        self.cache.record(turn.prompt_tokens, turn.cached_tokens)
        episode.prompt_tokens += turn.prompt_tokens
        episode.cached_tokens += turn.cached_tokens
        episode.reasoning_tokens += turn.reasoning_tokens
        episode.completion_tokens += turn.completion_tokens
        episode.latency_ms += turn.latency_ms
        return turn

    def _publish_turn(self, turn: client.Turn) -> None:
        """Publish only buffered content from a turn approved by the guards."""
        if self.on_delta is None:
            return
        deltas = getattr(turn, "_sol_content_deltas", [])
        if deltas and "".join(deltas) != turn.content:
            # Use the final turn as authoritative when a processor changes content.
            self.on_delta(turn.content, False)
        else:
            for text in deltas:
                self.on_delta(text, False)
        turn._sol_content_deltas = []

    def _remember(self, message: dict) -> None:
        self.log.append(message)
        if self.session is not None:
            self.session.add_message(message)

    def _absorb_cache(self, episode: Episode) -> None:
        """Copy cache promotion observations into the episode result."""
        episode.cache_promotion = self.cache.promotion
        episode.cache_max_lag = self.cache.max_lag
        episode.cache_promotion_misses = self.cache.promotion_misses
        episode.warm_prompt_tokens = self.cache.warm_prompt_tokens
        episode.warm_cached_tokens = self.cache.warm_cached_tokens

    def _terminal_issues(self, answer: str = "") -> list[str]:
        """Collect unresolved conditions observed by processors at shutdown."""
        return self.processors.terminal_issues(answer)

    @staticmethod
    def _fallback_summary(episode: Episode, reason: str) -> str:
        """Return a non-empty summary when the model wrap-up also fails."""
        succeeded = sum(1 for item in episode.trace if item.ok)
        failed = len(episode.trace) - succeeded
        lines = ["작업을 완전히 마치지 못했습니다.", "",
                 f"- 종료 사유: {reason}",
                 f"- 도구 실행 기록: {len(episode.trace)}회 "
                 f"(성공 {succeeded}, 실패 {failed})"]
        if episode.incomplete_reasons:
            lines.append("- 남은 조건: " + "; ".join(
                episode.incomplete_reasons[:5]))
        lines.append("- 다음 단계: 남은 조건을 확인한 뒤 작업과 검증을 이어가야 합니다.")
        return "\n".join(lines)

    def _wrap_up(self, episode: Episode, reason_key: str, reason: str,
                 issues: list[str] | None = None) -> Iterator[Event]:
        """Summarize the current state in a final no-tool turn."""
        issues = list(issues or [])
        episode.wrap_up_reason = reason_key
        details = [reason, *issues]
        detail_text = "\n".join(f"- {item}" for item in details if item)
        instruction = (f"{WRAP_UP_ASK.get(reason_key, reason)}\n\n"
                       f"현재 관찰된 상태:\n{detail_text}\n\n{WRAP_UP_TASK}")
        messages = ([{"role": "system", "content": self.prefix.system}]
                    + self.log.messages
                    + [{"role": "user", "content": instruction}])
        screened = self.processors.before_request(messages)
        yield Event("wrap_up_start", {"reason": reason_key,
                                      "detail": reason})

        turn = None
        failure = ""
        if isinstance(screened, Block):
            failure = screened.reason
        else:
            # tools 배열을 유지한 채 tool_choice="none"으로 도구만 잠근다.
            # tools를 제거하면 프리픽스가 바이트 0부터 달라져 마지막 요청의
            # 캐시가 전부 무효화된다(2026-08-03 실행에서 신선 입력의 51%).
            try:
                turn = self._request(screened, episode,
                                     tool_choice="none",
                                     effort=model.Effort.OFF,
                                     stream_output=False)
            except Exception:
                # 공급자가 tool_choice="none"을 거부하면 도구 없는 요청으로
                # 폴백한다. 캐시는 잃지만 최종 보고는 지킨다.
                yield Event("wrap_up_fallback",
                            {"reason": "tool_choice=none 요청이 실패해 "
                                       "도구 없는 요청으로 재시도합니다"})
                try:
                    turn = self._request(screened, episode, use_tools=False,
                                         effort=model.Effort.OFF,
                                         stream_output=False)
                except Exception as exc:  # Never lose the final status report.
                    failure = f"최종 정리 요청 실패: {type(exc).__name__}: {exc}"

        if turn is not None and turn.tool_calls and not failure:
            # tool_choice="none"을 무시한 응답은 한 번만 다시 요청한다.
            try:
                turn = self._request(screened, episode, use_tools=False,
                                     effort=model.Effort.OFF,
                                     stream_output=False)
            except Exception as exc:
                failure = f"최종 정리 요청 실패: {type(exc).__name__}: {exc}"

        if turn is not None and (turn.starved or turn.tool_calls
                                 or not turn.content.strip()):
            if turn.starved:
                failure = "최종 정리 응답도 출력 예산을 소진했습니다."
            elif turn.tool_calls:
                failure = "도구 없는 최종 정리 턴에서 도구 호출이 반환되었습니다."
            else:
                failure = "최종 정리 응답이 비어 있습니다."

        if failure:
            episode.finalization_failed = True
            episode.mark_incomplete(failure)
            content = self._fallback_summary(episode, reason)
            message = {"role": "assistant", "content": content}
            yield Event("wrap_up_failed", {"reason": failure})
        else:
            content = turn.content
            if reason_key != "empty_answer" or issues:
                issue_lines = "\n".join(
                    f"- {item}" for item in issues if item)
                header = ("작업을 완전히 마치지 못했습니다.\n\n"
                          f"- 종료 사유: {reason}")
                if issue_lines:
                    header += f"\n{issue_lines}"
                content = f"{header}\n\n{content.strip()}"
            message = {"role": "assistant", "content": content}

        self._remember(message)
        episode.answer = content
        episode.wrapped_up = True
        yield Event("answer", {"content": content})
        yield Event("wrap_up_end", {"reason": reason_key,
                                    "fallback": bool(failure)})

    def run(self, task: str, summarize=None) -> Episode:
        """Run to completion and return the episode for CLI or batch use."""
        episode = Episode()
        for _ in self.stream(task, summarize, episode):
            pass
        return episode

    def stream(self, task: str, summarize=None,
               episode: Episode | None = None) -> Iterator[Event]:
        """Stream progress events and convert API failures into explicit status."""
        episode = episode if episode is not None else Episode()
        try:
            yield from self._stream_episode(task, summarize, episode)
        except client.SolarError as exc:
            reason = f"모델 요청을 완료하지 못했습니다: {exc}"
            episode.mark_incomplete(reason)
            episode.wrap_up_reason = "request_error"
            content = self._fallback_summary(episode, reason)
            self._remember({"role": "assistant", "content": content})
            episode.answer = content
            episode.wrapped_up = True
            episode.messages = self.log.messages
            self._absorb_cache(episode)
            yield Event("run_error", {"reason": reason})
            yield Event("answer", {"content": content})
            yield Event("run_end", {"answer": episode.answer,
                                    "rounds": episode.rounds,
                                    "requests": episode.requests,
                                    "starved": episode.starved,
                                    "status": episode.status,
                                    "incomplete": True,
                                    "rounds_exhausted": episode.rounds_exhausted,
                                    "gate_exhausted": episode.gate_exhausted,
                                    "wrapped_up": True,
                                    "reasons": episode.incomplete_reasons})

    def _stream_episode(self, task: str, summarize,
                        episode: Episode) -> Iterator[Event]:
        """Run while streaming progress and fill the supplied episode."""
        self.prefix.assert_stable()
        self.processors.start_episode(task)
        self._remember({"role": "user", "content": task})
        yield Event("run_start", {"task": task, "effort": self.effort})

        # max_rounds<=0이면 고정 상한 없이 돌고, 정체는 StallDetector가 잡는다.
        # 절대 상한은 감지기가 놓치는 병리에 대비한 안전 그물이다.
        bounded = self.max_rounds > 0
        limit = self.max_rounds if bounded else ABSOLUTE_ROUND_CAP
        stall = StallDetector()
        stalled_reason = ""
        for _ in range(limit):
            episode.rounds += 1
            history = self.log.messages
            result = compaction.compact(history, self.context_budget, summarize)
            if result.stage not in ("none", "disabled", "tail-only"):
                rewritten = result.messages
                # Recite the live plan immediately after a summary so goals stay
                # near the end of context and do not drift after compression.
                if (result.stage == "summary" and self.tracker is not None
                        and getattr(self.tracker, "items", None)):
                    plan_text = self.tracker.render()
                    if plan_text and plan_text != "등록된 작업이 없습니다":
                        rewritten = list(rewritten) + [{
                            "role": "user",
                            "content": f"[현재 계획 상태]\n{plan_text}",
                        }]
                self.log.rewrite(rewritten)
                episode.compactions.append(result.render())
                history = rewritten
                if self.session is not None:
                    self.session.record_compaction(result.stage, result.saved)
                yield Event("compaction", {"stage": result.stage,
                                           "saved": result.saved})

            messages = [{"role": "system", "content": self.prefix.system}] + history
            screened = self.processors.before_request(messages)
            if isinstance(screened, Block):
                episode.blocked.append(screened.reason)
                episode.mark_incomplete(screened.reason)
                episode.answer = screened.reason
                yield Event("blocked", {"reason": screened.reason})
                break
            messages = screened

            yield Event("turn_start", {"round": episode.rounds})
            turn = self._request(messages, episode)

            # A low-effort request can omit tool calls. Retry once with required
            # tool selection when tools are available and no answer was returned.
            if (not turn.tool_calls and not turn.content.strip()
                    and len(self.registry) and self.effort == model.Effort.OFF
                    and model.EFFORT_OFF_NEEDS_TOOL_NUDGE and not episode.nudged):
                episode.nudged = True
                yield Event("nudge", {"reason": "effort=off omitted tool call"})
                turn = self._request(messages, episode, tool_choice="required")

            if turn.starved:
                # A response may have been cut off intermittently. Retry the same
                # request within the configured starvation limit, then stop.
                recovered = False
                for _ in range(model.STARVED_RETRIES):
                    yield Event("starved_retry",
                                {"reasoning_tokens": turn.reasoning_tokens})
                    turn = self._request(messages, episode)
                    if not turn.starved:
                        recovered = True
                        break
                if not recovered:
                    episode.starved = True
                    reason = "재시도 후에도 추론 출력 예산이 고갈되었습니다."
                    episode.mark_incomplete(reason)
                    yield Event("starved",
                                {"reasoning_tokens": turn.reasoning_tokens,
                                 "reason": reason})
                    issues = self._terminal_issues()
                    for issue in issues:
                        episode.mark_incomplete(issue)
                    yield from self._wrap_up(episode, "starved", reason, issues)
                    break

            if turn.reasoning:
                yield Event("reasoning", {"tokens": turn.reasoning_tokens})

            # Output guardrail: discard this turn and retry when requested.
            retry = self.processors.after_turn(turn)
            attempts = 0
            retry_starved = False
            while isinstance(retry, Retry) and attempts < MAX_RETRIES_PER_TURN:
                active_retry = retry
                attempts += 1
                yield Event("retry", {"reason": retry.reason})
                nudge = messages + [{"role": "user", "content": retry.reason}]
                turn = self._request(nudge, episode, tool_choice=retry.tool_choice)
                if turn.starved:
                    recovered = False
                    for _ in range(model.STARVED_RETRIES):
                        yield Event("starved_retry", {
                            "reasoning_tokens": turn.reasoning_tokens,
                            "reason": "가드레일 재시도 응답이 절단되었습니다."})
                        turn = self._request(
                            nudge, episode,
                            tool_choice=active_retry.tool_choice)
                        if not turn.starved:
                            recovered = True
                            break
                    if not recovered:
                        reason = ("완료 조건을 고치는 재시도 중 추론 출력 "
                                  "예산이 고갈되었습니다.")
                        episode.starved = True
                        episode.mark_incomplete(active_retry.reason)
                        episode.mark_incomplete(reason)
                        yield Event("starved", {
                            "reasoning_tokens": turn.reasoning_tokens,
                            "reason": reason})
                        issues = self._terminal_issues()
                        for issue in issues:
                            episode.mark_incomplete(issue)
                        yield from self._wrap_up(
                            episode, "starved", reason,
                            [active_retry.reason, *issues])
                        retry_starved = True
                        break
                if not turn.answered:
                    retry = Retry(
                        "완료 조건 재시도 응답이 비어 있습니다. "
                        + active_retry.reason,
                        tool_choice=active_retry.tool_choice)
                else:
                    retry = self.processors.after_turn(turn)

            if retry_starved:
                break

            if isinstance(retry, Retry):
                episode.gate_exhausted = True
                episode.mark_incomplete(retry.reason)
                yield Event("gate_exhausted", {"reason": retry.reason,
                                               "attempts": attempts})
                issues = self._terminal_issues(turn.content)
                for issue in issues:
                    episode.mark_incomplete(issue)
                yield from self._wrap_up(episode, "gate_exhausted",
                                         retry.reason, issues)
                break

            if not turn.tool_calls:
                if not turn.content.strip():
                    issues = self._terminal_issues()
                    for issue in issues:
                        episode.mark_incomplete(issue)
                    yield Event("empty_answer", {})
                    yield from self._wrap_up(
                        episode, "empty_answer",
                        "모델이 도구 호출도 최종 답변도 반환하지 않았습니다.",
                        issues)
                    break
                issues = self._terminal_issues(turn.content)
                for issue in issues:
                    episode.mark_incomplete(issue)
                if issues and not turn.content.startswith(
                        "작업을 완전히 마치지 못했습니다."):
                    issue_lines = "\n".join(f"- {item}" for item in issues)
                    turn.content = ("작업을 완전히 마치지 못했습니다.\n\n"
                                    f"{issue_lines}\n\n{turn.content}")
                self._remember(turn.assistant_message())
                episode.answer = turn.content
                self._publish_turn(turn)
                yield Event("answer", {"content": turn.content})
                break

            self._remember(_sanitize_tool_calls(turn.assistant_message()))
            self._publish_turn(turn)
            round_records = []
            for call in turn.tool_calls:
                name = call.get("function", {}).get("name", "")
                yield Event("tool_start", {"tool": name})
                message, record, streamed = self._run_tool(call, name)
                episode.trace.append(record)
                round_records.append(record)
                self._remember(message)
                yield Event("tool_end", {"tool": name, "ok": record.ok,
                                         "error": record.error,
                                         "result": message.get("content", ""),
                                         "streamed": streamed})

            # 진행이 멈췄는지 판정한다. 라운드 수가 아니라 병리를 본다.
            stalled_reason = stall.observe_round(round_records)
            if stalled_reason:
                episode.stalled = True
                episode.stall_reason = stalled_reason
                episode.mark_incomplete(stalled_reason)
                issues = self._terminal_issues()
                for issue in issues:
                    episode.mark_incomplete(issue)
                yield Event("stalled", {"reason": stalled_reason,
                                        "rounds": episode.rounds})
                yield from self._wrap_up(episode, "stalled", stalled_reason,
                                         issues)
                break

        else:
            episode.rounds_exhausted = True
            if bounded:
                reason = (f"도구 호출이 {self.max_rounds}개 라운드 안에 최종 "
                          "답변으로 수렴하지 않았습니다.")
            else:
                reason = (f"절대 안전 상한({ABSOLUTE_ROUND_CAP} 라운드)에 "
                          "도달했습니다. 진행 정체 신호 없이 이만큼 반복된 것은 "
                          "예상되지 않은 상태입니다.")
            episode.mark_incomplete(reason)
            issues = self._terminal_issues()
            for issue in issues:
                episode.mark_incomplete(issue)
            yield Event("rounds_exhausted", {"rounds": episode.rounds,
                                             "reason": reason})
            yield from self._wrap_up(episode, "rounds_exhausted", reason,
                                     issues)

        episode.messages = self.log.messages
        self._absorb_cache(episode)
        yield Event("run_end", {"answer": episode.answer,
                                "rounds": episode.rounds,
                                "requests": episode.requests,
                                "starved": episode.starved,
                                "status": episode.status,
                                "incomplete": episode.incomplete,
                                "rounds_exhausted": episode.rounds_exhausted,
                                "stalled": episode.stalled,
                                "gate_exhausted": episode.gate_exhausted,
                                "wrapped_up": episode.wrapped_up,
                                "reasons": episode.incomplete_reasons})

    def _run_tool(self, call: dict, name: str) -> tuple[dict, Invocation, bool]:
        """Run a tool through processors and always return one tool message."""
        raw = call.get("function", {}).get("arguments") or "{}"
        streamed = False

        def emit(text: str) -> None:
            nonlocal streamed
            if not text:
                return
            text = self.processors.stream_tool(name, text)
            streamed = True
            if self.on_tool_output is not None:
                self.on_tool_output(name, text)
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            args = None

        if isinstance(args, dict):
            screened = self.processors.before_tool(name, args)
            if isinstance(screened, Block):
                return ({"role": "tool", "tool_call_id": call.get("id", ""),
                         "name": name,
                         "content": json.dumps({"error": screened.reason},
                                               ensure_ascii=False)},
                        Invocation(name, False, screened.reason), False)
            call = {**call, "function": {**call["function"],
                                         "arguments": json.dumps(screened,
                                                                 ensure_ascii=False)}}

        output = emit if self.on_tool_output is not None else None
        message, record = execute(self.registry, call, self.approve, emit=output)
        message["content"] = self.processors.after_tool(name, message["content"])
        return message, record, streamed
