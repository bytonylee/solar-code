"""Keyless web-fetch boundary with explicit failure envelopes.

Fetch one URL over plain HTTP(S) without any API key and return extracted
text or raw HTML. Every failure is an ``{"error", "error_type"}`` envelope
instead of an exception, mirroring websearch.py. Design and verification
record: PLAN-web-fetch.md and PLAN-keyless-web.md.
"""

import gzip
import io
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

from .websearch import _challenge_hit, _decode_html

MAX_CHARS = 40_000
READ_LIMIT = 2_000_000
MAX_REDIRECTS = 5
TIMEOUT = 30
FETCH_UA = "sol-webfetch/1.0"
# Below this much extracted text a 200 page is likely a JS shell or a bot
# wall, so it must not be cited as evidence.
MIN_CONTENT_CHARS = 200
PASSAGE_WINDOWS = 5
PASSAGE_FALLBACK_CHARS = 1_000
TEXT_TYPES = ("", "text/html", "text/plain", "application/json",
              "text/markdown")

_BLOCK_TAGS = frozenset({
    "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td",
    "th", "br", "hr", "section", "article", "header", "footer", "nav",
    "ul", "ol", "table", "blockquote", "pre", "main", "aside", "figure",
    "figcaption", "dl", "dt", "dd",
})
_SKIP_TAGS = frozenset({
    "script", "style", "iframe", "noscript", "template", "svg",
})


def _is_public_ip(ip) -> bool:
    """Globally routable only; rejects private/loopback/link-local ranges."""
    return ip.is_global


def _is_public_host(host: str, resolve) -> bool:
    """Validate an IP literal directly or every resolved address."""
    host = (host or "").strip("[]")
    if not host:
        return False
    try:
        return _is_public_ip(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = resolve(host, None)
    except Exception:  # noqa: BLE001 - DNS failure means "cannot verify".
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError, TypeError):
            return False
        if not _is_public_ip(ip):
            return False
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface 3xx as HTTPError so each hop can be re-validated."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _TextExtractor(HTMLParser):
    """Extract readable text, skipping script/style/iframe/noscript."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS and not self._skip_depth:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS and not self._skip_depth:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        return "\n".join(line for line in lines if line)


def extract_text(html: str) -> str:
    """Convert HTML to newline-separated readable text."""
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


def extract_passages(text: str, query: str,
                     limit: int = PASSAGE_WINDOWS) -> dict:
    """Return deterministic query-relevant windows from extracted text.

    Blocks are non-empty lines. Score is the sum of case-insensitive
    substring counts of each query token. Ties go to the earlier block;
    overlapping windows merge. No matches yields an empty passage list
    plus a head fallback so callers never refetch blindly.
    """
    blocks = [line for line in text.split("\n") if line.strip()]
    tokens = [token.lower() for token in query.split() if token.strip()]
    hits = []
    for index, block in enumerate(blocks):
        lowered = block.lower()
        score = sum(lowered.count(token) for token in tokens)
        if score:
            hits.append((index, score))
    if not hits:
        return {
            "passages": [],
            "matched": 0,
            "total_blocks": len(blocks),
            "fallback_content": text[:PASSAGE_FALLBACK_CHARS],
        }
    top = sorted(hits, key=lambda item: (-item[1], item[0]))[:limit]
    windows: list[list[int]] = []
    for index, _score in sorted(top, key=lambda item: item[0]):
        start = max(0, index - 1)
        end = min(len(blocks), index + 2)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(end, windows[-1][1])
        else:
            windows.append([start, end])
    return {
        "passages": ["\n".join(blocks[s:e]) for s, e in windows],
        "matched": len(hits),
        "total_blocks": len(blocks),
    }


def _decompress(raw: bytes, encoding: str) -> bytes:
    """Decode a gzip/deflate body with an output cap, else pass through."""
    if encoding == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            return stream.read(READ_LIMIT + 1)
    if encoding == "deflate":
        return zlib.decompress(raw, bufsize=READ_LIMIT + 1)[:READ_LIMIT + 1]
    return raw


class FetchClient:
    """Fetch one URL with SSRF, redirect, and size boundaries."""

    def __init__(self, opener=None, resolver=None) -> None:
        if opener is None:
            opener = urllib.request.build_opener(_NoRedirect).open
        self._open = opener
        self._resolve = resolver or socket.getaddrinfo

    def _validate(self, url: str) -> dict | None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return {
                "error": f"http(s) URL만 가져올 수 있습니다: "
                         f"{parsed.scheme or '(스킴 없음)'}",
                "error_type": "unsupported_scheme",
                "url": url,
            }
        if not parsed.netloc:
            return {
                "error": "URL에 호스트가 없습니다.",
                "error_type": "invalid_args",
                "url": url,
            }
        if not _is_public_host(parsed.hostname or "", self._resolve):
            return {
                "error": f"사설·내부 주소는 가져올 수 없습니다: "
                         f"{parsed.hostname}",
                "error_type": "private_address",
                "url": url,
            }
        return None

    def fetch(self, url: str, fmt: str = "text",
              query: str | None = None) -> dict:
        current = url
        raw = b""
        status = 0
        headers = {}
        for _hop in range(MAX_REDIRECTS + 1):
            invalid = self._validate(current)
            if invalid is not None:
                return invalid
            request = urllib.request.Request(
                current,
                headers={"User-Agent": FETCH_UA,
                         "Accept": "text/html, text/plain, */*;q=0.5",
                         "Accept-Encoding": "identity"})
            try:
                with self._open(request, timeout=TIMEOUT) as response:
                    raw = response.read(READ_LIMIT + 1)
                    status = int(getattr(response, "status", 200) or 200)
                    headers = response.headers
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    location = ""
                    try:
                        location = str(exc.headers.get("Location", ""))
                    except Exception:  # noqa: BLE001
                        location = ""
                    if not location:
                        return {
                            "error": f"리다이렉트 대상이 없는 HTTP "
                                     f"{exc.code} 응답입니다.",
                            "error_type": "invalid_response",
                            "url": url,
                        }
                    current = urllib.parse.urljoin(current, location)
                    continue
                return {
                    "error": f"페이지 요청 오류: HTTP {exc.code}",
                    "error_type": "http_error",
                    "http_status": exc.code,
                    "url": url,
                }
            except Exception as exc:  # noqa: BLE001 - network boundary.
                return {
                    "error": f"페이지 가져오기 실패: {exc}",
                    "error_type": "network_error",
                    "url": url,
                }
            break
        else:
            return {
                "error": f"리다이렉트가 {MAX_REDIRECTS}회를 넘었습니다.",
                "error_type": "invalid_response",
                "url": url,
            }

        content_type = str(headers.get("Content-Type", ""))
        media = content_type.split(";")[0].strip().lower()
        if media not in TEXT_TYPES and not media.endswith("+json"):
            return {
                "error": f"지원하지 않는 콘텐츠 형식입니다: {media}",
                "error_type": "unsupported_content",
                "content_type": media,
                "url": url,
            }
        encoding = str(headers.get("Content-Encoding", "")).lower()
        try:
            raw = _decompress(raw, encoding)
        except Exception as exc:  # noqa: BLE001
            return {
                "error": f"압축 해제 실패: {exc}",
                "error_type": "invalid_response",
                "url": url,
            }

        decoded = _decode_html(raw[:READ_LIMIT], content_type)
        marker = _challenge_hit(decoded)
        if marker:
            return {
                "error": f"봇 차단 페이지를 반환했습니다({marker}).",
                "error_type": "blocked",
                "url": url,
            }

        text = decoded if fmt == "html" else extract_text(decoded)
        envelope = {
            "url": url,
            "final_url": current,
            "status": status,
            "content_type": media,
            "format": fmt,
        }
        if len(text.strip()) < MIN_CONTENT_CHARS:
            envelope["content_warning"] = "empty_or_js_shell"

        if query:
            envelope.update(extract_passages(text, query))
            return envelope
        envelope["content"] = text[:MAX_CHARS]
        envelope["truncated"] = len(text) > MAX_CHARS
        return envelope


def web_fetch_tool(client: FetchClient | None = None):
    """Build the read-only web-fetch tool."""
    from .tools import Tool
    client = client or FetchClient()

    def run(args: dict) -> dict:
        url = str(args.get("url") or "").strip()
        if not url:
            return {"error": "url이 비어 있습니다"}
        fmt = str(args.get("format") or "text").strip().lower()
        if fmt not in ("text", "html"):
            return {
                "error": f"지원하지 않는 format입니다: {fmt}",
                "error_type": "invalid_args",
                "url": url,
            }
        query = str(args.get("query") or "").strip() or None
        if query and fmt == "html":
            return {
                "error": "query는 text 형식에서만 사용할 수 있습니다.",
                "error_type": "invalid_args",
                "url": url,
            }
        return client.fetch(url, fmt=fmt, query=query)

    return Tool(
        name="web_fetch",
        description=(
            "URL 하나를 키 없이 직접 읽어 텍스트(또는 HTML)를 가져온다. "
            "검색 스니펫은 '확인'이 아니므로, 공식 페이지를 참고했다고 쓰기 "
            "전에 이 도구로 읽는다. content_warning이 있으면 근거로 쓰지 "
            "않는다. 오류 봉투면 실패 사유를 그대로 보고한다."),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "가져올 http(s) 절대 URL"},
                "format": {"type": "string",
                           "enum": ["text", "html"],
                           "description": "text(기본): 추출 텍스트, "
                                          "html: 원본 HTML"},
                "query": {"type": "string",
                          "description": "있으면 관련 구절만 반환 "
                                         "(text 형식 전용)"},
            },
            "required": ["url"],
        },
        run=run,
        read_only=True,
    )
