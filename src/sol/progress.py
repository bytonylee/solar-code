"""CLI output helpers for assistant content and tool results.

Keep lifecycle state out of the visible transcript. The CLI may still animate a
spinner on a TTY, but only the input's think/yolo mode is printed as a footer.
"""

from __future__ import annotations

import json
import sys
import threading
import time

from .loop import Event

# The classic spinner uses only the top three Braille dot rows. Leave row four
# (dots 7/8) empty to align it with prompt and footer text.
THREE_DOT_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼",
                    "⠴", "⠦", "⠧", "⠇", "⠏")
CLASSIC_FRAMES = THREE_DOT_FRAMES  # Keep a legacy alias for older callers.
SPINNER = CLASSIC_FRAMES
LAVENDER = 189  # Closest xterm-256 color to the lavender accent.
SPINNER_INTERVAL = 0.08  # Ten frames at 0.08 seconds make one 0.8-second loop.
TOOL_DISPLAY_MAX = 12_000


def format_mode_status(effort: str, yolo: bool) -> str:
    """Render the one-line mode status shown below each input."""
    return f"think={effort} · yolo={'on' if yolo else 'off'}"


def format_tool_result(raw: str, max_chars: int = TOOL_DISPLAY_MAX) -> str:
    """Format a tool JSON envelope for terminal output."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        text = str(raw)
    else:
        if isinstance(value, dict) and "output" in value:
            text = str(value.get("output") or "")
            if not text and "exit_code" in value:
                text = f"exit_code: {value['exit_code']}"
        elif isinstance(value, dict) and "content" in value:
            prefix = f"{value.get('path')}\n" if value.get("path") else ""
            text = prefix + str(value.get("content") or "")
        elif isinstance(value, dict) and isinstance(value.get("entries"), list):
            text = "\n".join(str(item) for item in value["entries"])
        elif isinstance(value, dict) and isinstance(value.get("hits"), list):
            text = "\n".join(str(item) for item in value["hits"])
        else:
            text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n… 화면 표시에서 {omitted:,}자 생략"


class Spinner:
    """Animate a phase-free spinner on a TTY."""

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stderr
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._lock = threading.RLock()
        self._active = False
        self._closed = False
        self._phase = ""
        self._index = 0
        self._thread = None
        if self.tty:
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()

    def _clear_locked(self) -> None:
        if self.tty:
            self.stream.write("\r\x1b[2K")
            self.stream.flush()

    def set(self, phase: str) -> None:
        with self._lock:
            self._phase = phase
            self._active = True

    def pause(self) -> None:
        with self._lock:
            self._active = False
            self._clear_locked()

    def log(self, text: str) -> None:
        with self._lock:
            self._clear_locked()
            self.stream.write(text.rstrip("\n") + "\n")
            self.stream.flush()

    def synchronized(self, fn, clear: bool = True) -> None:
        with self._lock:
            if clear:
                self._clear_locked()
            fn()

    def close(self) -> None:
        with self._lock:
            self._active = False
            self._closed = True
            self._clear_locked()
        if self._thread is not None:
            self._thread.join(timeout=0.3)

    def _animate(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    return
                if self._active:
                    frame = SPINNER[self._index % len(SPINNER)]
                    self._index += 1
                    self.stream.write(f"\r\x1b[2K\x1b[38;5;{LAVENDER}m{frame}\x1b[0m")
                    self.stream.flush()
            time.sleep(SPINNER_INTERVAL)


class LiveCLI:
    """Convert agent events and deltas into clean CLI output."""

    def __init__(self, stdout=None, stderr=None, effort: str = "off",
                 yolo: bool = False) -> None:
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.spinner = Spinner(self.stderr)
        self.footer = format_mode_status(effort, yolo)
        self._footer_written = False
        self.current_round = 0
        self.current_tool = ""
        self.current_turn_streamed = False
        self.answer_written = False
        self._stdout_newline = True
        self._reasoning_chars = 0
        self._tool_buffer = ""

    def start(self) -> None:
        self.spinner.set("")

    def on_delta(self, text: str, is_reasoning: bool) -> None:
        if is_reasoning:
            self._reasoning_chars += len(text)
            self.spinner.set(f"추론 중 · {self._reasoning_chars:,}자")
            return
        if not text:
            return
        self.current_turn_streamed = True
        self.answer_written = True
        self.spinner.pause()

        def write() -> None:
            self.stdout.write(text)
            self.stdout.flush()

        self.spinner.synchronized(write)
        self._stdout_newline = text.endswith("\n")

    def on_tool_output(self, tool: str, text: str) -> None:
        self._ensure_stdout_newline()
        self._tool_buffer += text
        while "\n" in self._tool_buffer:
            line, self._tool_buffer = self._tool_buffer.split("\n", 1)
            self.spinner.log("    " + line)
        self.spinner.set("")

    def on_event(self, event: Event) -> None:
        kind, data = event.kind, event.data
        if kind == "run_start":
            self.spinner.set("")
        elif kind == "pass_start":
            self.spinner.set("")
        elif kind == "turn_start":
            self.current_round = data.get("round", self.current_round)
            self.current_turn_streamed = False
            self._reasoning_chars = 0
            self.spinner.set("")
        elif kind == "reasoning":
            self.spinner.set("")
        elif kind == "tool_start":
            self.current_tool = data.get("tool", "tool")
            self._tool_buffer = ""
            self.spinner.set("")
        elif kind == "tool_end":
            self._flush_tool_buffer()
            if not data.get("ok") and data.get("error"):
                self.spinner.log(f"! {data.get('error')}")
            if not data.get("streamed"):
                rendered = format_tool_result(data.get("result", ""))
                for line in rendered.splitlines():
                    self.spinner.log("    " + line)
            self.spinner.set("")
        elif kind == "answer":
            if not self.current_turn_streamed:
                self.on_delta(data.get("content", ""), False)
            self._ensure_stdout_newline()
        elif kind == "compaction":
            # Keep lifecycle and cache bookkeeping out of the transcript.
            self.spinner.set("")
        elif kind in ("nudge", "retry", "starved_retry"):
            self.spinner.set("")
        elif kind == "blocked":
            reason = data.get("reason", "blocked")
            self._ensure_stdout_newline()
            self.spinner.log(f"! {reason}")
            if not self.current_turn_streamed:
                self.on_delta(reason, False)
                self._ensure_stdout_newline()
        elif kind == "starved":
            self._ensure_stdout_newline()
            self.spinner.log(f"! {data.get('reason', kind)}")
        elif kind == "rounds_exhausted":
            self._ensure_stdout_newline()
            self.spinner.log(f"! {data.get('reason', '라운드 상한 도달')}")
        elif kind == "stalled":
            self._ensure_stdout_newline()
            self.spinner.log(f"! 진행 정체로 중단: {data.get('reason', '')}")
        elif kind == "gate_exhausted":
            self._ensure_stdout_newline()
            self.spinner.log(f"! 완료 게이트 소진: {data.get('reason', '')}")
        elif kind == "empty_answer":
            self._ensure_stdout_newline()
            self.spinner.log("! 빈 최종 응답을 감지했습니다")
        elif kind == "wrap_up_start":
            self.spinner.set("")
        elif kind == "wrap_up_failed":
            self._ensure_stdout_newline()
            self.spinner.log(f"! {data.get('reason', '최종 정리 실패')}")
        elif kind == "wrap_up_end":
            self.spinner.set("")
        elif kind == "run_error":
            self._ensure_stdout_newline()
            self.spinner.log(f"! {data.get('reason', '모델 요청 실패')}")
        elif kind == "goal_unmet":
            self.spinner.set("")
        elif kind == "goal_complete":
            self.spinner.set("")
        elif kind == "goal_limit":
            self._ensure_stdout_newline()
            self.spinner.log("! 목표 회차 상한 도달")
        elif kind == "run_end":
            self.spinner.pause()

    def wrap_approver(self, approve):
        if approve is None:
            return None

        def wrapped(tool, args):
            self.spinner.pause()
            try:
                return approve(tool, args)
            finally:
                self.spinner.set("")
        return wrapped

    def close(self) -> None:
        self._flush_tool_buffer()
        self._ensure_stdout_newline()
        self.spinner.close()
        if not self._footer_written:
            self.stderr.write(self.footer + "\n")
            self.stderr.flush()
            self._footer_written = True

    def _flush_tool_buffer(self) -> None:
        if self._tool_buffer:
            self.spinner.log("    " + self._tool_buffer)
            self._tool_buffer = ""

    def _ensure_stdout_newline(self) -> None:
        if self.answer_written and not self._stdout_newline:
            self.spinner.synchronized(lambda: (self.stdout.write("\n"),
                                               self.stdout.flush()), clear=False)
            self._stdout_newline = True
