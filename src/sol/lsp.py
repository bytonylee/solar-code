"""Minimal LSP client for definition and reference lookups.

Servers are resolved once at startup so the tool schema stays part of the
stable prefix. Server processes start lazily on the first call for their
language, and every request is bounded by a timeout. Failures return error
envelopes instead of raising, so stall detection can see broken lookups.
"""

import json
import os
import select
import shutil
import subprocess
import threading

from .tools import Tool

REQUEST_TIMEOUT = 15
STARTUP_TIMEOUT = 20
MAX_LOCATIONS = 40

# Default servers, used only when the binary exists on PATH at startup.
# A project can override or extend this via .sol/lsp.json:
#   {"servers": [{"command": ["pylsp"], "extensions": [".py"]}]}
DEFAULT_SERVERS = (
    {"command": ["pyright-langserver", "--stdio"],
     "extensions": [".py"], "name": "pyright"},
    {"command": ["typescript-language-server", "--stdio"],
     "extensions": [".js", ".jsx", ".ts", ".tsx", ".mjs"],
     "name": "typescript"},
)

LSP_FILE = ".sol/lsp.json"


def resolve_servers(root: str) -> list[dict]:
    """Return available server configs, decided once at startup."""
    configured: list[dict] | None = None
    path = os.path.join(root, LSP_FILE)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            configured = list(data.get("servers", []))
        except (OSError, json.JSONDecodeError, AttributeError):
            configured = None
    candidates = configured if configured is not None else list(DEFAULT_SERVERS)
    found = []
    for entry in candidates:
        command = entry.get("command") or []
        if command and shutil.which(command[0]):
            found.append(entry)
    return found


class ServerError(RuntimeError):
    pass


class _Server:
    """One LSP server process with synchronous request handling."""

    def __init__(self, command: list[str], root: str) -> None:
        self.command = command
        self.root = root
        self.process: subprocess.Popen | None = None
        self._id = 0
        self._opened: set[str] = set()
        self._lock = threading.Lock()

    # --- Framing ---------------------------------------------------------

    def _write(self, payload: dict) -> None:
        assert self.process is not None and self.process.stdin is not None
        blob = json.dumps(payload).encode("utf-8")
        head = f"Content-Length: {len(blob)}\r\n\r\n".encode("ascii")
        self.process.stdin.write(head + blob)
        self.process.stdin.flush()

    def _read_message(self, timeout: float) -> dict:
        assert self.process is not None and self.process.stdout is not None
        stdout = self.process.stdout
        header = b""
        while b"\r\n\r\n" not in header:
            ready, _, _ = select.select([stdout], [], [], timeout)
            if not ready:
                raise ServerError("LSP 응답 시간 초과")
            chunk = stdout.read1(1)
            if not chunk:
                raise ServerError("LSP 서버가 종료되었습니다")
            header += chunk
        length = 0
        for line in header.decode("ascii", "replace").split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        body = b""
        while len(body) < length:
            ready, _, _ = select.select([stdout], [], [], timeout)
            if not ready:
                raise ServerError("LSP 응답 시간 초과")
            chunk = stdout.read1(length - len(body))
            if not chunk:
                raise ServerError("LSP 서버가 종료되었습니다")
            body += chunk
        return json.loads(body.decode("utf-8"))

    def _request(self, method: str, params: dict,
                 timeout: float = REQUEST_TIMEOUT) -> object:
        self._id += 1
        current = self._id
        self._write({"jsonrpc": "2.0", "id": current,
                     "method": method, "params": params})
        # Servers interleave notifications and requests; skip anything that is
        # not the response to this id. Server-to-client requests get an empty
        # result so the server does not block.
        while True:
            message = self._read_message(timeout)
            if message.get("id") == current and "method" not in message:
                if "error" in message:
                    raise ServerError(str(message["error"].get("message")))
                return message.get("result")
            if "id" in message and "method" in message:
                self._write({"jsonrpc": "2.0", "id": message["id"],
                             "result": None})

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # --- Lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.process = subprocess.Popen(
            self.command, cwd=self.root, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._opened.clear()
        self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": f"file://{self.root}",
            "workspaceFolders": [{"uri": f"file://{self.root}",
                                  "name": os.path.basename(self.root)}],
            "capabilities": {"textDocument": {
                "definition": {}, "references": {}}},
        }, timeout=STARTUP_TIMEOUT)
        self._notify("initialized", {})

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
        self.process = None

    # --- Queries ---------------------------------------------------------

    def _ensure_open(self, target: str) -> None:
        if target in self._opened:
            return
        with open(target, encoding="utf-8") as fh:
            text = fh.read()
        language = {"py": "python", "ts": "typescript", "tsx": "typescriptreact",
                    "js": "javascript", "jsx": "javascriptreact",
                    "mjs": "javascript"}.get(
            target.rsplit(".", 1)[-1], "plaintext")
        self._notify("textDocument/didOpen", {"textDocument": {
            "uri": f"file://{target}", "languageId": language,
            "version": 1, "text": text}})
        self._opened.add(target)

    def locate(self, method: str, target: str, line: int, column: int) -> list:
        with self._lock:
            self.start()
            self._ensure_open(target)
            params = {"textDocument": {"uri": f"file://{target}"},
                      "position": {"line": line - 1, "character": column - 1}}
            if method == "textDocument/references":
                params["context"] = {"includeDeclaration": True}
            result = self._request(method, params)
        if result is None:
            return []
        if isinstance(result, dict):
            result = [result]
        spots = []
        for item in result:
            uri = item.get("uri") or item.get("targetUri", "")
            span = item.get("range") or item.get("targetSelectionRange", {})
            start = span.get("start", {})
            path = uri.removeprefix("file://")
            if path.startswith(self.root):
                path = os.path.relpath(path, self.root)
            spots.append({"path": path,
                          "line": start.get("line", 0) + 1,
                          "column": start.get("character", 0) + 1})
        return spots


class Pool:
    """Lazily started LSP servers keyed by file extension."""

    def __init__(self, root: str, configs: list[dict]) -> None:
        self.root = os.path.realpath(root)
        self._configs = configs
        self._servers: dict[int, _Server] = {}

    def _server_for(self, path: str) -> _Server | None:
        ext = os.path.splitext(path)[1]
        for index, entry in enumerate(self._configs):
            if ext in entry.get("extensions", []):
                if index not in self._servers:
                    self._servers[index] = _Server(
                        list(entry["command"]), self.root)
                return self._servers[index]
        return None

    def _resolve(self, path: str) -> str:
        target = os.path.realpath(os.path.join(self.root, path))
        if target != self.root and not target.startswith(self.root + os.sep):
            raise ValueError(f"작업 디렉터리 밖입니다: {path}")
        return target

    def _query(self, method: str, args: dict) -> dict:
        path = args["path"]
        line = int(args["line"])
        column = int(args.get("column", 1))
        try:
            target = self._resolve(path)
        except ValueError as exc:
            return {"error": str(exc)}
        if not os.path.isfile(target):
            return {"error": f"파일이 없습니다: {path}"}
        server = self._server_for(target)
        if server is None:
            return {"error": f"이 확장자를 지원하는 LSP 서버가 없습니다: {path}"}
        try:
            spots = server.locate(method, target, line, column)
        except (ServerError, OSError, json.JSONDecodeError) as exc:
            server.stop()
            return {"error": f"LSP 조회 실패: {exc}"}
        return {"locations": spots[:MAX_LOCATIONS], "count": len(spots),
                "truncated": len(spots) > MAX_LOCATIONS}

    def definition(self, args: dict) -> dict:
        return self._query("textDocument/definition", args)

    def references(self, args: dict) -> dict:
        return self._query("textDocument/references", args)

    def close(self) -> None:
        for server in self._servers.values():
            server.stop()
        self._servers.clear()


_POSITION_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string",
                            "description": "작업 디렉터리 기준 상대 경로"},
                   "line": {"type": "integer",
                            "description": "1부터 시작하는 줄 번호"},
                   "column": {"type": "integer",
                              "description": "1부터 시작하는 열 번호. "
                                             "심벌 이름 위의 위치"}},
    "required": ["path", "line"],
}


def tools(pool: Pool) -> list[Tool]:
    """Build definition/reference tools backed by one server pool."""
    return [
        Tool("definition",
             "지정한 위치의 심벌이 정의된 파일과 줄을 LSP로 찾는다. "
             "import를 따라가거나 호출 대상의 실제 구현을 열 때 쓴다.",
             _POSITION_SCHEMA, pool.definition),
        Tool("references",
             "지정한 위치의 심벌을 사용하는 모든 곳을 LSP로 찾는다. "
             "수정 전 영향 범위를 확인할 때 쓴다.",
             _POSITION_SCHEMA, pool.references),
    ]
