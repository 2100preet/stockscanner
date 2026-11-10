"""Keep Webull credentials out of logs.

The Webull SDK logs whole requests on errors (``json.dumps(vars(request))``)
and token values at INFO, which includes the ``x-app-key`` header, the request
signature, and the access token. On Fly those lines land in the app logs.

``install_webull_log_redaction`` puts a filter on every ``webull.*`` logger,
on their handlers, and on the root handlers. The filter rewrites the record
in place (message, args, and formatted traceback), so every handler that
sees the record afterwards gets the redacted text. SDK loggers are also
raised to WARNING so header dumps at DEBUG/INFO are not emitted at all.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Iterable

MASK = "[REDACTED]"

_SENSITIVE_KEYS = (
    r"x-app-key",
    r"x-signature(?:-nonce)?",
    r"x-access-token",
    r"authorization",
    r"app[_-]?secret",
    r"app[_-]?key",
    r"access[_-]?token",
    r"refresh[_-]?token",
    r"token",
    r"signature",
    r"string_to_sign",
    r"secret",
)
# key: value / key=value / "key": "value" / 'key': 'value' (dict reprs and JSON)
_KV_RE = re.compile(
    r"""(?P<k>["']?(?:%s)["']?\s*[:=]\s*)(?:(?P<q>["'])[^"']*(?P=q)|[^"',\s}\]\)&]+)""" % "|".join(_SENSITIVE_KEYS),
    re.IGNORECASE,
)
# "Bearer xyz" anywhere
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]+")

_SECRET_ENV = ("WEBULL_APP_KEY", "WEBULL_APP_SECRET", "WEBULL_ACCESS_TOKEN")
_extra: set[str] = set()
_lock = threading.Lock()


def _literal_secrets() -> list[str]:
    vals = {str(os.environ.get(name) or "").strip() for name in _SECRET_ENV}
    vals |= _extra
    # Short values would mask ordinary words; real keys are long.
    return sorted((v for v in vals if len(v) >= 8), key=len, reverse=True)


def remember_secret(*values: str | None) -> None:
    """Mask these literal values too (e.g. keys read from config, not env)."""
    with _lock:
        for value in values:
            text = str(value or "").strip()
            if len(text) >= 8:
                _extra.add(text)


def redact(text: str) -> str:
    if not text:
        return text
    out = _KV_RE.sub(lambda m: f"{m.group('k')}{m.group('q') or ''}{MASK}{m.group('q') or ''}", str(text))
    out = _BEARER_RE.sub(lambda m: m.group(1) + MASK, out)
    for secret in _literal_secrets():
        if secret in out:
            out = out.replace(secret, MASK)
    return out


class RedactFilter(logging.Filter):
    """Rewrites the record; never drops it."""

    _fmt = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if getattr(record, "_wb_redacted", False):
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = self._fmt.formatException(record.exc_info)
            except Exception:  # noqa: BLE001
                record.exc_text = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        record._wb_redacted = True
        return True


_FILTER = RedactFilter()


def _add(target: logging.Filterer) -> None:
    if not any(isinstance(f, RedactFilter) for f in getattr(target, "filters", [])):
        target.addFilter(_FILTER)


def _sdk_logger_names(extra: Iterable[str] = ()) -> list[str]:
    names = {"webull", "webull.core", "webull.trade", "webull.data", "webull.core.http.response"}
    names |= {n for n in logging.root.manager.loggerDict if n == "webull" or n.startswith("webull.")}
    names |= set(extra)
    return sorted(names)


def install_webull_log_redaction(*, sdk_level: int = logging.WARNING) -> None:
    """Idempotent. Call after the SDK modules are imported and after app logging is set up."""
    for name in _sdk_logger_names():
        log = logging.getLogger(name)
        _add(log)
        for handler in list(log.handlers):
            _add(handler)
        # The SDK sets some of its loggers to DEBUG at import time.
        if log.level == logging.NOTSET or log.level < sdk_level:
            log.setLevel(sdk_level)
    for handler in list(logging.getLogger().handlers):
        _add(handler)
