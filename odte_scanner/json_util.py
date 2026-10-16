"""Strict JSON helpers — browsers reject NaN/Infinity that Python json.dumps allows."""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def finite_float(value: Any, default: float | None = None) -> float | None:
    """Coerce to a finite float, else ``default`` (usually None)."""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def sanitize_for_json(obj: Any) -> Any:
    """Recursively replace NaN/Inf and non-JSON values so a snapshot can be encoded."""
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Enum):
        return sanitize_for_json(obj.value)
    if isinstance(obj, (set, frozenset)):
        return [sanitize_for_json(v) for v in obj]
    if is_dataclass(obj) and not isinstance(obj, type):
        try:
            return sanitize_for_json(asdict(obj))
        except Exception:  # noqa: BLE001
            return str(obj)
    # numpy / pandas scalars
    if hasattr(obj, "item") and callable(obj.item):
        try:
            return sanitize_for_json(obj.item())
        except Exception:  # noqa: BLE001
            return None
    if isinstance(obj, dict):
        return {str(k): sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_json(v) for v in obj]
    return obj


def dumps_strict(obj: Any, **kwargs: Any) -> str:
    """json.dumps with NaN/Inf stripped; refuse to emit non-standard tokens."""
    clean = sanitize_for_json(obj)
    kwargs.setdefault("allow_nan", False)
    return json.dumps(clean, **kwargs)


def atomic_write_text(path: str | Path, text: str) -> None:
    """Write ``path`` via a same-directory temp file and ``os.replace``.

    A crash mid-write leaves the previous file intact. When the previous
    contents parse, they are copied to ``<name>.bak`` first so a later load
    can fall back to the last good copy.
    """
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        try:
            previous = dest.read_text(encoding="utf-8")
            json.loads(previous)
        except Exception:  # noqa: BLE001
            previous = None
        if previous is not None and previous != text:
            _replace_text(dest.with_name(dest.name + ".bak"), previous)
    _replace_text(dest, text)


def _replace_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _strip_trailing_commas(text: str) -> str:
    """Drop commas that sit immediately before ``}`` or ``]`` outside strings."""
    out: list[str] = []
    in_str = False
    escape = False
    index = 0
    length = len(text)
    while index < length:
        ch = text[index]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            index += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            index += 1
            continue
        if ch == ",":
            look = index + 1
            while look < length and text[look] in " \t\r\n":
                look += 1
            if look < length and text[look] in "}]":
                index += 1
                continue
        out.append(ch)
        index += 1
    return "".join(out)


def _close_truncated(text: str, depth: int = 0) -> str | None:
    """Close a JSON value that was cut off mid-write. Incomplete strings are dropped."""
    if depth > 8:
        return None
    body = text
    while True:
        stack: list[str] = []
        in_str = False
        escape = False
        string_start = 0
        for index, ch in enumerate(body):
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                string_start = index
                continue
            if ch == "{":
                stack.append("}")
            elif ch == "[":
                stack.append("]")
            elif ch in "}]":
                if stack and stack[-1] == ch:
                    stack.pop()
        if not in_str:
            break
        if string_start <= 0:
            return None
        body = body[:string_start]
    body = body.rstrip()
    while body.endswith((",", ":")):
        body = body[:-1].rstrip()
    if not body:
        return None
    # A dangling key ("name") with no value cannot be closed. Drop it.
    stack = []
    in_str = False
    escape = False
    last_string_end = -1
    for index, ch in enumerate(body):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
                last_string_end = index
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    tail = body[last_string_end + 1 :].strip() if last_string_end >= 0 else "x"
    if stack and stack[-1] == "}" and tail == "":
        # body ends on a key. Cut that key off.
        body = body[: body.rfind('"', 0, last_string_end)].rstrip().rstrip(",:").rstrip()
        return _close_truncated(body, depth + 1)
    return body + "".join(reversed(stack))


def salvage_json(text: str) -> Any | None:
    """Parse JSON that was truncated or saved with a trailing comma.

    Complete values already in the text are kept. A cut-off tail is dropped.
    """
    if not text or not str(text).strip():
        return None
    blobs = [text, _strip_trailing_commas(text)]
    seen: set[str] = set()
    for blob in blobs:
        if blob in seen:
            continue
        seen.add(blob)
        try:
            return json.loads(blob)
        except json.JSONDecodeError as exc:
            cut = blob[: exc.pos]
        for _ in range(16):
            closed = _close_truncated(cut)
            if not closed:
                break
            cleaned = _strip_trailing_commas(closed)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                nxt = max(cut.rfind("}"), cut.rfind("]"), cut.rfind(","))
                if nxt <= 0:
                    break
                cut = cut[:nxt]
    return None


def load_json_state(path: str | Path) -> Any | None:
    """Load a state file. On decode failure, keep the bytes and recover.

    The bad text is copied to ``<name>.corrupt-<utc>`` before anything is
    rewritten. Recovery uses a repaired parse of those bytes, then
    ``<name>.bak`` when the live file cannot be repaired. ``None`` means the
    caller should use an empty section — the corrupt bytes stay on disk.
    """
    dest = Path(path)
    if not dest.exists():
        return None
    try:
        text = dest.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("state read failed %s: %s", dest.name, exc)
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("corrupt state %s: %s", dest.name, exc)
    _backup_corrupt(dest, text)
    salvaged = salvage_json(text)
    if salvaged is not None:
        try:
            atomic_write_text(dest, dumps_strict(salvaged, indent=2))
        except Exception as exc:  # noqa: BLE001
            logger.warning("repaired state rewrite failed %s: %s", dest.name, exc)
        return salvaged
    bak = dest.with_name(dest.name + ".bak")
    if bak.exists():
        try:
            restored = json.loads(bak.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("state bak unreadable %s: %s", bak.name, exc)
        else:
            try:
                atomic_write_text(dest, dumps_strict(restored, indent=2))
            except Exception as exc:  # noqa: BLE001
                logger.warning("state bak restore failed %s: %s", dest.name, exc)
            return restored
    return None


def _backup_corrupt(path: Path, text: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = path.with_name(f"{path.name}.corrupt-{stamp}")
    suffix = 0
    while dest.exists():
        suffix += 1
        dest = path.with_name(f"{path.name}.corrupt-{stamp}-{suffix}")
    try:
        _replace_text(dest, text)
    except OSError as exc:
        logger.warning("corrupt backup failed %s: %s", path.name, exc)
