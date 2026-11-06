"""Paths and settings. Keys come from the repo's gitignored .env and never reach the model's context."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent
DOMAIN_LISTS = ROOT / "app" / "data"
OUTPUT_DIR = PACKAGE / "output"
CACHE_DIR = PACKAGE / "cache"

CRUSTDATA_MCP_URL = "https://install.crustdata.com/mcp"
CRUSTDATA_API_URL = "https://api.crustdata.com"
CRUSTDATA_API_VERSION = "2025-11-01"

load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    crustdata_api_key: str
    anthropic_workspace_id: str
    research_model: str     # fills gaps from the web: mostly mechanical, so a lighter model
    research_effort: str
    write_model: str        # writes user.md from everything gathered
    write_effort: str
    max_usd: float
    max_turns: int
    use_cache: bool


def settings(**overrides: object) -> Settings:
    key = os.environ.get("CRUSTDATA_API_KEY", "")
    if not key:
        raise SystemExit("CRUSTDATA_API_KEY is missing from .env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is missing from .env")
    values = {
        "crustdata_api_key": key,
        # Only needed for an Anthropic key that isn't scoped to a workspace.
        "anthropic_workspace_id": os.environ.get("ANTHROPIC_WORKSPACE_ID", ""),
        "research_model": os.environ.get("PROFILE_RESEARCH_MODEL", "claude-sonnet-5-5"),
        "research_effort": os.environ.get("PROFILE_RESEARCH_EFFORT", "low"),
        "write_model": os.environ.get("PROFILE_WRITE_MODEL", "claude-opus-5-5"),
        "write_effort": os.environ.get("PROFILE_WRITE_EFFORT", "medium"),
        "max_usd": float(os.environ.get("PROFILE_MAX_USD", "5")),
        "max_turns": int(os.environ.get("PROFILE_MAX_TURNS", "40")),
        "use_cache": os.environ.get("PROFILE_CACHE", "1") != "0",
    }
    # The pipeline (app/pipeline/react_profiles.py) still passes max_credits; there is no cap any more.
    overrides.pop("max_credits", None)
    values.update({k: v for k, v in overrides.items() if v is not None})
    return Settings(**values)  # type: ignore[arg-type]


def claude_env(config: Settings) -> dict[str, str]:
    """Environment for the Claude Code process: the workspace header, when the key needs one."""
    if not config.anthropic_workspace_id:
        return {}
    return {"ANTHROPIC_CUSTOM_HEADERS": f"anthropic-workspace-id: {config.anthropic_workspace_id}"}


def email_slug(email: str) -> str:
    return re.sub(r"[^a-z0-9@._-]+", "-", email.strip().lower())
