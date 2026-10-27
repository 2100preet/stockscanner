"""Scorecard page and the desk section that links to it."""

from __future__ import annotations

import json
from typing import Any

from odte_scanner.scorecard.service import DISCLAIMER, MOVE_NOTE

SECTION_HTML = f"""
    <section class="tabpane" id="tab-scorecard">
      <h2>Scorecard</h2>
      <p class="lede" id="scorecardDisclaimer">{DISCLAIMER}</p>
      <p class="status" id="scorecardChain">Checking the public ledger…</p>
      <div class="metric-row" id="scorecardMetrics"></div>
      <div class="toolbar" id="scorecardFilters">
        <label class="status">Type
          <select id="scType" class="sc-input">
            <option value="all">All</option>
            <option value="Gold Setup">Gold Setup</option>
            <option value="Golden Flow">Golden Flow</option>
          </select>
        </label>
        <label class="status">Direction
          <select id="scDir" class="sc-input">
            <option value="all">All</option>
            <option value="long">Long</option>
            <option value="short">Short</option>
          </select>
        </label>
        <label class="status">Result
          <select id="scResult" class="sc-input">
            <option value="all">Wins and losses</option>
            <option value="win">Has a win</option>
            <option value="loss">Has a loss</option>
            <option value="pending">Still pending</option>
          </select>
        </label>
        <label class="status">Ticker
          <input id="scTicker" class="sc-input" type="search" placeholder="NVDA" autocomplete="off" />
        </label>
      </div>
      <h3>SPX 0DTE paper structures</h3>
      <p class="lede" id="scorecardSpxNote"></p>
      <div id="scorecardSpx" class="empty">No SPX structure logged yet.</div>
      <div style="overflow-x:auto">
        <table id="scorecardTable"></table>
      </div>
      <p class="lede" id="scorecardFoot">{MOVE_NOTE}</p>
      <p class="lede"><a href="scorecard.html">Public scorecard page</a> · <a id="scorecardLedgerLink" href="data/scorecard_ledger.jsonl">Raw ledger</a> · <a href="https://github.com/2100preet/stockscanner/blob/main/scorecard/ledger.jsonl">Git history</a></p>
    </section>
"""

CLIENT_JS = r"""
    function scEsc(value) {
      return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
        return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch];
      });
    }
    function scMove(horizon) {
      if (!horizon || horizon.status !== "settled" || horizon.return_pct == null) return "pending";
      var n = Number(horizon.return_pct);
      var cls = n > 0 ? "up" : (n < 0 ? "down" : "");
      var sign = n > 0 ? "+" : "";
      return '<span class="' + cls + '">' + sign + n.toFixed(2) + '%</span>';
    }
    function scExcursion(value, status) {
      if (value == null) return "—";
      var n = Number(value);
      var cls = n > 0 ? "up" : (n < 0 ? "down" : "");
      var sign = n > 0 ? "+" : "";
      var suffix = status === "partial" ? " · partial" : "";
      return '<span class="' + cls + '">' + sign + n.toFixed(2) + "%</span>" + suffix;
    }
    function scRate(block) {
      if (!block || !block.settled) return "—";
      return (block.win_rate * 100).toFixed(1) + "% · " + block.wins + "W/" + block.losses + "L";
    }
    function scAvg(block) {
      if (!block || block.avg_move_pct == null) return "—";
      var n = Number(block.avg_move_pct);
      var sign = n > 0 ? "+" : "";
      return sign + n.toFixed(2) + "%";
    }
    function scStats(rows) {
      function horizon(key) {
        var wins = 0, losses = 0, pending = 0, sum = 0, n = 0;
        rows.forEach(function (row) {
          var h = (row.outcomes || {})[key] || {};
          if (h.status !== "settled") { pending += 1; return; }
          if (h.win === true) wins += 1; else losses += 1;
          if (h.return_pct != null) { sum += Number(h.return_pct); n += 1; }
        });
        var settled = wins + losses;
        return {
          settled: settled,
          pending: pending,
          wins: wins,
          losses: losses,
          win_rate: settled ? wins / settled : null,
          avg_move_pct: n ? sum / n : null
        };
      }
      return { count: rows.length, eod: horizon("eod"), d1: horizon("d1"), d5: horizon("d5") };
    }
    function renderScorecard() {
      var root = document.getElementById("tab-scorecard") || document.getElementById("scorecardRoot");
      if (!root) return;
      if (!root.dataset.bound) {
        root.dataset.bound = "1";
        root.querySelectorAll("select,input").forEach(function (el) {
          el.addEventListener("input", renderScorecard);
          el.addEventListener("change", renderScorecard);
        });
      }
      var sc = (DATA && DATA.rows) ? DATA : ((DATA && DATA.scorecard) || {});
      var chainEl = document.getElementById("scorecardChain");
      var chain = sc.chain || {};
      if (chainEl) {
        if (chain.ok) {
          var head = (sc.audit && sc.audit.head_short) || (chain.head ? String(chain.head).slice(0, 12) : "—");
          chainEl.innerHTML = 'Hash chain intact · ' + (chain.entries || 0) + ' ledger lines · head <span class="mono">' + scEsc(head) + '</span>';
        } else {
          chainEl.innerHTML = '<span class="down">Tamper warning: hash chain does not verify'
            + (chain.broken_at ? ' (line ' + scEsc(chain.broken_at) + ')' : '')
            + '. ' + scEsc(chain.error || 'The public log was changed.') + '</span>';
        }
      }
      var ledgerLink = document.getElementById("scorecardLedgerLink");
      if (ledgerLink && !window.SIGNAL_DESK_STATIC && location.pathname.indexOf("scorecard") === -1) {
        ledgerLink.href = "/scorecard/ledger.jsonl";
      }
      var spx = sc.spx_strategies || {};
      var spxNote = document.getElementById("scorecardSpxNote");
      var spxEl = document.getElementById("scorecardSpx");
      if (spxNote) spxNote.textContent = spx.note || "";
      if (spxEl) {
        var spxRows = spx.rows || [];
        if (!spxRows.length) {
          spxEl.innerHTML = '<div class="empty">No SPX structure logged yet.</div>';
        } else {
          var groups = spx.by_strategy || {};
          var cards = Object.keys(groups).map(function (name) {
            var bucket = groups[name] || {};
            return '<div class="metric"><div class="k">' + scEsc(name) + '</div><div class="v">'
              + scEsc(bucket.closed || 0) + ' closed · '
              + scEsc(bucket.wins || 0) + 'W/' + scEsc(bucket.losses || 0) + 'L · $'
              + Number(bucket.pnl || 0).toFixed(2) + '</div></div>';
          }).join("");
          var lines = spxRows.slice(0, 12).map(function (row) {
            var outcome = row.outcomes || {};
            var pnl = outcome.pnl == null ? "—" : ("$" + Number(outcome.pnl).toFixed(2));
            return '<div class="why">' + scEsc(row.signal_type) + ' ' + scEsc(row.contract || "")
              + ' · ' + scEsc(outcome.status || "open") + ' · ' + scEsc(pnl)
              + (outcome.exit_reason ? (' · ' + scEsc(outcome.exit_reason)) : '') + '</div>';
          }).join("");
          spxEl.innerHTML = '<div class="metric-row">' + cards + '</div>' + lines;
        }
      }
      var rows = sc.rows || [];
      var type = (document.getElementById("scType") || {}).value || "all";
      var dir = (document.getElementById("scDir") || {}).value || "all";
      var result = (document.getElementById("scResult") || {}).value || "all";
      var ticker = String((document.getElementById("scTicker") || {}).value || "").trim().toUpperCase();
      var filtered = rows.filter(function (row) {
        if (type !== "all" && row.signal_type !== type) return false;
        if (dir !== "all" && row.direction !== dir) return false;
        if (ticker && String(row.ticker || "").toUpperCase().indexOf(ticker) === -1) return false;
        var outcomes = row.outcomes || {};
        var horizons = ["eod", "d1", "d5"].map(function (key) { return outcomes[key] || {}; });
        if (result === "win") return horizons.some(function (h) { return h.status === "settled" && h.win === true; });
        if (result === "loss") return horizons.some(function (h) { return h.status === "settled" && h.win === false; });
        if (result === "pending") return horizons.some(function (h) { return h.status !== "settled"; });
        return true;
      });
      var stats = scStats(filtered);
      var metrics = document.getElementById("scorecardMetrics");
      if (metrics) {
        function card(k, v) {
          return '<div class="metric"><div class="k">' + scEsc(k) + '</div><div class="v">' + v + '</div></div>';
        }
        metrics.innerHTML = [
          card("Signals", String(stats.count)),
          card("EOD win rate", scEsc(scRate(stats.eod))),
          card("EOD avg move", scEsc(scAvg(stats.eod))),
          card("1-day win rate", scEsc(scRate(stats.d1))),
          card("1-day avg move", scEsc(scAvg(stats.d1))),
          card("5-day win rate", scEsc(scRate(stats.d5))),
          card("5-day avg move", scEsc(scAvg(stats.d5)))
        ].join("");
      }
      var table = document.getElementById("scorecardTable");
      if (!table) return;
      if (!filtered.length) {
        table.innerHTML = '<tbody><tr><td class="empty">No Gold Setup or Golden Flow rows for this filter. New signals are appended when they fire. Wins and losses both stay on the card.</td></tr></tbody>';
        return;
      }
      var head = '<thead><tr>'
        + '<th>Signal time (CT)</th><th>Ticker</th><th>Signal</th><th>Direction</th>'
        + '<th>Ref entry</th><th>EOD</th><th>1 day</th><th>5 days</th>'
        + '<th>Max favorable</th><th>Max adverse</th><th>Premium ref</th>'
        + '</tr></thead>';
      var body = filtered.map(function (row) {
        var outcomes = row.outcomes || {};
        var ref = row.reference_entry == null ? "—" : Number(row.reference_entry).toFixed(2);
        var premium = row.premium_reference == null ? "—" : Number(row.premium_reference).toFixed(2);
        var signal = scEsc(row.signal_type || "");
        if (row.conviction) signal += ' · ' + scEsc(row.conviction);
        var dirCls = row.direction === "short" ? "down" : "up";
        return '<tr>'
          + '<td class="mono">' + scEsc(row.signaled_at_ct || row.signaled_at || "—") + '</td>'
          + '<td class="mono">' + scEsc(row.ticker) + '</td>'
          + '<td>' + signal + '</td>'
          + '<td class="' + dirCls + '">' + scEsc(row.direction || "") + '</td>'
          + '<td class="mono">' + scEsc(ref) + '</td>'
          + '<td class="mono">' + scMove(outcomes.eod) + '</td>'
          + '<td class="mono">' + scMove(outcomes.d1) + '</td>'
          + '<td class="mono">' + scMove(outcomes.d5) + '</td>'
          + '<td class="mono">' + scExcursion(outcomes.mfe_pct, outcomes.mfe_status) + '</td>'
          + '<td class="mono">' + scExcursion(outcomes.mae_pct, outcomes.mfe_status) + '</td>'
          + '<td class="mono">' + scEsc(premium) + '</td>'
          + '</tr>';
      }).join("");
      table.innerHTML = head + '<tbody>' + body + '</tbody>';
    }
"""


def _boot_js(*, live: bool) -> str:
    data_url = "/api/scorecard" if live else "data/scorecard.json"
    return f"""
    const SCORECARD_DATA_URL = {json.dumps(data_url)};
    async function loadScorecard() {{
      try {{
        const res = await fetch(SCORECARD_DATA_URL + "?t=" + Date.now(), {{ cache: "no-store" }});
        if (res.ok) {{
          DATA = await res.json();
          renderScorecard();
          return;
        }}
      }} catch (err) {{}}
      renderScorecard();
    }}
    loadScorecard();
    if ({str(live).lower()}) setInterval(loadScorecard, 60000);
    """


def render_scorecard_page(*, live: bool = False, initial: dict[str, Any] | None = None) -> str:
    """Standalone public page. ``initial`` is embedded so the static file still renders offline."""
    blob = json.dumps(initial or {"rows": [], "summary": {}, "chain": {"ok": True, "entries": 0}, "disclaimer": DISCLAIMER, "move_note": MOVE_NOTE})
    blob = blob.replace("<", "\\u003c")
    refresh = "This page reloads the ledger about once a minute." if live else "This page is the public snapshot published with the desk."
    section = SECTION_HTML.replace('class="tabpane" id="tab-scorecard"', 'id="scorecardEmbedded"').replace(
        "<h2>Scorecard</h2>", ""
    )
    if live:
        section = section.replace('href="data/scorecard_ledger.jsonl"', 'href="/scorecard/ledger.jsonl"')
        section = section.replace('href="scorecard.html"', 'href="/scorecard"')
    from odte_scanner.edge.page import render_edge_static
    from odte_scanner.edge.view import build_edge_report

    edge_html = render_edge_static(build_edge_report(scorecard=initial if isinstance(initial, dict) else None))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Scorecard — ZeroLoss Desk</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
  <style>
    :root {{
      --bg: #0c1210; --ink: #eef4f0; --muted: #8b9e94; --line: rgba(238,244,240,.1);
      --long: #3ecf8e; --short: #ff6b5a; --accent: #7ec8b8; --sure-gold: #f0c14b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; color: var(--ink); font-family: "DM Sans", sans-serif;
      background:
        radial-gradient(900px 480px at 8% -5%, rgba(240,193,75,.14), transparent 55%),
        linear-gradient(165deg, #08100e, var(--bg) 42%, #0e1814);
    }}
    .wrap {{ width: min(1240px, calc(100% - 1.4rem)); margin: 0 auto; padding: 1.1rem 0 2.5rem; }}
    h1 {{ font-family: "Instrument Serif", Georgia, serif; font-weight: 400; font-size: clamp(2rem, 5vw, 3rem); margin: 0; }}
    h1 em {{ font-style: italic; color: var(--sure-gold); }}
    a {{ color: var(--accent); }}
    .lede {{ color: var(--muted); max-width: 46rem; line-height: 1.45; }}
    .disclaimer {{
      border: 1px solid rgba(240,193,75,.45); border-radius: .75rem; padding: .8rem .9rem;
      background: rgba(240,193,75,.08); color: var(--ink);
    }}
    .status {{ color: var(--muted); font-family: "JetBrains Mono", monospace; font-size: .76rem; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: .6rem; align-items: end; margin: .8rem 0; }}
    .metric-row {{ display: grid; gap: .65rem; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); margin: .8rem 0 1rem; }}
    .metric {{ border: 1px solid var(--line); border-radius: .75rem; padding: .7rem .8rem; background: rgba(255,255,255,.03); }}
    .metric .k {{ color: var(--muted); font-size: .68rem; text-transform: uppercase; letter-spacing: .06em; }}
    .metric .v {{ font-family: "Instrument Serif", Georgia, serif; font-size: 1.35rem; margin-top: .15rem; }}
    table {{ width: 100%; border-collapse: collapse; font-size: .84rem; }}
    th, td {{ text-align: left; padding: .5rem .35rem; border-bottom: 1px solid var(--line); vertical-align: top; }}
    th {{ color: var(--muted); font-size: .68rem; text-transform: uppercase; letter-spacing: .05em; font-weight: 500; }}
    .mono {{ font-family: "JetBrains Mono", monospace; }}
    .up {{ color: var(--long); }} .down {{ color: var(--short); }}
    .empty {{ color: var(--muted); padding: .6rem 0; }}
    .sc-input {{
      display: block; margin-top: .2rem; background: rgba(255,255,255,.03); color: var(--ink);
      border: 1px solid var(--line); border-radius: .45rem; padding: .35rem .5rem; font: 500 .84rem/1.2 "DM Sans", sans-serif;
    }}
    .back {{ font-size: .86rem; }}
  </style>
</head>
<body>
  <div class="wrap" id="scorecardRoot">
    <p class="back"><a href="./">← ZeroLoss Desk</a></p>
    <h1>Public <em>Scorecard</em></h1>
    {edge_html}
    <p class="disclaimer">{DISCLAIMER}</p>
    <p class="lede">{refresh} Times are US Central. The reference price is the underlying at signal time.</p>
    {section}
  </div>
  <script>
    let DATA = {blob};
    {CLIENT_JS}
    {_boot_js(live=live)}
  </script>
</body>
</html>
"""
