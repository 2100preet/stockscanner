"""JSON state files for one desk.

Load refuses a file tagged for a different desk and does not rewrite it.
There is no process-wide lock and no shared counter in this module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from odte_scanner.json_util import atomic_write_text, load_json_state


class DeskStateError(RuntimeError):
    """The path belongs to a different desk. It was left untouched."""


def load_desk_state(path: str | Path, desk: str) -> dict[str, Any] | None:
    """Return the saved book, or ``None`` when the file is missing.

    A file whose ``desk`` tag is something else raises ``DeskStateError``
    without writing.
    """
    dest = Path(path)
    if not dest.exists():
        return None
    raw = load_json_state(dest)
    if not isinstance(raw, dict):
        return None
    tag = raw.get("desk")
    if tag != desk:
        raise DeskStateError(f"{dest} belongs to {tag or 'another book'}, not {desk}")
    return raw


def save_desk_state(path: str | Path, payload: dict[str, Any]) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(dest, json.dumps(payload, indent=2, default=str))
