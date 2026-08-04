"""Resolve Solar's user-scoped configuration and state directories."""

import os


def solar_home() -> str:
    """Return the Solar home, following Codex's configurable home pattern."""
    configured = os.environ.get("SOLAR_HOME", "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.expanduser("~/.solar")


def user_agents_file() -> str:
    return os.path.join(solar_home(), "AGENTS.md")


def user_skills_dir() -> str:
    return os.path.join(solar_home(), "skills")


def sessions_dir() -> str:
    return os.path.join(solar_home(), "sessions")
