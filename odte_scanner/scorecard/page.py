"""Scorecard page and the desk section that links to it."""

from __future__ import annotations

import json
from typing import Any

from odte_scanner.scorecard.daily_pnl import PAPER_LABEL
from odte_scanner.scorecard.service import DISCLAIMER, MOVE_NOTE

SECTION_HTML = f"""
    <section class="tabpane" id="tab-scorecard">
      <h2>Scorecard</h2>
      <p class="lede" id="scorecardDisclaimer">{DISCLAIMER}</p>
      <p class="status" id="scorecardChain">Checking the public ledger…</p>
      <h3>Daily P&amp;L</h3>
      <p class="lede" id="dailyPnlLabel">{PAPER_LABEL}</p>
      <p class="status" id="dailyPnlGap"></p>
      <div id="dailyPnlChart"></div>
      <p class="lede" id="dailyPnlOpen"></p>
      <div style="overflow-x:auto">
        <table id="dailyPnlTable"></table>
      </div>
      <h3>Not in the Webull total</h3>
      <div id="dailyPnlSide" class="empty">SPX modeled results and the internal journal stay out of the paper total.</div>
      <div class="metric-row" id="scorecardMetrics"></div>
      <div class="toolbar" id="scorecardFilters">
        <label class="status">Type
          <select id="scType" class="sc-input">
            <option value="all">All</option>
            <option value="Gold Setup">Gold Setup</option>
            <option value="Golden Flow">Golden Flow</option>
            <option value="Diamond">Diamond</option>
            <option value="Sniper">Sniper</option>
            <option value="Afternoon Puts">Afternoon Puts</option>
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
    function dailyPnlMoney(value) {
      if (value == null || Number.isNaN(Number(value))) return "—";
      var n = Number(value);
      var body = "$" + Math.abs(n).toFixed(2);
      if (n > 0) return '<span class="up">+' + body + "</span>";
      if (n < 0) return '<span class="down">−' + body + "</span>";
      return body;
    }
    function dailyPnlRecord(cell) {
      cell = cell || {};
      var pnl = dailyPnlMoney(cell.pnl || 0);
      var rec = (cell.wins || 0) + "W/" + (cell.losses || 0) + "L";
      if (!cell.trades) return "—";
      return pnl + " · " + rec;
    }
    function dailyPnlChart(chart) {
      if (!chart || !chart.length) return "";
      var w = 640, h = 150, padL = 8, padR = 8, padT = 12, padB = 22;
      var pnls = chart.map(function (row) { return Number(row.pnl) || 0; });
      var cums = chart.map(function (row) { return Number(row.cumulative) || 0; });
      var max = 1;
      pnls.concat(cums).forEach(function (n) { max = Math.max(max, Math.abs(n)); });
      var innerW = w - padL - padR;
      var innerH = h - padT - padB;
      var mid = padT + innerH / 2;
      var step = innerW / chart.length;
      var y = function (n) { return mid - (n / max) * (innerH / 2); };
      var bars = chart.map(function (row, i) {
        var pnl = Number(row.pnl) || 0;
        var x = padL + i * step + step * 0.2;
        var bw = Math.max(2, step * 0.6);
        var y0 = y(0), y1 = y(pnl);
        var top = Math.min(y0, y1);
        var color = pnl > 0 ? "#3ecf8e" : (pnl < 0 ? "#ff6b5a" : "#8b9e94");
        return '<rect x="' + x.toFixed(1) + '" y="' + top.toFixed(1) + '" width="' + bw.toFixed(1)
          + '" height="' + Math.max(1, Math.abs(y1 - y0)).toFixed(1) + '" fill="' + color + '"></rect>';
      }).join("");
      var line = chart.map(function (row, i) {
        var x = padL + i * step + step * 0.5;
        return x.toFixed(1) + "," + y(Number(row.cumulative) || 0).toFixed(1);
      }).join(" ");
      var label = chart.length ? (scEsc(chart[0].day) + " → " + scEsc(chart[chart.length - 1].day)) : "";
      return '<svg viewBox="0 0 ' + w + " " + h + '" width="100%" height="150" role="img" aria-label="Daily realized bars and cumulative line">'
        + '<line x1="' + padL + '" y1="' + mid + '" x2="' + (w - padR) + '" y2="' + mid + '" stroke="rgba(238,244,240,.25)"></line>'
        + bars
        + '<polyline fill="none" stroke="#f0c14b" stroke-width="2" points="' + line + '"></polyline>'
        + '<text x="' + padL + '" y="' + (h - 6) + '" fill="#8b9e94" font-size="11">' + label + " · bars daily, line cumulative</text>"
        + "</svg>";
    }
    function renderSideDays(block) {
      var days = (block && block.days) || [];
      if (!days.length) return "<p class='empty'>" + scEsc((block && block.label) || "None yet.") + "</p>";
      var rows = days.map(function (row) {
        var extra = row.unrealized_pnl_usd ? (" · unrealized " + dailyPnlMoney(row.unrealized_pnl_usd)) : "";
        return "<tr><td class='mono'>" + scEsc(row.day) + "</td><td class='mono'>" + dailyPnlMoney(row.realized_pnl_usd)
          + "</td><td class='mono'>" + scEsc(row.trades || 0) + extra + "</td></tr>";
      }).join("");
      return "<p class='lede'>" + scEsc(block.label || "") + "</p><table><thead><tr><th>Day</th><th>Realized</th><th>Trades</th></tr></thead><tbody>"
        + rows + "</tbody></table>";
    }
    function renderScorecardDailyPnl() {
      var table = document.getElementById("dailyPnlTable");
      if (!table) return;
      var board = window.SCORECARD_DAILY_PNL || (DATA && DATA.scorecard_daily_pnl) || null;
      var gap = document.getElementById("dailyPnlGap");
      var chart = document.getElementById("dailyPnlChart");
      var openEl = document.getElementById("dailyPnlOpen");
      var side = document.getElementById("dailyPnlSide");
      var label = document.getElementById("dailyPnlLabel");
      if (label && board && board.label) label.textContent = board.label;
      if (!board || board.ok === false) {
        if (gap) gap.textContent = (board && board.error) ? "Daily P&L is not available yet." : "Loading the paper ledger…";
        return;
      }
      if (gap) gap.textContent = board.gap_note || "";
      if (chart) chart.innerHTML = dailyPnlChart(board.chart || []);
      var totals = board.totals || {};
      if (openEl) {
        if (!totals.open_n) {
          openEl.textContent = "No open Webull paper positions.";
        } else if (totals.unrealized_pnl_usd == null) {
          openEl.textContent = totals.open_n + " open today. Unrealized mark unavailable.";
        } else {
          openEl.innerHTML = totals.open_n + " open today · " + (totals.unrealized_label || "unrealized")
            + " " + dailyPnlMoney(totals.unrealized_pnl_usd) + " (unrealized, not in the realized total).";
        }
      }
      var desks = board.desks || [];
      var head = "<thead><tr><th>Day</th><th>Realized</th><th>Cumulative</th><th>W / L</th><th>Win rate</th><th>Trades</th>"
        + desks.map(function (name) { return "<th>" + scEsc(name) + "</th>"; }).join("")
        + "</tr></thead>";
      var rows = (board.days || []).map(function (row) {
        var rate = row.win_rate_pct == null ? "—" : (Number(row.win_rate_pct).toFixed(1) + "%");
        var openNote = row.open_n ? ("<div class='why'>" + row.open_n + " open"
          + (row.unrealized_label ? (" · " + scEsc(row.unrealized_label)) : "")
          + (row.unrealized_pnl_usd == null ? "" : (" " + dailyPnlMoney(row.unrealized_pnl_usd)))
          + "</div>") : "";
        var contractNote = row.contracts_note ? ("<div class='why'>" + scEsc(row.contracts_note) + "</div>") : "";
        var logNote = row.from_retained_log ? "<div class='why'>retained round-trip log</div>" : "";
        var cells = desks.map(function (name) {
          return "<td class='mono'>" + dailyPnlRecord((row.desks || {})[name]) + "</td>";
        }).join("");
        return "<tr><td class='mono'>" + scEsc(row.day) + openNote + "</td>"
          + "<td class='mono'>" + dailyPnlMoney(row.realized_pnl_usd) + "</td>"
          + "<td class='mono'>" + dailyPnlMoney(row.cumulative_pnl_usd) + "</td>"
          + "<td class='mono'>" + scEsc((row.wins || 0) + "W/" + (row.losses || 0) + "L") + "</td>"
          + "<td class='mono'>" + scEsc(rate) + "</td>"
          + "<td class='mono'>" + scEsc(row.trades || 0) + contractNote + logNote + "</td>"
          + cells + "</tr>";
      }).join("");
      table.innerHTML = head + "<tbody>" + (rows || "<tr><td class='empty'>No closed paper fills in the retained history.</td></tr>") + "</tbody>";
      if (side) {
        var journal = board.journal || {};
        var sleeve = journal.webull_sleeve_fills || [];
        var sleeveHtml = "";
        if (sleeve.length) {
          sleeveHtml = "<p class='lede'>Webull fills on journal sleeves, still not in the paper total.</p><table><tbody>"
            + sleeve.map(function (row) {
              return "<tr><td class='mono'>" + scEsc(row.day) + "</td><td>" + scEsc(row.desk)
                + "</td><td class='mono'>" + dailyPnlMoney(row.realized_pnl_usd) + "</td></tr>";
            }).join("") + "</tbody></table>";
        }
        side.innerHTML = renderSideDays(board.spx_modeled) + renderSideDays(journal) + sleeveHtml;
      }
    }
    function ensureDailyPnlPoll() {
      if (window.__dailyPnlPoll || window.SIGNAL_DESK_STATIC) return;
      window.__dailyPnlPoll = 1;
      function pull() {
        fetch("/api/scorecard/daily_pnl?t=" + Date.now(), { cache: "no-store" })
          .then(function (res) { return res.ok ? res.json() : null; })
          .then(function (body) {
            if (!body || !body.days) return;
            window.SCORECARD_DAILY_PNL = body;
            renderScorecardDailyPnl();
          })
          .catch(function () {});
      }
      pull();
      setInterval(pull, 5000);
    }
    function renderScorecard() {
      var root = document.getElementById("tab-scorecard") || document.getElementById("scorecardRoot");
      if (!root) return;
      ensureDailyPnlPoll();
      renderScorecardDailyPnl();
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
        var rowCls = row.signal_type === "Diamond" ? "diamond-row" : (row.signal_type === "Sniper" ? "sniper-row" : "");
        return '<tr class="' + rowCls + '">'
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
    try:
        from odte_scanner.scorecard.daily_pnl import build_scorecard_daily_pnl

        daily_payload = build_scorecard_daily_pnl()
    except Exception as exc:  # noqa: BLE001
        daily_payload = {"ok": False, "label": PAPER_LABEL, "days": [], "error": str(exc)[:200]}
    daily_blob = json.dumps(daily_payload).replace("<", "\\u003c")
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
    tr.diamond-row {{ box-shadow: inset 3px 0 0 #7dd3fc; }}
    tr.diamond-row td {{ background: linear-gradient(90deg, rgba(224,242,254,.16), transparent 55%); }}
    tr.sniper-row {{ box-shadow: inset 3px 0 0 #34d399; }}
    tr.sniper-row td {{ background: linear-gradient(90deg, rgba(209,250,229,.2), transparent 55%); }}
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
    window.SCORECARD_DAILY_PNL = {daily_blob};
    {CLIENT_JS}
    {_boot_js(live=live)}
  </script>
</body>
</html>
"""
