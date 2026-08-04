"""Handle git operations and repository-specific commit conventions.

Inspect recent commit subjects before composing a message. Do not force a
typed convention on a repository that uses plain imperative subjects.
"""

import os
import re
import subprocess

GIT_TIMEOUT = 120

# Sample size for convention detection. Too few samples are noisy; too many
# make the prompt unnecessarily large.
CONVENTION_SAMPLE = 20

CONVENTIONAL = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(\([^)]+\))?!?: .+")


class GitError(RuntimeError):
    pass


def run(args: list[str], cwd: str, keep_trailing: bool = False) -> tuple[int, str]:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    merged = done.stdout + done.stderr
    return done.returncode, merged if keep_trailing else merged.strip()


def is_repo(root: str) -> bool:
    code, out = run(["rev-parse", "--is-inside-work-tree"], root)
    return code == 0 and out == "true"


def status(root: str) -> dict:
    code, out = run(["status", "--porcelain"], root)
    if code != 0:
        raise GitError(out[:300])
    staged, unstaged, untracked = [], [], []
    for line in out.splitlines():
        if len(line) < 3:
            continue
        index, tree, path = line[0], line[1], line[3:]
        if index == "?" and tree == "?":
            untracked.append(path)
            continue
        if index != " ":
            staged.append(path)
        if tree != " ":
            unstaged.append(path)
    _, branch = run(["rev-parse", "--abbrev-ref", "HEAD"], root)
    return {"branch": branch, "staged": staged, "unstaged": unstaged,
            "untracked": untracked,
            "clean": not (staged or unstaged or untracked)}


def diff(root: str, staged: bool = True, max_chars: int = 24_000) -> str:
    """Return the review diff used to compose a commit message."""
    args = ["diff", "--cached"] if staged else ["diff"]
    code, out = run(args, root, keep_trailing=True)
    if code != 0:
        return ""
    if len(out) > max_chars:
        return out[:max_chars] + "\n...[diff가 길어 일부만 표시]"
    return out


def stage(root: str, paths: list[str] | None = None) -> dict:
    code, out = run(["add", "--", *paths] if paths else ["add", "-A"], root)
    if code != 0:
        return {"error": out[:300]}
    return {"staged": status(root)["staged"]}


def recent_subjects(root: str, limit: int = CONVENTION_SAMPLE) -> list[str]:
    code, out = run(["log", f"-{limit}", "--format=%s"], root)
    if code != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]


def detect_convention(root: str) -> dict:
    """Detect the commit convention used by the repository.

    Use a typed convention only when most recent subjects follow it; otherwise
    use a plain imperative subject.
    """
    subjects = recent_subjects(root)
    if not subjects:
        return {"style": "unknown", "samples": [], "max_subject": 72,
                "conventional_ratio": 0.0}

    hits = sum(1 for s in subjects if CONVENTIONAL.match(s))
    ratio = hits / len(subjects)
    lengths = sorted(len(s) for s in subjects)
    typical = lengths[len(lengths) // 2]
    return {"style": "conventional" if ratio >= 0.5 else "plain",
            "conventional_ratio": round(ratio, 2),
            "samples": subjects[:8],
            "max_subject": max(50, min(72, typical + 20))}


def commit(root: str, message: str, allow_empty: bool = False) -> dict:
    if not message.strip():
        return {"error": "커밋 메시지가 비어 있습니다"}
    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    code, out = run(args, root)
    if code != 0:
        return {"error": out[:400]}
    _, sha = run(["rev-parse", "--short", "HEAD"], root)
    return {"sha": sha, "message": message.splitlines()[0]}


def create_branch(root: str, name: str) -> dict:
    code, out = run(["switch", "-c", name], root)
    if code != 0:
        return {"error": out[:300]}
    return {"branch": name}


def switch_branch(root: str, name: str) -> dict:
    """Switch to a local branch, creating it from HEAD when it is absent."""
    if not name.strip():
        return {"error": "브랜치 이름이 비어 있습니다"}
    code, out = run(["check-ref-format", "--branch", name], root)
    if code != 0:
        return {"error": out[:300] or f"잘못된 브랜치 이름: {name}"}

    exists, _ = run(["show-ref", "--verify", "--quiet",
                     f"refs/heads/{name}"], root)
    action = "switched" if exists == 0 else "created"
    args = ["switch", name] if exists == 0 else ["switch", "-c", name]
    code, out = run(args, root)
    if code != 0:
        return {"error": out[:300]}
    return {"branch": name, "action": action}


def push(root: str, branch: str | None = None,
         set_upstream: bool = True) -> dict:
    branch = branch or status(root)["branch"]
    args = ["push"]
    if set_upstream:
        args += ["-u", "origin", branch]
    code, out = run(args, root)
    if code != 0:
        return {"error": out[:400]}
    return {"pushed": branch}


def build_commit_prompt(root: str, diff_text: str) -> str:
    """Build a commit-message prompt containing repository examples."""
    convention = detect_convention(root)
    lines = ["아래 staged diff에 대한 커밋 메시지를 작성하십시오.", ""]

    if convention["samples"]:
        lines.append("이 저장소의 최근 커밋 제목:")
        lines += [f"  {s}" for s in convention["samples"]]
        lines.append("")

    if convention["style"] == "conventional":
        lines.append("이 저장소는 Conventional Commits를 씁니다. "
                     "`type(scope): summary` 형식을 따르십시오.")
    elif convention["style"] == "plain":
        lines.append("이 저장소는 Conventional Commits를 쓰지 않습니다. "
                     "`feat:` 같은 접두사를 붙이지 마십시오.")

    lines += [
        f"제목은 명령형으로 {convention['max_subject']}자 이내, 마침표 없이 씁니다.",
        "변경 이유가 제목만으로 분명하지 않을 때만 빈 줄 뒤에 본문을 답니다.",
        "diff에 없는 내용을 쓰지 말고, 테스트 통과 여부를 임의로 주장하지 마십시오.",
        "커밋 메시지만 출력하고 설명이나 코드 블록은 붙이지 마십시오.",
        "", "--- staged diff ---", diff_text]
    return "\n".join(lines)


def clean_message(text: str) -> str:
    """Keep only the commit message from model output."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            body = parts[1]
            if "\n" in body:
                first, rest = body.split("\n", 1)
                # Drop an informational fence label such as ```text.
                body = rest if first.strip().isalpha() else body
            text = body.strip()
    for prefix in ("커밋 메시지:", "Commit message:", "메시지:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text.strip()
