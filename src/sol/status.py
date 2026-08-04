"""Make cache and cost behavior visible.

Expose measured usage, cache promotion, failures, and evidence counts without
turning unverified cost or performance assumptions into facts.
"""

from dataclasses import dataclass, field

from . import model

# Hosting rates are supplied by the caller rather than guessed here.
DEFAULT_RATES = {"input_per_mtok": None, "cached_per_mtok": None,
                 "output_per_mtok": None}


@dataclass
class Usage:
    """Accumulated usage for one session."""

    requests: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    starved: int = 0
    nudged: int = 0
    incomplete: int = 0
    rounds_exhausted: int = 0
    stalled: int = 0
    gate_exhausted: int = 0
    wrap_ups: int = 0
    finalization_failures: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    # Cache promotion observation.
    cache_promotion: str = ""
    cache_max_lag: int = 0
    cache_promotion_misses: int = 0
    warm_prompt_tokens: int = 0
    warm_cached_tokens: int = 0
    prefix_changes: list[str] = field(default_factory=list)

    def absorb(self, episode) -> None:
        requests = getattr(episode, "requests", 0)
        self.requests += requests or episode.rounds
        self.prompt_tokens += episode.prompt_tokens
        self.cached_tokens += episode.cached_tokens
        self.reasoning_tokens += episode.reasoning_tokens
        self.completion_tokens += episode.completion_tokens
        self.latency_ms += episode.latency_ms
        self.starved += 1 if episode.starved else 0
        self.nudged += 1 if episode.nudged else 0
        self.incomplete += 1 if getattr(episode, "incomplete", False) else 0
        self.rounds_exhausted += 1 if getattr(
            episode, "rounds_exhausted", False) else 0
        self.stalled += 1 if getattr(episode, "stalled", False) else 0
        self.gate_exhausted += 1 if getattr(
            episode, "gate_exhausted", False) else 0
        self.wrap_ups += 1 if getattr(episode, "wrapped_up", False) else 0
        self.finalization_failures += 1 if getattr(
            episode, "finalization_failed", False) else 0
        self.tool_calls += len(episode.trace)
        self.tool_failures += episode.tool_failures
        self.cache_promotion = getattr(episode, "cache_promotion", "") or self.cache_promotion
        self.cache_max_lag = max(self.cache_max_lag,
                                 getattr(episode, "cache_max_lag", 0))
        self.cache_promotion_misses += getattr(
            episode, "cache_promotion_misses", 0)
        self.warm_prompt_tokens += getattr(episode, "warm_prompt_tokens", 0)
        self.warm_cached_tokens += getattr(episode, "warm_cached_tokens", 0)

    @property
    def cache_hit_rate(self) -> float:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0

    @property
    def warm_cache_hit_rate(self) -> float:
        """Return the hit rate after excluding the uncached first request."""
        if not self.warm_prompt_tokens:
            return 0.0
        return self.warm_cached_tokens / self.warm_prompt_tokens

    @property
    def reasoning_share(self) -> float:
        """Return the fraction of output tokens spent on reasoning."""
        if not self.completion_tokens:
            return 0.0
        return self.reasoning_tokens / self.completion_tokens

    def cost(self, rates: dict | None = None) -> float | None:
        """Return cost when rates are provided, otherwise ``None``."""
        rates = rates or DEFAULT_RATES
        if any(rates.get(k) is None for k in
               ("input_per_mtok", "cached_per_mtok", "output_per_mtok")):
            return None
        fresh = max(0, self.prompt_tokens - self.cached_tokens)
        return ((fresh * rates["input_per_mtok"]
                 + self.cached_tokens * rates["cached_per_mtok"]
                 + self.completion_tokens * rates["output_per_mtok"]) / 1_000_000)

    def savings(self, rates: dict | None = None) -> float | None:
        """Return the difference between uncached and cached cost."""
        rates = rates or DEFAULT_RATES
        if rates.get("input_per_mtok") is None or rates.get("cached_per_mtok") is None:
            return None
        gap = rates["input_per_mtok"] - rates["cached_per_mtok"]
        return self.cached_tokens * gap / 1_000_000


def render(usage: Usage, prefix=None, rates: dict | None = None,
           verification=None) -> str:
    """Render a human-readable status summary for ``/status``."""
    lines = ["요청 %d회 · %.1f초" % (usage.requests, usage.latency_ms / 1000)]

    lines.append(
        "토큰  입력 %s (캐시 %s, %.0f%%) · 출력 %s (추론 %s, %.0f%%)" % (
            f"{usage.prompt_tokens:,}", f"{usage.cached_tokens:,}",
            usage.cache_hit_rate * 100, f"{usage.completion_tokens:,}",
            f"{usage.reasoning_tokens:,}", usage.reasoning_share * 100))

    if usage.warm_prompt_tokens:
        # The first request is always uncached. Keep it separate from later
        # misses so structural cost is not confused with a failed promotion.
        line = ("캐시  첫 요청 제외 %.0f%% · 승격 %s" % (
            usage.warm_cache_hit_rate * 100, usage.cache_promotion or "미분류"))
        if usage.cache_promotion_misses:
            line += (f" · 승격 지연 {usage.cache_promotion_misses}회"
                     f"(최대 {usage.cache_max_lag}청크)")
        lines.append(line)

    if usage.tool_calls:
        note = f"도구  {usage.tool_calls}회"
        if usage.tool_failures:
            note += f" (실패 {usage.tool_failures})"
        lines.append(note)

    if (usage.starved or usage.nudged or usage.incomplete
            or usage.rounds_exhausted or usage.gate_exhausted
            or usage.stalled
            or usage.wrap_ups or usage.finalization_failures):
        marks = []
        if usage.starved:
            marks.append(f"예산 고갈 {usage.starved}회")
        if usage.nudged:
            marks.append(f"도구 보정 {usage.nudged}회")
        if usage.rounds_exhausted:
            marks.append(f"라운드 소진 {usage.rounds_exhausted}회")
        if usage.stalled:
            marks.append(f"진행 정체 중단 {usage.stalled}회")
        if usage.gate_exhausted:
            marks.append(f"완료 게이트 소진 {usage.gate_exhausted}회")
        if usage.wrap_ups:
            marks.append(f"강제 최종 정리 {usage.wrap_ups}회")
        if usage.finalization_failures:
            marks.append(f"정리 응답 대체 {usage.finalization_failures}회")
        if usage.incomplete:
            marks.append(f"미완료 실행 {usage.incomplete}회")
        lines.append("주의  " + " · ".join(marks))

    if verification is not None and verification.evidence:
        # Show evidence counts instead of trusting a claim that verification ran.
        report = verification.summary()
        line = (f"검증  실행 {report['dynamic']}회"
                f"(성공 {report['dynamic_succeeded']}) · "
                f"쓰기 {report['writes']}건"
                f"(성공 {report['writes_succeeded']})")
        if report.get("inspections"):
            # Inspection commands do not verify the artifact, so keep them out
            # of the execution-check count.
            line += f" · 조회 {report['inspections']}회(검증 아님)"
        if report["warnings"]:
            line += f" · 신선도 경고 {report['warnings']}회"
        lines.append(line)

    if prefix is not None:
        state = "적격" if prefix.cache_eligible else \
            (f"임계({model.CACHE_MIN_PREFIX_TOKENS}) 미만이라 캐시 부적격"
             " 추정")
        lines.append(f"프리픽스  {prefix.approx_tokens:,} 토큰 · {state} "
                     f"· 지문 {prefix.fingerprint} · v{prefix.version}")
        if prefix.breaks:
            lines.append(f"          프리픽스 변경 {len(prefix.breaks)}회 (캐시 손실)")

    total = usage.cost(rates)
    if total is not None:
        saved = usage.savings(rates)
        line = f"비용  {total:,.2f}"
        if saved:
            line += f" (캐시로 {saved:,.2f} 절감)"
        lines.append(line)

    return "\n".join(lines)


def diagnose(usage: Usage, prefix=None) -> list[str]:
    """Internal documentation."""
    notes: list[str] = []

    if prefix is not None and not prefix.cache_eligible:
        notes.append(
            f"프리픽스가 {prefix.approx_tokens} 토큰으로 캐시 임계"
            f"({model.CACHE_MIN_PREFIX_TOKENS}) 미만입니다(추정치 기준). "
            "AGENTS.md를 채우거나 스킬을 추가하면 캐시가 붙기 시작합니다.")

    if usage.requests > model.CACHE_WARMUP_REQUESTS and usage.cache_hit_rate < 0.3:
        notes.append(
            f"캐시 히트율이 {usage.cache_hit_rate:.0%}로 낮습니다. "
            "시스템 프롬프트나 도구 목록이 요청마다 바뀌고 있을 수 있습니다.")

    if usage.starved:
        notes.append(
            f"출력 예산 고갈이 {usage.starved}회 있었습니다. "
            "max_tokens를 제한했다면 해제하십시오.")

    if usage.rounds_exhausted:
        notes.append(
            f"라운드 상한 소진이 {usage.rounds_exhausted}회 있었습니다. "
            "작업 목록을 더 작게 나누거나 남은 작업을 이어서 실행하십시오.")

    if usage.stalled:
        notes.append(
            f"진행 정체로 중단된 실행이 {usage.stalled}회 있었습니다. "
            "같은 도구 호출 반복이나 연속 실패가 원인이므로, 실패한 도구의 "
            "오류 메시지를 먼저 확인하십시오.")

    if usage.gate_exhausted:
        notes.append(
            f"완료 조건 재시도 상한 소진이 {usage.gate_exhausted}회 있었습니다. "
            "최종 정리의 미완료 사유를 해결해야 성공 상태가 됩니다.")

    if usage.reasoning_share > 0.8 and usage.completion_tokens > 2000:
        notes.append(
            f"출력의 {usage.reasoning_share:.0%}가 추론입니다. "
            "정답이 나아지지 않는다면 effort=off가 훨씬 빠르고 쌉니다.")

    if usage.tool_failures and usage.tool_calls:
        rate = usage.tool_failures / usage.tool_calls
        if rate > 0.3:
            notes.append(
                f"도구 실패율이 {rate:.0%}입니다. "
                "도구 설명이나 스키마가 모호할 수 있습니다.")

    return notes
