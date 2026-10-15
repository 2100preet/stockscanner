"""Copy the public subscribe pages into a GitHub Pages export."""

from __future__ import annotations

from pathlib import Path

from odte_scanner.billing.pages import render_site
from odte_scanner.billing.stripe_client import public_api_base


def write_subscribe_pages(out_dir: str | Path) -> Path:
    dest = Path(out_dir) / "subscribe"
    dest.mkdir(parents=True, exist_ok=True)
    api = public_api_base()
    for name, html in render_site(api).items():
        (dest / name).write_text(html)
    return dest
