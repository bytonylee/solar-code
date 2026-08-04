"""Assemble project instructions and skills into a stable prefix.

Keep the order deterministic, omit timestamps and random values, and skip empty
blocks. List skill names and descriptions first so their full Markdown content
can be read only when needed.
"""

import os

from .paths import user_agents_file, user_skills_dir
from .tools import Tool

AGENTS_FILE = "AGENTS.md"

# Keep instruction text stable for repeated requests. For long files, read only
# the fixed contract at the beginning instead of appending dynamic logs.
AGENTS_MAX_CHARS = 32_000

PROJECT_SKILL_DIRS = (".solar/skills", ".sol/skills", ".agents/skills")
SKILL_MAX_CHARS = 60_000


def find_agents_file(root: str) -> str | None:
    path = os.path.join(root, AGENTS_FILE)
    return path if os.path.isfile(path) else None


def load_agents(root: str) -> str:
    """Load user defaults followed by project-specific instructions."""
    paths = [("사용자 공용 규범", user_agents_file())]
    project_path = find_agents_file(root)
    if project_path and os.path.abspath(project_path) != os.path.abspath(paths[0][1]):
        paths.append(("프로젝트 규범", project_path))

    blocks: list[str] = []
    for label, path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            continue
        if len(text) > AGENTS_MAX_CHARS:
            text = text[:AGENTS_MAX_CHARS] + "\n...[규범 파일이 길어 일부만 표시]"
        if text:
            blocks.append(f"## {label}\n{text}")
    return "\n\n".join(blocks)


# Skill descriptions are the only skill text that enters the stable prefix.
# Keep them short so the prefix remains a search interface, not a manual.
SKILL_DESCRIPTION_MAX = 60


class Skill:
    """Describe one skill whose body can be read on demand."""

    def __init__(self, name: str, description: str, path: str) -> None:
        self.name = name
        self.description = description
        self.path = path


def validate_skill_description(description: str,
                               max_len: int = SKILL_DESCRIPTION_MAX) -> list[str]:
    """Return validation problems for a skill description.

    Descriptions longer than ``max_len`` are not auto-truncated. Callers must
    supply a short summary so the prefix stays stable and high-signal.
    """
    problems: list[str] = []
    text = (description or "").strip()
    if not text:
        problems.append("description is empty")
    if len(text) > max_len:
        problems.append(
            f"description is {len(text)} chars; keep it to {max_len} chars or fewer")
    return problems


def _parse_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta: dict = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip("'\"")
    return meta, text[end + 4:].lstrip()


def load_skills(root: str) -> list[Skill]:
    """Scan user and project skill directories in deterministic order."""
    found: dict[str, Skill] = {}
    directories = [(user_skills_dir(), None)]
    directories.extend((os.path.join(root, relative), root)
                       for relative in PROJECT_SKILL_DIRS)
    for base, relative_to in directories:
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            path = os.path.join(base, entry, "SKILL.md")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    meta, body = _parse_front_matter(fh.read())
            except OSError:
                continue
            description = meta.get("description") or body.split("\n", 1)[0][:120]
            name = meta.get("name") or entry
            display_path = (os.path.relpath(path, relative_to)
                            if relative_to else path)
            found[name] = Skill(name, description, display_path)
    return [found[name] for name in sorted(found)]


def skill_tool(root: str, skills: list[Skill]) -> Tool:
    """Build a read-only tool limited to skills discovered at startup."""
    paths = {
        skill.name: (skill.path if os.path.isabs(skill.path)
                     else os.path.join(root, skill.path))
        for skill in skills
    }

    def read_skill(args: dict) -> dict:
        name = args.get("name", "")
        path = paths.get(name)
        if not path:
            return {"error": f"등록되지 않은 스킬입니다: {name}"}
        try:
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            return {"error": str(exc)}
        result = {"name": name, "path": path, "content": content[:SKILL_MAX_CHARS]}
        if len(content) > SKILL_MAX_CHARS:
            result["truncated"] = True
            result["note"] = f"{len(content)}자 중 앞 {SKILL_MAX_CHARS}자만 표시"
        return result

    return Tool(
        "read_skill",
        "사용 가능한 스킬의 전체 실행 절차를 읽는다. 스킬을 적용하기 전에 호출한다.",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                   "enum": sorted(paths)}},
         "required": ["name"]},
        read_skill,
    )


def build_system_blocks(base: str, root: str, skills: list[Skill] | None = None,
                        agents: str | None = None,
                        environment: str = "") -> list[tuple[str, str]]:
    """Return ordered named blocks that form the system prefix.

    The concatenated text of the returned blocks is identical to ``build_system``.
    Callers that need segment fingerprints use the named blocks; callers that
    only need the prompt keep using ``build_system``.
    """
    blocks: list[tuple[str, str]] = [("base", base.strip())]

    agents_text = load_agents(root) if agents is None else agents
    if agents_text:
        blocks.append((
            "agents",
            "# 사용자 및 프로젝트 규범\n이 규범은 아래의 모든 기본 동작보다 "
            f"우선합니다. 충돌하면 프로젝트 규범이 우선합니다.\n\n{agents_text}",
        ))

    skills = load_skills(root) if skills is None else skills
    if skills:
        lines = ["# 사용 가능한 스킬",
                 "스킬을 적용할 때 read_skill로 전체 절차를 먼저 읽으십시오."]
        for skill in skills:
            lines.append(f"- {skill.name}: {skill.description} ({skill.path})")
        blocks.append(("skills", "\n".join(lines)))

    if environment:
        blocks.append(("environment", f"# 환경\n{environment}"))

    return blocks


def build_system(base: str, root: str, skills: list[Skill] | None = None,
                 agents: str | None = None, environment: str = "",
                 *, return_blocks: bool = False):
    """Assemble the prefix in a deterministic order.

    Stable blocks come before blocks that change more often, allowing an earlier
    cacheable section to survive changes in later sections.

    When ``return_blocks`` is true, return ``(text, blocks)`` where ``blocks`` is
    the list from ``build_system_blocks``. Default remains a plain string so
    existing callers stay compatible.
    """
    blocks = build_system_blocks(base, root, skills=skills, agents=agents,
                                 environment=environment)
    text = "\n\n".join(body for _, body in blocks if body)
    if return_blocks:
        return text, blocks
    return text


def describe_environment(root: str, allow_write: bool, allow_shell: bool) -> str:
    """Return environment details that do not change the cache prefix.

    Exclude timestamps, process IDs, and file listings because they vary on every
    run.
    """
    lines = [f"작업 디렉터리: {root}",
             f"쓰기 권한: {'있음' if allow_write else '없음'}",
             f"명령 실행: {'가능' if allow_shell else '불가'}"]
    if os.path.isdir(os.path.join(root, ".git")):
        lines.append("git 저장소: 예")
    return "\n".join(lines)
