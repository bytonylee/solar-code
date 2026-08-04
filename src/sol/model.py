"""Measured runtime contract for the supported Solar models.

Keep model limits, cache boundaries, sampling defaults, and token estimates in
one place so callers do not duplicate assumptions.
"""

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

# Keep the runtime default separate from the measured contract. The default
# can change independently while the measured model remains the baseline used
# to label verified limits.
MEASURED_MODEL = "solar-open2"
DEFAULT_MODEL = "solar-pro4"
SUPPORTED_MODELS = (MEASURED_MODEL, DEFAULT_MODEL)
MODEL = os.environ.get("SOL_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
# Use SOL_API_URL only for an endpoint that preserves the same chat-completions
# contract. The request contract itself does not change.
API_URL = os.environ.get(
    "SOL_API_URL", "https://api.upstage.ai/v1/chat/completions")

# Maximum output length enforced by the API:
#   "exceeds this model's maximum output length of 131072 tokens"
MAX_OUTPUT_TOKENS = 131_072

# The context limit is set just below the largest confirmed successful request.
VERIFIED_CONTEXT_TOKENS = 1_000_000

# Prefix caching begins at a fixed chunk boundary. The current confirmed boundary
# is 1,088 tokens, and cached token counts are multiples of that chunk size.
CACHE_CHUNK_TOKENS = 1_088
# One full chunk is required before caching can begin. approx_tokens is character
# based and may differ from actual tokens, so boundary decisions are estimates.
CACHE_MIN_PREFIX_TOKENS = CACHE_CHUNK_TOKENS
CACHE_BLOCK_TOKENS = 64
# Use a larger target prefix so most input is cached after warm-up.
CACHE_TARGET_PREFIX_TOKENS = 13_056
# The first few requests for an identical prefix can be uncached. Exclude them
# from the aggregate so the cache statistic reflects steady-state behavior.
CACHE_WARMUP_REQUESTS = 3

# Korean text uses more tokens per character than English. Use separate estimates
# so a single English-oriented constant does not distort Korean budgets.
KO_CHARS_PER_TOKEN = 1.85
EN_CHARS_PER_TOKEN = 6.48


class Effort:
    """Expose two stable effort choices mapped to API reasoning levels."""

    OFF = "off"
    ON = "on"

    CHOICES = (OFF, ON)
    _API = {OFF: "none", ON: "high"}

    @classmethod
    def to_api(cls, choice: str) -> str:
        try:
            return cls._API[choice]
        except KeyError:
            raise ValueError(
                f"effort must be one of {cls.CHOICES}, got {choice!r}"
            ) from None


@dataclass(frozen=True)
class ModelSettings:
    """Read-only snapshot of model settings for one request."""

    model: str
    measured_model: str
    supported: bool
    contract_verified: bool
    endpoint: str
    effort: str
    reasoning_effort: str
    max_output_tokens: int
    verified_context_tokens: int
    temperature: int | float
    top_p: int | float
    presence_penalty: int | float
    frequency_penalty: int | float
    cache_chunk_tokens: int
    cache_warmup_requests: int

    def rows(self) -> tuple[tuple[str, str], ...]:
        """Return ordered rows for a human-readable settings panel."""
        source = ("measured ceiling" if self.contract_verified
                  else f"{self.measured_model} baseline")
        context_source = ("verified" if self.contract_verified
                          else f"{self.measured_model} baseline")
        cache_source = ("measured" if self.contract_verified
                        else f"{self.measured_model} baseline")
        return (
            ("Model", self.model),
            ("Endpoint", self.endpoint),
            ("Thinking", f"{self.effort} "
             f"(reasoning_effort={self.reasoning_effort})"),
            ("Output budget", f"{self.max_output_tokens:,} tokens ({source})"),
            ("Context", f"{self.verified_context_tokens:,} tokens {context_source}"),
            ("Sampling", f"temperature={self.temperature} · top_p={self.top_p}"),
            ("Penalties", f"presence={self.presence_penalty} "
             f"· frequency={self.frequency_penalty}"),
            ("Cache", f"{self.cache_chunk_tokens:,}-token chunks "
             f"· warmup {self.cache_warmup_requests} ({cache_source})"),
        )


def endpoint_display(url: str | None = None) -> str:
    """Return an endpoint display value without query, fragment, or credentials."""
    url = API_URL if url is None else url
    parsed = urlsplit(url)
    if parsed.scheme and parsed.netloc:
        # Never expose credentials that may be embedded in the endpoint URL.
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, netloc,
                           parsed.path or "/", "", ""))
    return url.split("?", 1)[0].split("#", 1)[0]


def settings(effort: str = Effort.OFF) -> ModelSettings:
    """Return the read-only contract snapshot for the current request."""
    reasoning_effort = Effort.to_api(effort)
    sampling = sampling_params()
    return ModelSettings(
        model=MODEL,
        measured_model=MEASURED_MODEL,
        supported=MODEL in SUPPORTED_MODELS,
        contract_verified=MODEL == MEASURED_MODEL,
        endpoint=endpoint_display(),
        effort=effort,
        reasoning_effort=reasoning_effort,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        verified_context_tokens=VERIFIED_CONTEXT_TOKENS,
        temperature=sampling["temperature"],
        top_p=sampling["top_p"],
        presence_penalty=sampling["presence_penalty"],
        frequency_penalty=sampling["frequency_penalty"],
        cache_chunk_tokens=CACHE_CHUNK_TOKENS,
        cache_warmup_requests=CACHE_WARMUP_REQUESTS,
    )


def select(name: str) -> str:
    """Select a supported model for subsequent requests."""
    name = name.strip()
    if name not in SUPPORTED_MODELS:
        raise ValueError(
            f"model must be one of {SUPPORTED_MODELS}, got {name!r}"
        )
    global MODEL
    MODEL = name
    return MODEL


# Low-effort requests can occasionally omit tool calls. When a tool is required,
# make one additional request with tool_choice=required.
EFFORT_OFF_NEEDS_TOOL_NUDGE = True

# Identical requests can still vary in prose even with deterministic sampling.
# Compare structured output after normalization rather than requiring exact text.
DETERMINISTIC_AT_T0 = False

# Starvation can result from an intermittent server-side cutoff as well as a
# budget limit. Retrying the same request can recover, so the loop retries once.
STARVED_RETRIES = 1

# Reasoning requests need room for both reasoning and answer content. Keep the
# default reasoning budget above the minimum observed successful range.
SAFE_REASONING_BUDGET = 8_192

# Models may exceed requested counts. When quantity matters, count the output
# directly instead of trusting the model's self-report.
COUNT_INSTRUCTIONS_OVERSHOOT = True

# Tool-call identifiers vary by selection mode, so they must not be part of a
# reproducibility key.
VOLATILE_RESPONSE_FIELDS = ("reasoning", "id", "created", "tool_call_id")


def sampling_params() -> dict:
    """Return fixed sampling parameters chosen for output quality."""
    return {
        "temperature": 0,
        "top_p": 1,
        "presence_penalty": 0,
        "frequency_penalty": 0,
    }


def estimate_tokens(text: str) -> int:
    """Estimate token count from character classes without a tokenizer.

    Use this only for context and cache eligibility decisions, not billing.
    """
    if not text:
        return 0
    hangul = sum(1 for ch in text if "\uac00" <= ch <= "\ud7a3")
    rest = len(text) - hangul
    return int(hangul / KO_CHARS_PER_TOKEN + rest / EN_CHARS_PER_TOKEN) + 1
