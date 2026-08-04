"""Persist sessions as a tree that supports rewind and branching.

Each JSONL entry points to its parent, so a new path can start from an earlier
turn without destroying the original history. A crash can lose only the final
partial line, while stable system prefixes remain unchanged across branches.

A new session stays in memory until its first entry is appended. A run that
never receives input leaves no file behind and therefore has no ID to report,
while every session that records something is written and can be resumed by ID.
"""

import json
import os
import shutil
import time
import uuid

from .paths import sessions_dir

SCHEMA_VERSION = 1


def _now() -> float:
    return time.time()


def default_root() -> str:
    return sessions_dir()


class Session:
    """Append-only entry tree with rewind and branching."""

    def __init__(self, session_id: str, path: str, cwd: str = "") -> None:
        self.id = session_id
        self.path = path
        self.cwd = cwd
        self.entries: dict[str, dict] = {}
        self.leaf: str | None = None
        self._order: list[str] = []
        # Header held until the first entry arrives; None once it is on disk.
        self._pending_header: dict | None = None

    @property
    def saved(self) -> bool:
        """Return whether this session has been written to disk."""
        return self._pending_header is None

    # --- Creation and loading -----------------------------------------

    @classmethod
    def create(cls, root: str | None = None, cwd: str = "") -> "Session":
        root = root or default_root()
        session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        session = cls(session_id, os.path.join(root, f"{session_id}.jsonl"), cwd)
        # Hold the header so an unused session leaves no file and no ID.
        session._pending_header = {"type": "header", "version": SCHEMA_VERSION,
                                   "id": session_id, "cwd": cwd, "at": _now()}
        return session

    @classmethod
    def load(cls, path: str) -> "Session":
        session: "Session" | None = None
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # The final line may be truncated by a crash; earlier data is valid.
                    break
                if record.get("type") == "header":
                    session = cls(record["id"], path, record.get("cwd", ""))
                    continue
                if session is None:
                    continue
                session.entries[record["id"]] = record
                session._order.append(record["id"])
                if record.get("type") != "label":
                    session.leaf = record["id"]
        if session is None:
            raise ValueError(f"no header in session file: {path}")
        return session

    @classmethod
    def latest(cls, root: str | None = None, cwd: str = "") -> "Session | None":
        root = root or default_root()
        if not os.path.isdir(root):
            return None
        files = sorted((f for f in os.listdir(root) if f.endswith(".jsonl")),
                       reverse=True)
        for name in files:
            try:
                session = cls.load(os.path.join(root, name))
            except (OSError, ValueError):
                continue
            if not cwd or session.cwd == cwd:
                return session
        return None

    @classmethod
    def find(cls, session_id: str, root: str | None = None,
             cwd: str = "") -> "Session | None":
        """Load an exact session ID without allowing path traversal."""
        root = root or default_root()
        if not session_id or os.path.basename(session_id) != session_id:
            return None
        path = os.path.join(root, f"{session_id}.jsonl")
        try:
            session = cls.load(path)
        except (OSError, ValueError):
            return None
        if session.id != session_id or (cwd and session.cwd != cwd):
            return None
        return session

    # --- Recording -----------------------------------------------------

    def _write(self, record: dict) -> None:
        header = self._pending_header
        if header is not None:
            # Clear the pending header only after it reaches disk, so a failed
            # first write cannot leave later entries in a headerless file.
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(header, ensure_ascii=False) + "\n")
            self._pending_header = None
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append(self, kind: str, payload: dict) -> str:
        entry_id = uuid.uuid4().hex[:12]
        record = {"id": entry_id, "parent": self.leaf, "type": kind,
                  "at": _now(), **payload}
        self.entries[entry_id] = record
        self._order.append(entry_id)
        self.leaf = entry_id
        self._write(record)
        return entry_id

    def add_message(self, message: dict) -> str:
        return self.append("message", {"message": message})

    def label(self, name: str) -> str:
        """Name the current point so it can be revisited later."""
        return self.append("label", {"name": name})

    # --- Reading -------------------------------------------------------

    def branch(self, leaf: str | None = None) -> list[dict]:
        """Return messages on the path from the current leaf to the root."""
        chain: list[dict] = []
        for record in reversed(self.records(leaf)):
            if record.get("type") == "message":
                chain.append(record["message"])
            elif record.get("type") == "compaction":
                # Replace the history before a compaction point with its summary.
                chain.append({"role": "assistant",
                              "content": f"[이전 대화 요약]\n{record['summary']}"})
                break
        chain.reverse()
        return chain

    def records(self, leaf: str | None = None) -> list[dict]:
        """Return entries through the current leaf in root order.

        Include session state such as task plans and return only entries on the
        selected branch after a rewind.
        """
        chain: list[dict] = []
        cursor = leaf if leaf is not None else self.leaf
        while cursor:
            record = self.entries.get(cursor)
            if record is None:
                break
            chain.append(record)
            cursor = record.get("parent")
        chain.reverse()
        return chain

    def rewind(self, target: str) -> None:
        """Move the leaf backward; later appends branch from that point."""
        if target not in self.entries:
            raise KeyError(f"unknown entry: {target}")
        self.leaf = target
        self.append("rewind", {"to": target})

    def rewind_to_label(self, name: str) -> None:
        for entry_id in reversed(self._order):
            record = self.entries[entry_id]
            if record.get("type") == "label" and record.get("name") == name:
                self.rewind(record["parent"] or entry_id)
                return
        raise KeyError(f"unknown label: {name}")

    # --- Branching and replay -----------------------------------------

    def fork(self, at: str | None = None, root: str | None = None) -> "Session":
        """Create a new session by copying the path through a selected entry.

        Leave the original untouched and exclude entries from sibling branches so
        the child context cannot inherit unrelated failed attempts.
        """
        at = at or self.leaf
        path_ids: list[str] = []
        cursor = at
        while cursor:
            path_ids.append(cursor)
            record = self.entries.get(cursor)
            if record is None:
                break
            cursor = record.get("parent")
        path_ids.reverse()

        child = Session.create(root=root or os.path.dirname(self.path),
                               cwd=self.cwd)
        child.append("fork", {"from": self.id, "at": at or ""})
        for entry_id in path_ids:
            record = self.entries.get(entry_id)
            if record is None or record.get("type") in ("rewind", "fork"):
                continue
            payload = {k: v for k, v in record.items()
                       if k not in ("id", "parent", "type", "at")}
            child.append(record["type"], payload)
        return child

    def replay(self, at: str | None = None) -> list[dict]:
        """Return the messages sent through a selected entry.

        ``branch()`` prepares a new path, while ``replay()`` returns the recorded
        messages for inspection or re-execution.
        """
        return self.branch(at)

    def timeline(self) -> list[dict]:
        """Return a timeline summary used to choose rewind points."""
        out = []
        for entry_id in self._order:
            record = self.entries[entry_id]
            item = {"id": entry_id, "type": record["type"], "at": record["at"]}
            if record["type"] == "message":
                message = record["message"]
                item["role"] = message.get("role")
                content = str(message.get("content") or "")
                item["preview"] = content[:80]
                if message.get("tool_calls"):
                    item["tools"] = [c["function"]["name"]
                                     for c in message["tool_calls"]]
            elif record["type"] == "label":
                item["name"] = record.get("name")
            out.append(item)
        return out

    def record_compaction(self, summary: str, saved: int) -> str:
        return self.append("compaction", {"summary": summary, "saved": saved})

    def labels(self) -> list[str]:
        return [self.entries[i]["name"] for i in self._order
                if self.entries[i].get("type") == "label"]
