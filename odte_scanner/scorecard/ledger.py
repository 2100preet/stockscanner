"""Append-only SHA-256 hash chain for the public scorecard ledger.

Each line is one canonical JSON event. ``entry_hash`` is the SHA-256 of that
event without the hash field, and ``prev_hash`` is the previous line's
``entry_hash``. Rewriting or deleting an earlier line breaks verification.
The writer appends lines and does not rewrite a valid file.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENESIS = "0" * 64

SIGNAL_EVENT = "signal"
EVAL_EVENT = "evaluation"
NOTE_EVENT = "note"


def canonical(obj: Any) -> str:
    """Stable JSON used both on disk and as the hash preimage."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _seal_body(body: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(body)
    sealed.pop("entry_hash", None)
    sealed["entry_hash"] = hashlib.sha256(canonical(sealed).encode("utf-8")).hexdigest()
    return sealed


def seal(seq: int, prev_hash: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _seal_body(
        {
            "seq": int(seq),
            "prev_hash": str(prev_hash),
            "event": str(event),
            "payload": payload,
        }
    )


def append_event(events: list[dict[str, Any]], event: str, payload: dict[str, Any]) -> dict[str, Any]:
    prev = events[-1]["entry_hash"] if events else GENESIS
    seq = int(events[-1]["seq"]) + 1 if events else 1
    sealed = seal(seq, prev, event, payload)
    events.append(sealed)
    return sealed


@dataclass(frozen=True)
class ChainStatus:
    ok: bool
    entries: int
    head: str | None
    broken_at: int | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "entries": self.entries,
            "head": self.head,
            "broken_at": self.broken_at,
            "error": self.error,
        }


def verify_chain(events: list[dict[str, Any]]) -> ChainStatus:
    """Recompute every hash and the link to the previous line."""
    prev = GENESIS
    for index, event in enumerate(events):
        seq = index + 1
        if not isinstance(event, dict):
            return ChainStatus(False, len(events), None, seq, "event is not an object")
        claimed = event.get("entry_hash")
        body = {k: v for k, v in event.items() if k != "entry_hash"}
        expect = hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()
        if claimed != expect:
            return ChainStatus(False, len(events), None, seq, "entry hash does not match its payload")
        if int(event.get("seq") or -1) != seq:
            return ChainStatus(False, len(events), None, seq, "sequence is not contiguous")
        if event.get("prev_hash") != prev:
            return ChainStatus(False, len(events), None, seq, "previous hash link does not match")
        if event.get("event") not in {SIGNAL_EVENT, EVAL_EVENT, NOTE_EVENT}:
            return ChainStatus(False, len(events), None, seq, "unknown event type")
        prev = str(claimed)
    head = events[-1]["entry_hash"] if events else None
    return ChainStatus(True, len(events), head, None, None)


def load_events(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Load a ledger. A JSON failure is tamper evidence, not a line to skip."""
    if not path.exists():
        return [], None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"unreadable ledger: {exc}"
    events: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return events, f"line {line_no} is not valid JSON"
        if not isinstance(obj, dict):
            return events, f"line {line_no} is not an object"
        events.append(obj)
    return events, None


def is_hash_prefix(shorter: list[dict[str, Any]], longer: list[dict[str, Any]]) -> bool:
    if len(shorter) > len(longer):
        return False
    for left, right in zip(shorter, longer):
        if left.get("entry_hash") != right.get("entry_hash"):
            return False
    return True


def extend_file(path: Path, events: list[dict[str, Any]]) -> None:
    """Append canonical lines. Existing bytes are left untouched."""
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = "".join(canonical(event) + "\n" for event in events)
    prefix = ""
    if path.exists() and path.stat().st_size:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                prefix = "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(prefix + blob)
        handle.flush()
        os.fsync(handle.fileno())


def signal_payloads(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != SIGNAL_EVENT:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("id"):
            out.append(payload)
    return out
