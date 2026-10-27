"""Edge tab on the desk, and the same table on the public scorecard page."""

from __future__ import annotations

import html
from typing import Any

EDGE_SECTION = """
    <section class="tabpane" id="tab-edge">
      <h2>Edge</h2>
      <p class="lede" id="edgeVerdict">Checking which signals have a measured edge…</p>
      <p class="status" id="edgeBar"></p>
      <h3>Option dollars — information only</h3>
      <p class="lede">Expectancy is dollars per 1 contract. A source clears the bar at 40 settled trades, a 95% interval above zero, and a positive later half. This table does not mute Telegram or paper buys.</p>
      <h3>Filters on the card</h3>
      <ul id="edgeFilters"></ul>
      <div style="overflow-x:auto"><table id="edgeOption"></table></div>
      <h3>Underlying scorecard — not the gate</h3>
      <p class="lede" id="edgeMove"></p>
      <div style="overflow-x:auto"><table id="edgeUnderlying"></table></div>
      <h3>Strongest and weakest slices</h3>
      <p class="lede">Same underlying move, split by direction, DTE, time of day, moneyness, and symbol. Slices under 8 settled trades are hidden. These numbers do not turn alerts off.</p>
      <div style="overflow-x:auto"><table id="edgeSlices"></table></div>
      <h3>Recorded paper audit, 27 Oct 2026</h3>
      <p class="lede">Fly paper books at 03:04 UTC. The live table above is the same book, still information. Zero-hold flats are not an edge.</p>
      <div style="overflow-x:auto"><table id="edgeAudit"></table></div>
      <h3>Index studies, 2023–2026 — not routed</h3>
      <p class="lede" id="edgeStudyNote"></p>
      <div style="overflow-x:auto"><table id="edgeStudy"></table></div>
      <h3>Limits</h3>
      <ul id="edgeLimits"></ul>
    </section>
"""

EDGE_JS = r"""
    function edgeEsc(value) {
      return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
        return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch];
      });
    }
    function edgeNum(value, digits) {
      if (value == null || Number.isNaN(Number(value))) return "—";
      return Number(value).toFixed(digits == null ? 2 : digits);
    }
    function edgePct(value) {
      if (value == null || Number.isNaN(Number(value))) return "—";
      return (Number(value) * 100).toFixed(1) + "%";
    }
    function edgeCi(mean, lo, hi, digits) {
      if (mean == null) return "—";
      var body = edgeNum(mean, digits);
      if (lo == null || hi == null) return body;
      return body + " [" + edgeNum(lo, digits) + ", " + edgeNum(hi, digits) + "]";
    }
    function edgeTable(headers, rows) {
      var head = "<thead><tr>" + headers.map(function (h) { return "<th>" + edgeEsc(h) + "</th>"; }).join("") + "</tr></thead>";
      var body = rows.map(function (cells) {
        return "<tr>" + cells.map(function (cell) { return "<td class='mono'>" + cell + "</td>"; }).join("") + "</tr>";
      }).join("");
      return head + "<tbody>" + (body || "<tr><td class='mono' colspan='" + headers.length + "'>No settled trades.</td></tr>") + "</tbody>";
    }
    function renderEdge() {
      var edge = DATA && DATA.edge;
      var verdict = document.getElementById("edgeVerdict");
      if (!verdict) return;
      if (!edge) {
        verdict.textContent = "Edge report is not on this snapshot yet.";
        return;
      }
      verdict.textContent = edge.verdict || "";
      var bar = document.getElementById("edgeBar");
      var rule = edge.bar || {};
      if (bar) {
        var kept = (edge.kept || []).join(", ") || "none";
        bar.textContent = "Bar: n ≥ " + (rule.min_samples || 40)
          + ", 95% interval above 0, later half above 0, "
          + (rule.unit || "dollars per contract")
          + ". Kept: " + kept
          + ". Bad spot rows excluded from the underlying table: " + (edge.bad_spots_excluded || 0) + ".";
      }
      var move = document.getElementById("edgeMove");
      if (move) move.textContent = edge.move_note || "";
      var filterEl = document.getElementById("edgeFilters");
      if (filterEl) {
        filterEl.innerHTML = (edge.filters || []).map(function (item) {
          return "<li><strong>" + edgeEsc(item.applies_to) + ".</strong> " + edgeEsc(item.reason) + "</li>";
        }).join("");
      }
      var optionEl = document.getElementById("edgeOption");
      if (optionEl) {
        optionEl.innerHTML = edgeTable(
          ["Source", "n", "Win rate", "Avg win $", "Avg loss $", "Expectancy $", "Later half", "Route"],
          (edge.option_sources || []).map(function (row) {
            return [
              edgeEsc(row.source),
              edgeEsc(row.n),
              edgePct(row.win_rate) + (row.win_lo == null ? "" : " [" + edgePct(row.win_lo) + ", " + edgePct(row.win_hi) + "]"),
              edgeNum(row.avg_win),
              edgeNum(row.avg_loss),
              edgeCi(row.expectancy, row.lo, row.hi, 2),
              edgeEsc(row.h2_n) + " · " + edgeNum(row.h2_expectancy),
              row.routed ? "clears bar" : "does not"
            ];
          })
        );
      }
      var underEl = document.getElementById("edgeUnderlying");
      if (underEl) {
        underEl.innerHTML = edgeTable(
          ["Signal", "n", "Win rate", "Avg win", "Avg loss", "Expectancy", "Later half", "$ / share"],
          (edge.underlying || []).map(function (row) {
            return [
              edgeEsc(row.slice),
              edgeEsc(row.n),
              edgePct(row.win_rate),
              edgeNum(row.avg_win) + "%",
              edgeNum(row.avg_loss) + "%",
              edgeCi(row.expectancy, row.lo, row.hi, 2) + "%",
              edgeNum(row.h2_expectancy) + "%",
              edgeCi(row.per_share, row.per_share_lo, row.per_share_hi, 2)
            ];
          })
        );
      }
      var sliceEl = document.getElementById("edgeSlices");
      if (sliceEl) {
        sliceEl.innerHTML = edgeTable(
          ["Slice", "n", "Win rate", "Expectancy", "Interval"],
          (edge.slices || []).map(function (row) {
            return [
              edgeEsc(row.slice),
              edgeEsc(row.n),
              edgePct(row.win_rate),
              edgeNum(row.expectancy) + "%",
              edgeCi(row.expectancy, row.lo, row.hi, 2) + "%"
            ];
          })
        );
      }
      var auditEl = document.getElementById("edgeAudit");
      if (auditEl) {
        auditEl.innerHTML = edgeTable(
          ["Book", "n", "Wins", "Losses", "Expectancy $", "Interval"],
          (edge.paper_audit || []).map(function (row) {
            return [
              edgeEsc(row.source),
              edgeEsc(row.n),
              edgeEsc(row.wins),
              edgeEsc(row.losses),
              edgeNum(row.expectancy),
              edgeCi(row.expectancy, row.lo, row.hi, 2)
            ];
          })
        );
      }
      var studyNote = document.getElementById("edgeStudyNote");
      if (studyNote) studyNote.textContent = (edge.study_note || "") + " " + (edge.spread_note || "");
      var studyEl = document.getElementById("edgeStudy");
      if (studyEl) {
        studyEl.innerHTML = edgeTable(
          ["Study", "n", "Win rate", "$ per $100", "Interval", "Route"],
          (edge.study || []).map(function (row) {
            return [
              edgeEsc(row.name),
              edgeEsc(row.n),
              edgePct(row.win_rate),
              edgeNum(row.expectancy_per_100),
              edgeCi(row.expectancy_per_100, row.lo, row.hi, 2),
              "not routed"
            ];
          })
        );
      }
      var limits = document.getElementById("edgeLimits");
      if (limits) {
        limits.innerHTML = (edge.limitations || []).map(function (line) {
          return "<li>" + edgeEsc(line) + "</li>";
        }).join("");
      }
    }
"""


def _cell(value: Any) -> str:
    return html.escape("—" if value is None else str(value))


def render_edge_static(report: dict[str, Any]) -> str:
    """Server-rendered table for the public scorecard page."""
    def row(cells: list[Any]) -> str:
        return "<tr>" + "".join(f"<td class='mono'>{_cell(cell)}</td>" for cell in cells) + "</tr>"

    option_rows = "".join(
        row(
            [
                item.get("source"),
                item.get("n"),
                item.get("expectancy"),
                item.get("lo"),
                item.get("hi"),
                "clears bar" if item.get("routed") else "does not",
            ]
        )
        for item in report.get("option_sources") or []
    )
    under_rows = "".join(
        row([item.get("slice"), item.get("n"), item.get("expectancy"), item.get("lo"), item.get("hi")])
        for item in report.get("underlying") or []
    )
    return f"""
    <section id="edgeEmbedded">
      <h2>Edge</h2>
      <p class="disclaimer">{_cell(report.get("verdict"))}</p>
      <p class="lede">{_cell(report.get("disclaimer"))}</p>
      <ul>
        {"".join(f"<li><strong>{_cell(item.get('applies_to'))}.</strong> {_cell(item.get('reason'))}</li>" for item in report.get("filters") or [])}
      </ul>
      <h3>Option dollars per contract</h3>
      <table>
        <thead><tr><th>Source</th><th>n</th><th>Expectancy $</th><th>Low</th><th>High</th><th>Route</th></tr></thead>
        <tbody>{option_rows or "<tr><td>No settled option trades.</td></tr>"}</tbody>
      </table>
      <h3>Underlying move, not the gate</h3>
      <table>
        <thead><tr><th>Signal</th><th>n</th><th>Expectancy %</th><th>Low</th><th>High</th></tr></thead>
        <tbody>{under_rows or "<tr><td>No settled scorecard rows.</td></tr>"}</tbody>
      </table>
      <p class="lede">Bad spot rows excluded: {_cell(report.get("bad_spots_excluded"))}. Full slices, the paper audit, and the index study are on the desk Edge tab.</p>
    </section>
    """
