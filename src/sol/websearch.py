"""Web-search boundary with explicit failure envelopes.

Return ``{"error": ...}`` when every provider is unavailable or returns
invalid data instead of inventing search results. The default chain tries
TinyFish (API key) first, then keyless HTML providers (Naver, Mojeek)
that were verified to answer plain GETs without a key. See
PLAN-keyless-web.md for the verification record.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from html.parser import HTMLParser

from . import credentials

TINYFISH_URL = "https://api.search.tinyfish.ai"
DEFAULT_COUNT = 5
MAX_COUNT = 10
TIMEOUT = 30

# Static UA for keyless providers. Never carry per-request values.
SEARCH_UA = "sol-websearch/1.0"

# Bot-challenge markers observed in the 2026-08-04 verification plus common
# Korean portal blocks. Presence means "blocked", not "no results".
CHALLENGE_MARKERS = (
    "bots use duckduckgo",
    "verifying your browser",
    "checking your browser",
    "anomaly-modal",
    "captcha",
    "비정상적인 접근",
    "자동입력 방지",
)

# Keyless providers are shared infrastructure: pace calls to avoid blocks.
MIN_INTERVAL = 1.5
MAX_PER_MINUTE = 3
HTML_READ_LIMIT = 2_000_000


def load_secret(name: str) -> str:
    """Load a secret from the environment or a local ``.env`` file.

    The current project directory is checked first so global npm, Homebrew,
    curl, and git installs can still use the caller's local configuration.
    """
    package_root = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    return credentials.load(name, package_root=package_root)


def _is_public_source(url: str) -> bool:
    """Return whether a URL is an absolute HTTP(S) source."""
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _clamp_count(count) -> int:
    try:
        return max(1, min(int(count), MAX_COUNT))
    except (TypeError, ValueError):
        return DEFAULT_COUNT


class TinyFishProvider:
    """Provider for TinyFish structured search responses."""

    name = "tinyfish"

    def __init__(self, api_key: str | None = None, opener=None) -> None:
        self.api_key = (api_key if api_key is not None
                        else load_secret("TINYFISH_API_KEY"))
        self._open = opener or urllib.request.urlopen

    def available(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str, count: int = DEFAULT_COUNT) -> dict:
        if not self.api_key:
            return {
                "error": "TINYFISH_API_KEY가 없습니다. 환경변수나 .env에 "
                         "설정하면 웹 검색이 활성화됩니다.",
                "error_type": "not_configured",
                "provider": self.name,
            }

        count = _clamp_count(count)
        url = TINYFISH_URL + "?" + urllib.parse.urlencode({
            "query": query,
            "location": "KR",
            "language": "ko",
        })
        request = urllib.request.Request(
            url,
            headers={"X-API-Key": self.api_key,
                     "Accept": "application/json"},
        )
        try:
            with self._open(request, timeout=TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {
                "error": f"검색 API 오류: HTTP {exc.code}",
                "error_type": "http_error",
                "http_status": exc.code,
                "provider": self.name,
            }
        except json.JSONDecodeError as exc:
            return {
                "error": f"검색 API JSON 해석 실패: {exc}",
                "error_type": "invalid_json",
                "provider": self.name,
            }
        except Exception as exc:  # noqa: BLE001 - network boundary is broad.
            return {
                "error": f"검색 실패: {exc}",
                "error_type": "network_error",
                "provider": self.name,
            }

        if not isinstance(data, dict):
            return {
                "error": "검색 API 응답이 객체가 아닙니다.",
                "error_type": "invalid_response",
                "provider": self.name,
            }
        raw = data.get("results") or []
        if not isinstance(raw, list):
            return {
                "error": "검색 API 결과 형식을 해석할 수 없습니다.",
                "error_type": "invalid_response",
                "provider": self.name,
            }

        results = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            if not title or not _is_public_source(url):
                continue
            results.append({
                "title": title,
                "url": url,
                "snippet": str(item.get("snippet") or ""),
                "site": str(item.get("site_name") or ""),
            })
            if len(results) >= count:
                break

        if raw and not results:
            return {
                "error": "검색 API 응답에서 유효한 HTTP(S) 출처를 "
                         "확인할 수 없습니다.",
                "error_type": "invalid_response",
                "provider": self.name,
            }
        return {
            "results": results,
            "count": len(results),
            "provider": self.name,
            "query": query,
        }


def default_provider() -> TinyFishProvider:
    """Return the TinyFish provider (used when a key is configured)."""
    return TinyFishProvider()


def _challenge_hit(text: str) -> str:
    """Return the matched bot-challenge marker, or "" when absent."""
    lowered = text.lower()
    for marker in CHALLENGE_MARKERS:
        if marker in lowered:
            return marker
    return ""


def _decode_html(raw: bytes, content_type: str) -> str:
    """Decode HTML honoring header charset, then meta, then UTF-8."""
    charset = ""
    match = re.search(r"charset=([\w.-]+)", content_type or "", re.IGNORECASE)
    if match:
        charset = match.group(1)
    if not charset:
        head = raw[:4096].decode("ascii", errors="ignore")
        match = re.search(r"charset=[\"']?([\w.-]+)", head, re.IGNORECASE)
        if match:
            charset = match.group(1)
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class _RateLimiter:
    """Serialize and pace calls to one keyless provider."""

    def __init__(self, clock=None, sleeper=None,
                 min_interval: float = MIN_INTERVAL,
                 per_minute: int = MAX_PER_MINUTE) -> None:
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._min_interval = min_interval
        self._per_minute = per_minute
        self._last: float | None = None
        self._window: deque = deque()
        self._lock = threading.Lock()

    def throttle(self) -> None:
        with self._lock:
            now = self._clock()
            while self._window and now - self._window[0] >= 60.0:
                self._window.popleft()
            delay = 0.0
            if self._last is not None:
                delay = max(delay, self._min_interval - (now - self._last))
            if len(self._window) >= self._per_minute:
                delay = max(delay, 60.0 - (now - self._window[0]))
            if delay > 0:
                self._sleep(delay)
            self._last = self._clock()
            self._window.append(self._last)


class _ResultLinkParser(HTMLParser):
    """Collect external result anchors from a search-result HTML page."""

    MIN_TITLE = 6

    def __init__(self, skip_hosts: tuple) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = skip_hosts
        self._href = ""
        self._text: list[str] = []
        self.results: list[dict] = []
        self._seen: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        if href.startswith(("http://", "https://")):
            self._href = href
            self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or not self._href:
            return
        title = " ".join("".join(self._text).split())
        url = self._href
        self._href = ""
        self._text = []
        if len(title) < self.MIN_TITLE or url in self._seen:
            return
        host = urllib.parse.urlparse(url).netloc.lower()
        if any(host == skip or host.endswith("." + skip)
               for skip in self._skip):
            return
        self._seen.add(url)
        self.results.append({"title": title, "url": url})


class _HtmlSearchProvider:
    """Base for keyless HTML search providers with failure envelopes."""

    name = "html"
    search_url = ""
    skip_hosts: tuple = ()

    def __init__(self, opener=None, limiter: _RateLimiter | None = None
                 ) -> None:
        self._open = opener or urllib.request.urlopen
        self._limiter = limiter or _RateLimiter()

    def available(self) -> bool:
        return True  # Keyless by definition.

    def _build_url(self, query: str) -> str:
        raise NotImplementedError

    def search(self, query: str, count: int = DEFAULT_COUNT) -> dict:
        count = _clamp_count(count)
        self._limiter.throttle()
        request = urllib.request.Request(
            self._build_url(query),
            headers={"User-Agent": SEARCH_UA, "Accept": "text/html",
                     "Accept-Encoding": "identity"})
        try:
            with self._open(request, timeout=TIMEOUT) as response:
                raw = response.read(HTML_READ_LIMIT)
                content_type = str(response.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            return {
                "error": f"검색 요청 오류: HTTP {exc.code}",
                "error_type": "http_error",
                "http_status": exc.code,
                "provider": self.name,
            }
        except Exception as exc:  # noqa: BLE001 - network boundary is broad.
            return {
                "error": f"검색 실패: {exc}",
                "error_type": "network_error",
                "provider": self.name,
            }

        text = _decode_html(raw, content_type)
        marker = _challenge_hit(text)
        if marker:
            return {
                "error": f"{self.name} 검색이 봇 차단 페이지를 반환했습니다"
                         f"({marker})",
                "error_type": "blocked",
                "provider": self.name,
            }

        parser = _ResultLinkParser(self.skip_hosts)
        parser.feed(text)
        results = []
        for item in parser.results:
            results.append({
                "title": item["title"],
                "url": item["url"],
                "snippet": "",
                "site": urllib.parse.urlparse(item["url"]).netloc,
            })
            if len(results) >= count:
                break
        if not results:
            # Zero parsed links on a real page means the markup changed or the
            # query had no results; report instead of fabricating.
            return {
                "error": f"{self.name} 결과를 해석할 수 없습니다"
                         "(구조 변경 또는 결과 없음)",
                "error_type": "invalid_response",
                "provider": self.name,
            }
        return {
            "results": results,
            "count": len(results),
            "provider": self.name,
            "query": query,
        }


class NaverProvider(_HtmlSearchProvider):
    """Keyless Naver mobile search. Verified 2026-08-04 (PLAN-keyless-web)."""

    name = "naver"
    skip_hosts = ("naver.com", "pstatic.net", "navercorp.com")

    def _build_url(self, query: str) -> str:
        return ("https://m.search.naver.com/search.naver?"
                + urllib.parse.urlencode({"query": query}))


class MojeekProvider(_HtmlSearchProvider):
    """Keyless Mojeek search. Weak for Korean; used as general fallback."""

    name = "mojeek"
    skip_hosts = ("mojeek.com",)

    def _build_url(self, query: str) -> str:
        return ("https://www.mojeek.com/search?"
                + urllib.parse.urlencode({"q": query}))


class SearchChain:
    """Try providers in a fixed order and record every attempt."""

    name = "chain"

    def __init__(self, providers: list) -> None:
        self.providers = list(providers)
        self._cache: dict[tuple, dict] = {}

    def search(self, query: str, count: int = DEFAULT_COUNT) -> dict:
        attempts = []
        last_error: dict | None = None
        for provider in self.providers:
            key = (provider.name, query, _clamp_count(count))
            if key in self._cache:
                attempts.append({"provider": provider.name, "ok": True,
                                 "cached": True})
                envelope = dict(self._cache[key])
                envelope["attempts"] = attempts
                return envelope
            envelope = provider.search(query, count)
            ok = isinstance(envelope, dict) and "error" not in envelope
            attempts.append({"provider": provider.name, "ok": ok,
                             "error_type": None if ok
                             else envelope.get("error_type")})
            if ok:
                envelope = dict(envelope)
                envelope["attempts"] = attempts
                self._cache[key] = {k: v for k, v in envelope.items()
                                    if k != "attempts"}
                return envelope
            last_error = envelope
        out = dict(last_error or {
            "error": "사용 가능한 검색 provider가 없습니다.",
            "error_type": "not_configured",
        })
        out["attempts"] = attempts
        return out


def default_chain() -> SearchChain:
    """Build the default provider chain: TinyFish(key) -> Naver -> Mojeek.

    ``SOL_SEARCH_PROVIDER`` forces one provider (tinyfish|naver|mojeek)
    for reproducible tests; anything else means the automatic order.
    """
    forced = os.environ.get("SOL_SEARCH_PROVIDER", "auto").strip().lower()
    tinyfish = TinyFishProvider()
    by_name = {"tinyfish": tinyfish,
               "naver": NaverProvider(),
               "mojeek": MojeekProvider()}
    if forced in by_name:
        return SearchChain([by_name[forced]])
    providers = [tinyfish] if tinyfish.available() else []
    providers.extend([by_name["naver"], by_name["mojeek"]])
    return SearchChain(providers)


def web_search_tool(provider=None):
    """Build the read-only web-search tool over the provider chain."""
    from .tools import Tool
    provider = provider or default_chain()

    def run(args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "query가 비어 있습니다"}
        return provider.search(query, args.get("count") or DEFAULT_COUNT)

    return Tool(
        name="web_search",
        description=(
            "웹의 공개 정보를 검색한다. TINYFISH_API_KEY가 있으면 "
            "TinyFish를, 없으면 무키 provider(네이버, Mojeek)를 순서대로 "
            "시도한다. 결과가 오류 봉투면 검색한 것으로 주장하지 말고 "
            "원인을 그대로 보고한다. 검색 스니펫만으로 '공식 사이트를 "
            "확인했다'고 쓰지 않고, 확인이 필요하면 web_fetch로 직접 "
            "읽는다."),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색어"},
                "count": {
                    "type": "integer",
                    "description": f"결과 수 (1~{MAX_COUNT}, "
                                   f"기본 {DEFAULT_COUNT})",
                },
            },
            "required": ["query"],
        },
        run=run,
        read_only=True,
    )
