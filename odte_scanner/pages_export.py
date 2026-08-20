"""Export a static ZeroLoss desk for GitHub Pages (read-only snapshot)."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STATIC_BOOT = """
<script>
  window.SIGNAL_DESK_STATIC = true;
  window.SIGNAL_DESK_DATA_URL = "./data/snapshot.json";
</script>
"""


def _static_html(page: str) -> str:
    html = page.replace("<head>", "<head>\n" + STATIC_BOOT, 1)
    html = html.replace(
        'const res = await fetch("/api/snapshot", { signal: ctrl.signal });',
        'const res = await fetch((window.SIGNAL_DESK_DATA_URL||"./data/snapshot.json") + "?t=" + Date.now(), { signal: ctrl.signal, cache: "no-store" });',
    )
    html = html.replace(
        "    async function runScan(mode) {\n",
        "    async function runScan(mode) {\n"
        "      if (window.SIGNAL_DESK_STATIC) {\n"
        '        const note = document.getElementById("loadNote");\n'
        '        note.style.display = "block";\n'
        '        note.textContent = "Scan is disabled on GitHub Pages — Actions re-publishes ZeroLoss on a schedule. Use workflow_dispatch to refresh now.";\n'
        "        return;\n"
        "      }\n",
        1,
    )
    html = html.replace(
        "    async function syncWebull() {\n",
        "    async function syncWebull() {\n"
        "      if (window.SIGNAL_DESK_STATIC) {\n"
        '        const note = document.getElementById("loadNote");\n'
        "        if (note) {\n"
        '          note.style.display = "block";\n'
        '          note.textContent = "Webull sync needs a live Flask host (Railway/Render), not Pages.";\n'
        "        }\n"
        "        return;\n"
        "      }\n",
    )
    # Soften refresh cadence on static host (snapshot file only changes when Actions publishes)
    html = html.replace(
        "    setInterval(loadAll, 60000);",
        "    setInterval(loadAll, window.SIGNAL_DESK_STATIC ? 180000 : 60000);",
    )
    # Badge after lede via small DOM hook at start of paint/load
    html = html.replace(
        "    async function loadAll() {\n"
        '      const note = document.getElementById("loadNote");\n'
        '      note.style.display = "block";\n'
        '      note.textContent = "Refreshing…";\n',
        "    async function loadAll() {\n"
        "      if (window.SIGNAL_DESK_STATIC && !document.getElementById('pagesHostBadge')) {\n"
        "        const lede = document.querySelector('.lede');\n"
        "        if (lede) {\n"
        "          const b = document.createElement('div');\n"
        "          b.id = 'pagesHostBadge';\n"
        "          b.style.cssText = 'color:var(--accent);font-size:.85rem;margin:.2rem 0 .6rem;';\n"
        "          b.textContent = 'ZeroLoss on GitHub Pages · auto-scan via Actions · read-only snapshot · not only-winners';\n"
        "          lede.insertAdjacentElement('afterend', b);\n"
        "        }\n"
        "      }\n"
        '      const note = document.getElementById("loadNote");\n'
        '      note.style.display = "block";\n'
        '      note.textContent = "Refreshing…";\n',
    )
    # Challenge POST helpers — no-op on static
    html = html.replace(
        'const r = await fetch("/api/challenge/exit"',
        'if (window.SIGNAL_DESK_STATIC) { alert("Paper challenge needs a live host"); return; }\n'
        '              const r = await fetch("/api/challenge/exit"',
    )
    html = html.replace(
        'const r = await fetch("/api/challenge/enter"',
        'if (window.SIGNAL_DESK_STATIC) { alert("Paper challenge needs a live host"); return; }\n'
        '              const r = await fetch("/api/challenge/enter"',
    )
    return html


def export_pages(
    out_dir: str | Path = "site",
    config_path: str | None = None,
) -> Path:
    """Build site/index.html + site/data/snapshot.json from disk scan (offline)."""
    from odte_scanner.ui import PAGE, create_app

    out = Path(out_dir)
    if not out.is_absolute():
        out = ROOT / out
    data_dir = out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    os.environ["SIGNAL_DESK_OFFLINE"] = "1"
    app = create_app(config_path)
    with app.test_client() as client:
        res = client.get("/api/snapshot?offline=1")
        if res.status_code != 200:
            raise RuntimeError(f"snapshot export failed: HTTP {res.status_code} {res.data[:500]!r}")
        payload = res.get_json()
        if not isinstance(payload, dict):
            raise RuntimeError("snapshot export returned non-JSON object")

    (data_dir / "snapshot.json").write_text(json.dumps(payload, indent=2, default=str))
    scan_src = ROOT / "outputs" / "latest_scan.json"
    (data_dir / "latest_scan.json").write_text(scan_src.read_text() if scan_src.exists() else "{}")

    # Copy paper ledgers into static site (gitignored outputs/ otherwise vanish on Pages)
    ledgers_dir = data_dir / "ledgers"
    ledgers_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "signal_journal.json",
        "recommendation_log.json",
        "challenge_ledger.json",
        "odte_1k_ledger.json",
        "paper_ledger.json",
        "webull_orders.json",
        "ui_snapshot_cache.json",
    ):
        src = ROOT / "outputs" / name
        if src.exists():
            (ledgers_dir / name).write_text(src.read_text())

    (out / "index.html").write_text(_static_html(PAGE))
    (out / ".nojekyll").write_text("")
    insights = payload.get("insights") or {}
    acts = payload.get("actions") or {}
    meta = {
        "generated_at": payload.get("generated_at"),
        "offline": True,
        "scores": len(payload.get("scores") or []),
        "universe_mode": payload.get("universe_mode"),
        "sell_now": (acts.get("counts") or {}).get("sell_now"),
        "buy_now_puts": (acts.get("counts") or {}).get("buy_now_puts"),
        "just_exited": len(acts.get("just_exited") or []),
        "closed_journal": len(insights.get("closed_trades") or []),
        "zeroloss_dnm": len(((payload.get("zeroloss") or {}).get("do_not_miss") or [])),
        "url_hint": "https://2100preet.github.io/zeroloss/",
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    return out
