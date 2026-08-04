"""Track task plans and completion state across an episode.

Persist plans outside compacted conversation history. Project trackers use JSONL;
session-owned trackers use the session log. Keep task contents out of the system
prefix so status changes do not invalidate it.
"""

import json
import os
import time
import uuid

TRACKER_FILE = ".sol/tasks.jsonl"

OPEN = "open"
DOING = "doing"
DONE = "done"
BLOCKED = "blocked"
STATES = (OPEN, DOING, DONE, BLOCKED)

MARKS = {OPEN: "[ ]", DOING: "[~]", DONE: "[x]", BLOCKED: "[!]"}


class Tracker:
    """Store an append-only task list with state changes as new records."""

    def __init__(self, root: str, path: str | None = None, session=None,
                 isolated: bool = False) -> None:
        self.root = os.path.realpath(root)
        self.session = session
        self.isolated = isolated and session is None
        self.path = path if path is not None else os.path.join(self.root,
                                                               TRACKER_FILE)
        self.items: dict[str, dict] = {}
        self._order: list[str] = []
        self._load()

    def _load(self) -> None:
        if self.session is not None:
            for record in self.session.records():
                if record.get("type") not in ("task", "task_update"):
                    continue
                self._apply_session_record(record)
            return
        if self.isolated or not self.path:
            return
        if not os.path.isfile(self.path):
            return
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    break
                self._apply_record(record)

    def _apply_record(self, record: dict) -> None:
        """Apply one project-log record to the current state."""
        item_id = record.get("id")
        if not item_id:
            return
        if item_id not in self.items:
            self._order.append(item_id)
            self.items[item_id] = record
        else:
            self.items[item_id].update(record)

    def _apply_session_record(self, record: dict) -> None:
        """Apply a task or task-update entry from a session JSONL log."""
        item_id = record.get("task_id")
        if not item_id:
            return
        if record.get("type") == "task":
            item = {"id": item_id,
                    "title": record.get("title", ""),
                    "state": record.get("state", OPEN),
                    "note": record.get("note", ""),
                    "at": record.get("created_at", record.get("at", 0))}
        else:
            item = {"id": item_id}
            if "state" in record:
                item["state"] = record["state"]
            if "note" in record:
                item["note"] = record["note"]
            item["at"] = record.get("at", 0)
        self._apply_record(item)

    def _write(self, record: dict) -> None:
        if self.isolated or not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _persist(self, kind: str, record: dict) -> None:
        """Persist a task change at the active storage boundary."""
        if self.session is None:
            self._write(record)
            return

        payload = {"task_id": record["id"]}
        if kind == "task":
            payload.update({"title": record["title"],
                            "state": record["state"],
                            "note": record.get("note", ""),
                            "created_at": record.get("at", time.time())})
        else:
            if "state" in record:
                payload["state"] = record["state"]
            if "note" in record:
                payload["note"] = record["note"]
        self.session.append(kind, payload)

    def reload(self) -> None:
        """Reload task state from the current storage boundary."""
        self.items.clear()
        self._order.clear()
        self._load()

    def add(self, title: str, note: str = "") -> dict:
        item_id = uuid.uuid4().hex[:8]
        record = {"id": item_id, "title": title, "state": OPEN,
                  "note": note, "at": time.time()}
        self.items[item_id] = record
        self._order.append(item_id)
        self._persist("task", record)
        return record

    def update(self, item_id: str, state: str = "", note: str = "") -> dict:
        if item_id not in self.items:
            return {"error": f"알 수 없는 작업: {item_id}"}
        if state and state not in STATES:
            return {"error": f"상태는 {', '.join(STATES)} 중 하나여야 합니다"}
        record = {"id": item_id, "at": time.time()}
        if state:
            record["state"] = state
        if note:
            record["note"] = note
        self.items[item_id].update(record)
        self._persist("task_update", record)
        return self.items[item_id]

    def plan(self, titles: list[str]) -> list[dict]:
        """Register several tasks at once for planning."""
        return [self.add(title) for title in titles]

    def list(self, state: str = "") -> list[dict]:
        found = [self.items[i] for i in self._order]
        if state:
            found = [i for i in found if i.get("state") == state]
        return found

    @property
    def pending(self) -> list[dict]:
        return [i for i in self.list() if i.get("state") in (OPEN, DOING)]

    def summary(self) -> dict:
        counts = {state: 0 for state in STATES}
        for item in self.list():
            counts[item.get("state", OPEN)] = counts.get(item.get("state", OPEN), 0) + 1
        return {"total": len(self.items), **counts}

    def render(self) -> str:
        if not self.items:
            return "등록된 작업이 없습니다"
        lines = []
        for item in self.list():
            mark = MARKS.get(item.get("state", OPEN), "[ ]")
            line = f"{mark} {item['id']}  {item['title']}"
            if item.get("note"):
                line += f"\n           {item['note']}"
            lines.append(line)
        counts = self.summary()
        lines.append("")
        lines.append(f"전체 {counts['total']} · 완료 {counts[DONE]} · "
                     f"진행 {counts[DOING]} · 대기 {counts[OPEN]} · "
                     f"막힘 {counts[BLOCKED]}")
        return "\n".join(lines)


def tools(tracker: "Tracker") -> list:
    """Build tracking tools without adding task data to the prefix."""
    from .tools import Tool

    def plan(args: dict) -> dict:
        items = tracker.plan(args["titles"])
        return {"planned": [{"id": i["id"], "title": i["title"]} for i in items]}

    def update(args: dict) -> dict:
        return tracker.update(args["id"], args.get("state", ""),
                              args.get("note", ""))

    def show(args: dict) -> dict:
        return {"items": tracker.list(args.get("state", "")),
                "summary": tracker.summary()}

    return [
        Tool("plan_tasks",
             "여러 단계로 나뉘는 작업을 시작할 때 계획을 등록한다. "
             "한두 단계로 끝나는 일에는 쓰지 않는다.",
             {"type": "object",
              "properties": {"titles": {"type": "array",
                                        "items": {"type": "string"}}},
              "required": ["titles"]}, plan, read_only=False),
        Tool("update_task",
             "작업의 상태를 바꾼다. 시작할 때 doing, 끝나면 done, "
             "진행할 수 없으면 blocked로 표시한다.",
             {"type": "object",
              "properties": {"id": {"type": "string"},
                             "state": {"type": "string", "enum": list(STATES)},
                             "note": {"type": "string"}},
              "required": ["id"]}, update, read_only=False),
        Tool("show_tasks", "등록된 작업과 진행 상황을 확인한다",
             {"type": "object",
              "properties": {"state": {"type": "string", "enum": list(STATES)}}},
             show),
    ]
