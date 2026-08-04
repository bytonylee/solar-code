"""Probe cache hits using usage fields from real API responses.

The probe sends repeated requests with the same stable prefix and computes the
hit rate only from cached token counts. It does not estimate cache usage.
Warm-up requests are excluded from the aggregate, and the prefix must reach
the configured minimum chunk size before it is eligible.
"""

from . import model
from .prefix import CacheLedger, StablePrefix

# Keep the padding paragraph deterministic so the prefix stays stable.
_PARAGRAPH = ("캐시 프리픽스 측정용 고정 문단이다. 이 문장의 내용은 "
              "의미가 없으며 실행마다 바이트가 같아야 한다. " * 4)


def build_probe_system(min_tokens: int | None = None) -> str:
    """Build a deterministic system prompt large enough to cover most input.

    The default target leaves room above the cache boundary. Tests can pass a
    smaller ``min_tokens`` value when checking boundary behavior.
    """
    target = (min_tokens if min_tokens is not None
              else model.CACHE_TARGET_PREFIX_TOKENS)
    system = ""
    while model.estimate_tokens(system) < target:
        system += _PARAGRAPH
    return system


def run_probe(complete, requests: int = 5, system: str | None = None,
              question: str = "2+2는? 숫자만.") -> dict:
    """Send ``requests`` calls with one prefix and return observations.

    ``complete(messages)`` must accept the same arguments as the client helper.
    The result contains per-request rows, a ledger, a post-warm-up hit rate,
    and an eligibility flag.
    """
    system = system if system is not None else build_probe_system()
    prefix = StablePrefix(system)
    ledger = CacheLedger()
    rows = []
    for index in range(1, requests + 1):
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": question}]
        turn = complete(messages)
        ledger.observe(prefix)
        ledger.record(turn.prompt_tokens, turn.cached_tokens)
        rows.append({"request": index,
                     "prompt_tokens": turn.prompt_tokens,
                     "cached_tokens": turn.cached_tokens,
                     "measured": index > model.CACHE_WARMUP_REQUESTS})
    return {"rows": rows, "ledger": ledger, "hit_rate": ledger.hit_rate,
            "measured_requests": ledger.requests,
            "eligible": prefix.cache_eligible,
            "approx_tokens": prefix.approx_tokens}


def run_prefix_probe(complete, prefix: StablePrefix, task: str,
                     requests: int = 5) -> dict:
    """Measure the actual agent prefix with a task over repeated requests.

    Pass the system and tool specifications assembled by ``build_agent`` directly
    to the completion function so tool-enabled tasks are measured separately.
    """
    ledger = CacheLedger()
    rows = []
    for index in range(1, requests + 1):
        messages = [{"role": "system", "content": prefix.system},
                    {"role": "user", "content": task}]
        turn = complete(messages, tools=prefix.tools)
        ledger.observe(prefix)
        ledger.record(turn.prompt_tokens, turn.cached_tokens)
        rows.append({"request": index,
                     "prompt_tokens": turn.prompt_tokens,
                     "cached_tokens": turn.cached_tokens,
                     "measured": index > model.CACHE_WARMUP_REQUESTS})
    return {"rows": rows, "ledger": ledger, "hit_rate": ledger.hit_rate,
            "measured_requests": ledger.requests,
            "eligible": prefix.cache_eligible,
            "approx_tokens": prefix.approx_tokens,
            "task": task,
            "fingerprint": prefix.fingerprint}


def render(result: dict) -> str:
    """Render probe results for a human reader."""
    lines = []
    for row in result["rows"]:
        mark = "측정" if row["measured"] else "워밍업"
        lines.append(f"  #{row['request']} [{mark}] "
                     f"prompt={row['prompt_tokens']:,} "
                     f"cached={row['cached_tokens']:,}")
    lines.append(f"프리픽스 {result['approx_tokens']:,} 토큰 "
                 f"({'적격' if result['eligible'] else '임계 미만'}) · "
                 f"측정 히트율 {result['hit_rate']:.0%} "
                 f"(워밍업 {model.CACHE_WARMUP_REQUESTS}회 제외, "
                 f"{result['measured_requests']}회 측정)")
    return "\n".join(lines)
