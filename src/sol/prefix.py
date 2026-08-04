"""Keep the system and tool prefix stable at the byte level.

Serialize the prefix deterministically, expose intentional invalidation, and
record which segment changed when cache reuse becomes unavailable.
"""

import hashlib
import json

from . import model


def _digest(value: object) -> str:
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def canonical_tools(tools: list[dict] | None) -> list[dict] | None:
    """Serialize tool specifications deterministically by name."""
    if not tools:
        return None
    return sorted(tools, key=lambda t: t.get("function", {}).get("name", ""))


class PrefixBroken(RuntimeError):
    """The prefix changed without explicit invalidation."""


class StablePrefix:
    """Snapshot of the system prompt and tool specifications."""

    def __init__(self, system: str, tools: list[dict] | None = None,
                 system_blocks: list[tuple[str, str]] | None = None) -> None:
        self._system = system
        self._tools = canonical_tools(tools)
        # Optional named system blocks (base/agents/skills/environment/mcp...).
        # Fingerprint still hashes the full system string so adding blocks does
        # not change the episode contract when the assembled text is identical.
        self._system_blocks = list(system_blocks or [])
        self._fingerprint = self._compute()
        self.version = 1
        self.breaks: list[str] = []

    def _compute(self) -> str:
        return _digest([self._system, self._tools])

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def system(self) -> str:
        return self._system

    @property
    def tools(self) -> list[dict] | None:
        return self._tools

    @property
    def approx_tokens(self) -> int:
        total = model.estimate_tokens(self._system)
        if self._tools:
            total += model.estimate_tokens(
                json.dumps(self._tools, ensure_ascii=False))
        return total

    @property
    def cache_eligible(self) -> bool:
        """Return whether the estimated prefix reaches the cache minimum."""
        return self.approx_tokens >= model.CACHE_MIN_PREFIX_TOKENS

    def segments(self) -> dict[str, str]:
        """Return per-segment fingerprints for diagnosing cache breaks.

        When system blocks are available, expose each block (base, agents,
        skills, environment, mcp, ...) separately. Always include the full
        system digest and the tools digest so older diagnostics keep working.
        """
        out: dict[str, str] = {}
        if self._system_blocks:
            for name, body in self._system_blocks:
                out[name] = _digest(body)
        else:
            out["system"] = _digest(self._system)
        # Keep a full-system digest even when blocks exist so a silent
        # reassembly bug still shows up as a segment change.
        if "system" not in out:
            out["system"] = _digest(self._system)
        out["tools"] = _digest(self._tools)
        return out

    def invalidate(self, system: str | None = None,
                   tools: list[dict] | None = None,
                   system_blocks: list[tuple[str, str]] | None = None) -> None:
        """Replace the prefix intentionally and mark the next request cold."""
        if system is not None:
            self._system = system
        if tools is not None:
            self._tools = canonical_tools(tools)
        if system_blocks is not None:
            self._system_blocks = list(system_blocks)
        self._fingerprint = self._compute()
        self.version += 1

    def assert_stable(self) -> None:
        """Verify that the prefix content has not changed silently."""
        current = self._compute()
        if current != self._fingerprint:
            self.breaks.append(f"{self._fingerprint} -> {current}")
            raise PrefixBroken(
                f"prefix mutated without invalidate(): {self._fingerprint} -> {current}")


class AppendOnlyLog:
    """Append conversation messages without rewriting prior turns."""

    def __init__(self) -> None:
        self._messages: list[dict] = []
        self.rewrites = 0

    def __len__(self) -> int:
        return len(self._messages)

    @property
    def messages(self) -> list[dict]:
        return list(self._messages)

    def append(self, message: dict) -> None:
        self._messages.append(message)

    def extend(self, messages: list[dict]) -> None:
        self._messages.extend(messages)

    def rewrite(self, messages: list[dict]) -> None:
        """Replace history for an explicit compaction operation."""
        self._messages = list(messages)
        self.rewrites += 1

    def approx_tokens(self) -> int:
        return sum(model.estimate_tokens(str(m.get("content") or ""))
                   for m in self._messages)


class CacheLedger:
    """Record cache reuse, warm-up statistics, and promotion lag."""

    def __init__(self) -> None:
        self.requests = 0
        self.prompt_tokens = 0
        self.cached_tokens = 0
        self._skipped = 0
        # Measure promotion lag for every request. Warm-up exclusion affects the
        # hit-rate statistic, not promotion behavior.
        self._previous_prompt = 0
        self.lags: list[int] = []
        self.promotion_misses = 0
        # Exclude the first request from the hit-rate aggregate because it is
        # structurally uncached and would mask later misses.
        self.warm_prompt_tokens = 0
        self.warm_cached_tokens = 0
        # Record when and where the prefix changed so a cache miss has an
        # actionable cause.
        self.baseline: dict[str, str] | None = None
        self.breaks: list[str] = []

    def record(self, prompt_tokens: int, cached_tokens: int) -> None:
        self._record_lag(prompt_tokens, cached_tokens)
        if self._skipped < model.CACHE_WARMUP_REQUESTS:
            self._skipped += 1
            return
        self.requests += 1
        self.prompt_tokens += prompt_tokens
        self.cached_tokens += cached_tokens

    def _record_lag(self, prompt_tokens: int, cached_tokens: int) -> None:
        """Record promotion lag for this request and update warm aggregates."""
        chunk = model.CACHE_CHUNK_TOKENS
        previous = self._previous_prompt
        if previous:
            # Only the previous prompt can be a cache candidate; later tokens
            # could not have been cached and must not count toward lag.
            warm_cap = min(prompt_tokens, previous) // chunk
            lag = warm_cap - cached_tokens // chunk
            self.lags.append(lag)
            if lag > 0:
                self.promotion_misses += 1
            self.warm_prompt_tokens += prompt_tokens
            self.warm_cached_tokens += cached_tokens
        self._previous_prompt = prompt_tokens

    def reset_warm(self, reason: str = "") -> None:
        """Mark the prefix cold and optionally record the reason."""
        self._previous_prompt = 0
        if reason:
            self.breaks.append(reason)

    @property
    def max_lag(self) -> int:
        return max(self.lags) if self.lags else 0

    @property
    def warm_hit_rate(self) -> float:
        """Return hit rate after excluding the first request."""
        if not self.warm_prompt_tokens:
            return 0.0
        return self.warm_cached_tokens / self.warm_prompt_tokens

    @property
    def promotion(self) -> str:
        """Classify observed promotion behavior as eager, laggy, or steady."""
        if self.promotion_misses >= 2:
            return "laggy"
        if not self.lags:
            return "steady"
        if any(lag < 0 for lag in self.lags) and not self.promotion_misses:
            return "eager"
        return "steady"

    def observe(self, prefix: "StablePrefix") -> bool:
        """Observe the prefix and return whether it can be reused."""
        segments = prefix.segments()
        if self.baseline is None:
            self.baseline = segments
            return False  # The first request is always uncached.
        if segments == self.baseline:
            return True
        for name, digest in segments.items():
            was = self.baseline.get(name, "?")
            if was != digest:
                self.breaks.append(
                    f"Request #{self.requests + 1} {name} changed: {was} -> {digest}")
        self.baseline = segments
        # A changed prefix makes the previous prompt ineligible for reuse.
        self._previous_prompt = 0
        return False

    @property
    def hit_rate(self) -> float:
        if not self.prompt_tokens:
            return 0.0
        return self.cached_tokens / self.prompt_tokens

    def render(self) -> str:
        if not self.requests:
            return "cache: no measured requests (warmup only)"
        line = (f"cache: {self.hit_rate:.0%} hit "
                f"({self.cached_tokens}/{self.prompt_tokens} tokens, "
                f"{self.requests} requests after warmup)")
        if self.lags:
            line += (f"\n  승격 {self.promotion} · 최대 lag {self.max_lag} · "
                     f"지연 {self.promotion_misses}회 · "
                     f"첫 요청 제외 히트율 {self.warm_hit_rate:.0%}")
        if self.breaks:
            line += f"\n  프리픽스 변경 {len(self.breaks)}회: " + \
                "; ".join(self.breaks[:3])
        return line
