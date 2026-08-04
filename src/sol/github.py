"""Use the GitHub CLI for pull requests and issues.

Keep credentials outside session records and prompts. Report missing installation
or authentication explicitly instead of claiming that an operation succeeded.
"""

import json
import shutil
import subprocess

GH_TIMEOUT = 120


class GitHubError(RuntimeError):
    pass


def _gh(args: list[str], cwd: str, stdin: str | None = None) -> tuple[int, str]:
    if shutil.which("gh") is None:
        return 1, "gh CLI가 설치되어 있지 않습니다 (https://cli.github.com)"
    try:
        done = subprocess.run(["gh", *args], cwd=cwd, input=stdin,
                              capture_output=True, text=True, timeout=GH_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


def available(cwd: str) -> tuple[bool, str]:
    """Check whether the GitHub CLI is installed and authenticated."""
    if shutil.which("gh") is None:
        return False, "gh CLI가 설치되어 있지 않습니다 (https://cli.github.com)"
    code, out = _gh(["auth", "status"], cwd)
    if code != 0:
        return False, "gh 인증이 필요합니다: gh auth login"
    return True, "사용 가능"


def _require(cwd: str) -> None:
    ok, reason = available(cwd)
    if not ok:
        raise GitHubError(reason)


def create_issue(cwd: str, title: str, body: str = "",
                 labels: list[str] | None = None) -> dict:
    _require(cwd)
    args = ["issue", "create", "--title", title, "--body", body or title]
    for label in labels or []:
        args += ["--label", label]
    code, out = _gh(args, cwd)
    if code != 0:
        return {"error": out[:400]}
    return {"url": out.splitlines()[-1].strip()}


def create_pr(cwd: str, title: str, body: str = "", base: str = "",
              draft: bool = True) -> dict:
    """Create a pull request, using draft mode by default."""
    _require(cwd)
    args = ["pr", "create", "--title", title, "--body", body or title]
    if base:
        args += ["--base", base]
    if draft:
        args.append("--draft")
    code, out = _gh(args, cwd)
    if code != 0:
        return {"error": out[:400]}
    return {"url": out.splitlines()[-1].strip(), "draft": draft}


def list_issues(cwd: str, limit: int = 20, state: str = "open",
                assignee: str = "") -> dict:
    _require(cwd)
    args = ["issue", "list", "--limit", str(limit), "--state", state,
            "--json", "number,title,state,labels,updatedAt"]
    if assignee:
        args += ["--assignee", assignee]
    code, out = _gh(args, cwd)
    if code != 0:
        return {"error": out[:400]}
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return {"error": "gh 출력을 해석하지 못했습니다"}
    return {"issues": [{"number": i["number"], "title": i["title"],
                        "state": i["state"],
                        "labels": [l["name"] for l in i.get("labels", [])]}
                       for i in items]}


def read_issue(cwd: str, number: int) -> dict:
    _require(cwd)
    code, out = _gh(["issue", "view", str(number), "--json",
                     "number,title,body,state,comments"], cwd)
    if code != 0:
        return {"error": out[:400]}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"error": "gh 출력을 해석하지 못했습니다"}
    return {"number": data["number"], "title": data["title"],
            "state": data["state"], "body": (data.get("body") or "")[:8000],
            "comments": [c.get("body", "")[:1000]
                         for c in (data.get("comments") or [])[:10]]}


def comment(cwd: str, number: int, body: str, on_pr: bool = False) -> dict:
    _require(cwd)
    kind = "pr" if on_pr else "issue"
    code, out = _gh([kind, "comment", str(number), "--body", body], cwd)
    if code != 0:
        return {"error": out[:400]}
    return {"url": out.splitlines()[-1].strip()}


def build_pr_prompt(diff_text: str, commits: list[str], base: str) -> str:
    """Build a pull-request prompt from the complete branch diff."""
    lines = ["아래 변경에 대한 Pull Request 제목과 본문을 작성하십시오.", ""]
    if commits:
        lines.append("이 브랜치의 커밋:")
        lines += [f"  {c}" for c in commits[:20]]
        lines.append("")
    lines += [
        f"기준 브랜치: {base}" if base else "",
        "",
        "형식:",
        "  첫 줄은 제목만 씁니다. 명령형으로 72자 이내, 마침표 없이.",
        "  빈 줄 뒤에 본문을 Markdown으로 씁니다.",
        "",
        "본문에는 다음을 담습니다:",
        "  ## 요약 - 무엇이 바뀌었는지 2~4개 항목",
        "  ## 배경 - 왜 필요했는지",
        "  ## 검증 - 실제로 수행한 확인만. 하지 않았다면 '미실행: 이유'라고 씁니다.",
        "",
        "diff에 없는 내용을 지어내지 말고, 테스트 통과를 임의로 주장하지 마십시오.",
        "", "--- diff ---", diff_text]
    return "\n".join(line for line in lines if line != "" or True)


def split_title_body(text: str) -> tuple[str, str]:
    """Split a model response into a title and body."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        if len(parts) >= 2:
            cleaned = parts[1].strip()
    if not cleaned:
        return "", ""
    lines = cleaned.splitlines()
    title = lines[0].strip().lstrip("#").strip()
    body = "\n".join(lines[1:]).strip()
    return title, body
