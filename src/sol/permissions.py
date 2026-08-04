"""Tool permission presets.

Provide four explicit modes and fix the selected permissions before execution.
The tool set is part of the cache prefix, so changing modes mid-run would change
the session contract.

read-only  expose read tools only
ask        expose writes and shell with approval for each call
auto-edit  auto-approve file and state changes, ask for shell
yolo       expose writes and shell without tool approval

yolo does not remove workspace boundaries, read-before-edit checks, checkpoints,
or dangerous-command blocking. Shell commands can still affect operating-system
state, so the TUI keeps this mode visible.
"""

from dataclasses import dataclass


MODES = ("read-only", "ask", "auto-edit", "yolo")
ALLOW = "allow"
ASK = "ask"


@dataclass(frozen=True)
class PermissionPolicy:
    mode: str
    allow_write: bool
    allow_shell: bool
    auto_edit: bool = False
    auto_shell: bool = False
    explicit_yolo: bool = False

    def decision(self, tool_name: str) -> str:
        """Return the approval decision for an exposed non-read-only tool.

        Auto-edit asks only for shell and approves other exposed mutation tools.
        """
        if tool_name == "shell":
            return ALLOW if self.auto_shell else ASK
        return ALLOW if self.auto_edit else ASK

    @property
    def label(self) -> str:
        if self.explicit_yolo:
            return "YOLO (write + shell auto-approved)"
        if self.mode == "auto-edit":
            return "Auto Edit (shell asks)"
        if self.mode == "ask":
            scopes = []
            if self.allow_write:
                scopes.append("write")
            if self.allow_shell:
                scopes.append("shell")
            return "Ask (" + " + ".join(scopes) + ")"
        if self.mode == "legacy-auto":
            scopes = []
            if self.allow_write:
                scopes.append("write")
            if self.allow_shell:
                scopes.append("shell")
            return "Auto (" + " + ".join(scopes) + ")"
        return "Read Only"

    @property
    def auto_all(self) -> bool:
        return self.auto_edit and (self.auto_shell or not self.allow_shell)


def resolve(mode: str | None = None, *, write: bool = False,
            shell: bool = False, yes: bool = False) -> PermissionPolicy:
    """Normalize a preset or existing flags into one permission policy.

    Reject conflicting inputs instead of silently preferring one source of
    authority over another.
    """
    if mode is not None and mode not in MODES:
        raise ValueError(f"알 수 없는 권한 모드: {mode}")
    if mode is not None and (write or shell or yes):
        raise ValueError("--permission은 --write/--shell/--yes와 함께 쓸 수 없습니다")

    if mode == "read-only":
        return PermissionPolicy("read-only", False, False)
    if mode == "ask":
        return PermissionPolicy("ask", True, True)
    if mode == "auto-edit":
        return PermissionPolicy("auto-edit", True, True, auto_edit=True)
    if mode == "yolo":
        return PermissionPolicy("yolo", True, True, auto_edit=True,
                                auto_shell=True, explicit_yolo=True)

    if not write and not shell:
        return PermissionPolicy("read-only", False, False)
    if yes:
        return PermissionPolicy("legacy-auto", write, shell,
                                auto_edit=True, auto_shell=shell)
    return PermissionPolicy("ask", write, shell)


def mcp_server_prefix(tool_name: str) -> str | None:
    """Return mcp__{server}__ prefix for MCP tools, else None."""
    if not tool_name.startswith("mcp__"):
        return None
    parts = tool_name.split("__", 2)
    if len(parts) < 3 or not parts[1]:
        return None
    return f"mcp__{parts[1]}__"


def allows_prefix(tool_name: str, approved_prefixes: set[str] | list[str]) -> bool:
    """True when tool_name matches a server-level MCP approval prefix."""
    prefix = mcp_server_prefix(tool_name)
    if prefix is None:
        return False
    return prefix in set(approved_prefixes)
