"""Provide bounded file, search, and execution tools for the coding agent.

Keep every path inside the workspace root, require a read before editing, and
stream command output without exposing secrets. These boundaries protect both
the filesystem and the context budget.
"""

import codecs
import os
import re
import select
import subprocess
import time

from .checkpoint import CheckpointLog
from .tools import Tool

READ_MAX_CHARS = 60_000
GREP_MAX_HITS = 80
SHELL_TIMEOUT = 60
SHELL_OUTPUT_MAX = 20_000
# 한 번의 write_file에 권장하는 콘텐츠 길이. 2026-08-03 실행에서 40,100바이트
# 단일 생성이 스트림 절단(94.5초 유실)과 전체 재생성(126.6초)으로 이어졌다.
# 큰 파일은 조각으로 나눠 append로 이어 쓰게 권고해 실패 반경을 조각 하나로
# 제한한다. 다만 완성되어 도착한 content는 크기와 무관하게 기록한다. 절단은
# JSON 파싱 단계에서 이미 실패하므로, 여기까지 도달한 내용을 폐기하면
# 거부→재생성 루프(시간·출력 토큰 손실의 주원인)만 남는다.
WRITE_MAX_CHARS = 12_000

# URL을 파일 경로로 착각한 호출을 잡는다.
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")

# Commands blocked from execution regardless of model decisions.
DANGEROUS = ("rm -rf /", "mkfs", "dd if=", ":(){", "shutdown", "reboot",
             "> /dev/sd")

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "target",
             "dist", "build", ".next"}


class Workspace:
    """Run file operations inside root and require reads before edits."""

    def __init__(self, root: str, allow_write: bool = False,
                 allow_shell: bool = False, checkpoints: bool = True) -> None:
        self.root = os.path.realpath(root)
        self.allow_write = allow_write
        self.allow_shell = allow_shell
        self._read: set[str] = set()
        # Irreversible edits are the most expensive failure in a coding agent.
        self.log = CheckpointLog(self.root) if (allow_write and checkpoints) else None

    def resolve(self, path: str) -> str:
        """Return a real path inside root or raise an error."""
        target = os.path.realpath(os.path.join(self.root, path))
        if target != self.root and not target.startswith(self.root + os.sep):
            raise ValueError(f"작업 디렉터리 밖입니다: {path}")
        return target

    # --- Reading -------------------------------------------------------

    def read_file(self, args: dict) -> dict:
        path = args["path"]
        if _URL_RE.match(path.strip()):
            return {"error": (
                f"{path}는 파일 경로가 아니라 URL입니다. 웹 페이지는 "
                "shell의 curl로 가져오십시오. read_file은 작업 디렉터리 "
                "안의 파일만 읽습니다.")}
        try:
            target = self.resolve(path)
            with open(target, encoding="utf-8") as fh:
                content = fh.read()
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            return {"error": str(exc)}
        self._read.add(target)
        start = args.get("start_line")
        end = args.get("end_line")
        if start is not None or end is not None:
            # 범위 조회: 이미 읽은 큰 파일을 통째로 다시 받아 히스토리를
            # 부풀리는 대신 필요한 구간만 돌려준다.
            lines = content.splitlines(keepends=True)
            total = len(lines)
            first = max(int(start or 1), 1)
            last = min(int(end or total), total)
            if first > total:
                return {"error": f"start_line {first}이 전체 {total}줄을 "
                                 "벗어납니다"}
            piece = "".join(lines[first - 1:last])
            return {"path": path, "content": piece,
                    "start_line": first, "end_line": last,
                    "total_lines": total}
        if len(content) > READ_MAX_CHARS:
            return {"path": path, "truncated": True,
                    "content": content[:READ_MAX_CHARS],
                    "note": f"{len(content)}자 중 앞 {READ_MAX_CHARS}자만 표시"}
        return {"path": path, "content": content}

    def list_dir(self, args: dict) -> dict:
        try:
            target = self.resolve(args.get("path", "."))
            names = sorted(os.listdir(target))
        except (OSError, ValueError) as exc:
            return {"error": str(exc)}
        entries = []
        for name in names:
            if name in SKIP_DIRS:
                continue
            full = os.path.join(target, name)
            entries.append(f"{name}/" if os.path.isdir(full) else name)
        return {"entries": entries[:400]}

    def grep(self, args: dict) -> dict:
        """Search content with ``rg`` when available, otherwise scan in Python."""
        pattern = args["pattern"]
        where = args.get("path", ".")
        try:
            target = self.resolve(where)
        except ValueError as exc:
            return {"error": str(exc)}
        try:
            done = subprocess.run(
                ["rg", "-n", "--no-heading", "-m", "3", pattern, target],
                capture_output=True, text=True, timeout=30)
            lines = done.stdout.splitlines()
        except (FileNotFoundError, subprocess.SubprocessError):
            lines = self._grep_fallback(target, pattern)
        hits = [line.replace(self.root + os.sep, "") for line in lines[:GREP_MAX_HITS]]
        return {"hits": hits, "count": len(hits)}

    def _grep_fallback(self, target: str, pattern: str) -> list[str]:
        found: list[str] = []
        for base, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                full = os.path.join(base, name)
                try:
                    with open(full, encoding="utf-8") as fh:
                        for number, line in enumerate(fh, 1):
                            if pattern in line:
                                found.append(f"{full}:{number}:{line.rstrip()}")
                                if len(found) >= GREP_MAX_HITS:
                                    return found
                except (OSError, UnicodeDecodeError):
                    continue
        return found

    # --- Writing -------------------------------------------------------

    def write_file(self, args: dict) -> dict:
        if not self.allow_write:
            return {"error": "쓰기 권한이 없습니다"}
        path, content = args["path"], args["content"]
        append = bool(args.get("append"))
        oversize = len(content) > WRITE_MAX_CHARS
        try:
            target = self.resolve(path)
        except ValueError as exc:
            return {"error": str(exc)}
        if append and not os.path.exists(target):
            return {"error": f"{path}가 없어 이어 쓸 수 없습니다. "
                             "append 없이 먼저 만드십시오"}
        if os.path.exists(target) and target not in self._read:
            return {"error": f"{path}를 먼저 read_file로 읽어야 수정할 수 있습니다"}

        def apply() -> None:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            mode = "a" if append else "w"
            with open(target, mode, encoding="utf-8") as fh:
                fh.write(content)

        try:
            label = f"append {path}" if append else f"write {path}"
            self._checkpointed([path], apply, label)
        except OSError as exc:
            return {"error": str(exc)}
        self._read.add(target)
        result = {"path": path, "written": len(content)}
        if append:
            result["appended"] = True
        if oversize:
            result["note"] = (
                f"{len(content):,}자를 한 번에 기록했습니다. 다음부터는 "
                f"{WRITE_MAX_CHARS:,}자 이하 조각으로 나눠 append=true로 이어 "
                "쓰면 스트림 절단 시 손실이 조각 하나로 줄어듭니다.")
        return result

    def edit_file(self, args: dict) -> dict:
        """Replace exactly one matching string.

        Reject multiple matches because an ambiguous edit is unsafe.
        """
        if not self.allow_write:
            return {"error": "쓰기 권한이 없습니다"}
        path, old, new = args["path"], args["old"], args["new"]
        try:
            target = self.resolve(path)
            with open(target, encoding="utf-8") as fh:
                content = fh.read()
        except (OSError, ValueError) as exc:
            return {"error": str(exc)}
        if target not in self._read:
            return {"error": f"{path}를 먼저 read_file로 읽어야 합니다"}
        occurrences = content.count(old)
        if occurrences == 0:
            return {"error": "일치하는 내용이 없습니다"}
        if occurrences > 1:
            return {"error": f"{occurrences}곳에 일치합니다. 더 긴 문맥을 포함하세요"}

        def apply() -> None:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(content.replace(old, new))

        try:
            self._checkpointed([path], apply, f"edit {path}")
        except OSError as exc:
            return {"error": str(exc)}
        return {"path": path, "replaced": 1}

    def _checkpointed(self, paths: list[str], apply, label: str) -> None:
        if self.log is None:
            apply()
            return
        self.log.record(paths, apply, label)

    def undo(self, force: bool = False) -> dict:
        """Undo the last edit and reject conflicting external changes."""
        from .checkpoint import Conflict
        if self.log is None:
            return {"error": "체크포인트가 비활성화되어 있습니다"}
        try:
            return {"restored": self.log.undo(force=force)}
        except (Conflict, ValueError) as exc:
            return {"error": str(exc)}

    # --- Execution -----------------------------------------------------

    def shell(self, args: dict) -> dict:
        return self.shell_stream(args, lambda chunk: None)

    def shell_stream(self, args: dict, emit) -> dict:
        """Emit shell output as it arrives and return the final result envelope."""
        if not self.allow_shell:
            return {"error": "명령 실행 권한이 없습니다"}
        command = args["command"]
        for bad in DANGEROUS:
            if bad in command:
                return {"error": f"위험한 명령이 차단되었습니다: {bad}"}
        timeout = args.get("timeout", SHELL_TIMEOUT)
        try:
            process = subprocess.Popen(
                command, shell=True, cwd=self.root, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            return {"error": str(exc)}
        assert process.stdout is not None
        fd = process.stdout.fileno()
        os.set_blocking(fd, False)
        started = time.monotonic()
        output = bytearray()
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending_text = ""

        def deliver(text: str, final: bool = False) -> None:
            """Emit complete lines so secret redaction can see line boundaries."""
            nonlocal pending_text
            pending_text += text
            while "\n" in pending_text:
                line, pending_text = pending_text.split("\n", 1)
                emit(line + "\n")
            if final and pending_text:
                emit(pending_text)
                pending_text = ""
        clipped = False
        timed_out = False
        while True:
            if time.monotonic() - started > timeout:
                timed_out = True
                try:
                    os.killpg(process.pid, 15)
                except (OSError, ProcessLookupError):
                    process.kill()
            ready, _, _ = select.select([fd], [], [], 0.08)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    chunk = b""
                if chunk:
                    room = SHELL_OUTPUT_MAX - len(output)
                    kept = chunk[:max(0, room)]
                    if kept:
                        output.extend(kept)
                        decoded = decoder.decode(kept, final=False)
                        if decoded:
                            deliver(decoded)
                    if len(kept) < len(chunk) and not clipped:
                        clipped = True
                        deliver(f"\n… 출력이 {SHELL_OUTPUT_MAX:,}자로 잘렸습니다\n")
            if process.poll() is not None:
                # Drain bytes that remain in the pipe after process exit.
                while len(output) < SHELL_OUTPUT_MAX:
                    try:
                        tail = os.read(fd, 4096)
                    except (BlockingIOError, OSError):
                        break
                    if not tail:
                        break
                    kept = tail[:SHELL_OUTPUT_MAX - len(output)]
                    output.extend(kept)
                    decoded = decoder.decode(kept, final=False)
                    if decoded:
                        deliver(decoded)
                break
            if timed_out:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                break
        final = decoder.decode(b"", final=True)
        deliver(final, final=True)
        text = output.decode("utf-8", "replace")
        if timed_out:
            return {"error": "시간 초과", "output": text}
        return {"exit_code": process.returncode, "output": text,
                "truncated": clipped}

    # --- Registration --------------------------------------------------

    def tools(self) -> list[Tool]:
        found = [
            Tool("read_file",
                 "파일 내용을 읽는다. 수정 전에는 반드시 먼저 읽어야 한다. "
                 "이미 읽은 파일의 일부만 다시 볼 때는 start_line/end_line "
                 "범위 조회나 grep을 사용하고 전체를 재조회하지 않는다.",
                 {"type": "object",
                  "properties": {"path": {"type": "string",
                                          "description": "작업 디렉터리 기준 상대 경로"},
                                 "start_line": {"type": "integer",
                                                "description": "1부터 시작하는 첫 줄 번호"},
                                 "end_line": {"type": "integer",
                                              "description": "마지막 줄 번호(포함)"}},
                  "required": ["path"]}, self.read_file),
            Tool("list_dir", "디렉터리 목록을 본다",
                 {"type": "object",
                  "properties": {"path": {"type": "string"}}}, self.list_dir),
            Tool("grep", "작업 디렉터리에서 문자열을 검색한다",
                 {"type": "object",
                  "properties": {"pattern": {"type": "string"},
                                 "path": {"type": "string"}},
                  "required": ["pattern"]}, self.grep),
        ]
        if self.allow_write:
            found.append(Tool(
                "write_file",
                "파일을 새로 쓰거나 통째로 덮어쓴다. 긴 파일은 "
                f"{WRITE_MAX_CHARS:,}자 이하 조각으로 나눠 첫 조각을 쓴 뒤 "
                "append=true로 이어 쓰는 편이 안전하다. 인자 스트림이 절단되면 "
                "전체를 다시 만들지 말고 나머지 조각만 이어 쓴다.",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "content": {"type": "string"},
                                "append": {"type": "boolean",
                                           "description": "기존 파일 끝에 이어 쓴다"}},
                 "required": ["path", "content"]}, self.write_file,
                read_only=False))
            found.append(Tool(
                "edit_file", "파일에서 정확히 일치하는 한 곳을 바꾼다",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "old": {"type": "string",
                                        "description": "바꿀 원본. 파일에서 유일해야 한다"},
                                "new": {"type": "string"}},
                 "required": ["path", "old", "new"]}, self.edit_file,
                read_only=False))
        if self.allow_shell:
            found.append(Tool(
                "shell", "작업 디렉터리에서 셸 명령을 실행한다",
                {"type": "object",
                 "properties": {"command": {"type": "string"},
                                "timeout": {"type": "integer"}},
                 "required": ["command"]}, self.shell, read_only=False,
                stream=self.shell_stream))
        return found


class WorkspaceFactory:
    """Build a new tool set rooted at an isolated path."""

    def __init__(self, root: str, allow_shell: bool = False) -> None:
        self.root = os.path.realpath(root)
        self.allow_shell = allow_shell

    def __call__(self, path: str):
        from . import codesearch
        from .tools import Registry

        # Child edits stay inside the worktree, so discarding that worktree is the
        # rollback and no parent checkpoint is needed.
        workspace = Workspace(path, allow_write=True,
                              allow_shell=self.allow_shell, checkpoints=False)
        registry = Registry()
        for tool in workspace.tools():
            registry.add(tool)
        for tool in codesearch.tools(path):
            registry.add(tool)
        return registry, lambda: None
