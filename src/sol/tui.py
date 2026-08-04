"""Full-screen terminal interface for sol.

The interface uses ANSI, termios, and select from the standard library. It
separates title, conversation, input, command parsing, and frame rendering from
terminal I/O so the pure display logic can run without a live terminal.
"""

from __future__ import annotations

import json
import os
import queue
import re
import select
import shutil
import sys
import threading
import unicodedata
from dataclasses import dataclass

from . import credentials, model, status
from .loop import Agent, Episode, Event
from .progress import (LAVENDER, SPINNER,
                       SPINNER_INTERVAL as SPINNER_INTERVAL_S,
                       format_mode_status, format_tool_result)
from .permissions import ALLOW, PermissionPolicy, resolve as resolve_permissions

# ---------------------------------------------------------------------------
# Colors. Use the 256-color palette for broad terminal compatibility.

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
# Braille spinner dots sit high in the cell in common terminal fonts.  SGR
# 74/75 adjusts only the spinner's baseline and then restores normal text;
# terminals without subscript support simply ignore the attributes.
SPINNER_BASELINE_DOWN = "\x1b[74m"
SPINNER_BASELINE_NORMAL = "\x1b[75m"


def fg(n: int) -> str:
    return f"\x1b[38;5;{n}m"


def bg(n: int) -> str:
    return f"\x1b[48;5;{n}m"


# Brand colors for the terminal interface.
BRAND_KEY = 63       # Primary key color.
BRAND_KEY_SOFT = 147  # Lighter key color for dark backgrounds.
BRAND_LAVENDER = 189  # Lavender accent.
BRAND_GREEN = 120    # Green accent.
# Status colors use the same palette with semantic meanings.
LAVENDER_GREEN = 157  # Success.
LAVENDER_RED = 175    # Failure and automatic mode.

EDGE = BRAND_KEY     # Box border.
ACCENT = BRAND_GREEN  # Spinner and streaming cursor.
GRAY = 243           # Dim body text.
WHITE = 15           # Bright white text.
RED = LAVENDER_RED   # Errors.
OK_GREEN = LAVENDER_GREEN  # Successful tools.
USER = BRAND_LAVENDER   # User input.
WARN = 227           # Approval prompt.
PROMPT_MARK = "❯"    # Vertically centered prompt marker.

# Title gradient from lavender to the primary key color.
LOGO_GRADIENT = (BRAND_LAVENDER, 147, 141, 105, 99, BRAND_KEY)

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def plain(s: str) -> str:
    """Return a string with ANSI sequences removed."""
    return ANSI_RE.sub("", s)


def disp_width(s: str) -> int:
    """Return display width, counting wide characters as two cells."""
    width = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def truncate_ansi(s: str, width: int) -> str:
    """Truncate to display width while preserving ANSI sequences."""
    out, used, i = [], 0, 0
    for m in ANSI_RE.finditer(s):
        for ch in s[i:m.start()]:
            w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if used + w > width:
                return "".join(out) + RESET
            out.append(ch)
            used += w
        out.append(m.group(0))
        i = m.end()
    for ch in s[i:]:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width:
            return "".join(out) + RESET
        out.append(ch)
        used += w
    return "".join(out)


def pad_ansi(s: str, width: int) -> str:
    """Pad the right side to exactly ``width`` display cells."""
    gap = width - disp_width(plain(s))
    return s + " " * max(0, gap)


def wrap_text(text: str, width: int) -> list[str]:
    """Wrap text by display width, preserving explicit line breaks."""
    lines: list[str] = []
    for raw in text.split("\n"):
        current, used = "", 0
        for ch in raw:
            w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if used + w > width and current:
                lines.append(current)
                current, used = "", 0
            current += ch
            used += w
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Embedded title art. Apply the row gradient at render time.

TITLE_ART = (
    " ███████╗  ██████╗  ██╗       █████╗  ██████╗ ",
    " ██╔════╝ ██╔═══██╗ ██║      ██╔══██╗ ██╔══██╗",
    " ███████╗ ██║   ██║ ██║      ███████║ ██████╔╝",
    " ╚════██║ ██║   ██║ ██║      ██╔══██║ ██╔══██╗",
    " ███████║ ╚██████╔╝ ███████╗ ██║  ██║ ██║  ██║",
    " ╚══════╝  ╚═════╝  ╚══════╝ ╚═╝  ╚═╝ ╚═╝  ╚═╝",
)

LOGO_WORD = "SOLAR"


def title_lines() -> list[str]:
    """Return title-art rows with the configured gradient."""
    rows = [line.rstrip() for line in TITLE_ART]
    width = max(disp_width(line) for line in rows)
    return [fg(LOGO_GRADIENT[i % len(LOGO_GRADIENT)]) + BOLD
            + line + " " * (width - disp_width(line)) + RESET
            for i, line in enumerate(rows)]


def _display_root(root: str, width: int) -> str:
    """Shorten the home directory and keep the tail of a long CWD."""
    home = os.path.expanduser("~")
    shown = "~" + root[len(home):] if root.startswith(home) else root
    if disp_width(shown) <= width:
        return shown
    budget = max(1, width - 1)
    tail = shown
    while tail and disp_width(tail) > budget:
        tail = tail[1:]
    return "…" + tail


def _permission_short(permission: PermissionPolicy) -> str:
    """Return the short permission label used in the prompt divider."""
    labels = {
        "read-only": "read-only",
        "ask": "ask",
        "auto-edit": "auto-edit",
        "legacy-auto": "auto",
        "yolo": "YOLO",
    }
    return labels.get(permission.mode, permission.mode)


def _welcome_field(label: str, value: str, width: int) -> str:
    """Fit one label-value row to the requested welcome-panel width."""
    label_w = min(11, max(7, width // 3))
    prefix = DIM + label + RESET
    gap = max(1, label_w - disp_width(label))
    value_w = max(1, width - label_w)
    value = truncate_ansi(fg(255) + value + RESET, value_w)
    return truncate_ansi(prefix + " " * gap + value, width)


def _welcome_box(rows: list[str], width: int) -> list[str]:
    """Wrap welcome content in a padded box sized to the screen width."""
    panel_w = max(18, width - 4)
    panel_w = min(panel_w, width)
    inner = panel_w - 2
    margin = max(0, (width - panel_w) // 2)
    indent = " " * margin
    edge = fg(EDGE)
    return [
        indent + edge + "╭" + "─" * inner + "╮" + RESET,
        *(indent + edge + "│" + RESET + pad_ansi(truncate_ansi(row, inner), inner)
          + edge + "│" + RESET for row in rows),
        indent + edge + "╰" + "─" * inner + "╯" + RESET,
    ]


def banner_lines(width: int, effort: str, root: str,
                 permission: PermissionPolicy | None = None) -> list[str]:
    """Return rows for the Solar welcome panel.

    Runtime mode status belongs in the footer, not in the welcome panel.
    """
    from . import __version__

    permission = permission or resolve_permissions()
    panel_w = max(18, min(width, width - 4))
    inner = panel_w - 2
    content_w = max(1, inner - 4)
    title = title_lines()
    title_w = max(disp_width(plain(line)) for line in title)

    if content_w >= title_w + 4 + 34:
        right_w = content_w - title_w - 4
        right = [
            fg(BRAND_LAVENDER) + BOLD + "Solar" + RESET
            + DIM + f"  v{__version__}" + RESET,
            "",
            _welcome_field("Model", model.MODEL, right_w),
            "",
            fg(BRAND_LAVENDER) + BOLD + "/help" + RESET + DIM + "      명령 목록" + RESET,
            fg(BRAND_LAVENDER) + BOLD + "/model" + RESET + DIM + "     모델 설정" + RESET,
            fg(BRAND_LAVENDER) + BOLD + "/status" + RESET + DIM + "    세션 상태" + RESET,
            fg(BRAND_LAVENDER) + BOLD + "/think on" + RESET + DIM + "  추론 켜기" + RESET,
        ]
        row_count = max(len(right), len(title) + 4)
        logo_top = (row_count - len(title)) // 2
        rows = []
        for index in range(row_count):
            logo = title[index - logo_top] if logo_top <= index < logo_top + len(title) else ""
            info = right[index] if index < len(right) else ""
            rows.append("  " + pad_ansi(logo, title_w) + " " * 4
                        + pad_ansi(truncate_ansi(info, right_w), right_w) + "  ")
        return _welcome_box(rows, width)

    brand = (fg(BRAND_LAVENDER) + BOLD + LOGO_WORD + RESET
             + DIM + f"  v{__version__}" + RESET)
    if content_w >= 38:
        rows = [
            "  " + brand,
            "  " + DIM + "solar coding agent" + RESET,
            "",
            "  " + _welcome_field("Model", model.MODEL, content_w - 2),
            "",
            "  " + fg(BRAND_LAVENDER) + "/help" + RESET + DIM + " 명령"
            + RESET + "  " + fg(BRAND_LAVENDER) + "/model" + RESET + DIM + " 설정" + RESET,
            "  " + fg(BRAND_LAVENDER) + "/status" + RESET + DIM + " 상태" + RESET
            + "  " + fg(BRAND_LAVENDER) + "/think on" + RESET + DIM + " 추론" + RESET,
        ]
    else:
        rows = [
            truncate_ansi(" " + brand, inner),
            truncate_ansi(" " + DIM + model.MODEL + RESET, inner),
            truncate_ansi(" " + fg(BRAND_LAVENDER) + "/help" + RESET, inner),
        ]
    return _welcome_box(rows, width)


# ---------------------------------------------------------------------------
# Conversation model. Convert loop events into display items.

@dataclass
class Item:
    role: str   # user | input-status | sol | tool | result | info | error.
    text: str


class Conversation:
    """Collection of display items assembled from loop events."""

    def __init__(self) -> None:
        self.items: list[Item] = []
        self._open_tool: int | None = None
        self._open_answer: int | None = None
        self._thinking: int | None = None
        self._thinking_chars = 0
        self._tool_output: int | None = None

    def add(self, role: str, text: str) -> None:
        self.items.append(Item(role, text))

    def clear(self) -> None:
        self.items.clear()
        self._open_tool = None
        self._open_answer = None
        self._thinking = None
        self._thinking_chars = 0
        self._tool_output = None

    def add_event(self, event: Event) -> None:
        kind, data = event.kind, event.data
        if kind == "run_start":
            self._open_answer = None
            self._thinking = None
            self._thinking_chars = 0
        elif kind == "pass_start":
            pass
        elif kind == "goal_unmet":
            pass
        elif kind == "goal_complete":
            pass
        elif kind == "goal_limit":
            unmet = ", ".join(data.get("unmet") or [])
            self.add("error", f"회차 상한 도달, 미충족 기준 남음: {unmet}")
        elif kind == "turn_start":
            self._open_answer = None
            self._thinking = None
            self._thinking_chars = 0
        elif kind == "answer_delta":
            if data.get("reasoning"):
                pass
            else:
                if self._open_answer is None:
                    self.add("sol", "")
                    self._open_answer = len(self.items) - 1
                self.items[self._open_answer].text += data.get("text", "")
        elif kind == "answer":
            content = data.get("content", "")
            if self._open_answer is not None:
                # Replace the streamed item with the final content.
                self.items[self._open_answer] = Item("sol", content)
                self._open_answer = None
            else:
                self.add("sol", content)
            self._thinking = None
        elif kind == "tool_start":
            self._open_tool = None
            self._tool_output = None
        elif kind == "tool_output":
            text = data.get("text", "")
            if self._tool_output is None:
                self.add("result", text)
                self._tool_output = len(self.items) - 1
            else:
                self.items[self._tool_output].text += text
        elif kind == "tool_end":
            if not data.get("ok") and data.get("error"):
                self.add("error", str(data["error"]))
            if not data.get("streamed"):
                rendered = format_tool_result(data.get("result", ""))
                if rendered:
                    self.add("result", rendered)
            self._tool_output = None
        elif kind == "reasoning":
            pass
        elif kind == "compaction":
            pass
        elif kind == "starved_retry":
            pass
        elif kind == "starved":
            self.add("error", "추론이 출력 예산을 모두 소진해 답을 내지 못했다")
        elif kind == "nudge":
            pass
        elif kind == "retry":
            pass
        elif kind == "rounds_exhausted":
            self.add("error", data.get("reason", "라운드 상한에 도달했다"))
        elif kind == "stalled":
            self.add("error", "진행 정체로 중단: " + data.get("reason", ""))
        elif kind == "gate_exhausted":
            self.add("error", "완료 게이트 소진: " + data.get("reason", ""))
        elif kind == "empty_answer":
            self.add("error", "빈 최종 응답을 감지했다")
        elif kind == "wrap_up_start":
            pass
        elif kind == "wrap_up_failed":
            self.add("error", data.get("reason", "최종 정리 실패"))
        elif kind == "wrap_up_end":
            pass
        elif kind == "run_error":
            self.add("error", data.get("reason", "모델 요청 실패"))
        elif kind == "blocked":
            self.add("error", data.get("reason", "차단됨"))
        elif kind == "run_end":
            pass


def render_item(item: Item, width: int) -> list[str]:
    """Render one conversation item as screen rows."""
    body_w = max(10, width - 4)
    if item.role == "user":
        lines = wrap_text(item.text, body_w)
        return [fg(USER) + BOLD + "> " + line + RESET for line in lines]
    if item.role == "input-status":
        return [DIM + "  • " + line + RESET
                for line in wrap_text(item.text, body_w)]
    if item.role == "tool":
        color = OK_GREEN if item.text.startswith("✓") else (
            RED if item.text.startswith("✗") else GRAY)
        return [fg(color) + "  " + line + RESET
                for line in wrap_text(item.text, body_w)]
    if item.role == "result":
        return [fg(245) + "    " + line + RESET
                for line in wrap_text(item.text, body_w)]
    if item.role == "info":
        if item.text.startswith("추론 중"):
            return [fg(WHITE) + "  · " + line + RESET
                    for line in wrap_text(item.text, body_w)]
        return [DIM + "  · " + line + RESET
                for line in wrap_text(item.text, body_w)]
    if item.role == "error":
        return [fg(RED) + "  ! " + line + RESET
                for line in wrap_text(item.text, body_w)]
    return [""] + wrap_text(item.text, body_w) + [""]


def input_status_items(effort: str, yolo: bool, model_name: str) -> list[Item]:
    """Snapshot the runtime settings shown below a submitted input."""
    return [
        Item("input-status", f"think: {effort}"),
        Item("input-status", f"yolo: {'on' if yolo else 'off'}"),
        Item("input-status", f"model: {model_name}"),
    ]


# ---------------------------------------------------------------------------
# Input line. This pure model handles only the buffer, cursor, and history.

class InputLine:
    """Editable single-line input with grouped undo history."""

    def __init__(self) -> None:
        self.buf: list[str] = []
        self.pos = 0
        self.history: list[str] = []
        self._hidx: int | None = None
        self._stash = ""
        self._undos: list[tuple[list[str], int]] = []
        self._edit_kind: str | None = None

    def _remember(self, kind: str) -> None:
        if self._edit_kind != kind:
            self._undos.append((self.buf.copy(), self.pos))
            if len(self._undos) > 100:
                del self._undos[0]
        self._edit_kind = kind

    def _break_edit(self) -> None:
        self._edit_kind = None

    def text(self) -> str:
        return "".join(self.buf)

    def insert(self, s: str) -> None:
        if not s:
            return
        self._remember("insert")
        for ch in s:
            self.buf.insert(self.pos, ch)
            self.pos += 1
        self._hidx = None

    def backspace(self) -> None:
        if self.pos > 0:
            self._remember("backspace")
            del self.buf[self.pos - 1]
            self.pos -= 1

    def delete(self) -> None:
        if self.pos < len(self.buf):
            self._remember("delete")
            del self.buf[self.pos]

    def left(self) -> None:
        self._break_edit()
        self.pos = max(0, self.pos - 1)

    def right(self) -> None:
        self._break_edit()
        self.pos = min(len(self.buf), self.pos + 1)

    def home(self) -> None:
        self._break_edit()
        self.pos = 0

    def end(self) -> None:
        self._break_edit()
        self.pos = len(self.buf)

    def clear(self) -> None:
        self.buf, self.pos = [], 0
        self._hidx = None
        self._undos.clear()
        self._break_edit()

    def replace(self, text: str) -> None:
        """Apply a whole-buffer replacement as one undoable edit."""
        if text == self.text():
            return
        self._break_edit()
        self._remember("replace")
        self.buf = list(text)
        self.pos = len(self.buf)
        self._hidx = None

    def undo(self) -> bool:
        """Undo the last input edit and return whether anything was restored."""
        if not self._undos:
            return False
        self.buf, self.pos = self._undos.pop()
        self._hidx = None
        self._break_edit()
        return True

    def history_prev(self) -> None:
        if not self.history:
            return
        self._remember("history")
        if self._hidx is None:
            self._stash = self.text()
            self._hidx = len(self.history) - 1
        elif self._hidx > 0:
            self._hidx -= 1
        self.buf = list(self.history[self._hidx])
        self.pos = len(self.buf)

    def history_next(self) -> None:
        if self._hidx is None:
            return
        self._remember("history")
        if self._hidx < len(self.history) - 1:
            self._hidx += 1
            self.buf = list(self.history[self._hidx])
        else:
            self._hidx = None
            self.buf = list(self._stash)
        self.pos = len(self.buf)

    def take(self) -> str:
        """Return the submitted text, add it to history, and clear the buffer."""
        text = self.text()
        if text.strip():
            self.history.append(text)
        self.clear()
        return text

    def view(self, width: int) -> tuple[str, int]:
        """Return visible text and cursor cell position, trimming around the cursor."""
        text = self.text()
        before = text[:self.pos]
        if disp_width(text) <= width:
            return text, disp_width(before)
        # Trim from the left so the cursor remains visible.
        visible, used = "", 0
        for ch in reversed(before):
            w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if used + w > width:
                break
            visible = ch + visible
            used += w
        return visible, disp_width(visible)


def parse_command(text: str) -> tuple[str, str] | None:
    """Parse ``/think on`` into ``('think', 'on')`` or return ``None``."""
    if not text.startswith("/"):
        return None
    parts = text[1:].split(None, 1)
    name = parts[0].lower() if parts and parts[0] else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    return (name, arg) if name else None


# ---------------------------------------------------------------------------
# Application state and frame rendering.

def platform_hints(platform: str | None = None) -> str:
    """Return the minimal footer hint needed to start a task."""
    return " [enter] 전송 · / 명령 · shift+tab 권한 변경 · Ctrl+D 종료 "


HINTS = platform_hints()
COMPLETION_HINTS = " tab 완성 · enter 실행 · ↑↓ 선택 · esc 닫기 "


@dataclass(frozen=True)
class Command:
    """Slash command and its optional completion arguments."""

    name: str
    summary: str
    args: tuple = ()


COMMANDS = (
    Command("help", "이 도움말"),
    Command("think", "추론 켜기/끄기 (on|off)", ("on", "off")),
    Command("yolo", "자동 승인 켜기/끄기 (on|off)", ("on", "off")),
    Command("plan", "완성 기준 모드 켜기/끄기 (on|off)", ("on", "off")),
    Command("goal", "선언된 완성 기준 보기"),
    Command("model", "모델 설정과 측정된 계약 보기"),
    Command("api-key", "전역 $UPSTAGE_API_KEY 보안 입력"),
    Command("tinyfish-key", "전역 $TINYFISH_API_KEY 보안 입력"),
    Command("status", "세션 사용량과 검증 근거 요약"),
    Command("undo", "마지막 파일 수정 되돌리기"),
    Command("clear", "화면 정리"),
    Command("quit", "종료"),
)

HELP_TEXT = ("명령:\n"
             + "\n".join(f"  /{c.name:<14} {c.summary}" for c in COMMANDS)
             + "\n그 외 입력은 작업 지시로 실행된다.")


def model_settings_text(effort: str) -> str:
    """Render the current model contract as a read-only settings panel."""
    snapshot = model.settings(effort)
    if snapshot.contract_verified:
        intro = "측정된 solar-open2 계약을 현재 요청에 적용합니다."
    elif snapshot.supported:
        intro = (f"{snapshot.model} 지원 모델을 사용합니다. "
                 "계약 수치는 solar-open2 기준이며 별도 실측이 필요합니다.")
    else:
        intro = (f"{snapshot.model}는 지원 목록 밖의 외부 모델입니다. "
                 "아래 값은 검증되지 않은 기준값입니다.")
    rows = ["모델 설정", intro, ""]
    label_width = max(disp_width(label) for label, _ in snapshot.rows())
    for label, value in snapshot.rows():
        rows.append(f"{label:<{label_width}}  {value}")
    rows.extend([
        "",
        "지원 모델  " + " · ".join(model.SUPPORTED_MODELS),
        "런타임 변경  /think on|off",
        "고정 값      모델 · 출력 상한 · 샘플링 파라미터",
        "API 키       화면에 표시하지 않음",
    ])
    return "\n".join(rows)

# Progress phase shown in the conversation body while the agent is busy.
PHASE_CONNECT = "연결 중"
PHASE_THINKING = "추론 중"
PHASE_WRITING = "응답 작성 중"
PHASE_WORKING = "작업 중"
SCROLL_STEP = 5


class App:
    """Logical TUI state independent of terminal I/O."""

    def __init__(self, root: str, effort: str = model.Effort.OFF,
                 agent: Agent | None = None,
                 permission: PermissionPolicy | None = None) -> None:
        self.root = root
        self.effort = effort
        self.agent = agent
        self.permission = permission or resolve_permissions()
        self._permission_before_yolo = (
            resolve_permissions("ask") if self.permission.explicit_yolo
            else self.permission)
        self.conv = Conversation()
        self.input = InputLine()
        self.usage = status.Usage()
        self.queue: queue.Queue = queue.Queue()
        self.busy = False
        self.round = 0
        self.tick = 0
        self.phase = PHASE_WORKING
        self.scroll = 0        # Number of lines scrolled up from the bottom.
        self.quit = False
        self.exit_code = 0
        self.approval: tuple[str, dict, threading.Event] | None = None
        self.completion_index = 0
        self.completion_dismissed = False
        self.secret_name: str | None = None
        self._secret_buffer: list[str] = []
        # run()이 주입하는 계획 모드 전환 콜백. None이면 전환 경로가 없다.
        self.set_plan = None

    @property
    def plan_active(self) -> bool:
        """계획 모드 여부는 에이전트의 goal_holder 유무로 판정한다."""
        return (self.agent is not None
                and getattr(self.agent, "goal_holder", None) is not None)

    # -- Slash-command completion --------------------------------------

    def completions(self) -> tuple[str, list] | None:
        """Return command-name or argument completion candidates."""
        if self.completion_dismissed:
            return None
        text = self.input.text()
        if not text.startswith("/"):
            return None
        head = text[1:]
        if " " not in head:
            items = [c for c in COMMANDS if c.name.startswith(head)]
            if items and not (len(items) == 1 and items[0].name == head):
                return ("name", items)
            return None
        name, _, arg = head.partition(" ")
        cmd = next((c for c in COMMANDS if c.name == name), None)
        if cmd is None or not cmd.args:
            return None
        items = [a for a in cmd.args if a.startswith(arg)]
        if items and not (len(items) == 1 and items[0] == arg):
            return ("arg", items)
        return None

    def _complete(self, execute: bool) -> None:
        """Fill the selected completion and optionally execute it."""
        comp = self.completions()
        if comp is None:
            if execute:
                self.submit()
            return
        kind, items = comp
        choice = items[self.completion_index % len(items)]
        if kind == "name":
            if execute and not choice.args:
                self.input.replace("/" + choice.name)
                self.submit()
            else:
                # Fill the command name and let the argument popup take over.
                self.input.replace("/" + choice.name + " ")
        else:
            name = self.input.text()[1:].partition(" ")[0]
            self.input.replace(f"/{name} {choice}")
            if execute:
                self.submit()
        self.completion_index = 0

    def _reset_completion(self) -> None:
        self.completion_index = 0
        self.completion_dismissed = False

    # -- Agent-thread connection ---------------------------------------

    def submit(self) -> None:
        """Process the input buffer without clearing it while busy."""
        self._reset_completion()
        text = self.input.text().strip()
        if not text:
            self.input.clear()
            return
        command = parse_command(text)
        if command is not None:
            self.input.take()
            self.run_command(*command)
            return
        if self.busy:
            self.conv.add("info", "작업이 끝난 뒤에 전송할 수 있다")
            return
        self.input.take()
        self.scroll = 0
        self.conv.add("user", text)
        self.conv.items.extend(input_status_items(
            self.effort, self.permission.explicit_yolo, model.MODEL))
        if self.agent is None:
            self.conv.add("error", "에이전트가 준비되지 않았다")
            return
        self.busy = True
        self.round = 0
        self.phase = PHASE_CONNECT
        worker = threading.Thread(target=_pump,
                                  args=(self.agent, text, self.queue, self.root),
                                  daemon=True)
        worker.start()

    def make_approve(self, permission: PermissionPolicy | None = None):
        """Return an approval callback that reads y/n input from the UI."""
        def approve(tool, tool_args) -> bool:
            active = permission or self.permission
            if active.decision(tool.name) == ALLOW:
                return True
            done = threading.Event()
            holder = {"ok": False}
            preview = json.dumps(tool_args, ensure_ascii=False)[:120]
            self.queue.put(("approve", (f"{tool.name} {preview}", holder, done)))
            done.wait()
            return holder["ok"]
        return approve

    def handle_message(self, kind: str, payload) -> None:
        if kind == "event":
            event = payload
            if event.kind == "turn_start":
                self.round = event.data.get("round", self.round)
                if self.phase != PHASE_WORKING:
                    self.phase = PHASE_WORKING
            elif event.kind == "answer_delta":
                self.phase = (PHASE_THINKING if event.data.get("reasoning")
                              else PHASE_WRITING)
            elif event.kind == "tool_start":
                self.phase = f"{event.data.get('tool', '')} 실행 중"
            elif event.kind == "tool_output":
                self.phase = f"{event.data.get('tool', '')} 출력 수신 중"
            elif event.kind == "tool_end":
                self.phase = PHASE_WORKING
            elif event.kind == "wrap_up_start":
                self.phase = "최종 정리 중"
            self.conv.add_event(event)
            # Zero follows the newest output; a positive value pins the view so
            # earlier content remains readable during streaming.
        elif kind == "done":
            episode = payload
            self.busy = False
            if isinstance(episode, Episode):
                self.usage.absorb(episode)
                if episode.incomplete:
                    self.exit_code = 1
        elif kind == "error":
            self.busy = False
            self.exit_code = 1
            self.conv.add("error", f"{type(payload).__name__}: {payload}")
        elif kind == "approve":
            self.approval = payload

    # -- Commands ------------------------------------------------------

    def run_command(self, name: str, arg: str) -> None:
        if name in ("quit", "exit", "q"):
            self.quit = True
        elif name == "help":
            self.conv.add("sol", HELP_TEXT)
        elif name == "clear":
            self.conv.clear()
            self.scroll = 0
        elif name in ("model", "settings"):
            self.conv.add("sol", model_settings_text(self.effort))
        elif name == "api-key":
            self._begin_secret_input("UPSTAGE_API_KEY", arg)
        elif name == "tinyfish-key":
            self._begin_secret_input("TINYFISH_API_KEY", arg)
        elif name == "think":
            if arg not in model.Effort.CHOICES:
                self.conv.add("error", "/think on 또는 /think off")
                return
            self.effort = arg
            if self.agent is not None:
                self.agent.effort = arg
        elif name == "yolo":
            if arg == "":
                arg = "off" if self.permission.explicit_yolo else "on"
            if arg not in ("on", "off"):
                self.conv.add("error", "/yolo on 또는 /yolo off")
                return
            if arg == "on":
                if not self.permission.explicit_yolo:
                    self._permission_before_yolo = self.permission
                self.permission = resolve_permissions("yolo")
            else:
                self.permission = self._permission_before_yolo
            if self.agent is not None:
                self.agent.permission_policy = self.permission
        elif name == "plan":
            if arg == "":
                arg = "off" if self.plan_active else "on"
            if arg not in ("on", "off"):
                self.conv.add("error", "/plan on 또는 /plan off")
                return
            want = arg == "on"
            if want == self.plan_active:
                self.conv.add("info", f"plan={arg} (이미 적용됨)")
                return
            if self.busy:
                self.conv.add("error", "작업 중에는 계획 모드를 바꿀 수 없다")
                return
            if self.set_plan is None:
                self.conv.add("error", "이 세션에서는 계획 모드를 전환할 수 없다")
                return
            try:
                self.set_plan(want)
            except Exception as exc:  # rebuild failure must not kill the UI.
                self.conv.add("error", f"계획 모드 전환 실패: {exc}")
                return
            self.conv.add("info", f"plan={arg}")
        elif name == "goal":
            if not self.plan_active:
                self.conv.add(
                    "error",
                    "계획 모드가 꺼져 있다. /plan on 후 작업을 시작하면 "
                    "모델이 set_goal로 완성 기준을 선언한다")
                return
            goal = (self.agent.goal_holder or {}).get("goal")
            if goal is None:
                self.conv.add("info", "아직 선언된 완성 기준이 없다")
            else:
                self.conv.add("sol", goal.render())
        elif name == "status":
            verification = getattr(self.agent, "verification", None)
            prefix = self.agent.prefix if self.agent is not None else None
            rendered = status.render(self.usage, prefix,
                                     verification=verification)
            session = getattr(self.agent, "session", None)
            saved = session is not None and getattr(session, "saved", True)
            session_line = f"\n세션  {session.id}" if saved else ""
            self.conv.add("sol", rendered + "\n권한  " + self.permission.label
                          + session_line)
        elif name == "undo":
            self._undo()
        else:
            self.conv.add("error", f"알 수 없는 명령: /{name} (/help 참고)")

    def _begin_secret_input(self, name: str, arg: str) -> None:
        if arg:
            command = "api-key" if name == "UPSTAGE_API_KEY" else "tinyfish-key"
            self.conv.add(
                "error", f"값을 명령에 쓰지 마세요. /{command}만 실행하세요")
            return
        if self.busy:
            self.conv.add("error", "작업이 끝난 뒤 API key를 입력할 수 있다")
            return
        self.secret_name = name
        self._secret_buffer = []
        self.conv.add("info", f"전역 ${name} 보안 입력 · Enter 저장 · Esc 취소")

    def _clear_secret_input(self) -> None:
        for index in range(len(self._secret_buffer)):
            self._secret_buffer[index] = ""
        self._secret_buffer.clear()
        self.secret_name = None

    def _handle_secret_key(self, kind: str, value: str) -> None:
        assert self.secret_name is not None
        if (kind == "key" and value == "esc") or (
                kind in ("ctrl", "cmd") and value in ("c", "d")):
            marker = f"${self.secret_name}"
            self._clear_secret_input()
            self.conv.add("info", f"{marker} 입력 취소")
            return
        if kind == "key" and value == "backspace":
            if self._secret_buffer:
                self._secret_buffer.pop()
            return
        if kind == "key" and value == "enter":
            name = self.secret_name
            value_text = "".join(self._secret_buffer)
            self._clear_secret_input()
            try:
                marker = credentials.save_global(name, value_text)
            except credentials.CredentialError as exc:
                self.conv.add("error", str(exc))
                return
            self.conv.add("info", f"전역 {marker} 저장 완료 (macOS Keychain)")
            return
        if kind == "char":
            self._secret_buffer.extend(value)

    def _undo(self) -> None:
        from .workspace import Workspace
        outcome = Workspace(self.root, allow_write=True).undo()
        if "error" in outcome:
            self.conv.add("error", outcome["error"])
        else:
            self.conv.add("info", "되돌림: " + ", ".join(outcome["restored"]))

    # -- Key handling --------------------------------------------------

    def scroll_body(self, delta: int) -> None:
        """Scroll the body view independently from the input line."""
        self.scroll = max(0, self.scroll + delta)

    def handle_key(self, kind: str, value: str) -> None:
        if self.secret_name is not None:
            self._handle_secret_key(kind, value)
            return
        if self.approval is not None:
            if kind == "mouse":
                return
            _, holder, done = self.approval
            if kind == "char" and value in ("y", "Y"):
                holder["ok"] = True
            self.approval = None
            done.set()
            return
        if kind == "ctrl" and value in ("c", "d"):
            self.quit = True
            return
        if kind in ("ctrl", "cmd") and value == "r":
            # Undo execution by restoring the last file change from a checkpoint.
            self._undo()
            return
        if kind in ("ctrl", "cmd") and value == "z":
            # Undo input by restoring only the last prompt edit.
            if self.input.undo():
                self._reset_completion()
            return
        if kind == "key":
            if value == "enter":
                self._complete(execute=True)
            elif value == "tab":
                self._complete(execute=False)
            elif value == "backtab":
                # shift+tab은 /yolo 토글과 같은 경로를 탄다.
                self.run_command("yolo", "")
            elif value == "esc":
                self.completion_dismissed = True
            elif value == "backspace":
                self.input.backspace()
                self._reset_completion()
            elif value == "delete":
                self.input.delete()
                self._reset_completion()
            elif value == "left":
                self.input.left()
            elif value == "right":
                self.input.right()
            elif value == "home":
                self.input.home()
            elif value == "end":
                self.input.end()
            elif value == "up":
                if self.completions():
                    count = len(self.completions()[1])
                    self.completion_index = (self.completion_index - 1) % count
                else:
                    self.input.history_prev()
            elif value == "down":
                if self.completions():
                    count = len(self.completions()[1])
                    self.completion_index = (self.completion_index + 1) % count
                else:
                    self.input.history_next()
            elif value == "pgup":
                self.scroll_body(SCROLL_STEP)
            elif value == "pgdn":
                self.scroll_body(-SCROLL_STEP)
        elif kind == "mouse":
            if value == "wheel_up":
                self.scroll_body(SCROLL_STEP)
            elif value == "wheel_down":
                self.scroll_body(-SCROLL_STEP)
        elif kind == "char":
            self.input.insert(value)
            self._reset_completion()


def _pump(agent: Agent, task: str, out: queue.Queue, root: str = "") -> None:
    """Run an agent thread, queue events, and enqueue its final episode."""
    episode = Episode()
    try:
        holder = getattr(agent, "goal_holder", None)
        if holder is not None:
            # 계획 모드는 ralph 바깥 루프가 미충족 기준을 후속 회차로 넘긴다.
            from . import goal as goal_mod
            stream = goal_mod.ralph_stream(agent, task, holder,
                                           root or os.getcwd())
            while True:
                try:
                    event = next(stream)
                except StopIteration as done:
                    episode, _goal = done.value
                    break
                out.put(("event", event))
        else:
            for event in agent.stream(task, episode=episode):
                out.put(("event", event))
    except Exception as exc:  # The UI displays the error and clears busy state.
        out.put(("error", exc))
        return
    out.put(("done", episode))


def report_session_id(session, stream=None) -> None:
    """Print the current session ID after leaving the alternate screen.

    A session that recorded nothing was never written, so there is no ID to
    resume and none is printed.
    """
    if session is None or not getattr(session, "saved", True):
        return
    output = stream if stream is not None else sys.stderr
    print(f"\n세션  {session.id}", file=output, flush=True)


# ---------------------------------------------------------------------------
# Frame rendering. Convert state into screen text with pure functions.

def _prompt_info(app: App, width: int) -> str:
    """Render the selected model in the input divider."""
    return truncate_ansi(fg(BRAND_LAVENDER) + model.MODEL + RESET, width)


def _mode_footer(app: App) -> str:
    """Return the one-line mode status for the current input."""
    return format_mode_status(app.effort, app.permission.explicit_yolo)


def _fit_footer_text(width: int, *candidates: str) -> str:
    """Choose the richest footer text that fits without clipping."""
    for candidate in candidates:
        if disp_width(plain(candidate)) <= width:
            return candidate
    return truncate_ansi(candidates[-1], width)


def _right_align(line: str, width: int) -> str:
    """Right-align an ANSI string within the requested width."""
    line = truncate_ansi(line, width)
    return " " * max(0, width - disp_width(plain(line))) + line


def _activity_rows(app: App, width: int) -> list[str]:
    """Render transient progress or approval state in the conversation body."""
    body_w = max(10, width - 4)
    if app.approval is not None:
        desc = app.approval[0]
        message = f"권한 요청: {desc} · 실행할까요? [y/N]"
        return [fg(WARN) + "  ! " + line + RESET
                for line in wrap_text(message, body_w)]
    if app.busy:
        spin = SPINNER[app.tick % len(SPINNER)]
        spinner = (fg(LAVENDER) + SPINNER_BASELINE_DOWN + spin
                   + SPINNER_BASELINE_NORMAL + RESET)
        return ["  " + spinner + DIM + " " + app.phase + RESET]
    return []


def render_frame(app: App, width: int, height: int) -> str:
    w = max(20, width)
    h = max(8, height)
    rows: list[str] = []

    # Command completion popup above the input line.
    popup: list[str] = []
    comp = app.completions()
    if comp is not None:
        kind, items = comp
        sel = app.completion_index % len(items)
        popup_w = min(w, 48)
        for i, item in enumerate(items[:6]):
            if kind == "name":
                label, desc = "/" + item.name, item.summary
            else:
                label, desc = item, ""
            gap = popup_w - 2 - disp_width(label) - disp_width(desc)
            if i == sel:
                row = ("  " + fg(255) + label + " " * max(1, gap)
                       + desc)
                popup.append(bg(238) + pad_ansi(row, popup_w) + RESET)
            else:
                popup.append("  " + fg(BRAND_KEY_SOFT) + label + RESET
                             + " " * max(1, gap) + DIM + desc + RESET)

    # Keep the working directory and vertical prompt padding on small terminals.
    popup = popup[:max(0, h - 7)]
    body_h = h - 6 - len(popup)
    workspace_h = max(0, body_h - 1)

    # Keep the working directory in a fixed top bar. Replace the welcome panel
    # with conversation content once the first message appears.
    root = _display_root(app.root, max(1, w - 2))
    rows.append(truncate_ansi(" " + DIM + root + RESET, w))

    content: list[str] = []
    for item in app.conv.items:
        content.extend(render_item(item, w - 2))
    if app.busy and app.conv.items and app.conv.items[-1].role == "sol":
        # Add a cursor to the end of the streaming answer.
        for i in range(len(content) - 1, -1, -1):
            if plain(content[i]).strip():
                content[i] = content[i] + fg(ACCENT) + "▌" + RESET
                break
    content.extend(_activity_rows(app, w - 2))
    if content:
        content = [truncate_ansi(line, w) for line in content]
        max_scroll = max(0, len(content) - workspace_h)
        app.scroll = min(app.scroll, max_scroll)
        start = max(0, len(content) - workspace_h - app.scroll)
        visible = content[start:start + workspace_h]
        rows.extend(visible + [""] * (workspace_h - len(visible)))
    else:
        app.scroll = 0
        welcome = banner_lines(w, app.effort, app.root, app.permission)
        if len(welcome) > workspace_h:
            welcome = welcome[:workspace_h]
        top_pad = max(0, (workspace_h - len(welcome)) // 2)
        bottom_pad = max(0, workspace_h - top_pad - len(welcome))
        rows.extend([""] * top_pad + welcome + [""] * bottom_pad)
    rows.extend(popup)

    # Use equal padding above and below the input so the prompt is vertically centered.
    edge = fg(EDGE)
    rows.append(edge + "╭" + "─" * (w - 2) + "╮" + RESET)
    input_inner = w - 2
    empty_input_row = (edge + "│" + RESET + " " * input_inner
                       + edge + "│" + RESET)
    rows.append(empty_input_row)
    input_prefix = " " + fg(BRAND_LAVENDER) + PROMPT_MARK + RESET + " "
    if app.secret_name is not None:
        view = f"${app.secret_name} · 입력값 숨김"
        cursor_col = min(disp_width(view), max(1, input_inner - 3))
    else:
        view, cursor_col = app.input.view(max(1, input_inner - 3))
    input_content = input_prefix + truncate_ansi(view, max(1, input_inner - 3))
    rows.append(edge + "│" + RESET + pad_ansi(input_content, input_inner)
                + edge + "│" + RESET)
    rows.append(empty_input_row)

    prompt_info = _prompt_info(app, max(1, w - 8))
    info_w = disp_width(plain(prompt_info))
    left_rule_w = max(1, w - info_w - 5)
    rows.append(edge + "╰" + "─" * left_rule_w + RESET + " "
                + prompt_info + " " + edge + "─╯" + RESET)

    # Footer hint.
    mode = _mode_footer(app)
    if app.secret_name is not None:
        text = _fit_footer_text(
            w,
            f"{mode} · [enter] Keychain 저장 · esc 취소 · 값은 표시/기록하지 않음",
            mode)
        footer = DIM + " " + text + RESET
    elif app.approval is not None or app.busy:
        footer = DIM + " " + mode + RESET
    elif popup:
        text = _fit_footer_text(w, f"{mode} · {COMPLETION_HINTS.strip()}", mode)
        footer = DIM + " " + text + RESET
    else:
        text = _fit_footer_text(
            w,
            f"{mode} · {HINTS.strip()}",
            f"{mode} · [enter] 전송 · shift+tab 권한 변경 · Ctrl+D 종료",
            f"{mode} · shift+tab 권한 변경 · Ctrl+D 종료",
            mode)
        footer = DIM + " " + text + RESET
    rows.append(_right_align(footer, w))

    assert len(rows) == h, f"frame rows {len(rows)} != height {h}"
    frame = "\x1b[H" + "\x1b[?25l"
    # Raw mode disables ONLCR, so each newline must include a carriage return to
    # start the next row at column zero.
    frame += "".join(truncate_ansi(row, w) + "\x1b[K" + "\r\n"
                     for row in rows[:-1])
    frame += truncate_ansi(rows[-1], w) + "\x1b[K"
    if app.approval is None and not app.busy:
        # Keep the cursor inside the input box.
        frame += f"\x1b[{h - 3};{5 + cursor_col}H" + "\x1b[?25h"
    return frame


# ---------------------------------------------------------------------------
# Terminal I/O. The pure logic above is testable without a terminal.

_ESCAPES = {
    b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
    b"\x1b[H": "home", b"\x1b[F": "end", b"\x1b[1~": "home",
    b"\x1b[4~": "end", b"\x1b[3~": "delete", b"\x1b[5~": "pgup",
    b"\x1b[6~": "pgdn", b"\x1bOH": "home", b"\x1bOF": "end",
    b"\x1b[Z": "backtab",
}


def _sgr_mouse(data: bytes) -> tuple[tuple[str, str] | None, int] | None:
    """Parse a leading SGR mouse event and return consumed bytes."""
    if not data.startswith(b"\x1b[<"):
        return None
    end = next((i for i, byte in enumerate(data[3:], 3)
                if byte in (ord("M"), ord("m"))), -1)
    if end < 0:
        return None
    fields = data[3:end].split(b";")
    if len(fields) != 3 or any(not field.isdigit() for field in fields):
        return None
    button = int(fields[0])
    consumed = end + 1
    if data[end:end + 1] != b"M" or not (button & 64):
        return (None, consumed)
    direction = "wheel_up" if (button & 1) == 0 else "wheel_down"
    return (("mouse", direction), consumed)


# CSI-u modifier bits. macOS Command is represented as SUPER.
# Format: CSI unicode-key-code ; (1 + modifier-bits) u.
_MOD_SHIFT = 1
_MOD_CTRL = 4
_MOD_SUPER = 8


def _extended_key(data: bytes) -> tuple[tuple[str, str] | None, int] | None:
    """Parse a leading CSI-u key event and return consumed bytes."""
    if not data.startswith(b"\x1b["):
        return None
    end = data.find(b"u", 2)
    if end < 0:
        return None
    payload = data[2:end]
    # Restrict CSI-u fields to digits, ':', and ';' so other CSI commands are
    # not consumed accidentally.
    if not payload or any(ch not in b"0123456789:;" for ch in payload):
        return None
    fields = payload.split(b";")
    try:
        key_code = int(fields[0].split(b":", 1)[0])
        modifier_field = fields[1].split(b":") if len(fields) > 1 else [b"1"]
        modifiers = int(modifier_field[0] or b"1") - 1
        event_type = int(modifier_field[1]) if len(modifier_field) > 1 else 1
    except ValueError:
        return None
    consumed = end + 1
    if event_type == 3:  # Key release; the press event was already handled.
        return (None, consumed)

    kind = "cmd" if modifiers & _MOD_SUPER else (
        "ctrl" if modifiers & _MOD_CTRL else "key")
    if kind == "key":
        # CSI-u codes for the basic editing keys.
        if key_code == 9 and modifiers & _MOD_SHIFT:
            return (("key", "backtab"), consumed)
        special_keys = {9: "tab", 13: "enter", 127: "backspace"}
        if key_code in special_keys:
            return (("key", special_keys[key_code]), consumed)
    if kind in ("cmd", "ctrl"):
        shortcuts = {
            ord("a"): ("key", "home"),
            ord("e"): ("key", "end"),
            ord("c"): (kind, "c"),
            ord("d"): (kind, "d"),
            ord("r"): (kind, "r"),
            ord("z"): (kind, "z"),
        }
        return (shortcuts.get(key_code), consumed)
    if key_code == 27:
        return (("key", "esc"), consumed)
    return (None, consumed)


def parse_keys(data: bytes) -> tuple[list[tuple[str, str]], bytes]:
    """Split a byte stream into key events and return an incomplete tail."""
    keys: list[tuple[str, str]] = []
    i = 0
    while i < len(data):
        if data[i:i + 1] == b"\x1b":
            if data.startswith(b"\x1b[<", i):
                mouse = _sgr_mouse(data[i:])
                if mouse is not None:
                    event, consumed = mouse
                    if event is not None:
                        keys.append(event)
                    i += consumed
                    continue
                # Append the remaining bytes on the next read.
                tail = data[i + 3:]
                if all(ch in b"0123456789;" for ch in tail):
                    break
                i += 1
                continue
            extended = _extended_key(data[i:])
            if extended is not None:
                event, consumed = extended
                if event is not None:
                    keys.append(event)
                i += consumed
                continue
            tail = data[i + 2:] if data.startswith(b"\x1b[", i) else b""
            if tail and all(ch in b"0123456789:;" for ch in tail):
                break  # Incomplete CSI-u sequence; wait for the next read.
            match = None
            for seq, name in _ESCAPES.items():
                if data.startswith(seq, i):
                    match = (seq, name)
                    break
            if match is not None:
                keys.append(("key", match[1]))
                i += len(match[0])
            elif i + 1 < len(data) and data[i + 1:i + 2] == b"[":
                # The sequence may be incomplete; wait for the next read.
                if any(seq.startswith(data[i:]) for seq in _ESCAPES):
                    break
                i += 1  # Discard only ESC from an unknown sequence.
            elif i + 1 == len(data):
                break  # Wait for the rest of a standalone ESC sequence.
            else:
                keys.append(("key", "esc"))
                i += 1
        elif data[i] in (0x0D, 0x0A):
            keys.append(("key", "enter"))
            i += 1
        elif data[i] in (0x7F, 0x08):
            keys.append(("key", "backspace"))
            i += 1
        elif data[i] == 0x01:
            keys.append(("key", "home"))
            i += 1
        elif data[i] == 0x05:
            keys.append(("key", "end"))
            i += 1
        elif data[i] == 0x03:
            keys.append(("ctrl", "c"))
            i += 1
        elif data[i] == 0x04:
            keys.append(("ctrl", "d"))
            i += 1
        elif data[i] == 0x12:
            keys.append(("ctrl", "r"))
            i += 1
        elif data[i] == 0x1A:
            keys.append(("ctrl", "z"))
            i += 1
        elif data[i] == 0x09:
            keys.append(("key", "tab"))
            i += 1
        elif data[i] < 0x20:
            i += 1  # Ignore other control characters.
        else:
            # Decode one complete UTF-8 character.
            for size in (1, 2, 3, 4):
                try:
                    ch = data[i:i + size].decode("utf-8")
                except UnicodeDecodeError:
                    continue
                keys.append(("char", ch))
                i += size
                break
            else:
                if len(data) - i < 4:
                    break  # Incomplete multibyte character; wait for the next read.
                i += 1
    return keys, data[i:]


class Terminal:
    """Full-screen terminal running in raw mode."""

    def __init__(self) -> None:
        self._fd = sys.stdin.fileno()
        self._saved = None
        self._pending = b""

    def __enter__(self) -> "Terminal":
        import termios
        import tty
        self._saved = termios.tcgetattr(self._fd)
        tty.setraw(self._fd)
        # Enter the alternate screen, enable extended key reporting, and enable
        # SGR mouse input so wheel events can scroll the body.
        sys.stdout.write("\x1b[?1049h\x1b[?1000h\x1b[?1006h"
                         "\x1b[>1u\x1b[2J\x1b[H")
        sys.stdout.flush()
        return self

    def __exit__(self, *exc) -> None:
        import termios
        # Restore mouse and keyboard modes before leaving the alternate screen.
        sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[<u"
                         "\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if self._saved is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size()
        return size.columns, size.lines

    def write(self, s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    def read_keys(self, timeout: float) -> list[tuple[str, str]]:
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return []
        data = self._pending + os.read(self._fd, 256)
        keys, self._pending = parse_keys(data)
        return keys


def run(build_agent, args) -> int:
    """Run the TUI using a supplied agent builder."""
    if not sys.stdin.isatty():
        print("TUI는 대화형 터미널에서만 동작합니다", file=sys.stderr)
        return 1
    def wire(agent) -> None:
        # Re-read app.permission for every tool approval so /yolo can change
        # at runtime. Tokens queue as they arrive and stream as answer_delta.
        agent.approve = app.make_approve()
        agent.on_delta = lambda text, is_reasoning: app.queue.put(
            ("event", Event("answer_delta",
                            {"text": text, "reasoning": is_reasoning})))
        agent.on_tool_output = lambda tool, text: app.queue.put(
            ("event", Event("tool_output", {"tool": tool, "text": text})))

    current: dict = {}

    def set_plan(enabled: bool) -> None:
        """Rebuild the agent with plan tools and the deterministic plan prompt.

        The tool schema is part of the stable prefix, so plan mode cannot be
        toggled by editing the live registry. Rebuilding keeps the session, so
        the restored conversation carries over to the new agent.
        """
        old = current["agent"]
        session = current.get("session")
        args.plan = enabled
        prior = session if session is not None and session.saved else None
        args.continue_session = prior.id if prior is not None else None
        new_agent, _prefix, new_session = build_agent(args)
        for pool_name in ("lsp_pool", "mcp_manager"):
            pool = getattr(old, pool_name, None)
            if pool is not None:
                pool.close()
        current["agent"] = new_agent
        current["session"] = new_session
        app.agent = new_agent
        wire(new_agent)

    agent, _prefix, session = build_agent(args)
    current["agent"] = agent
    current["session"] = session
    policy = args.permission_policy
    app = App(root=os.path.abspath(args.root), effort=args.effort, agent=agent,
              permission=policy)
    app.set_plan = set_plan
    if getattr(args, "branch_notice", ""):
        app.conv.add("info", args.branch_notice)
    wire(agent)
    try:
        with Terminal() as term:
            if getattr(args, "task", None):
                app.input.insert(args.task)
                app.submit()
            dirty = True
            while not app.quit:
                while True:
                    try:
                        kind, payload = app.queue.get_nowait()
                    except queue.Empty:
                        break
                    app.handle_message(kind, payload)
                    dirty = True
                if app.busy:
                    app.tick += 1
                    dirty = True
                if dirty:
                    w, h = term.size()
                    term.write(render_frame(app, w, h))
                    dirty = False
                for kind, value in term.read_keys(SPINNER_INTERVAL_S):
                    app.handle_key(kind, value)
                    dirty = True
    finally:
        # Display the session ID after restoring the normal terminal screen.
        report_session_id(current.get("session"))
        last = current.get("agent")
        if getattr(last, "lsp_pool", None) is not None:
            last.lsp_pool.close()
        if getattr(last, "mcp_manager", None) is not None:
            last.mcp_manager.close()
    return app.exit_code
