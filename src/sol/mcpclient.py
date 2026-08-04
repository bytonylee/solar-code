"""MCP client with a fixed tool schema and restorable large results.

Design contracts (PLAN-context-capability.md + PLAN-context-management.md):

- stdio transport only for the first cut
- tools registered once at startup; never added/removed mid-episode
- dead servers keep their schema; calls return an error envelope
- tool names are mcp__{server}__{tool}
- results larger than SHELL_OUTPUT_MAX are externalized under .sol/notes/
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .subagent import write_notes
from .tools import Registry, Tool
from .workspace import SHELL_OUTPUT_MAX

DEFAULT_CONFIG = ".sol/mcp.json"
INIT_TIMEOUT = 10.0
CALL_TIMEOUT = 60.0


@dataclass
class ServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env_keys: list[str] = field(default_factory=list)
    read_only_tools: list[str] = field(default_factory=list)
    include_tools: list[str] | None = None
    enabled: bool = True


@dataclass
class McpServer:
    config: ServerConfig
    process: subprocess.Popen | None = None
    tools: list[dict] = field(default_factory=list)
    alive: bool = False
    restart_attempted: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    _next_id: int = 1
    _stderr: str = ""

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Only pass declared env keys; never put secret values into config.
        for key in self.config.env_keys:
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.alive = True
            return
        try:
            self.process = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self._env(),
            )
            self.alive = True
        except OSError as exc:
            self.alive = False
            self._stderr = str(exc)
            self.process = None

    def stop(self) -> None:
        proc = self.process
        self.process = None
        self.alive = False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except OSError:
            pass

    def _rpc(self, method: str, params: dict | None = None,
             timeout: float = CALL_TIMEOUT) -> dict:
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError(f"mcp server not running: {self.config.name}")
            assert self.process.stdin is not None
            assert self.process.stdout is not None
            req_id = self._next_id
            self._next_id += 1
            payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                payload["params"] = params
            line = json.dumps(payload, ensure_ascii=False)
            self.process.stdin.write(line + "\n")
            self.process.stdin.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                remaining = max(0.01, deadline - time.time())
                # readline blocks; rely on process death or timeout via polling.
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"mcp server exited: {self.config.name}")
                # Use a short select-like approach with a thread for timeout.
                result_box: dict[str, Any] = {}

                def reader() -> None:
                    try:
                        result_box["line"] = self.process.stdout.readline()
                    except Exception as exc:  # noqa: BLE001
                        result_box["error"] = str(exc)

                thread = threading.Thread(target=reader, daemon=True)
                thread.start()
                thread.join(remaining)
                if thread.is_alive():
                    raise TimeoutError(
                        f"mcp call timed out after {timeout:.0f}s: {method}")
                if "error" in result_box:
                    raise RuntimeError(result_box["error"])
                raw = result_box.get("line") or ""
                if not raw:
                    if self.process.poll() is not None:
                        raise RuntimeError(
                            f"mcp server exited: {self.config.name}")
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if message.get("id") != req_id:
                    # Skip unrelated notifications / other responses.
                    continue
                if "error" in message:
                    err = message["error"]
                    if isinstance(err, dict):
                        raise RuntimeError(err.get("message") or str(err))
                    raise RuntimeError(str(err))
                return message.get("result") or {}
            raise TimeoutError(
                f"mcp call timed out after {timeout:.0f}s: {method}")

    def initialize(self) -> None:
        self.start()
        if not self.alive:
            raise RuntimeError(
                f"failed to start mcp server {self.config.name}: {self._stderr}")
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "sol", "version": "0"},
        }, timeout=INIT_TIMEOUT)
        # Notifications have no id; write directly when a real stdio process exists.
        with self.lock:
            proc = self.process
            stdin = getattr(proc, "stdin", None) if proc is not None else None
            if stdin is not None:
                note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
                stdin.write(json.dumps(note) + "\n")
                stdin.flush()
        listed = self._rpc("tools/list", {}, timeout=INIT_TIMEOUT)
        tools = listed.get("tools") or []
        if self.config.include_tools is not None:
            allowed = set(self.config.include_tools)
            tools = [t for t in tools if t.get("name") in allowed]
        # Normalize descriptions so reconnects do not jitter the prefix.
        normalized = []
        for tool in tools:
            item = dict(tool)
            desc = " ".join(str(item.get("description") or "").split())
            item["description"] = desc
            normalized.append(item)
        # Sort by name for deterministic registration.
        self.tools = sorted(normalized, key=lambda t: t.get("name") or "")

    def _process_dead(self) -> bool:
        proc = self.process
        if proc is None:
            return True
        poll = getattr(proc, "poll", None)
        if callable(poll):
            try:
                return poll() is not None
            except Exception:
                return True
        # In-memory/test doubles have no poll(); trust self.alive.
        return not self.alive

    def call(self, tool_name: str, arguments: dict) -> object:
        try:
            if not self.alive or self._process_dead():
                if not self.restart_attempted:
                    self.restart_attempted = True
                    self.start()
                    if self.alive and not self._process_dead():
                        # Re-init is best-effort; schema stays from first list.
                        try:
                            self._rpc("initialize", {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "sol", "version": "0"},
                            }, timeout=INIT_TIMEOUT)
                        except Exception:
                            pass
                if not self.alive or self._process_dead():
                    self.alive = False
                    return {"error": f"mcp server unavailable: {self.config.name}",
                            "server": self.config.name}
            result = self._rpc("tools/call", {
                "name": tool_name,
                "arguments": arguments or {},
            })
            return result
        except Exception as exc:  # noqa: BLE001 - isolate server failures
            return {"error": str(exc), "server": self.config.name}


class McpManager:
    """Load MCP servers once and expose them as Registry tools."""

    def __init__(self) -> None:
        self.servers: dict[str, McpServer] = {}
        self.warnings: list[str] = []

    def load_config(self, root: str, path: str | None = None) -> dict[str, ServerConfig]:
        config_path = path or os.path.join(root, DEFAULT_CONFIG)
        if not os.path.isfile(config_path):
            return {}
        try:
            with open(config_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            self.warnings.append(f"mcp config unreadable: {exc}")
            return {}
        servers = data.get("servers") or {}
        out: dict[str, ServerConfig] = {}
        for name in sorted(servers):
            raw = servers[name] or {}
            if not raw.get("enabled", True):
                continue
            command = raw.get("command")
            if not command:
                self.warnings.append(f"mcp server {name} missing command")
                continue
            out[name] = ServerConfig(
                name=name,
                command=command,
                args=list(raw.get("args") or []),
                env_keys=list(raw.get("env_keys") or []),
                read_only_tools=list(raw.get("read_only_tools") or []),
                include_tools=(list(raw["include_tools"])
                               if "include_tools" in raw else None),
                enabled=True,
            )
        return out

    def start(self, root: str, path: str | None = None) -> list[Tool]:
        """Start configured servers and return tools for one-time registration."""
        tools: list[Tool] = []
        for name, config in self.load_config(root, path).items():
            server = McpServer(config=config)
            try:
                server.initialize()
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"mcp server {name} failed: {exc}")
                server.stop()
                continue
            self.servers[name] = server
            for tool in server.tools:
                tools.append(self._to_tool(server, tool))
        return tools

    def _to_tool(self, server: McpServer, tool: dict) -> Tool:
        server_name = server.config.name
        tool_name = tool.get("name") or "tool"
        full_name = f"mcp__{server_name}__{tool_name}"
        description = tool.get("description") or f"MCP tool {tool_name}"
        parameters = tool.get("inputSchema") or {
            "type": "object", "properties": {}}
        read_only = tool_name in set(server.config.read_only_tools)

        def run(args: dict, _server=server, _tool=tool_name,
                _server_name=server_name) -> object:
            result = _server.call(_tool, args or {})
            if isinstance(result, dict) and result.get("error"):
                # Ensure StallDetector sees failures via the error key.
                return {"error": result.get("error"),
                        "server": result.get("server", _server_name)}
            text = _result_text(result)
            if len(text) > SHELL_OUTPUT_MAX:
                notes_path = write_notes(
                    text, role=_tool, root=None, prefix=f"mcp-{_server_name}",
                    suffix=".json")
                truncated = text[:SHELL_OUTPUT_MAX]
                return {
                    "content": truncated,
                    "truncated": True,
                    "notes_path": notes_path,
                    "server": _server_name,
                    "message": (
                        f"결과가 {SHELL_OUTPUT_MAX:,}자를 넘어 "
                        f"{notes_path}에 저장되었습니다. 필요하면 read_file로 조회하십시오."
                    ),
                }
            # Prefer structured content when small enough.
            if isinstance(result, (dict, list)):
                return result
            return {"content": text, "server": _server_name}

        return Tool(name=full_name, description=description,
                    parameters=parameters, run=run, read_only=read_only)

    def close(self) -> None:
        for server in self.servers.values():
            server.stop()
        self.servers.clear()


def _result_text(result: object) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        # MCP tools/call often returns {content:[{type:text,text:...}]}
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            if parts:
                return "\n".join(parts)
        return json.dumps(result, ensure_ascii=False)
    if isinstance(result, list):
        return json.dumps(result, ensure_ascii=False)
    return str(result)


def register_mcp_tools(registry: Registry, root: str,
                       path: str | None = None,
                       enabled: bool = True) -> McpManager | None:
    """Start MCP servers and register tools once. Return manager for cleanup."""
    if not enabled:
        return None
    manager = McpManager()
    for tool in manager.start(root, path):
        if tool.name in registry:
            manager.warnings.append(f"skip duplicate tool name: {tool.name}")
            continue
        registry.add(tool)
    if not manager.servers and not manager.warnings:
        return None
    return manager
