"""Isolate parallel implementations with git worktrees.

Each child receives a separate filesystem view, allowing the parent to review a
diff before applying it. Keep worktree paths out of prompts so child prefixes
remain stable.
"""

import os
import shutil
import subprocess
import uuid

WORKTREE_DIR = ".sol/worktrees"
GIT_TIMEOUT = 120


class WorktreeError(RuntimeError):
    pass


def _git(args: list[str], cwd: str, keep_trailing: bool = False) -> tuple[int, str]:
    """Return a diff while preserving its trailing newline when requested."""
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    merged = done.stdout + done.stderr
    return done.returncode, merged if keep_trailing else merged.strip()


def is_repo(root: str) -> bool:
    code, out = _git(["rev-parse", "--is-inside-work-tree"], root)
    return code == 0 and out.strip() == "true"


def repo_root(root: str) -> str:
    code, out = _git(["rev-parse", "--show-toplevel"], root)
    if code != 0:
        raise WorktreeError(f"git 저장소가 아닙니다: {root}")
    return out.strip()


class Worktree:
    """Isolated worktree that cleans itself up as a context manager."""

    def __init__(self, path: str, branch: str, base: str) -> None:
        self.path = path
        self.branch = branch
        self.base = base

    def __enter__(self) -> "Worktree":
        return self

    def __exit__(self, *exc) -> None:
        self.remove()

    def diff(self) -> str:
        """Return changes from the base revision for parent review."""
        _git(["add", "-A"], self.path)
        code, out = _git(["diff", "--cached"], self.path, keep_trailing=True)
        return out if code == 0 else ""

    def changed_files(self) -> list[str]:
        _git(["add", "-A"], self.path)
        code, out = _git(["diff", "--cached", "--name-only"], self.path)
        return [line for line in out.splitlines() if line] if code == 0 else []

    def apply_to(self, target: str) -> tuple[bool, str]:
        """Apply this worktree's changes to another directory.

        Leave the target unchanged when application fails.
        """
        patch = self.diff()
        if not patch.strip():
            return True, "변경 없음"
        if not patch.endswith("\n"):
            patch += "\n"
        try:
            done = subprocess.run(["git", "apply", "--3way", "-"], cwd=target,
                                  input=patch, capture_output=True, text=True,
                                  timeout=GIT_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if done.returncode != 0:
            return False, (done.stdout + done.stderr).strip()[:600]
        return True, f"{len(self.changed_files())}개 파일 적용"

    def remove(self) -> None:
        code, _ = _git(["worktree", "remove", "--force", self.path], self.base)
        if code != 0 and os.path.isdir(self.path):
            shutil.rmtree(self.path, ignore_errors=True)
            _git(["worktree", "prune"], self.base)
        _git(["branch", "-D", self.branch], self.base)


def create(root: str, name: str = "") -> Worktree:
    """Create a worktree from the current HEAD."""
    base = repo_root(root)
    tag = f"{name or 'task'}-{uuid.uuid4().hex[:6]}"
    branch = f"sol/{tag}"
    path = os.path.join(base, WORKTREE_DIR, tag)

    os.makedirs(os.path.join(base, WORKTREE_DIR), exist_ok=True)
    code, out = _git(["worktree", "add", "-b", branch, path, "HEAD"], base)
    if code != 0:
        raise WorktreeError(f"worktree 생성 실패: {out[:300]}")
    return Worktree(path, branch, base)


def cleanup(root: str) -> int:
    """Remove remaining sol worktrees after a crash or interrupted run."""
    try:
        base = repo_root(root)
    except WorktreeError:
        return 0
    holder = os.path.join(base, WORKTREE_DIR)
    if not os.path.isdir(holder):
        return 0
    removed = 0
    for name in os.listdir(holder):
        path = os.path.join(holder, name)
        if not os.path.isdir(path):
            continue
        Worktree(path, f"sol/{name}", base).remove()
        removed += 1
    _git(["worktree", "prune"], base)
    return removed


def ensure_ignored(root: str) -> None:
    """Ensure worktree directories and local notes are not committed."""
    try:
        base = repo_root(root)
    except WorktreeError:
        return
    path = os.path.join(base, ".git", "info", "exclude")
    entries = [f"/{WORKTREE_DIR}/", "/.sol/notes/"]
    try:
        existing = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                existing = fh.read()
        missing = [entry for entry in entries if entry not in existing]
        if missing:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                for entry in missing:
                    fh.write(f"\n{entry}\n")
    except OSError:
        pass
