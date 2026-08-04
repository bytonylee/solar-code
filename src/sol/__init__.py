"""sol: an agent runtime for the Solar model.

Keep model behavior in measured configuration, stabilize tool specifications in
the cache prefix, and compact conversation history without breaking tool pairs.
"""

from .cassette import AUTO, RECORD, REPLAY, Cassette, CassetteMiss, wrap
from .client import SolarError, Turn, complete
from .checkpoint import Checkpoint, CheckpointLog, Conflict
from .context import build_system, describe_environment, load_agents, load_skills
from .determinism import canonicalize, coerce_number, same, stable_hash
from .git import detect_convention, is_repo
from .hooks import ShellHooks
from .loop import Agent, Episode, Event
from .model import (Effort, ModelSettings, SUPPORTED_MODELS, select,
                    settings)
from .permissions import MODES as PERMISSION_MODES, PermissionPolicy
from .paths import solar_home
from .prefix import CacheLedger, PrefixBroken, StablePrefix
from .processors import Block, Chain, PathGuard, Retry, SecretRedactor
from .recall import Index, index_sessions, recall_tool
from .session import Session
from .status import Usage, diagnose, render
from .subagent import ROLES, delegate, delegate_tool, spawn, spawn_tool
from .tools import Registry, Tool
from .tracker import Tracker
from .vcs import write_commit_message, write_issue, write_pr
from .workspace import Workspace, WorkspaceFactory
from .worktree import Worktree, WorktreeError

__version__ = "0.0.1"

__all__ = ["AUTO", "RECORD", "REPLAY", "ROLES", "Agent", "Block", "CacheLedger",
           "Cassette", "CassetteMiss", "Chain", "Checkpoint", "CheckpointLog",
           "Conflict", "Effort", "Episode", "Event", "Index", "PathGuard",
           "PERMISSION_MODES", "PermissionPolicy",
           "PrefixBroken", "Registry", "Retry", "SecretRedactor", "Session",
           "ShellHooks", "SolarError", "StablePrefix", "Tool", "Tracker",
           "Turn", "Usage", "Workspace", "WorkspaceFactory", "Worktree",
           "WorktreeError", "ModelSettings", "SUPPORTED_MODELS", "build_system",
           "canonicalize", "coerce_number",
           "complete", "delegate", "delegate_tool", "describe_environment",
           "detect_convention", "diagnose", "index_sessions", "is_repo",
           "load_agents", "load_skills", "recall_tool", "render", "same",
           "solar_home",
           "spawn", "spawn_tool", "stable_hash", "wrap",
           "select", "settings", "write_commit_message", "write_issue",
           "write_pr"]
