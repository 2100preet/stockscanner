"""Public build identity for /api/version. No secrets."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git_head() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.strip()


def version_payload() -> dict[str, str | None]:
    """Git SHA and deploy time. Environment wins over a local checkout."""
    sha = (os.environ.get("GIT_SHA") or os.environ.get("SOURCE_COMMIT") or "").strip()
    if not sha or sha == "unknown":
        sha = _git_head()
    deployed = (os.environ.get("DEPLOYED_AT") or os.environ.get("DEPLOY_TIME") or "").strip()
    return {
        "sha": sha or "unknown",
        "deployed_at": deployed or None,
        "service": "stockscanner",
    }
