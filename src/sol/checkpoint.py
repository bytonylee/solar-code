"""Save pre-edit state and provide guarded rollback.

The important behavior is conflict detection. If a file changes after a
checkpoint, rollback would remove that later work, so compare saved and current
digests before restoring it. Writes use a temporary file and rename to avoid
leaving a partially written original after a crash.
"""

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field

MISSING = "\0missing"


def _hash(content: str | None) -> str:
    payload = MISSING if content is None else content
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class FileSnapshot:
    path: str
    content: str | None
    digest: str

    @classmethod
    def capture(cls, root: str, path: str) -> "FileSnapshot":
        full = os.path.join(root, path)
        try:
            with open(full, encoding="utf-8") as fh:
                content = fh.read()
        except (OSError, UnicodeDecodeError):
            content = None
        return cls(path, content, _hash(content))

    def to_dict(self) -> dict:
        return {"path": self.path, "content": self.content, "digest": self.digest}

    @classmethod
    def from_dict(cls, data: dict) -> "FileSnapshot":
        return cls(data["path"], data["content"], data["digest"])


@dataclass
class Checkpoint:
    """Represent one file change with before and after snapshots."""

    id: str
    at: float
    label: str
    before: list[FileSnapshot] = field(default_factory=list)
    after: list[FileSnapshot] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "at": self.at, "label": self.label,
                "before": [s.to_dict() for s in self.before],
                "after": [s.to_dict() for s in self.after]}

    @classmethod
    def from_dict(cls, data: dict) -> "Checkpoint":
        return cls(data["id"], data["at"], data.get("label", ""),
                   [FileSnapshot.from_dict(s) for s in data["before"]],
                   [FileSnapshot.from_dict(s) for s in data["after"]])

    @property
    def paths(self) -> list[str]:
        return [s.path for s in self.before]


class Conflict(RuntimeError):
    """A file changed after the checkpoint and rollback could lose that work."""


class CheckpointLog:
    """Stack of checkpoints, undone from newest to oldest."""

    def __init__(self, root: str, path: str | None = None) -> None:
        self.root = os.path.realpath(root)
        self.path = path or os.path.join(self.root, ".sol", "checkpoints.jsonl")
        self.entries: list[Checkpoint] = []
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.entries.append(Checkpoint.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError):
                    break

    def _persist(self, checkpoint: Checkpoint) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(checkpoint.to_dict(), ensure_ascii=False) + "\n")

    def record(self, paths: list[str], apply, label: str = "") -> Checkpoint:
        """Record a change around an ``apply()`` operation."""
        unique = list(dict.fromkeys(paths))
        before = [FileSnapshot.capture(self.root, p) for p in unique]
        apply()
        after = [FileSnapshot.capture(self.root, p) for p in unique]
        checkpoint = Checkpoint(uuid.uuid4().hex[:12], time.time(), label,
                                before, after)
        self.entries.append(checkpoint)
        self._persist(checkpoint)
        return checkpoint

    def conflicts(self, checkpoint: Checkpoint) -> list[str]:
        """Find external changes that rollback would overwrite."""
        found = []
        for snapshot in checkpoint.after:
            current = FileSnapshot.capture(self.root, snapshot.path)
            if current.digest != snapshot.digest:
                found.append(snapshot.path)
        return found

    def undo(self, checkpoint: Checkpoint | None = None,
             force: bool = False) -> list[str]:
        """Restore the before state, rejecting conflicts unless forced."""
        target = checkpoint or (self.entries[-1] if self.entries else None)
        if target is None:
            raise ValueError("되돌릴 체크포인트가 없습니다")

        clashes = self.conflicts(target)
        if clashes and not force:
            raise Conflict(
                "체크포인트 이후 변경된 파일이 있어 되돌리지 않았습니다: "
                + ", ".join(clashes))

        restored = []
        for snapshot in target.before:
            full = os.path.join(self.root, snapshot.path)
            if snapshot.content is None:
                # The file did not exist before the checkpoint, so remove it.
                if os.path.exists(full):
                    os.remove(full)
                restored.append(snapshot.path)
                continue
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
            temporary = f"{full}.sol-undo-{os.getpid()}"
            try:
                with open(temporary, "w", encoding="utf-8") as fh:
                    fh.write(snapshot.content)
                os.replace(temporary, full)
            except OSError:
                if os.path.exists(temporary):
                    os.remove(temporary)
                raise
            restored.append(snapshot.path)

        if target in self.entries:
            self.entries.remove(target)
        return restored
