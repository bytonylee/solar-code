"""Enforce evidence-based guardrails around the agent loop.

The guards detect repetitive output, unfinished plans, missing research
evidence, unsupported verification claims, and incomplete final reports. They
return explicit retries or issues instead of silently accepting unsupported work.
"""

import json
import os
import re
import threading
import urllib.parse
from dataclasses import dataclass

from .processors import Block, Retry

# Repetition threshold for long responses, with room below the normal range.
REPEAT_NGRAM = 8
REPEAT_THRESHOLD = 0.05
# Very short responses do not produce a meaningful repetition ratio.
REPEAT_MIN_TOKENS = REPEAT_NGRAM * 8


def ngram_repeat_ratio(text: str, n: int = REPEAT_NGRAM) -> float:
    """Return the fraction of overlapping n-grams that repeat.

    Whitespace tokens are sufficient to expose long-form repetition.
    """
    tokens = text.split()
    if len(tokens) < n * 2:
        return 0.0
    grams = [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]
    return 1.0 - len(set(grams)) / len(grams)


class RepetitionGuard:
    """Discard and retry a turn when it contains excessive repetition."""

    # Repetition screening applies to delegated children too.
    child_safe = True

    def __init__(self, threshold: float = REPEAT_THRESHOLD) -> None:
        self.threshold = threshold
        self.hits: list[float] = []

    def after_turn(self, turn) -> Retry | None:
        content = turn.content or ""
        if len(content.split()) < REPEAT_MIN_TOKENS:
            return None
        ratio = ngram_repeat_ratio(content)
        if ratio < self.threshold:
            return None
        self.hits.append(ratio)
        return Retry(
            f"직전 응답에서 동일 구간이 비정상적으로 반복되었습니다"
            f"(반복률 {ratio:.2f}). 반복 없이 간결하게 다시 작성하십시오.")


class CompletionGate:
    """Prevent a final answer while tracked tasks remain unfinished."""

    def __init__(self, tracker) -> None:
        self.tracker = tracker

    def after_turn(self, turn) -> Retry | None:
        if turn.tool_calls:
            return None  # The turn is still doing work.
        if not (turn.content or "").strip():
            return None  # The loop handles empty or starved turns separately.
        pending = self.tracker.pending
        if pending:
            titles = ", ".join(i["title"] for i in pending[:3])
            more = f" 외 {len(pending) - 3}건" if len(pending) > 3 else ""
            return Retry(
                f"완료되지 않은 작업이 남아 있습니다: {titles}{more}. "
                "작업을 마저 수행하거나, 진행할 수 없다면 update_task로 "
                "상태를 blocked로 바꾸고 사유를 남긴 뒤 답변하십시오.",
                tool_choice="required")

        blocked = self.tracker.list("blocked")
        unreported = [item for item in blocked
                      if item["title"] not in turn.content]
        if not unreported:
            return None
        titles = ", ".join(item["title"] for item in unreported[:3])
        more = f" 외 {len(unreported) - 3}건" if len(unreported) > 3 else ""
        return Retry(
            f"차단된 작업이 최종 답변에 보고되지 않았습니다: {titles}{more}. "
            "각 작업을 완료하지 못한 이유와 필요한 다음 조치를 명시하십시오.")

    def terminal_issues(self, answer: str = "") -> list[str]:
        pending = self.tracker.pending
        issues = []
        if pending:
            titles = ", ".join(item["title"] for item in pending[:5])
            more = f" 외 {len(pending) - 5}건" if len(pending) > 5 else ""
            issues.append(
                f"작업 목록에 미완료 항목이 남아 있습니다: {titles}{more}")
        blocked = self.tracker.list("blocked")
        if blocked:
            details = []
            for item in blocked[:5]:
                detail = item["title"]
                if item.get("note"):
                    detail += f" ({item['note']})"
                details.append(detail)
            more = f" 외 {len(blocked) - 5}건" if len(blocked) > 5 else ""
            issues.append("차단되어 완료하지 못한 작업: "
                          + ", ".join(details) + more)
        return issues


# Verification claim markers. Keep matching conservative so honest answers are
# not rejected more often than unsupported claims are accepted.
V1_CLAIMS = ("테스트가 통과", "테스트 통과", "실행해 확인", "실행 결과 정상",
             "빌드가 성공", "동작을 확인했")
V2_CLAIMS = ("수정했습니다", "고쳤습니다", "변경했습니다")

# Tools whose results can provide execution evidence.
DYNAMIC_TOOLS = ("shell", "run_tests")
WRITE_TOOLS = ("write_file", "edit_file")

# Inspection commands observe the filesystem but do not verify artifact behavior.
# Content searches remain eligible because their exit code can be meaningful.
INSPECTION_COMMANDS = frozenset({
    "ls", "cat", "pwd", "echo", "find", "tree", "head", "tail", "stat",
    "file", "wc", "which", "basename", "dirname", "realpath", "cd",
    "mkdir", "touch", "env", "date", "hostname", "whoami", "du", "df",
})

# Terms used to detect an explicit disclosure that verification did not run.
# A sentence must contain both a verification term and a negative expression.
NO_VERIFY_TERMS = ("검증", "테스트", "빌드", "실행 확인", "렌더 확인", "확인")
NO_VERIFY_NEGATIONS = ("하지 않았", "하지 못했", "하지 않음", "못했",
                       "없이", "없음", "미실행")


def _discloses_no_verification(content: str) -> bool:
    """Return whether the final answer explicitly discloses no verification."""
    if "미검증" in content or "검증 없이" in content:
        return True
    for sentence in re.split(r"[.\n]", content):
        if (any(term in sentence for term in NO_VERIFY_TERMS)
                and any(neg in sentence for neg in NO_VERIFY_NEGATIONS)):
            return True
    return False

# Compatibility markers for verification results and no-verification disclosures.
REPORT_STATUS_TERMS = ("검증", "테스트", "빌드", "실행", "확인", "검사",
                       "미실행", "하지 않았", "하지 못")


def _is_inspection_only(command: str) -> bool:
    """Return whether every command segment only inspects the filesystem."""
    first_words = []
    for segment in re.split(r"&&|\|\||[;|]", command):
        tokens = segment.split()
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]   # Skip a VAR=value prefix.
        if tokens:
            first_words.append(tokens[0])
    if not first_words:
        return True
    return all(word in INSPECTION_COMMANDS for word in first_words)

# Match only requests that explicitly require external research before delivery.
# Combine a source noun with a research action to avoid false positives for local
# tasks that merely mention a search-related bug.
RESEARCH_REQUEST_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"레퍼런스(?:\s*사이트)?(?:를|을)?\s*(?:확인|검색|조사)",
    r"(?:공식|외부)\s*(?:사이트|페이지)(?:를|을)?[^\n]{0,16}"
    r"(?:확인|검색|조사|찾)",
    r"(?:웹|인터넷|온라인)(?:에서|으로)?[^\n]{0,12}(?:검색|조사|찾)",
    r"출처(?:를|을)?\s*(?:확인|제시|검색|조사|찾)",
    r"\b(?:search the web|browse the web|research online|look up sources)\b",
))

SEARCH_FAILURE_DISCLOSURES = (
    "검색이 실패", "검색은 실패", "검색 실패", "검색이 차단",
    "검색은 차단", "출처를 확보하지 못", "출처를 확인하지 못",
    "검색 결과를 확인하지 못", "웹 검색을 완료하지 못",
    "검색 결과를 얻지 못", "검색 결과를 가져오지 못",
    "provider가 실패", "provider에서 실패",
)

UNSUPPORTED_SOURCE_CLAIMS = (
    "참고하여", "참고해서", "확인한 결과", "조사 결과", "공통 구조",
    "레퍼런스 분석", "공식 사이트에서 확인", "검색 결과에 따르면",
)


# Claims that require an actual web_fetch record, not only search hits.
# Keep the list narrow so ordinary wording does not trigger false retries.
FETCH_REQUIRED_CLAIMS = (
    "공식 사이트에서 확인", "공식 페이지에서 확인", "공식 홈페이지에서 확인",
    "공식 사이트를 확인", "공식 페이지를 확인", "페이지를 직접 확인",
    "직접 읽어 확인", "공식 페이지를 직접 읽",
)

# Citation list entries like "[1] 제목 — https://example.com (verified_fetch)".
CITATION_LINE = re.compile(r"\[(\d+)\]\s+[^\n]*?(https?://\S+)")


def _normalize_url(url: str) -> str:
    """Canonicalize a URL for citation matching (case, port, trailing slash)."""
    cleaned = url.strip().rstrip("/").rstrip(").,;]}>\"'")
    parsed = urllib.parse.urlparse(cleaned)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = host
    if port and not (scheme == "http" and port == 80) \
            and not (scheme == "https" and port == 443):
        netloc = f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunparse((scheme, netloc, path, "", parsed.query, ""))


class WebSearchGate:
    """Keep required research from silently becoming unsupported implementation.

    Required searches block mutation tools until a valid HTTP(S) source is
    observed. Optional searches still require an honest failure disclosure.

    The gate is shared with delegated children (``child_safe``): their
    search/fetch results count as evidence, while child episodes never reset
    this state (see processors.ChildChain).
    """

    child_safe = True

    def __init__(self, mutating_tools: set[str] | None = None) -> None:
        self.mutating_tools = set(mutating_tools or ())
        self.required = False
        self.attempts = 0
        self.sources: list[dict] = []
        self.failures: list[str] = []
        self.fetches: list[dict] = []
        self.fetch_failures: list[dict] = []
        # The provider is unavailable, so repeated blocking would only exhaust
        # rounds without producing a source.
        self.unavailable = False
        self.blocks = 0
        # Parallel delegate children share this gate; serialize mutations.
        self._lock = threading.RLock()

    # Errors that indicate provider infrastructure failure rather than a bad query.
    # "blocked" (bot challenge) is provider-level, not a model mistake.
    INFRA_ERRORS = ("not_configured", "network_error", "http_error", "blocked")
    # Limit extra search attempts after an infrastructure failure so repeated
    # query changes do not consume all available rounds.
    MAX_ATTEMPTS_AFTER_OUTAGE = 2

    def start_episode(self, task: str) -> None:
        """Reset search state for a task and discard prior sources."""
        with self._lock:
            self.required = any(pattern.search(task or "")
                                for pattern in RESEARCH_REQUEST_PATTERNS)
            self.attempts = 0
            self.sources = []
            self.failures = []
            self.fetches = []
            self.fetch_failures = []
            self.unavailable = False
            self.blocks = 0
            # The landing benchmark supplies a pre-verified, immutable reference
            # brief before the agent starts. Keep the normal live-search contract
            # unchanged unless this explicit benchmark-only opt-in is set.
            if os.environ.get("SOL_BENCHMARK_REFERENCES_VERIFIED") == "1":
                self.required = False

    @staticmethod
    def _valid_source(item: object) -> bool:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            return False
        parsed = urllib.parse.urlparse(str(item.get("url") or ""))
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)

    def before_tool(self, name: str, args: dict) -> dict | Block:
        with self._lock:
            if (name == "web_search" and self.unavailable and not self.sources
                    and self.attempts >= self.MAX_ATTEMPTS_AFTER_OUTAGE):
                return Block(
                    f"web_search가 {self.attempts}회 연속 provider 장애로 "
                    "실패했습니다. 질의를 바꿔도 복구되지 않는 상태이므로 검색을 "
                    "중단하고, 로컬 작업을 진행한 뒤 최종 답변에 검색 실패와 "
                    "미검증 범위를 명시하십시오.")
            if not (self.required and not self.sources
                    and name in self.mutating_tools):
                return args
            # After an infrastructure failure, block once to prompt a search
            # attempt and then allow local work. Final checks still report
            # missing sources.
            if self.unavailable and self.blocks:
                return args
            self.blocks += 1
            state = ("아직 web_search를 실행하지 않았습니다" if not self.attempts
                     else "web_search가 유효한 출처를 반환하지 못했습니다")
            message = ("이 작업은 외부 레퍼런스 확인을 명시적으로 요구합니다. "
                       f"{state}. 출처를 확보하기 전에는 파일 쓰기, 셸 실행 또는 "
                       "위임 작업을 진행할 수 없습니다.")
            if self.unavailable:
                message += (" 검색 provider 자체가 응답하지 않는 상태이므로, "
                            "다음 시도부터는 로컬 작업을 진행하되 최종 답변에 "
                            "검색 실패와 미검증 범위를 반드시 명시하십시오.")
            return Block(message)

    def after_tool(self, name: str, result: str) -> str:
        if name == "web_fetch":
            return self._after_fetch(result)
        if name != "web_search":
            return result
        with self._lock:
            self.attempts += 1
            try:
                payload = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                self.failures.append("검색 도구 응답을 해석할 수 없음")
                return result
            if not isinstance(payload, dict):
                self.failures.append("검색 도구 응답이 객체가 아님")
                return result
            if "error" in payload:
                self.failures.append(str(payload.get("error")
                                         or "provider 오류"))
                if str(payload.get("error_type") or "") in self.INFRA_ERRORS:
                    self.unavailable = True
                return result
            results = payload.get("results")
            if not isinstance(results, list):
                self.failures.append("검색 결과 형식이 잘못됨")
                return result
            valid = [item for item in results if self._valid_source(item)]
            if not valid:
                self.failures.append("유효한 HTTP(S) 출처가 0건임")
                return result
            self.sources.extend(valid)
            return result

    def _after_fetch(self, result: str) -> str:
        """Record one web_fetch outcome as fetch evidence or failure."""
        try:
            payload = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            payload = None
        with self._lock:
            if not isinstance(payload, dict):
                self.fetch_failures.append({"url": "",
                                            "error_type": "invalid_response"})
                return result
            if "error" in payload:
                self.fetch_failures.append({
                    "url": str(payload.get("url") or ""),
                    "error_type": str(payload.get("error_type") or "unknown"),
                })
                return result
            content = payload.get("content")
            passages = payload.get("passages")
            if isinstance(content, str):
                chars = len(content)
            elif isinstance(passages, list):
                chars = sum(len(str(p)) for p in passages)
            else:
                chars = 0
            self.fetches.append({
                "final_url": str(payload.get("final_url")
                                 or payload.get("url") or ""),
                "status": payload.get("status"),
                "content_type": str(payload.get("content_type") or ""),
                "chars": chars,
                # A JS-shell or bot-wall page must not count as evidence.
                "warning": str(payload.get("content_warning") or ""),
            })
        return result

    def after_turn(self, turn) -> Retry | None:
        if turn.tool_calls:
            return None
        content = (turn.content or "").strip()
        if not content:
            return None
        with self._lock:
            if self.sources:
                if not self._verified_fetches() and any(
                        term in content for term in FETCH_REQUIRED_CLAIMS):
                    return Retry(
                        "공식 페이지를 직접 확인했다는 표현이 있지만 web_fetch "
                        "기록이 없습니다. web_fetch로 해당 페이지를 읽거나, "
                        "문구를 검색 결과 수준으로 낮추십시오.")
                citation_issue = self._check_citations(content)
                if citation_issue:
                    return Retry(citation_issue)
                return None
            if not self.attempts:
                if not self.required:
                    return None
                return Retry(
                    "사용자가 외부 레퍼런스 확인을 요구했지만 web_search 관측이 "
                    "없습니다. 먼저 검색하여 유효한 출처를 확보하거나, 검색할 수 "
                    "없다면 출처 의존 작업을 완료하지 못했다고 명시하십시오.")

            disclosed = any(term in content
                            for term in SEARCH_FAILURE_DISCLOSURES)
            unsupported = any(term in content
                              for term in UNSUPPORTED_SOURCE_CLAIMS)
            if disclosed and not unsupported:
                return None
            detail = self.failures[-1] if self.failures else "유효한 출처 없음"
            return Retry(
                "web_search가 출처를 제공하지 못했습니다"
                f"({detail}). 기억에 의존해 사이트를 참고했다고 쓰지 말고, 검색 "
                "실패와 출처 의존 작업의 미완료 범위를 명시하십시오.")

    def _verified_fetches(self) -> list[dict]:
        """Fetches whose pages carried no JS-shell/bot-wall warning."""
        return [f for f in self.fetches if not f.get("warning")]

    def grade(self) -> str | None:
        """Return the evidence grade observed so far: fetch > search > none."""
        with self._lock:
            if self._verified_fetches():
                return "verified_fetch"
            if self.sources:
                return "search_only"
            return None

    def child_view(self):
        """View shared with delegated children: evidence in, no contract.

        The parent episode's ``required`` and outage blocking must not force
        a child to re-satisfy them; the parent's final checks run on the
        parent's own turns. The child's search/fetch results still record
        here so delegated research counts as evidence.
        """
        gate = self

        class _View:
            def before_tool(self, name: str, args: dict) -> dict:
                return args

            def after_tool(self, name: str, result: str) -> str:
                return gate.after_tool(name, result)

            def after_turn(self, turn) -> None:
                return None

            def terminal_issues(self, answer: str = "") -> list:
                return []

        return _View()

    def _check_citations(self, content: str) -> str:
        """Return a retry reason when citation entries lack tool evidence."""
        entries = CITATION_LINE.findall(content)
        if not entries:
            return ""
        known = set()
        for item in self.sources:
            url = str(item.get("url") or "")
            if url:
                known.add(_normalize_url(url))
        for item in self._verified_fetches():
            url = str(item.get("final_url") or "")
            if url:
                known.add(_normalize_url(url))
        missing = [url for _num, url in entries
                   if _normalize_url(url) not in known]
        if not missing:
            return ""
        return ("출처 목록의 URL이 검색·페치 기록에 없습니다: "
                f"{missing[0]}. 실제로 확인한 출처만 목록에 쓰십시오.")

    def terminal_issues(self, answer: str = "") -> list[str]:
        if not self.required or self.sources:
            return []
        detail = (self.failures[-1] if self.failures
                  else "web_search 실행 기록이 없음")
        return ["외부 출처 확인이 필수인 작업이지만 유효한 출처를 확보하지 "
                f"못했습니다: {detail}"]


@dataclass
class Evidence:
    """Record one observation that can support a verification claim.

    ``ok`` is true for success, false for failure, and ``None`` when the result
    is unavailable or cannot be interpreted.
    """

    kind: str        # "dynamic" | "write".
    tool: str
    detail: str      # Command string or file path.
    ok: bool | None
    sequence: int
    # True when a dynamic command only inspects the filesystem and cannot verify
    # artifact behavior.
    inspection: bool = False


def _judge_result(result: str) -> bool | None:
    """Read success or failure from a tool result envelope."""
    try:
        payload = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if "error" in payload:
        return False
    if "exit_code" in payload:
        return payload["exit_code"] == 0
    return True


class VerificationGate:
    """Reject verification claims without matching tool evidence.

    Dynamic evidence supports execution claims, write evidence supports change
    claims, and a successful check after the last write keeps evidence fresh.

    Shared with delegated children so their builds and tests count as
    evidence for the parent's final report.
    """

    child_safe = True

    def __init__(self, checkpoints=None) -> None:
        # Checkpoint records are optional; without them, judge writes by evidence.
        self.checkpoints = checkpoints
        self.evidence: list[Evidence] = []
        self.violations: list[str] = []
        self.warnings: list[str] = []
        self._sequence = 0

    @property
    def dynamic_ok(self) -> bool:
        """Return whether a dynamic execution was attempted."""
        return any(e.kind == "dynamic" for e in self.evidence)

    @property
    def wrote(self) -> bool:
        """Return whether a write was attempted."""
        return any(e.kind == "write" for e in self.evidence)

    @property
    def last_write_sequence(self) -> int:
        """Return the sequence number of the latest non-failing write."""
        return max((e.sequence for e in self.evidence
                    if e.kind == "write" and e.ok is not False), default=0)

    @property
    def verified_after_write(self) -> bool:
        """Return whether a non-inspection check succeeded after the last write."""
        last_write = self.last_write_sequence
        return any(e.kind == "dynamic" and not e.inspection
                   and e.ok is not False and e.sequence > last_write
                   for e in self.evidence)

    def before_tool(self, name: str, args: dict) -> dict:
        kind = ("dynamic" if name in DYNAMIC_TOOLS
                else "write" if name in WRITE_TOOLS else None)
        if kind is not None:
            self._sequence += 1
            detail = str(args.get("command") or args.get("path") or "")
            inspection = (kind == "dynamic" and name == "shell"
                          and _is_inspection_only(detail))
            self.evidence.append(Evidence(kind, name, detail, None,
                                          self._sequence,
                                          inspection=inspection))
        return args

    def after_tool(self, name: str, result: str) -> str:
        """Attach a result to the latest pending observation without changing it."""
        for entry in reversed(self.evidence):
            if entry.tool == name and entry.ok is None:
                entry.ok = _judge_result(result)
                break
        return result

    def summary(self) -> dict:
        """Return an evidence summary for user-facing reporting."""
        dynamic = [e for e in self.evidence if e.kind == "dynamic"]
        writes = [e for e in self.evidence if e.kind == "write"]
        checks = [e for e in dynamic if not e.inspection]
        return {"dynamic": len(dynamic),
                "dynamic_succeeded": sum(1 for e in dynamic if e.ok is True),
                "checks": len(checks),
                "checks_succeeded": sum(1 for e in checks if e.ok is True),
                "inspections": len(dynamic) - len(checks),
                "writes": len(writes),
                "writes_succeeded": sum(1 for e in writes if e.ok is True),
                "warnings": len(self.warnings)}

    # --- Evidence decisions -------------------------------------------

    def _v1_backed(self) -> tuple[bool, str]:
        """Return whether execution evidence supports a V1 claim and why."""
        dynamic = [e for e in self.evidence if e.kind == "dynamic"]
        if not dynamic:
            return False, "no_attempt"
        if any(e.ok is not False for e in dynamic):
            return True, ""
        return False, "all_failed"

    def _v2_backed(self) -> tuple[bool, str]:
        """Return whether write evidence supports a V2 claim."""
        writes = [e for e in self.evidence if e.kind == "write"]
        if not writes:
            return False, "no_write"
        if not any(e.ok is not False for e in writes):
            return False, "all_failed"
        if self.checkpoints is not None and self.checkpoints.entries:
            changed = any(
                any(b.digest != a.digest for b, a in zip(cp.before, cp.after))
                for cp in self.checkpoints.entries)
            if not changed:
                return False, "no_change"
        return True, ""

    def _v1_stale(self) -> bool:
        """Return whether no successful execution followed the latest write."""
        last_write = max((e.sequence for e in self.evidence
                          if e.kind == "write" and e.ok is not False), default=0)
        if not last_write:
            return False
        last_good_run = max((e.sequence for e in self.evidence
                             if e.kind == "dynamic" and e.ok is not False),
                            default=0)
        return last_write > last_good_run

    def after_turn(self, turn) -> Retry | None:
        if turn.tool_calls:
            return None
        content = turn.content or ""
        if not content.strip():
            return None

        claim = next((c for c in V1_CLAIMS if c in content), None)
        if claim:
            backed, why = self._v1_backed()
            if not backed:
                self.violations.append(f"V1: {claim}")
                if why == "all_failed":
                    return Retry(
                        f"답변에 실행 검증 주장({claim!r})이 있지만 이 세션의 "
                        "실행은 전부 실패했습니다(exit_code != 0). 실패를 "
                        "해결한 뒤 다시 실행하거나, 실패 사실대로 작성하십시오.")
                return Retry(
                    f"답변에 실행 검증 주장({claim!r})이 있지만 이 세션에서 "
                    "명령이 실행된 기록이 없습니다. 실제로 실행해 확인하거나, "
                    "실행하지 않았다면 검증 주장을 빼고 다시 작성하십시오.")
            if self._v1_stale():
                warning = ("[검증 신선도 경고] 마지막 파일 수정 이후 실행된 "
                           "검증이 없습니다. 위 실행 결과는 수정 전 상태의 "
                           "것일 수 있습니다.")
                self.warnings.append(warning)
                turn.content = f"{content}\n\n{warning}"

        claim = next((c for c in V2_CLAIMS if c in content), None)
        if claim:
            backed, why = self._v2_backed()
            if not backed:
                self.violations.append(f"V2: {claim}")
                if why == "no_change":
                    return Retry(
                        f"답변에 수정 주장({claim!r})이 있지만 체크포인트 "
                        "기록상 파일 내용이 변경되지 않았습니다(해시 동일). "
                        "실제 변경을 수행하거나, 주장을 빼고 다시 작성하십시오.")
                if why == "all_failed":
                    return Retry(
                        f"답변에 수정 주장({claim!r})이 있지만 이 세션의 쓰기는 "
                        "전부 실패했습니다. 실패를 해결하거나, 주장을 빼고 "
                        "다시 작성하십시오.")
                return Retry(
                    f"답변에 수정 주장({claim!r})이 있지만 이 세션에서 파일이 "
                    "변경된 기록이 없습니다. 실제로 수정하거나, 수정하지 "
                    "않았다면 주장을 빼고 다시 작성하십시오.")
        return None


class FinalReportGate:
    """Require changed paths and verification status in final reports."""

    def __init__(self, verification: VerificationGate) -> None:
        self.verification = verification
        self.violations: list[str] = []

    def after_turn(self, turn) -> Retry | None:
        if turn.tool_calls:
            return None
        content = (turn.content or "").strip()
        if not content:
            return None
        writes = [entry for entry in self.verification.evidence
                  if entry.kind == "write" and entry.ok is not False]
        if not writes:
            return None

        paths = [entry.detail for entry in writes if entry.detail]
        mentions_path = (not paths or any(
            path in content or path.rsplit("/", 1)[-1] in content
            for path in paths))
        missing = []
        if not mentions_path:
            missing.append("변경한 파일 경로")
        if not self.verification.verified_after_write:
            disclosed = _discloses_no_verification(content)
            if not disclosed:
                missing.append("마지막 변경 이후의 검증 실행 또는 미검증 사유")
        if not missing:
            return None

        label = ", ".join(missing)
        self.violations.append(label)
        return Retry(
            f"파일 변경 뒤 최종 정리에 {label}가 빠져 있습니다. "
            "가장 작은 실행 확인(테스트·빌드·구문 검사·렌더 확인)을 하거나, "
            "할 수 없다면 미검증 사실과 사유를 명시해 다시 정리하십시오.")
