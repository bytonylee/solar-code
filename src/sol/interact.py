"""Ask the user for a decision while an episode is running.

Keep choices structured, inject the interaction channel, and record automatic
selection in non-interactive mode. Limit the number of questions per episode so
the tool does not repeatedly interrupt the user.
"""

import sys
from dataclasses import dataclass, field

MAX_QUESTIONS = 3

# Record automatic selection explicitly so assumptions cannot silently become
# facts in tool output or session history.
AUTO_NOTE = ("비대화형이라 권장안이 자동 선택되었습니다. "
             "이 가정을 최종 답변에 명시하십시오.")


def _valid_options(options: list) -> bool:
    """Validate one recommendation plus one to three alternatives."""
    return 2 <= len(options) <= 4


@dataclass
class Exchange:
    question: str
    options: list[str]
    answer: str
    auto: bool          # Whether the choice was made non-interactively.

    def to_dict(self) -> dict:
        return {"question": self.question, "options": list(self.options),
                "answer": self.answer, "auto": self.auto}


@dataclass
class AskLog:
    """Record questions and answers for reports and diagnostics."""
    exchanges: list[Exchange] = field(default_factory=list)

    @property
    def auto_count(self) -> int:
        return sum(1 for e in self.exchanges if e.auto)

    def to_dict(self) -> dict:
        return {"exchanges": [e.to_dict() for e in self.exchanges],
                "auto": self.auto_count}

    def render(self) -> str:
        """Render a human-readable summary and mark automatic choices."""
        lines = [f"질문 {len(self.exchanges)}회 "
                 f"(자동 선택 {self.auto_count}회)"]
        for exchange in self.exchanges:
            mark = " [자동]" if exchange.auto else ""
            lines.append(f"  Q: {exchange.question}")
            lines.append(f"  A: {exchange.answer}{mark}")
        return "\n".join(lines)


def cli_channel(question: str, options: list[str]) -> str | None:
    """Return a CLI question channel, or ``None`` when interaction is unavailable."""
    if not sys.stdin.isatty():
        return None
    print(f"\n[질문] {question}")
    for index, option in enumerate(options, 1):
        mark = " (권장)" if index == 1 else ""
        print(f"  {index}. {option}{mark}")
    try:
        raw = input("번호 또는 직접 입력 > ").strip()
    except EOFError:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    return raw or options[0]


def ask_user_tool(log: AskLog, channel=None, interactive: bool = True):
    """Build the read-only ``ask_user`` tool.

    ``channel`` receives a question and choices and may return ``None`` for
    automatic selection. Non-interactive mode always chooses the recommendation.
    """
    from .tools import Tool
    ask = channel or cli_channel

    def run(args: dict) -> dict:
        if len(log.exchanges) >= MAX_QUESTIONS:
            return {"error": f"질문 상한({MAX_QUESTIONS})을 넘었습니다. "
                             "지금까지의 답을 근거로 진행하십시오."}
        question = str(args.get("question") or "").strip()
        options = [str(o).strip() for o in args.get("options") or []
                   if str(o).strip()]
        if not question:
            return {"error": "question이 비어 있습니다."}
        if not _valid_options(options):
            return {"error": "options는 2~4개여야 합니다. "
                             "첫 번째 선택지는 권장안이어야 합니다."}
        answer = ask(question, options) if interactive else None
        auto = answer is None
        if auto:
            answer = options[0]
        log.exchanges.append(Exchange(question, options, answer, auto))
        payload = {"answer": answer}
        if auto:
            payload["note"] = AUTO_NOTE
        return payload

    return Tool(
        name="ask_user",
        description=(
            "진행 방향이 갈리는 결정을 사용자에게 묻는다. 조사로 알 수 "
            "있는 것은 묻지 않는다. 되돌리기 비싼 결정(구조 선택, 삭제 "
            "범위, 디자인 방향)에만 쓴다. 첫 번째 선택지는 권장안이다."),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"},
                            "description": "2~4개. 첫 번째가 권장안"}},
            "required": ["question", "options"]},
        run=run, read_only=True)


def render_log(log: AskLog) -> str:
    """Render a CLI status block, or an empty string without exchanges."""
    if not log.exchanges:
        return ""
    return log.render()
