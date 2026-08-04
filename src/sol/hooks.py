"""Run declared external command hooks at tool boundaries.

Commands in `.sol/hooks.json` run at the configured lifecycle points and receive
JSON on standard input. A non-zero exit code blocks the operation.

    {
      "before_tool": [
        {"match": "write_file|edit_file", "command": "./scripts/check.sh"}
      ],
      "after_tool": [
        {"match": "edit_file", "command": "ruff format --stdin-filename x.py"}
      ]
    }

Hooks are trusted commands. They are disabled by default and must be enabled
explicitly for a project.
"""

import json
import os
import re
import subprocess

from .processors import Block

HOOKS_FILE = ".sol/hooks.json"
HOOK_TIMEOUT = 30


class ShellHooks:
    """Run declared external commands at tool boundaries."""

    def __init__(self, root: str, config: dict | None = None) -> None:
        self.root = os.path.realpath(root)
        self.config = config if config is not None else self._load()

    def _load(self) -> dict:
        path = os.path.join(self.root, HOOKS_FILE)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def active(self) -> bool:
        return bool(self.config)

    def _matching(self, event: str, name: str) -> list[dict]:
        found = []
        for entry in self.config.get(event, []):
            pattern = entry.get("match", ".*")
            try:
                # Require an exact match so "read" cannot match "read_file".
                if re.fullmatch(pattern, name):
                    found.append(entry)
            except re.error:
                continue
        return found

    def _run(self, command: str, payload: dict) -> tuple[int, str]:
        try:
            done = subprocess.run(
                command, shell=True, cwd=self.root,
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True, text=True, timeout=HOOK_TIMEOUT)
        except subprocess.TimeoutExpired:
            return 1, f"훅 시간 초과: {command}"
        except OSError as exc:
            return 1, str(exc)
        return done.returncode, (done.stdout + done.stderr).strip()[:2000]

    def before_tool(self, name: str, args: dict) -> dict | Block:
        """Block non-zero exits and pass their output to the model as the reason."""
        for entry in self._matching("before_tool", name):
            code, output = self._run(entry["command"],
                                     {"event": "before_tool", "tool": name,
                                      "args": args})
            if code != 0:
                return Block(output or f"훅이 {name} 실행을 막았습니다")
        return args

    def after_tool(self, name: str, result: str) -> str:
        """Append hook output to a result without replacing the result itself."""
        for entry in self._matching("after_tool", name):
            code, output = self._run(entry["command"],
                                     {"event": "after_tool", "tool": name,
                                      "result": result[:4000]})
            if output:
                label = "훅" if code == 0 else "훅 경고"
                result = f"{result}\n\n[{label}] {output}"
        return result
