"""
session_state.py — session state object and persistence.

The session state is the shared contract between the Flask backend and the
MCP server. Flask owns writes; the MCP server reads it on every tool call.
"""

import json
from pathlib import Path

SESSION_PATH = Path(__file__).parent.parent / "data" / "session_state.json"

# Schema for v1. Fields beyond current_book_id and current_page are empty
# placeholders preserved for future features:
#   student_context — learning style, comfort level (future persona/memory upgrade)
#   persona         — professor personality (future persona upgrade)
#   session_notes   — running scratchpad across tool calls (future memory upgrade)
_DEFAULTS = {
    "current_book_id": None,
    "current_page": 1,
    "student_context": "",
    "persona": "",
    "session_notes": "",
}


def load() -> dict:
    if SESSION_PATH.exists():
        with open(SESSION_PATH, encoding="utf-8") as f:
            data = json.load(f)
        # Backfill any keys added after initial creation
        return {**_DEFAULTS, **data}
    return dict(_DEFAULTS)


def save(state: dict) -> None:
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSION_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def update(state: dict, **kwargs) -> dict:
    """Return a new state dict with kwargs applied, and persist it."""
    updated = {**state, **kwargs}
    save(updated)
    return updated
