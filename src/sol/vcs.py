"""Connect git and GitHub actions to model-generated text.

Commit messages and pull-request bodies are built from diffs rather than task
conversation. Use low-effort generation for concise repository metadata.
"""

from . import client, git, github, model
from .prefix import StablePrefix

COMMIT_SYSTEM = """당신은 커밋 메시지를 작성합니다.

staged diff에 실제로 담긴 변경만 기술합니다. 추측하거나 과장하지 않습니다.
메시지 본문만 출력하고 설명, 머리말, 코드 블록은 붙이지 않습니다."""

PR_SYSTEM = """당신은 Pull Request 설명을 작성합니다.

브랜치의 전체 변경을 근거로 씁니다. 수행하지 않은 검증을 했다고 쓰지 않습니다.
제목과 본문만 출력하고 설명이나 코드 블록은 붙이지 않습니다."""


def write_commit_message(root: str, complete=None) -> dict:
    """Read the staged diff and create a commit message."""
    send = complete or client.complete
    if not git.is_repo(root):
        return {"error": "git 저장소가 아닙니다"}

    state = git.status(root)
    if not state["staged"]:
        return {"error": "staged 변경이 없습니다. 먼저 파일을 스테이지하십시오"}

    diff_text = git.diff(root, staged=True)
    if not diff_text.strip():
        return {"error": "staged diff가 비어 있습니다"}

    turn = send([{"role": "system", "content": COMMIT_SYSTEM},
                 {"role": "user",
                  "content": git.build_commit_prompt(root, diff_text)}],
                effort=model.Effort.OFF)
    if turn.starved:
        return {"error": "모델이 답을 내지 못했습니다"}

    message = git.clean_message(turn.content)
    if not message:
        return {"error": "빈 메시지가 생성되었습니다"}
    return {"message": message, "files": state["staged"],
            "convention": git.detect_convention(root)["style"]}


def write_pr(root: str, base: str = "", complete=None) -> dict:
    """Read the complete branch diff and create a pull-request draft."""
    send = complete or client.complete
    if not git.is_repo(root):
        return {"error": "git 저장소가 아닙니다"}

    base = base or _default_base(root)
    code, out = git.run(["diff", f"{base}...HEAD"], root, keep_trailing=True)
    if code != 0 or not out.strip():
        return {"error": f"{base} 대비 변경이 없습니다"}
    diff_text = out[:24_000]

    _, log = git.run(["log", f"{base}..HEAD", "--format=%s"], root)
    commits = [line for line in log.splitlines() if line.strip()]

    turn = send([{"role": "system", "content": PR_SYSTEM},
                 {"role": "user",
                  "content": github.build_pr_prompt(diff_text, commits, base)}],
                effort=model.Effort.OFF)
    if turn.starved:
        return {"error": "모델이 답을 내지 못했습니다"}

    title, body = github.split_title_body(turn.content)
    if not title:
        return {"error": "빈 제목이 생성되었습니다"}
    return {"title": title, "body": body, "base": base, "commits": commits}


def write_issue(summary: str, context: str = "", complete=None) -> dict:
    """Format an observed problem as an issue."""
    send = complete or client.complete
    prompt = ["아래 내용을 GitHub 이슈로 정리하십시오.", "",
              "형식: 첫 줄은 제목만, 빈 줄 뒤에 본문을 Markdown으로.",
              "본문에는 현상, 재현 방법, 기대 동작을 담습니다.",
              "확인되지 않은 원인을 단정하지 마십시오.", "",
              f"내용: {summary}"]
    if context:
        prompt += ["", "--- 참고 ---", context[:8000]]

    turn = send([{"role": "system", "content": PR_SYSTEM},
                 {"role": "user", "content": "\n".join(prompt)}],
                effort=model.Effort.OFF)
    if turn.starved:
        return {"error": "모델이 답을 내지 못했습니다"}
    title, body = github.split_title_body(turn.content)
    if not title:
        return {"error": "빈 제목이 생성되었습니다"}
    return {"title": title, "body": body}


def _default_base(root: str) -> str:
    """Choose the base branch, preferring ``origin/HEAD`` when available."""
    code, out = git.run(["rev-parse", "--abbrev-ref", "origin/HEAD"], root)
    if code == 0 and "/" in out:
        return out.strip()
    for candidate in ("origin/main", "origin/master", "main", "master"):
        code, _ = git.run(["rev-parse", "--verify", candidate], root)
        if code == 0:
            return candidate
    return "HEAD~1"
