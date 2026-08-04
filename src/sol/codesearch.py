"""AST-backed symbol outline and workspace symbol search.

Python files are parsed with the standard ``ast`` module so results reflect
real structure instead of string matches. JavaScript and TypeScript fall back
to conservative declaration patterns. Both tools are read-only and bounded so
they never inflate the conversation history.
"""

import ast
import os
import re

from .tools import Tool
from .workspace import SKIP_DIRS

MAX_RESULTS = 50
MAX_FILE_BYTES = 1_000_000

PY_EXTS = (".py",)
JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs")

# Conservative declaration patterns for JS/TS. These are a fallback, not a
# parser; anonymous and deeply nested definitions are intentionally skipped.
_JS_PATTERNS = (
    ("class", re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")),
    ("function", re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
        r"function\s*\*?\s*([A-Za-z_$][\w$]*)")),
    ("function", re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
        r"(?:async\s*)?(?:\(|function\b|[A-Za-z_$][\w$]*\s*=>)")),
)


def _python_symbols(text: str) -> list[dict]:
    tree = ast.parse(text)
    found: list[dict] = []

    def walk(body: list, prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{node.name}"
                args = ", ".join(a.arg for a in node.args.args)
                head = "async def" if isinstance(node, ast.AsyncFunctionDef) \
                    else "def"
                found.append({"kind": "method" if prefix else "function",
                              "name": name, "line": node.lineno,
                              "end_line": node.end_lineno,
                              "signature": f"{head} {node.name}({args})"})
                walk(node.body, f"{name}.")
            elif isinstance(node, ast.ClassDef):
                name = f"{prefix}{node.name}"
                bases = ", ".join(ast.unparse(b) for b in node.bases)
                suffix = f"({bases})" if bases else ""
                found.append({"kind": "class", "name": name,
                              "line": node.lineno,
                              "end_line": node.end_lineno,
                              "signature": f"class {node.name}{suffix}"})
                walk(node.body, f"{name}.")

    walk(tree.body, "")
    return found


def _js_symbols(text: str) -> list[dict]:
    found: list[dict] = []
    for number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in _JS_PATTERNS:
            match = pattern.match(line)
            if match:
                found.append({"kind": kind, "name": match.group(1),
                              "line": number, "end_line": number,
                              "signature": line.strip()[:160]})
                break
    return found


def _file_symbols(target: str) -> list[dict] | dict:
    """Return symbols for one file or an error envelope."""
    ext = os.path.splitext(target)[1]
    try:
        if os.path.getsize(target) > MAX_FILE_BYTES:
            return {"error": "파일이 너무 큽니다"}
        with open(target, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": str(exc)}
    if ext in PY_EXTS:
        try:
            return _python_symbols(text)
        except SyntaxError as exc:
            return {"error": f"Python 구문 오류로 파싱하지 못했습니다: {exc}"}
    if ext in JS_EXTS:
        return _js_symbols(text)
    return {"error": f"지원하지 않는 확장자입니다: {ext or '(없음)'}"}


class CodeSearch:
    """Bounded read-only structure queries inside one root."""

    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(root)

    def _resolve(self, path: str) -> str:
        target = os.path.realpath(os.path.join(self.root, path))
        if target != self.root and not target.startswith(self.root + os.sep):
            raise ValueError(f"작업 디렉터리 밖입니다: {path}")
        return target

    def outline(self, args: dict) -> dict:
        path = args["path"]
        try:
            target = self._resolve(path)
        except ValueError as exc:
            return {"error": str(exc)}
        symbols = _file_symbols(target)
        if isinstance(symbols, dict):
            return symbols
        return {"path": path, "symbols": symbols[:MAX_RESULTS],
                "count": len(symbols),
                "truncated": len(symbols) > MAX_RESULTS}

    def find_symbol(self, args: dict) -> dict:
        query = args["name"].strip()
        if not query:
            return {"error": "name이 비어 있습니다"}
        where = args.get("path", ".")
        try:
            start = self._resolve(where)
        except ValueError as exc:
            return {"error": str(exc)}
        exact: list[dict] = []
        partial: list[dict] = []
        lowered = query.lower()
        for base, dirs, files in os.walk(start):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                if os.path.splitext(name)[1] not in PY_EXTS + JS_EXTS:
                    continue
                target = os.path.join(base, name)
                symbols = _file_symbols(target)
                if isinstance(symbols, dict):
                    continue
                relative = os.path.relpath(target, self.root)
                for symbol in symbols:
                    plain = symbol["name"].rsplit(".", 1)[-1]
                    hit = dict(symbol, path=relative)
                    if plain == query:
                        exact.append(hit)
                    elif lowered in symbol["name"].lower():
                        partial.append(hit)
                if len(exact) >= MAX_RESULTS:
                    break
            if len(exact) >= MAX_RESULTS:
                break
        hits = (exact + partial)[:MAX_RESULTS]
        return {"query": query, "hits": hits, "count": len(hits),
                "truncated": len(exact) + len(partial) > MAX_RESULTS}


def tools(root: str) -> list[Tool]:
    """Build the AST search tools for one workspace root."""
    search = CodeSearch(root)
    return [
        Tool("outline",
             "파일의 클래스·함수 구조를 AST로 요약한다. 긴 파일에서 특정 "
             "함수의 위치를 찾을 때 전체 read_file 대신 먼저 쓴다. "
             "지원: Python(ast), JS/TS(선언 패턴).",
             {"type": "object",
              "properties": {"path": {"type": "string",
                                      "description": "작업 디렉터리 기준 상대 경로"}},
              "required": ["path"]}, search.outline),
        Tool("find_symbol",
             "이름으로 함수·클래스 정의 위치를 워크스페이스 전체에서 찾는다. "
             "정의를 찾을 때는 grep보다 정확하다. 사용처 검색은 grep이나 "
             "references를 쓴다.",
             {"type": "object",
              "properties": {"name": {"type": "string",
                                      "description": "찾을 심벌 이름"},
                             "path": {"type": "string",
                                      "description": "검색 범위 디렉터리(선택)"}},
              "required": ["name"]}, search.find_symbol),
    ]
