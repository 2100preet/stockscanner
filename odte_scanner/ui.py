from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from odte_scanner.config import load_config
from odte_scanner.signals.actions import build_action_board
from odte_scanner.backtest.win_rates import build_win_rate_table, load_win_rate_table

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]

PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Signal Desk — Multi-Horizon</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
  <style>
    :root {
      --bg: #0c1210; --panel: rgba(18,28,24,.72); --ink: #eef4f0; --muted: #8b9e94;
      --line: rgba(238,244,240,.1); --long: #3ecf8e; --short: #ff6b5a; --wait: #e0b35a;
      --accent: #7ec8b8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; color: var(--ink);
      font-family: "DM Sans", sans-serif;
      background:
        radial-gradient(900px 480px at 8% -5%, rgba(62,207,142,.16), transparent 55%),
        radial-gradient(700px 420px at 95% 5%, rgba(126,200,184,.1), transparent 50%),
        linear-gradient(165deg, #08100e, var(--bg) 40%, #0e1814);
    }
    .wrap { width: min(1240px, calc(100% - 1.4rem)); margin: 0 auto; padding: 1.1rem 0 2.5rem; }
    .brand { font-family: "Instrument Serif", Georgia, serif; font-size: clamp(2.1rem, 5vw, 3.1rem); margin: 0; letter-spacing: -.03em; }
    .brand em { font-style: italic; color: var(--long); }
    .lede { color: var(--muted); margin: .35rem 0 1rem; max-width: 42rem; }
    .toolbar { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; margin-bottom: .9rem; }
    button, .pill {
      border: 1px solid var(--line); background: rgba(255,255,255,.03); color: var(--ink);
      border-radius: .55rem; padding: .48rem .9rem; font: 500 .86rem/1 "DM Sans", sans-serif; cursor: pointer;
    }
    button.primary { background: var(--long); color: #062016; border-color: transparent; }
    button:disabled { opacity: .55; cursor: wait; }
    .status { color: var(--muted); font-family: "JetBrains Mono", monospace; font-size: .76rem; }
    .tabs { display: flex; flex-wrap: wrap; gap: .35rem; border-bottom: 1px solid var(--line); padding-bottom: .55rem; margin-bottom: 1rem; }
    .tabs button { background: transparent; border: none; color: var(--muted); border-radius: .4rem; padding: .45rem .75rem; }
    .tabs button.active { color: var(--ink); background: rgba(255,255,255,.07); }
    .tabpane { display: none; }
    .tabpane.active { display: block; animation: fade .25s ease; }
    @keyframes fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
    .cards { display: grid; gap: .75rem; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
    .action-card {
      border: 1px solid var(--line); border-radius: .85rem; padding: .95rem 1rem;
      background: var(--panel); backdrop-filter: blur(8px);
      transition: transform .2s ease, border-color .2s ease;
    }
    .action-card:hover { transform: translateY(-2px); border-color: rgba(62,207,142,.35); }
    .action-card.long { box-shadow: inset 3px 0 0 var(--long); }
    .action-card.wait { box-shadow: inset 3px 0 0 var(--wait); }
    .action-card.short { box-shadow: inset 3px 0 0 var(--short); }
    .ac-top { display: flex; justify-content: space-between; align-items: baseline; gap: .5rem; }
    .ac-sym { font-family: "Instrument Serif", Georgia, serif; font-size: 1.45rem; }
    .ac-dir { font-family: "JetBrains Mono", monospace; font-size: .72rem; letter-spacing: .08em; font-weight: 500; }
    .ac-dir.long { color: var(--long); } .ac-dir.wait { color: var(--wait); } .ac-dir.short { color: var(--short); }
    .badge.skip { background: rgba(139,158,148,.14); color: var(--muted); }
    .badge.golden { background: rgba(224,179,90,.22); color: #f0d48a; }
    .badge.unusual { background: rgba(126,200,184,.18); color: var(--accent); }
    .badge.aggressive { background: rgba(62,207,142,.14); color: var(--long); }
    .playbook { display: flex; flex-wrap: wrap; gap: .25rem; margin-top: .45rem; }
    .echo-grid { display: grid; gap: .85rem; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
    .gex-bar { height: 8px; border-radius: 99px; background: rgba(255,255,255,.06); overflow: hidden; }
    .gex-bar > i { display: block; height: 100%; background: linear-gradient(90deg, var(--short), var(--wait), var(--long)); }
    .ac-conf { margin: .45rem 0 .2rem; font-size: .8rem; color: var(--muted); }
    .bar { height: 4px; border-radius: 99px; background: rgba(255,255,255,.08); overflow: hidden; margin-bottom: .55rem; }
    .bar > i { display: block; height: 100%; background: linear-gradient(90deg, var(--accent), var(--long)); }
    .ac-meta { display: grid; grid-template-columns: 1fr 1fr; gap: .35rem .6rem; font-family: "JetBrains Mono", monospace; font-size: .72rem; color: var(--muted); }
    .ac-meta strong { color: var(--ink); font-weight: 500; display: block; }
    .metric-row { display: grid; gap: .65rem; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); margin-bottom: 1rem; }
    .metric { border: 1px solid var(--line); border-radius: .75rem; padding: .7rem .8rem; background: rgba(255,255,255,.03); }
    .metric .k { color: var(--muted); font-size: .68rem; text-transform: uppercase; letter-spacing: .06em; }
    .metric .v { font-family: "Instrument Serif", Georgia, serif; font-size: 1.45rem; margin-top: .15rem; }
    table { width: 100%; border-collapse: collapse; font-size: .84rem; }
    th, td { text-align: left; padding: .5rem .28rem; border-bottom: 1px solid var(--line); vertical-align: top; }
    th { color: var(--muted); font-size: .68rem; text-transform: uppercase; letter-spacing: .05em; font-weight: 500; }
    .mono { font-family: "JetBrains Mono", monospace; }
    .up { color: var(--long); } .down { color: var(--short); }
    .empty { color: var(--muted); padding: .6rem 0; }
    .badge { font-family: "JetBrains Mono", monospace; font-size: .68rem; padding: .2rem .4rem; border-radius: .3rem; }
    .badge.buy, .badge.long { background: rgba(62,207,142,.16); color: var(--long); }
    .badge.sell { background: rgba(255,107,90,.16); color: var(--short); }
    .badge.wait, .badge.hold { background: rgba(224,179,90,.14); color: var(--wait); }
    .why { color: var(--muted); font-size: .76rem; max-width: 20rem; }
    .tag { display: inline-block; padding: .12rem .4rem; border-radius: .3rem; background: rgba(62,207,142,.1); color: #9BE7C0; font-size: .68rem; font-family: "JetBrains Mono", monospace; margin-right: .25rem; }
    h2 { font-family: "Instrument Serif", Georgia, serif; font-size: 1.25rem; font-weight: 400; margin: 0 0 .65rem; }
    .panel { margin-top: 1.1rem; }
    footer { margin-top: 1.6rem; color: var(--muted); font-size: .72rem; line-height: 1.45; }
    .loading { color: var(--wait); font-family: "JetBrains Mono", monospace; font-size: .8rem; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1 class="brand">Signal <em>Desk</em></h1>
    <p class="lede">Signa-style action cards + Intellectia-style horizons: <strong>0DTE</strong>, <strong>1 week</strong>, and <strong>1–3 month swing</strong> — separate algos and win rates. Screener covers ~100 liquid names.</p>
    <div class="toolbar">
      <button class="primary" id="btnScan">Scan focus</button>
      <button id="btnScanWide">Scan liquid universe</button>
      <button id="btnRefresh">Reload</button>
      <span class="pill" id="session">—</span>
      <span class="pill" id="universePill">—</span>
      <span class="status" id="counts"></span>
      <span class="status" id="updated">Loading…</span>
    </div>
    <div id="loadNote" class="loading" style="display:none;margin-bottom:.6rem"></div>

    <nav class="tabs" id="tabs">
      <button class="active" data-tab="overview">Overview</button>
      <button data-tab="odte">0DTE</button>
      <button data-tab="explosive">Explosive</button>
      <button data-tab="weekly">1 Week</button>
      <button data-tab="swing">Swing 1–3M</button>
      <button data-tab="echo">Echo Desk</button>
      <button data-tab="challenge">$1k→$1M</button>
      <button data-tab="screener">Screener</button>
      <button data-tab="journal">Journal</button>
    </nav>

    <section class="tabpane active" id="tab-overview">
      <div class="metric-row" id="perfCards"></div>
      <p class="lede" id="insightSummary"></p>
      <p class="lede" id="winLegend" style="font-size:.78rem">
        <strong>Hist win</strong> = % of past quality signals where the underlying finished green over the horizon.
        <strong>n</strong> = sample size (how many of those signals). Small n (e.g. 1–5) means the % is fragile.
        <strong>Strike rate ≥1%</strong> = how often the underlying ripped ≥1% after the signal (better proxy for call payoff than plain win%).
        <strong>BUY NOW gate</strong>: only symbols with hist win ≥80% and n≥5 are promoted (see hist-win gate card).
      </p>
      <div class="metric-row" id="histWinGate"></div>
      <h2>Top action cards</h2>
      <div class="cards" id="overviewCards"></div>
      <div class="panel">
        <h2>Lottery desk — BUY / SELL now</h2>
        <p class="lede" style="margin-top:0">Convex 0DTE/1DTE tickets gated by tape, liquidity, session timing, and multi-algo quality — not a blind list.</p>
        <div id="explosiveMini" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Live board (options)</h2>
        <div id="boardMini" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Dark pool (FINRA ATS) — also on Echo Desk</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">Official weekly ATS volume from FINRA OTC Transparency (~2 week delay). Open <strong>Echo Desk → Darkpool</strong> for venues + levels.</p>
        <div id="darkpoolMini" class="empty">—</div>
      </div>
    </section>

    <section class="tabpane" id="tab-odte">
      <h2>0DTE — same-day / next-session algos</h2>
      <p class="lede">Gap-and-go, breakout, volume thrust, VIX regime. Win% = next session green after quality signal. Strike rate = ≥1% / ≥2% underlying rip rate.</p>
      <div class="cards" id="cards0dte"></div>
      <div class="panel"><div id="table0dte" class="empty"></div></div>
    </section>

    <section class="tabpane" id="tab-explosive">
      <h2>Lottery desk — BUY THIS / SELL THIS now</h2>
      <p class="lede">
        Convexity scan finds cheap 0–1 DTE calls that can multi-bag on a +2–5% rip.
        The playbook then decides <strong>BUY NOW</strong> or <strong>SELL NOW</strong> using tape confirm, liquidity, premium band, anti-chase, session timing, and multi-algo underlying quality — same discipline desks use on parabolic 0DTE tickets.
      </p>
      <div class="metric-row" id="explosiveMetrics"></div>
      <div class="cards" id="lotteryPrimary"></div>
      <div class="panel">
        <h2>BUY NOW lottery</h2>
        <div id="lotteryBuy" class="empty">No BUY NOW lottery yet — waiting for tape + convexity clears.</div>
      </div>
      <div class="panel">
        <h2>SELL NOW lottery</h2>
        <div id="lotterySell" class="empty">No open lottery exits.</div>
      </div>
      <div class="panel">
        <h2>WAIT / SKIP (gated — not actionable)</h2>
        <div id="lotteryWait" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Convexity candidates (raw scan)</h2>
        <div id="explosiveTable" class="empty">Run a scan to populate explosive tickets.</div>
      </div>
    </section>

    <section class="tabpane" id="tab-weekly">
      <h2>1 Week — swinglet / weekly calls</h2>
      <p class="lede">EMA stack, MACD, RS, pullback entries. Win% ≈ 5-session forward return.</p>
      <div class="cards" id="cardsWeekly"></div>
      <div class="panel"><div id="tableWeekly" class="empty"></div></div>
    </section>

    <section class="tabpane" id="tab-swing">
      <h2>Swing — 1 to 3 months</h2>
      <p class="lede">Stage analysis, trend structure, medium RS, dip buys. Win% ≈ 42-session (~2mo) forward return.</p>
      <div class="cards" id="cardsSwing"></div>
    </section>

    <section class="tabpane" id="tab-challenge">
      <h2>$1,000 → $1,000,000 challenge</h2>
      <p class="lede">
        Swing / LEAP <strong>calls &amp; puts</strong> across mega + <strong>mid/small</strong> optionables.
        Sure-shot hist filter (prefer <strong>100% hist win</strong>, else ≥80% n≥5).
        Status: <strong>ENTRY · HOLD · EXIT</strong> with hold periods. Earnings bias:
        prefer <strong>post-print continuation</strong>; caution/LEAP into the print.
        <em>Hist 100% ≠ guaranteed future wins.</em>
      </p>
      <div class="metric-row" id="challengeMetrics"></div>
      <div class="cards" id="challengePrimary"></div>
      <div class="panel">
        <h2>Challenge update — ENTRY / HOLD / EXIT</h2>
        <div id="challengeStatus" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Open sleeve &amp; closed flips</h2>
        <div id="challengeBook" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Compounding path &amp; hold periods</h2>
        <div id="challengePath" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Recommended tickets — side · strike · expiry · hold · status</h2>
        <div id="challengeTickets" class="empty">—</div>
      </div>
      <p class="lede" id="challengeDisclaimer" style="font-size:.72rem"></p>
    </section>

    <section class="tabpane" id="tab-echo">
      <h2>Echo Desk — TradeEcho-style terminal</h2>
      <p class="lede">
        Modules inspired by <a href="https://tradeecho.com/" target="_blank" rel="noopener" style="color:var(--accent)">Trade Echo</a>:
        OptionFlow · DealerEdge (GEX) · Darkpool (FINRA ATS) · AlgoEdge · Pulse · Mirror · Cortex.
        Built from Yahoo chains + <a href="https://www.finra.org/filing-reporting/otc-transparency" target="_blank" rel="noopener" style="color:var(--accent)">FINRA OTC Transparency</a> — <strong>not affiliated</strong> with Trade Echo.
      </p>
      <div class="metric-row" id="echoMetrics"></div>
      <div class="panel">
        <h2>Cortex briefing</h2>
        <p class="lede" id="echoCortex" style="margin:0">—</p>
      </div>
      <div class="echo-grid" style="margin-top:1rem">
        <div class="panel" style="margin:0">
          <h2>OptionFlow</h2>
          <p class="lede" style="margin-top:0;font-size:.76rem">Golden / Unusual / Aggressive tiers from volume, OI, premium notional.</p>
          <div id="echoFlow" class="empty">—</div>
        </div>
        <div class="panel" style="margin:0">
          <h2>DealerEdge (GEX)</h2>
          <p class="lede" style="margin-top:0;font-size:.76rem">Call/put walls, flip, HVL from OI + BS gamma proxy.</p>
          <div id="echoGex" class="empty">—</div>
        </div>
      </div>
      <div class="panel">
        <h2>Darkpool</h2>
        <div id="echoDark" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>AlgoEdge channels</h2>
        <div id="echoAlgo" class="empty">—</div>
      </div>
      <div class="echo-grid">
        <div class="panel" style="margin:0">
          <h2>Pulse — tape + levels</h2>
          <div id="echoPulse" class="empty">—</div>
        </div>
        <div class="panel" style="margin:0">
          <h2>Mirror — paper copy desk</h2>
          <div id="echoMirror" class="empty">—</div>
        </div>
      </div>
      <p class="lede" id="echoDisclaimer" style="font-size:.72rem;margin-top:1rem"></p>
    </section>

    <section class="tabpane" id="tab-screener">
      <h2>Market screener</h2>
      <p class="lede">Ranked liquid universe across horizons. Default <strong>Scan focus</strong> = 46 optionable names; <strong>Scan liquid universe</strong> = ~147 S&amp;P100 + high-volume optionables (Yahoo rate limits block a full-market scan).</p>
      <div id="screener" class="empty">Run a liquid scan to populate.</div>
    </section>

    <section class="tabpane" id="tab-journal">
      <h2>Journal &amp; insights</h2>
      <div id="journal" class="empty">No journal trades yet.</div>
    </section>

    <footer>
      Quality gates require score + multiple confirming algos before a signal counts — fewer trades, higher measured win rates.
      Win% is underlying direction, not option P&amp;L. Research only — not affiliated with Signa, Intellectia, or Trade Echo.
    </footer>
  </div>
  <script>
    let DATA = {};
    const fmt = (n, d=2) => (n==null || Number.isNaN(Number(n))) ? "—" : Number(n).toFixed(d);
    const pctClass = (n) => (n||0) >= 0 ? "up" : "down";

    document.querySelectorAll("#tabs button").forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll("#tabs button").forEach(b => b.classList.remove("active"));
        document.querySelectorAll(".tabpane").forEach(p => p.classList.remove("active"));
        btn.classList.add("active");
        document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
      };
    });

    function winLookup(symbol, hz) {
      const table = DATA.win_rates || {};
      const row = (table.symbols || {})[symbol] || {};
      const s = row[hz] || {};
      return { pct: s.win_pct, n: s.trades || 0, hit1: s.hit_1pct, hit2: s.hit_2pct };
    }

    function cardHTML(t, hz) {
      const long = t.quality || (t.ensemble_score||0) >= 70;
      const conf = Math.round(t.ensemble_score||0);
      const w = winLookup(t.symbol, hz);
      const dir = long ? "LONG" : "WAIT";
      const winLabel = w.pct==null ? "—" : `${fmt(w.pct,0)}%`;
      const nLabel = w.pct==null ? "—" : `${w.n} samples`;
      const strikeRate = w.hit1==null ? "—" : `${fmt(w.hit1,0)}% ≥1%` + (w.hit2==null?"":` / ${fmt(w.hit2,0)}% ≥2%`);
      return `<article class="action-card ${long?"long":"wait"}">
        <div class="ac-top">
          <div class="ac-sym">${t.symbol}</div>
          <div class="ac-dir ${long?"long":"wait"}">${dir}</div>
        </div>
        <div class="ac-conf">Confidence ${conf}% · ${t.confirms||0} confirms${t.quality?" · quality":""}</div>
        <div class="bar"><i style="width:${Math.min(100,conf)}%"></i></div>
        <div class="ac-meta">
          <div>Entry<strong>${fmt(t.entry||t.last_price,2)}</strong></div>
          <div>Stop<strong>${fmt(t.stop,2)}</strong></div>
          <div>Target<strong>${fmt(t.target,2)}</strong></div>
          <div>R:R<strong>${t.risk_reward==null?"—":fmt(t.risk_reward,1)+":1"}</strong></div>
          <div>Hist win<strong>${winLabel}</strong></div>
          <div title="Sample size: number of historical quality signals">${w.n < 8 && w.pct!=null ? "n (low)" : "n"}<strong>${nLabel}</strong></div>
          <div title="How often underlying ripped after signal">Strike rate<strong>${strikeRate}</strong></div>
          <div>Exp move<strong>${fmt(t.expected_move_pct,1)}%</strong></div>
        </div>
        <p class="why" style="margin:.55rem 0 0">${(t.reasons||[]).filter(r=>!r.includes("/")).slice(0,4).join(" · ")||"—"}</p>
      </article>`;
    }

    function renderCards(elId, list, hz) {
      const el = document.getElementById(elId);
      const rows = (list||[]).slice(0, 12);
      if (!rows.length) { el.innerHTML = `<div class="empty">No quality setups yet — run a scan.</div>`; return; }
      el.innerHTML = rows.map(t => cardHTML(t, hz)).join("");
    }

    function renderOptionTable(elId, rows) {
      const el = document.getElementById(elId);
      if (!rows || !rows.length) { el.innerHTML = `<div class="empty">No listed calls in this bucket.</div>`; return; }
      el.innerHTML = `<table><thead><tr>
        <th>Action</th><th>Symbol</th><th>Call strike</th><th>Expiry</th><th>Bid/Ask</th><th>Score</th><th>Hist win</th><th>n</th><th>Strike rate</th><th>Why</th>
      </tr></thead><tbody>${rows.map(r=>{
        const a=(r.action||"WAIT").replace("_"," ");
        const cls=(r.action||"WAIT").toLowerCase().split("_")[0];
        const win=r.win_pct==null?"—":`${fmt(r.win_pct,0)}%`;
        const n=r.win_pct==null?"—":`${r.win_samples||0}`;
        const sr=r.hit_1pct==null?"—":`${fmt(r.hit_1pct,0)}% ≥1%`+(r.hit_2pct==null?"":` · ${fmt(r.hit_2pct,0)}% ≥2%`);
        return `<tr>
          <td><span class="badge ${cls}">${a}</span></td>
          <td><strong>${r.symbol}</strong></td>
          <td class="mono">${r.strike==null?"—":fmt(r.strike,2)}</td>
          <td class="mono">${r.expiry||"—"} <span class="status">DTE ${r.dte??"—"}</span></td>
          <td class="mono">${fmt(r.bid,2)} / ${fmt(r.ask,2)}</td>
          <td class="mono">${fmt(r.score,0)}</td>
          <td class="mono">${win}</td>
          <td class="mono" title="Historical sample size">${n}</td>
          <td class="mono" title="Underlying rip frequency after signal">${sr}</td>
          <td class="why">${r.detail||""}</td>
        </tr>`;
      }).join("")}</tbody></table>`;
    }

    function lotteryCard(r) {
      const act = (r.action||"WAIT");
      const kind = act.startsWith("BUY") ? "long" : act.startsWith("SELL") ? "short" : "wait";
      const tags = (r.playbook||[]).slice(0,6).map(p => `<span class="tag">${p}</span>`).join("");
      return `<article class="action-card ${kind}">
        <div class="ac-top">
          <div class="ac-sym">${r.symbol}</div>
          <div class="ac-dir ${kind}">${act.replaceAll("_"," ")}</div>
        </div>
        <div class="ac-conf">Strength ${fmt(r.strength,0)} · ${r.confirms||0} confirms${r.best_mult!=null?` · ~${fmt(r.best_mult,0)}× upside`:""}</div>
        <div class="bar"><i style="width:${Math.min(100,r.strength||0)}%"></i></div>
        <div class="ac-meta">
          <div>Strike<strong>${r.strike==null?"—":fmt(r.strike,2)+"c"}</strong></div>
          <div>Ask / Bid<strong>${fmt(r.ask,2)} / ${fmt(r.bid,2)}</strong></div>
          <div>@+3% / +5%<strong>${fmt(r.mult_at_3pct,1)}× / ${fmt(r.mult_at_5pct,1)}×</strong></div>
          <div>Lottery score<strong>${fmt(r.lottery_score,0)}</strong></div>
          <div>Tape 5m / 15m<strong>${r.mom_5m_pct==null?"—":fmt(r.mom_5m_pct,2)+"%"} / ${r.mom_15m_pct==null?"—":fmt(r.mom_15m_pct,2)+"%"}</strong></div>
          <div>Unreal%<strong>${r.option_unrealized_pct==null?"—":fmt(r.option_unrealized_pct,0)+"%"}</strong></div>
        </div>
        <p class="why" style="margin:.55rem 0 0">${r.detail||r.headline||"—"}</p>
        <div class="playbook">${tags}</div>
      </article>`;
    }

    function lotteryActionRows(rows) {
      if (!rows || !rows.length) return `<div class="empty">None right now.</div>`;
      return `<table><thead><tr>
        <th>Action</th><th>Symbol</th><th>Contract</th><th>Ask/Bid</th><th>@+3%</th><th>Strength</th><th>Why</th>
      </tr></thead><tbody>${rows.map(r=>{
        const a=(r.action||"WAIT").replaceAll("_"," ");
        const cls=(r.action||"WAIT").toLowerCase().split("_")[0];
        return `<tr>
          <td><span class="badge ${cls}">${a}</span></td>
          <td><strong>${r.symbol}</strong></td>
          <td class="mono">${r.strike==null?"—":fmt(r.strike,2)+"c"} ${r.expiry||""} <span class="status">DTE ${r.dte??"—"}</span></td>
          <td class="mono">${fmt(r.ask,2)} / ${fmt(r.bid,2)}</td>
          <td class="mono up">${fmt(r.mult_at_3pct,1)}×</td>
          <td class="mono">${fmt(r.strength,0)}</td>
          <td class="why">${r.detail||""}${(r.vetoes&&r.vetoes.length)?` · veto: ${r.vetoes.slice(0,2).join("; ")}`:""}</td>
        </tr>`;
      }).join("")}</tbody></table>`;
    }

    function renderExplosive(rows, lottery) {
      const mini = document.getElementById("explosiveMini");
      const table = document.getElementById("explosiveTable");
      const metrics = document.getElementById("explosiveMetrics");
      const primaryEl = document.getElementById("lotteryPrimary");
      const buyEl = document.getElementById("lotteryBuy");
      const sellEl = document.getElementById("lotterySell");
      const waitEl = document.getElementById("lotteryWait");
      const list = rows || [];
      const lot = lottery || {};
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      const counts = lot.counts || {};
      metrics.innerHTML = [
        m("BUY NOW", counts.buy_now||0, (counts.buy_now||0)>0?"up":""),
        m("SELL NOW", counts.sell_now||0, (counts.sell_now||0)>0?"down":""),
        m("WAIT", counts.wait||0),
        m("SKIP", counts.skip||0),
        m("Tickets scanned", list.length),
        m("Top mult", list[0] ? `${fmt(list[0].best_mult,0)}×` : "—", "up"),
      ].join("");

      const actionable = [...(lot.sell_now||[]), ...(lot.buy_now||[])].slice(0,4);
      if (primaryEl) {
        primaryEl.innerHTML = actionable.length
          ? actionable.map(lotteryCard).join("")
          : `<div class="empty">No lottery BUY/SELL cleared the playbook — see WAIT/SKIP for why tickets are gated.</div>`;
      }
      if (buyEl) buyEl.innerHTML = lotteryActionRows(lot.buy_now||[]);
      if (sellEl) sellEl.innerHTML = lotteryActionRows(lot.sell_now||[]);
      if (waitEl) {
        const gated = [...(lot.wait||[]), ...(lot.skip||[]).slice(0,8)];
        waitEl.innerHTML = lotteryActionRows(gated);
      }

      if (!list.length) {
        mini.innerHTML = `<div class="empty">No explosive tickets yet — need cheap 0DTE/1DTE calls with ≥3× convexity on a rip.</div>`;
        table.innerHTML = mini.innerHTML;
        return;
      }
      const rowHtml = (r) => `<tr>
        <td><strong>${r.symbol}</strong> <span class="tag">${r.dte<=0?"0DTE":r.dte+"DTE"}</span></td>
        <td class="mono">${fmt(r.strike,2)}c</td>
        <td class="mono">$${fmt(r.ask,2)}</td>
        <td class="mono">${fmt(r.moneyness_pct,2)}%</td>
        <td class="mono up"><strong>${fmt(r.mult_at_2pct,1)}×</strong></td>
        <td class="mono up">${fmt(r.mult_at_3pct,1)}×</td>
        <td class="mono up">${fmt(r.mult_at_5pct,1)}×</td>
        <td class="mono"><strong class="up">+${fmt(r.pct_gain_best,0)}%</strong> on +${fmt(r.best_move_pct,0)}%</td>
        <td class="mono">${fmt(r.lottery_score,0)}</td>
        <td class="why">${r.thesis||""}</td>
      </tr>`;
      const head = `<table><thead><tr>
        <th>Symbol</th><th>Strike</th><th>Ask</th><th>OTM%</th><th>On +2%</th><th>On +3%</th><th>On +5%</th><th>Best upside</th><th>Lottery</th><th>Why</th>
      </tr></thead><tbody>`;
      // Overview: actionable lottery first, else top gated tickets
      if (actionable.length) {
        mini.innerHTML = actionable.slice(0,3).map(lotteryCard).join("");
      } else {
        const topWait = (lot.wait||[]).slice(0,2);
        mini.innerHTML = topWait.length
          ? topWait.map(lotteryCard).join("") + `<p class="lede" style="margin-top:.6rem">${lot.playbook_note||""}</p>`
          : head + list.slice(0,4).map(rowHtml).join("") + "</tbody></table>";
      }
      table.innerHTML = head + list.map(rowHtml).join("") + "</tbody></table>";
    }

    function renderChallenge(ch) {
      const metrics = document.getElementById("challengeMetrics");
      const primaryEl = document.getElementById("challengePrimary");
      const pathEl = document.getElementById("challengePath");
      const ticketsEl = document.getElementById("challengeTickets");
      const statusEl = document.getElementById("challengeStatus");
      const bookEl = document.getElementById("challengeBook");
      const disc = document.getElementById("challengeDisclaimer");
      if (!ch || !Object.keys(ch).length) {
        if (ticketsEl) ticketsEl.innerHTML = `<div class="empty">Challenge board loading…</div>`;
        return;
      }
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      const path = ch.path || {};
      const c = ch.counts || {};
      const book = (ch.sync && ch.sync.book) || ch.book || {};
      const holdLbl = (t) => t.hold_approx_label || (t.hold_ideal_days!=null?`≈${t.hold_ideal_days}d (${t.hold_min_days||"?"}–${t.hold_max_days||"?"}d)`:(t.hold_period_label||"—"));
      const spotBadge = (t) => {
        const src=t.spot_source||"none";
        if (src==="live") return `<span class="badge buy">LIVE</span>`;
        if (src==="cache") return `<span class="badge wait">CACHE</span>`;
        if (src==="scan") return `<span class="badge skip">SCAN</span>`;
        return `<span class="badge sell">NO SPOT</span>`;
      };
      if (metrics) metrics.innerHTML = [
        m("Sleeve equity", book.equity!=null?`$${Number(book.equity).toLocaleString()}`:`$${(ch.start_usd||1000).toLocaleString()}`),
        m("Target", `$${(ch.target_usd||1000000).toLocaleString()}`, "up"),
        m("Need / flip", path.pct_per_flip==null?"—":`+${fmt(path.pct_per_flip,0)}%`, "up"),
        m("ENTRY / HOLD / EXIT", `${c.entry||0} / ${c.hold||0} / ${c.exit||0}`),
        m("Calls / Puts", `${c.calls||0} / ${c.puts||0}`),
        m("Approx hold", (ch.primary && holdLbl(ch.primary)) || "—"),
        m("Live / cache spot", `${c.live_spot||0} / ${c.cache_spot||0}`),
        m("Live asks", `${c.live_ask||0}/${c.tickets||0}`),
        m("Pre / Post earn", `${c.pre_earnings||0} / ${c.post_earnings||0}`),
        m("Closed flips", `${book.flips_closed||0} (W${book.wins||0}/L${book.losses||0})`),
      ].join("");

      const earnBadge = (t) => {
        const w = t.earnings_window||"none";
        if (w==="post_earnings") return `<span class="badge buy">POST-EARN</span>`;
        if (w==="pre_earnings") return `<span class="badge wait">PRE-EARN</span>`;
        if (w==="earnings_day") return `<span class="badge sell">EARN DAY</span>`;
        return `<span class="badge skip">—</span>`;
      };
      const reasonList = (t) => {
        const rs = t.reasons||[];
        if (!rs.length) return t.recommend_reason||t.thesis||"";
        return `<div class="why"><strong>${t.recommend_reason||""}</strong><ul style="margin:.25rem 0 0;padding-left:1.1rem">${rs.slice(0,7).map(r=>`<li>${r}</li>`).join("")}</ul></div>`;
      };

      const t0 = ch.primary;
      if (primaryEl) {
        if (!t0) primaryEl.innerHTML = `<div class="empty">No sure-shot swing/LEAP cleared the hist filter yet — run a scan.</div>`;
        else {
          const tier = t0.certainty_tier||"strong";
          const act = t0.action||"WAIT";
          const kind = act==="EXIT"?"short":(act==="ENTRY"||act==="HOLD"?"long":"wait");
          primaryEl.innerHTML = `<article class="action-card ${kind}">
            <div class="ac-top">
              <div class="ac-sym">${t0.symbol} <span class="tag">${t0.right==="P"?"PUT":"CALL"}</span> <span class="tag">${(t0.market_cap_tier||"").replace("_","/")}</span> ${earnBadge(t0)} ${spotBadge(t0)}</div>
              <div class="ac-dir ${kind}">${act} · ${tier.toUpperCase()}</div>
            </div>
            <div class="ac-conf">Hist win ${fmt(t0.hist_win_pct,0)}% · n=${t0.hist_samples} · <strong>approx hold ${holdLbl(t0)}</strong></div>
            <div class="bar"><i style="width:${Math.min(100,t0.hist_win_pct||0)}%"></i></div>
            <div class="ac-meta">
              <div>Approx hold<strong>${holdLbl(t0)}</strong></div>
              <div>Hold window<strong>${t0.hold_period_label||"—"}</strong></div>
              <div>Spot (${t0.spot_source||"—"})<strong>${t0.spot==null?"—":"$"+fmt(t0.spot,2)}</strong></div>
              <div>Strike / DTE<strong>${t0.strike==null?"—":fmt(t0.strike,2)+(t0.right==="P"?"p":"c")} · ${t0.dte??"—"}d</strong></div>
              <div>Ask → target<strong>${t0.ask==null?"zone only":"$"+fmt(t0.ask,2)} → ${t0.target_ask==null?"—":"$"+fmt(t0.target_ask,2)}</strong></div>
              <div>Days held<strong>${t0.hold_days==null?"not open":fmt(t0.hold_days,1)+"d"} / max ${t0.hold_max_days||"—"}d</strong></div>
            </div>
            <p class="why" style="margin:.55rem 0 0">${t0.status_detail||""}${t0.data_note?` · ${t0.data_note}`:""}</p>
            ${reasonList(t0)}
          </article>`;
        }
      }

      if (statusEl) {
        const rows = [...(ch.exit||[]), ...(ch.hold||[]), ...(ch.entry||[])].slice(0,12);
        statusEl.innerHTML = !rows.length
          ? `<div class="empty">No ENTRY/HOLD/EXIT updates yet.</div>`
          : `<table><thead><tr>
              <th>Status</th><th>Side</th><th>Symbol</th><th>Spot</th><th>Approx hold</th><th>Earn</th><th>Why recommended</th>
            </tr></thead><tbody>${rows.map(t=>{
              const a=(t.action||"WAIT");
              const cls=a==="EXIT"?"sell":(a==="ENTRY"?"buy":(a==="HOLD"?"hold":"wait"));
              return `<tr>
                <td><span class="badge ${cls}">${a}</span></td>
                <td class="mono">${t.right==="P"?"PUT":"CALL"}</td>
                <td><strong>${t.symbol}</strong> ${spotBadge(t)}</td>
                <td class="mono">${t.spot==null?"—":"$"+fmt(t.spot,2)} <span class="why">${t.spot_source||""}</span></td>
                <td class="mono"><strong>${holdLbl(t)}</strong>${t.hold_days!=null?` · held ${fmt(t.hold_days,1)}d`:""}</td>
                <td>${earnBadge(t)}</td>
                <td class="why">${t.recommend_reason||t.status_detail||t.thesis||""}</td>
              </tr>`;
            }).join("")}</tbody></table>`;
      }

      if (bookEl) {
        const open=(book.trades||[]).filter(t=>t.status==="open");
        const closed=(book.trades||[]).filter(t=>t.status==="closed").slice(-8).reverse();
        bookEl.innerHTML = `
          <div class="ac-meta" style="margin-bottom:.5rem">
            <div>Cash<strong>$${fmt(book.cash,0)}</strong></div>
            <div>Equity<strong>$${fmt(book.equity,0)}</strong></div>
            <div>Flips<strong>${book.flips_closed||0}</strong></div>
            <div>Win/Loss<strong>${book.wins||0}/${book.losses||0}</strong></div>
          </div>
          ${open.length?`<div class="status">OPEN</div><table><thead><tr>
            <th>Action</th><th>Side</th><th>Sym</th><th>Entry</th><th>Mark</th><th>Unreal%</th><th>Held</th><th>Max hold</th>
          </tr></thead><tbody>${open.map(t=>`<tr>
            <td><span class="badge ${t.last_action==="EXIT"?"sell":"hold"}">${t.last_action||"HOLD"}</span></td>
            <td class="mono">${t.right==="P"?"PUT":"CALL"}</td>
            <td><strong>${t.symbol}</strong></td>
            <td class="mono">$${fmt(t.entry_ask,2)}</td>
            <td class="mono">$${fmt(t.mark,2)}</td>
            <td class="mono ${pctClass(t.unrealized_pct)}">${t.unrealized_pct==null?"—":fmt(t.unrealized_pct,1)+"%"}</td>
            <td class="mono">${t.hold_days==null?"—":fmt(t.hold_days,1)+"d"}</td>
            <td class="mono">${t.hold_max_days||"—"}d</td>
          </tr>`).join("")}</tbody></table>`:`<div class="empty">No open challenge flip.</div>`}
          ${closed.length?`<div class="status" style="margin-top:.7rem">CLOSED</div><table><thead><tr>
            <th>Side</th><th>Sym</th><th>In→Out</th><th>P&L%</th><th>Held</th><th>Exit</th>
          </tr></thead><tbody>${closed.map(t=>`<tr>
            <td class="mono">${t.right==="P"?"PUT":"CALL"}</td>
            <td><strong>${t.symbol}</strong></td>
            <td class="mono">$${fmt(t.entry_ask,2)}→$${fmt(t.exit_bid,2)}</td>
            <td class="mono ${pctClass(t.profit_pct)}">${t.profit_pct==null?"—":fmt(t.profit_pct,1)+"%"}</td>
            <td class="mono">${t.hold_days==null?"—":fmt(t.hold_days,1)+"d"}</td>
            <td class="why">${t.exit_reason||""}</td>
          </tr>`).join("")}</tbody></table>`:""}`;
      }

      if (pathEl) {
        const paths = ch.paths || [];
        const hp = ch.hold_periods || {};
        pathEl.innerHTML = `
          <p class="lede" style="margin-top:0">${path.note||""}</p>
          <div class="playbook" style="margin-bottom:.55rem">
            ${["weekly","swing","leap"].map(k=>{
              const h=hp[k]||{};
              return `<span class="tag">${k}: ${h.label||"—"}</span>`;
            }).join("")}
          </div>
          <table><thead><tr><th>Flips</th><th>Need / flip</th><th>Multiple / flip</th></tr></thead>
          <tbody>${paths.map(p=>`<tr>
            <td class="mono">${p.flips}</td>
            <td class="mono up"><strong>+${fmt(p.pct_per_flip,0)}%</strong></td>
            <td class="mono">${fmt(p.mult_per_flip,2)}×</td>
          </tr>`).join("")}</tbody></table>
          <ul class="lede" style="font-size:.76rem">${(ch.rules||[]).map(r=>`<li>${r}</li>`).join("")}</ul>`;
      }

      const tickets = ch.tickets || [];
      if (ticketsEl) {
        if (!tickets.length) ticketsEl.innerHTML = `<div class="empty">No tickets.</div>`;
        else ticketsEl.innerHTML = `<table><thead><tr>
          <th>Status</th><th>Tier</th><th>Side</th><th>Symbol</th><th>Spot</th><th>Approx hold</th><th>Hist</th><th>Ask→tgt</th><th>Reason</th>
        </tr></thead><tbody>${tickets.map(t=>{
          const a=t.action||"WAIT";
          const cls=a==="EXIT"?"sell":(a==="ENTRY"?"buy":(a==="HOLD"?"hold":"wait"));
          return `<tr>
          <td><span class="badge ${cls}">${a}</span></td>
          <td><span class="badge ${t.certainty_tier==="perfect"?"golden":(t.certainty_tier==="elite"?"unusual":"aggressive")}">${(t.certainty_tier||"").toUpperCase()}</span></td>
          <td class="mono">${t.right==="P"?"PUT":"CALL"}</td>
          <td><strong>${t.symbol}</strong> ${spotBadge(t)} ${earnBadge(t)}</td>
          <td class="mono">${t.spot==null?"—":"$"+fmt(t.spot,2)}<div class="why">${t.data_note||t.spot_source||""}</div></td>
          <td class="mono"><strong>${holdLbl(t)}</strong><div class="why">${t.hold_period_label||""}</div></td>
          <td class="mono up"><strong>${fmt(t.hist_win_pct,0)}%</strong> <span class="why">n=${t.hist_samples}</span></td>
          <td class="mono">${t.ask==null?"zone / no live ask":"$"+fmt(t.ask,2)} → ${t.target_ask==null?"—":"$"+fmt(t.target_ask,2)}<div class="why">${t.strike==null?"—":"K "+fmt(t.strike,2)} · ${t.dte??"—"}d</div></td>
          <td class="why">${t.recommend_reason||t.status_detail||""}${(t.reasons&&t.reasons.length)?`<div style="margin-top:.2rem;color:var(--muted)">${t.reasons.slice(0,3).join(" · ")}</div>`:""}</td>
        </tr>`;
        }).join("")}</tbody></table>`;
      }
      if (disc) disc.textContent = ch.disclaimer || "";
    }

    function renderDarkpoolMini(echo) {
      const el = document.getElementById("darkpoolMini");
      if (!el) return;
      const dp = (echo && echo.dark_pool) || {};
      if (!dp.available) {
        el.innerHTML = `<div class="empty">${dp.reason || "FINRA ATS not loaded yet — open Echo Desk."}</div>`;
        return;
      }
      const rows = dp.rows || [];
      el.innerHTML = `<div class="status" style="margin-bottom:.35rem">FINRA week <strong>${dp.week_start||"—"}</strong> · ${dp.delay_note||""}</div>
        <table><thead><tr><th>Sym</th><th>ATS shares</th><th>WoW</th><th>Surge</th><th>Top venue</th></tr></thead>
        <tbody>${rows.slice(0,6).map(r=>`<tr>
          <td><strong>${r.symbol}</strong></td>
          <td class="mono">${Number(r.shares||0).toLocaleString()}</td>
          <td class="mono ${pctClass(r.wow_pct)}">${r.wow_pct==null?"—":fmt(r.wow_pct,1)+"%"}</td>
          <td class="mono">${r.surge_ratio==null?"—":fmt(r.surge_ratio,2)+"×"}</td>
          <td class="why">${(r.venues&&r.venues[0]&&r.venues[0].name)||"—"}</td>
        </tr>`).join("")}</tbody></table>`;
    }

    function renderEcho(echo) {
      const metrics = document.getElementById("echoMetrics");
      const flowEl = document.getElementById("echoFlow");
      const gexEl = document.getElementById("echoGex");
      const darkEl = document.getElementById("echoDark");
      const algoEl = document.getElementById("echoAlgo");
      const pulseEl = document.getElementById("echoPulse");
      const mirrorEl = document.getElementById("echoMirror");
      const cortexEl = document.getElementById("echoCortex");
      const disc = document.getElementById("echoDisclaimer");
      if (!echo || !Object.keys(echo).length) {
        if (metrics) metrics.innerHTML = "";
        if (flowEl) flowEl.innerHTML = `<div class="empty">Echo Desk not loaded — wait for snapshot refresh.</div>`;
        return;
      }
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      const fc = (echo.option_flow && echo.option_flow.counts) || {};
      const prim = (echo.dealer_edge && echo.dealer_edge.primary) || {};
      const dpc = (echo.dark_pool && echo.dark_pool.counts) || {};
      if (metrics) metrics.innerHTML = [
        m("Symbols", (echo.symbols||[]).length),
        m("Golden flow", fc.golden||0, "up"),
        m("DP surges", dpc.surges||0, (dpc.surges||0)>0?"up":""),
        m("Flow bull/bear", `${fc.bullish||0}/${fc.bearish||0}`),
        m("GEX regime", prim.regime ? String(prim.regime).replaceAll("_"," ") : "—"),
        m("ATS week", (echo.dark_pool && echo.dark_pool.week_start) || "—"),
      ].join("");

      const cx = echo.cortex || {};
      if (cortexEl) {
        cortexEl.innerHTML = `<strong style="color:var(--ink)">${cx.headline||"Echo briefing"}</strong><br/>`
          + (cx.summary || "")
          + ((cx.bullets||[]).length ? `<br/><span class="status">${cx.bullets.join(" · ")}</span>` : "");
      }

      const prints = (echo.option_flow && echo.option_flow.prints) || [];
      if (flowEl) {
        if (!prints.length) flowEl.innerHTML = `<div class="empty">No unusual prints — need chain volume on focus names.</div>`;
        else flowEl.innerHTML = `<table><thead><tr>
          <th>Tier</th><th>Sym</th><th>Side</th><th>Strike</th><th>Vol</th><th>OI</th><th>Premium</th><th>Score</th>
        </tr></thead><tbody>${prints.slice(0,18).map(p=>`<tr>
          <td><span class="badge ${p.tier||"aggressive"}">${(p.tier||"").toUpperCase()}</span></td>
          <td><strong>${p.symbol}</strong></td>
          <td class="mono">${p.right}</td>
          <td class="mono">${fmt(p.strike,2)}</td>
          <td class="mono">${p.volume??"—"}</td>
          <td class="mono">${p.open_interest??"—"}</td>
          <td class="mono">$${fmt((p.premium_notional||0)/1000,1)}k</td>
          <td class="mono ${p.flow_score>=0?"up":"down"}">${fmt(p.flow_score,0)}</td>
        </tr>`).join("")}</tbody></table>
        <p class="lede" style="font-size:.72rem;margin:.4rem 0 0">${echo.option_flow.note||""}</p>`;
      }

      if (gexEl) {
        const profiles = (echo.dealer_edge && echo.dealer_edge.profiles) || [];
        if (!profiles.length) gexEl.innerHTML = `<div class="empty">No GEX profiles yet.</div>`;
        else {
          const p = profiles[0];
          const rows = (p.by_strike||[]).slice(-12);
          const maxAbs = Math.max(1, ...rows.map(r=>Math.abs(r.gex||0)));
          gexEl.innerHTML = `
            <div class="ac-meta" style="margin-bottom:.55rem">
              <div>Symbol<strong>${p.symbol}</strong></div>
              <div>Spot<strong>${fmt(p.spot,2)}</strong></div>
              <div>Flip<strong>${p.flip??"—"}</strong></div>
              <div>HVL<strong>${p.hvl??"—"}</strong></div>
              <div>Call wall<strong class="up">${p.call_wall??"—"}</strong></div>
              <div>Put wall<strong class="down">${p.put_wall??"—"}</strong></div>
            </div>
            <table><thead><tr><th>Strike</th><th>Call OI</th><th>Put OI</th><th>GEX</th><th></th></tr></thead>
            <tbody>${rows.map(r=>{
              const w = Math.min(100, Math.abs(r.gex||0)/maxAbs*100);
              return `<tr>
                <td class="mono">${fmt(r.strike,2)}</td>
                <td class="mono">${r.call_oi}</td>
                <td class="mono">${r.put_oi}</td>
                <td class="mono ${r.gex>=0?"up":"down"}">${fmt(r.gex,0)}</td>
                <td><div class="gex-bar"><i style="width:${w}%;background:${r.gex>=0?"var(--long)":"var(--short)"}"></i></div></td>
              </tr>`;
            }).join("")}</tbody></table>
            <p class="lede" style="font-size:.72rem;margin:.4rem 0 0">${echo.dealer_edge.note||""}</p>`;
        }
      }

      const dp = echo.dark_pool || {};
      if (darkEl) {
        if (dp.available === false) {
          darkEl.innerHTML = `<div class="empty">${dp.reason||"Dark pool unavailable."}${dp.source_url?` · <a href="${dp.source_url}" target="_blank" rel="noopener" style="color:var(--accent)">FINRA OTC Transparency</a>`:""}</div>`;
        } else {
          const rows = dp.rows || [];
          const venues = dp.top_venues || [];
          darkEl.innerHTML = `
            <p class="lede" style="margin-top:0;font-size:.76rem">
              Source: <a href="${dp.source_url||"https://www.finra.org/filing-reporting/otc-transparency"}" target="_blank" rel="noopener" style="color:var(--accent)">FINRA ATS weekly</a>
              · week ${dp.week_start||"—"} · ${dp.delay_note||""}
            </p>
            ${rows.length?`<table><thead><tr>
              <th>Flag</th><th>Sym</th><th>ATS shares</th><th>Trades</th><th>Avg size</th><th>WoW</th><th>Surge</th><th>Levels</th><th>Top venues</th>
            </tr></thead><tbody>${rows.slice(0,12).map(r=>{
              const lv=(r.levels||[]).slice(0,4).map(l=>`${l.tag} ${fmt(l.price,2)}`).join(" · ") || "—";
              const ven=(r.venues||[]).slice(0,3).map(v=>v.name||v.mpid).join(", ") || "—";
              const flagCls = r.flag==="surge"?"up":(r.flag==="drop"?"down":"");
              return `<tr>
                <td><span class="badge ${r.flag==="surge"?"golden":(r.flag==="drop"?"sell":"wait")}">${(r.flag||"").toUpperCase()}</span></td>
                <td><strong>${r.symbol}</strong></td>
                <td class="mono">${r.shares==null?"—":Number(r.shares).toLocaleString()}</td>
                <td class="mono">${r.trades==null?"—":Number(r.trades).toLocaleString()}</td>
                <td class="mono">${fmt(r.avg_trade_size,0)}</td>
                <td class="mono ${pctClass(r.wow_pct)}">${r.wow_pct==null?"—":fmt(r.wow_pct,1)+"%"}</td>
                <td class="mono ${flagCls}">${r.surge_ratio==null?"—":fmt(r.surge_ratio,2)+"×"}</td>
                <td class="why">${lv}</td>
                <td class="why">${ven}</td>
              </tr>`;
            }).join("")}</tbody></table>`:`<div class="empty">No FINRA ATS rows for focus symbols this week.</div>`}
            ${venues.length?`<p class="status" style="margin:.55rem 0 .2rem">Top ATS venues (aggregated)</p>
              <div class="playbook">${venues.slice(0,8).map(v=>`<span class="tag">${v.name}: ${(v.shares||0).toLocaleString()}</span>`).join("")}</div>`:""}
            <p class="lede" style="font-size:.72rem;margin:.45rem 0 0">${dp.levels_note||""}</p>`;
        }
      }

      const algo = echo.algo_edge || {};
      const chans = algo.channels || {};
      if (algoEl) {
        const names = algo.channel_names || Object.keys(chans);
        algoEl.innerHTML = names.map(name=>{
          const rows = chans[name] || [];
          if (!rows.length) return `<div class="status" style="margin:.35rem 0"><span class="tag">${name}</span> —</div>`;
          return `<div style="margin:.45rem 0 .7rem"><span class="tag">${name}</span>
            <table><thead><tr><th>Symbol</th><th>Hz</th><th>Score</th><th>Confirms</th><th>Algos</th></tr></thead>
            <tbody>${rows.slice(0,6).map(r=>`<tr>
              <td><strong>${r.symbol}</strong>${r.quality?` <span class="tag">Q</span>`:""}</td>
              <td class="mono">${r.horizon||"—"}</td>
              <td class="mono">${fmt(r.ensemble_score,0)}</td>
              <td class="mono">${r.confirms??"—"}</td>
              <td class="why">${(r.active_algos||[]).slice(0,4).join(", ")}</td>
            </tr>`).join("")}</tbody></table></div>`;
        }).join("") + `<p class="lede" style="font-size:.72rem">${algo.note||""}</p>`;
      }

      const tape = (echo.pulse && echo.pulse.tape) || [];
      if (pulseEl) {
        pulseEl.innerHTML = !tape.length ? `<div class="empty">No tape.</div>` : `<table><thead><tr>
          <th>Sym</th><th>Last</th><th>Session</th><th>5m</th><th>Entry</th><th>Stop</th><th>Target</th>
        </tr></thead><tbody>${tape.slice(0,12).map(r=>`<tr>
          <td><strong>${r.symbol}</strong></td>
          <td class="mono">${fmt(r.last,2)}</td>
          <td class="mono ${pctClass(r.session_change_pct)}">${r.session_change_pct==null?"—":fmt(r.session_change_pct,2)+"%"}</td>
          <td class="mono ${pctClass(r.mom_5m_pct)}">${r.mom_5m_pct==null?"—":fmt(r.mom_5m_pct,2)+"%"}</td>
          <td class="mono">${fmt(r.entry,2)}</td>
          <td class="mono">${fmt(r.stop,2)}</td>
          <td class="mono">${fmt(r.target,2)}</td>
        </tr>`).join("")}</tbody></table>`;
      }

      const mir = echo.mirror || {};
      if (mirrorEl) {
        const open = mir.open || [];
        const perf = mir.performance || {};
        mirrorEl.innerHTML = `
          <div class="ac-meta" style="margin-bottom:.5rem">
            <div>Win rate<strong>${perf.win_rate_pct==null?"—":fmt(perf.win_rate_pct,1)+"%"}</strong></div>
            <div>Open<strong>${open.length}</strong></div>
            <div>Mode<strong>${mir.mode||"paper"}</strong></div>
          </div>
          ${open.length?`<table><thead><tr><th>Sym</th><th>Entry</th><th>Mark</th><th>Unreal%</th></tr></thead>
          <tbody>${open.slice(0,8).map(t=>`<tr>
            <td><strong>${t.symbol}</strong></td>
            <td class="mono">$${fmt(t.entry_ask,2)}</td>
            <td class="mono">$${fmt(t.mark,2)}</td>
            <td class="mono ${pctClass(t.unrealized_pct)}">${t.unrealized_pct==null?"—":fmt(t.unrealized_pct,1)+"%"}</td>
          </tr>`).join("")}</tbody></table>`:`<div class="empty">No open mirrored paper trades.</div>`}
          <p class="lede" style="font-size:.72rem;margin:.4rem 0 0">${mir.note||""}</p>`;
      }
      if (disc) disc.textContent = echo.disclaimer || "";
    }

    function renderScreener(horizons) {
      const el = document.getElementById("screener");
      const hz = horizons || {};
      const merge = {};
      ["0dte","weekly","swing"].forEach(h => {
        (hz[h]||[]).forEach(t => {
          if (!merge[t.symbol]) merge[t.symbol] = { symbol: t.symbol, last: t.last_price };
          merge[t.symbol][h] = t.ensemble_score;
          merge[t.symbol][h+"_q"] = t.quality;
          merge[t.symbol][h+"_c"] = t.confirms;
        });
      });
      const rows = Object.values(merge).sort((a,b)=>(b.swing||0)-(a.swing||0));
      if (!rows.length) { el.innerHTML = `<div class="empty">No screener data.</div>`; return; }
      el.innerHTML = `<table><thead><tr>
        <th>Symbol</th><th>Last</th><th>0DTE</th><th>1W</th><th>Swing</th><th>Hist win 0DTE / 1W / Swing</th>
      </tr></thead><tbody>${rows.slice(0,80).map(r=>{
        const w0=winLookup(r.symbol,"0dte"), ww=winLookup(r.symbol,"weekly"), ws=winLookup(r.symbol,"swing");
        const cell=(v,q)=> v==null?"—":`<span class="${q?"up":""}">${fmt(v,0)}</span>`;
        return `<tr>
          <td><strong>${r.symbol}</strong></td>
          <td class="mono">${fmt(r.last,2)}</td>
          <td class="mono">${cell(r["0dte"], r["0dte_q"])}</td>
          <td class="mono">${cell(r.weekly, r.weekly_q)}</td>
          <td class="mono">${cell(r.swing, r.swing_q)}</td>
          <td class="mono">${[w0,ww,ws].map(w=>w.pct==null?"—":fmt(w.pct,0)+"%").join(" / ")}</td>
        </tr>`;
      }).join("")}</tbody></table>`;
    }

    function renderInsights(ins) {
      const cards = document.getElementById("perfCards");
      const sum = document.getElementById("insightSummary");
      const journal = document.getElementById("journal");
      if (!ins) { cards.innerHTML=""; sum.textContent=""; return; }
      const p = ins.performance || {};
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      cards.innerHTML = [
        m("Journal win rate", p.win_rate_pct==null?"—":`${fmt(p.win_rate_pct,1)}%`),
        m("Avg profit%", p.avg_profit_pct==null?"—":`${fmt(p.avg_profit_pct,1)}%`, pctClass(p.avg_profit_pct)),
        m("Account", p.return_pct==null?"—":`${fmt(p.return_pct,2)}%`, pctClass(p.return_pct)),
        m("Universe", DATA.universe_size||"—"),
        m("Quality 0DTE", (DATA.action_cards&&DATA.action_cards["0dte_quality"]||[]).length),
        m("Quality swing", (DATA.action_cards&&DATA.action_cards.swing_quality||[]).length),
      ].join("");
      sum.textContent = ins.summary || "";
      const open = ins.open_positions||[], closed = ins.closed_trades||[];
      let html="";
      if (open.length) {
        html += `<div class="status" style="margin:.3rem 0">OPEN</div><table><thead><tr>
          <th>Symbol</th><th>Entered</th><th>Entry</th><th>Mark</th><th>Unreal%</th><th>Why</th></tr></thead><tbody>
          ${open.map(t=>`<tr><td><strong>${t.symbol}</strong></td>
          <td class="mono">${(t.entered_at||"").slice(0,16)}</td>
          <td class="mono">$${fmt(t.entry_ask,2)}</td><td class="mono">$${fmt(t.mark,2)}</td>
          <td class="mono ${pctClass(t.unrealized_pct)}">${t.unrealized_pct==null?"—":fmt(t.unrealized_pct,1)+"%"}</td>
          <td class="why">${t.entry_reason||""}</td></tr>`).join("")}</tbody></table>`;
      }
      if (closed.length) {
        html += `<div class="status" style="margin:.8rem 0 .3rem">CLOSED</div><table><thead><tr>
          <th>Symbol</th><th>In→Out</th><th>Profit%</th><th>P&L</th><th>Hold</th><th>Exit</th></tr></thead><tbody>
          ${closed.map(t=>`<tr><td><strong>${t.symbol}</strong></td>
          <td class="mono">$${fmt(t.entry_ask,2)}→$${fmt(t.exit_bid,2)}</td>
          <td class="mono ${pctClass(t.profit_pct)}"><strong>${fmt(t.profit_pct,1)}%</strong></td>
          <td class="mono ${pctClass(t.pnl_usd)}">$${fmt(t.pnl_usd,2)}</td>
          <td class="mono">${fmt(t.hold_minutes,0)}m</td>
          <td class="why">${t.exit_reason||""}</td></tr>`).join("")}</tbody></table>`;
      }
      journal.innerHTML = html || `<div class="empty">No journal trades yet.</div>`;
    }

    function paint() {
      const ac = DATA.action_cards || {};
      const hz = DATA.horizons || {};
      renderCards("overviewCards", [
        ...(ac["0dte_quality"]||[]).slice(0,4),
        ...(ac.weekly_quality||[]).slice(0,4),
        ...(ac.swing_quality||[]).slice(0,4),
      ].map(t => ({...t, _hz: t.horizon||"0dte"})), "0dte");
      // fix overview cards to use each card's horizon for win%
      const overview = document.getElementById("overviewCards");
      const mix = [
        ...(ac["0dte_quality"]||[]).slice(0,4).map(t=>({t,hz:"0dte"})),
        ...(ac.weekly_quality||[]).slice(0,4).map(t=>({t,hz:"weekly"})),
        ...(ac.swing_quality||[]).slice(0,4).map(t=>({t,hz:"swing"})),
      ];
      overview.innerHTML = mix.length ? mix.map(({t,hz}) => cardHTML(t,hz)).join("") : `<div class="empty">No quality cards — run Scan.</div>`;

      renderCards("cards0dte", ac["0dte_quality"] || (hz["0dte"]||[]).filter(x=>x.quality).slice(0,12), "0dte");
      renderCards("cardsWeekly", ac.weekly_quality || (hz.weekly||[]).filter(x=>x.quality).slice(0,12), "weekly");
      renderCards("cardsSwing", ac.swing_quality || (hz.swing||[]).filter(x=>x.quality).slice(0,12), "swing");

      const acts = DATA.actions || {};
      renderOptionTable("boardMini", (acts.all||[]).slice(0,10));
      renderOptionTable("table0dte", (acts.all||[]).filter(r=>(r.dte_bucket||"0dte")==="0dte" || (r.dte!=null && r.dte<=1)));
      renderOptionTable("tableWeekly", (acts.all||[]).filter(r=>r.dte_bucket==="weekly"));
      renderExplosive(DATA.explosive || [], DATA.lottery || {});
      renderEcho(DATA.echo || {});
      renderDarkpoolMini(DATA.echo || {});
      renderChallenge(DATA.challenge || {});
      renderScreener(hz);
      renderInsights(DATA.insights);
      document.getElementById("session").textContent = (DATA.session||"—") + " · " + (DATA.universe_mode||"focus");
      const uniPill = document.getElementById("universePill");
      if (uniPill) {
        uniPill.textContent = `Scanning ${DATA.universe_size??"—"} tickers` +
          (DATA.focus_size!=null ? ` · focus ${DATA.focus_size}` : "") +
          (DATA.liquid_size!=null ? ` · liquid ${DATA.liquid_size}` : "");
      }
      document.getElementById("updated").textContent = "Updated " + (DATA.generated_at||"").replace("T"," ").slice(0,19);
      const c = acts.counts || {};
      const lc = (DATA.lottery && DATA.lottery.counts) || {};
      document.getElementById("counts").textContent =
        `BUY ${c.buy_now||0} · SELL ${c.sell_now||0} · WAIT ${c.wait||0} · LOTTO B/S ${lc.buy_now||0}/${lc.sell_now||0}`;
      const gate = acts.hist_win_gate || DATA.hist_win_gate || {};
      const gateEl = document.getElementById("histWinGate");
      if (gateEl) {
        const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
        const ok = gate.target_met;
        gateEl.innerHTML = [
          m("Hist-win gate", gate.require===false ? "off" : `≥${gate.min_hist_win_pct??80}%`),
          m("Eligible rows", gate.eligible_count??0, (gate.eligible_count||0)>0?"up":""),
          m("Pooled win (eligible)", gate.pooled_win_pct==null?"—":`${fmt(gate.pooled_win_pct,1)}%`, ok?"up":"down"),
          m("Pooled n", gate.pooled_trades??0),
          m("Ungated win", gate.ungated_pooled_win_pct==null?"—":`${fmt(gate.ungated_pooled_win_pct,1)}%`),
        ].join("");
      }
    }

    async function loadAll() {
      const note = document.getElementById("loadNote");
      note.style.display = "block";
      note.textContent = "Refreshing…";
      try {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), 50000);
        const res = await fetch("/api/snapshot", { signal: ctrl.signal });
        clearTimeout(t);
        DATA = await res.json();
        paint();
        note.style.display = "none";
      } catch (e) {
        note.textContent = "Load failed: " + (e.message||e);
      }
    }

    async function runScan(mode) {
      const btn = mode==="liquid" ? document.getElementById("btnScanWide") : document.getElementById("btnScan");
      btn.disabled = true;
      const label = btn.textContent;
      btn.textContent = "Scanning…";
      try {
        await fetch("/api/scan?mode=" + encodeURIComponent(mode||"focus"), { method: "POST" });
        await new Promise(r => setTimeout(r, mode==="liquid" ? 20000 : 10000));
        await loadAll();
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    }

    document.getElementById("btnRefresh").onclick = loadAll;
    document.getElementById("btnScan").onclick = () => runScan("focus");
    document.getElementById("btnScanWide").onclick = () => runScan("liquid");
    loadAll();
    setInterval(loadAll, 15000);
  </script>
</body>
</html>
"""


def _read_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return None


def create_app(config_path: str | None = None) -> Flask:
    cfg_path = str(config_path or ROOT / "config.yaml")
    cfg = load_config(cfg_path)
    app = Flask(__name__)
    scan_lock = threading.Lock()
    actions_cfg = cfg.get("actions") or {}
    risk = cfg.get("risk") or {}

    @app.get("/")
    def index():
        return render_template_string(PAGE)

    @app.get("/api/snapshot")
    def snapshot():
        from odte_scanner.calendars import resolve_yahoo_symbol
        from odte_scanner.data.live_quotes import fetch_live_quote
        from odte_scanner.options.live_chain import refresh_candidate_quote

        scan = _read_json(ROOT / "outputs" / "latest_scan.json") or {}
        watch = _read_json(ROOT / "outputs" / "watch" / "latest_watch.json")
        ledger_path = Path(cfg.get("paper_trading", {}).get("ledger_path", "outputs/paper_ledger.json"))
        if not ledger_path.is_absolute():
            ledger_path = ROOT / ledger_path
        ledger = _read_json(ledger_path)
        quotes = dict((watch or {}).get("quotes") or {})

        merged: list[dict] = []
        for key in ("call_candidates_0dte", "call_candidates_weekly", "call_candidates"):
            for c in scan.get(key) or []:
                merged.append(dict(c))
        deduped: list[dict] = []
        seen: set[str] = set()
        for item in merged:
            key = item.get("contract") or f"{item.get('symbol')}-{item.get('expiry')}-{item.get('strike')}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        board_rows = deduped[:14]
        syms = sorted({str(c.get("symbol")) for c in board_rows if c.get("symbol")})
        # Also include action-card symbols for win rates
        for bucket in (scan.get("action_cards") or {}).values():
            for t in bucket or []:
                if t.get("symbol"):
                    syms.append(str(t["symbol"]))
        syms = sorted(set(syms))
        aliases = {s: resolve_yahoo_symbol(s, cfg) for s in syms}

        win_table = scan.get("win_rates") or load_win_rate_table()
        # Merge full cache so challenge can see mid/small hist beyond this scan slice
        try:
            cached_wr = load_win_rate_table()
            if cached_wr and isinstance(win_table, dict):
                merged_syms = dict(cached_wr.get("symbols") or {})
                merged_syms.update(win_table.get("symbols") or {})
                win_table = {**cached_wr, **win_table, "symbols": merged_syms}
            elif cached_wr and not win_table:
                win_table = cached_wr
        except Exception:  # noqa: BLE001
            pass
        if syms and (
            not win_table
            or not set(syms).issubset(set((win_table.get("symbols") or {}).keys()))
            or "swing" not in next(iter((win_table.get("symbols") or {}).values()), {})
        ):
            try:
                win_table = build_win_rate_table(syms[:20], config_path=cfg_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("win rates unavailable: %s", exc)
                win_table = win_table or {}

        # Challenge-eligible symbols need their own live/cache quotes (often outside focus candidates)
        challenge_syms: list[str] = []
        try:
            from odte_scanner.challenge.million import _eligible_rows

            challenge_syms = [
                str(r["symbol"])
                for r in _eligible_rows(win_table if isinstance(win_table, dict) else None)[:14]
            ]
        except Exception:  # noqa: BLE001
            challenge_syms = []
        quote_syms = sorted(set(syms[:20] + challenge_syms))
        for s in quote_syms:
            aliases.setdefault(s, resolve_yahoo_symbol(s, cfg))

        def _uq(sym: str):
            return sym, fetch_live_quote(sym, yahoo_symbol=aliases.get(sym))

        with ThreadPoolExecutor(max_workers=8) as pool:
            for sym, q in pool.map(_uq, quote_syms):
                if q:
                    quotes[sym] = q.to_dict()

        refreshed: list[dict] = []

        def _refresh(item: dict) -> dict:
            sym = str(item.get("symbol"))
            try:
                out = refresh_candidate_quote(item, yahoo_symbol=aliases.get(sym))
            except Exception:  # noqa: BLE001
                out = dict(item)
                out["quote_stale"] = True
            q = quotes.get(sym)
            if q:
                out["live_change_pct"] = q.get("session_change_pct", q.get("change_pct"))
                out["live_last"] = q.get("last")
            return out

        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(_refresh, item) for item in board_rows]
            for fut in as_completed(futs):
                refreshed.append(fut.result())

        refreshed.sort(key=lambda c: float(c.get("score") or 0), reverse=True)

        actions = build_action_board(
            candidates=refreshed,
            scores=scan.get("scores") or [],
            quotes=quotes,
            ledger=ledger if isinstance(ledger, dict) else None,
            buy_score=float(actions_cfg.get("buy_score", 70)),
            wait_score=float(actions_cfg.get("wait_score", 62)),
            sell_score=float(actions_cfg.get("sell_score", 48)),
            stop_loss_pct=float(risk.get("stop_loss_pct", 50)),
            take_profit_pct=float(risk.get("take_profit_pct", 80)),
            max_chase_pct=float(actions_cfg.get("max_chase_pct", 2.5)),
            win_rate_table=win_table,
            min_hist_win_pct=float(actions_cfg.get("min_hist_win_pct", 80)),
            min_hist_win_samples=int(actions_cfg.get("min_hist_win_samples", 5)),
            require_hist_win=bool(actions_cfg.get("require_hist_win", True)),
        )

        jcfg = cfg.get("journal") or {}
        insights = None
        journal_sync = None
        journal = None
        if jcfg.get("enabled", True):
            from odte_scanner.options.live_chain import fetch_live_option_quote
            from odte_scanner.trading.insights import build_insights
            from odte_scanner.trading.journal import SignalJournal

            jpath = Path(jcfg.get("path", "outputs/signal_journal.json"))
            if not jpath.is_absolute():
                jpath = ROOT / jpath
            journal = SignalJournal(
                jpath, starting_cash=float(jcfg.get("starting_cash", 5000))
            )
            journal_sync = journal.sync_from_actions(
                actions,
                max_risk_usd=float(jcfg.get("max_risk_per_trade_usd", 250)),
                auto_enter=bool(jcfg.get("auto_enter", True)),
                auto_exit=bool(jcfg.get("auto_exit", True)),
            )
            marks: dict[str, float] = {}
            for t in journal.book.trades:
                if t.status != "open":
                    continue
                if t.expiry and t.strike is not None:
                    q = fetch_live_option_quote(
                        t.symbol,
                        t.expiry,
                        float(t.strike),
                        yahoo_symbol=aliases.get(t.symbol) or resolve_yahoo_symbol(t.symbol, cfg),
                    )
                    if q:
                        if q.bid > 0 and q.ask > 0:
                            marks[t.contract] = (q.bid + q.ask) / 2
                        else:
                            marks[t.contract] = q.bid if q.bid > 0 else (q.ask or 0)
            if marks:
                journal.mark_open(marks)
            insights = build_insights(journal=journal, actions=actions, win_rates=win_table)

        from odte_scanner.options.explosive import build_explosive_board
        from odte_scanner.signals.lottery import build_lottery_board

        # Lottery / parabolic 0DTE–1DTE tickets (e.g. cheap calls that can 3×–100× on a rip)
        explosive = build_explosive_board(
            refreshed,
            scores=scan.get("scores") or [],
            quotes=quotes,
            aliases=aliases,
            enrich_live=True,
            per_symbol=2,
            max_total=24,
        )

        open_lottery_trades: list[dict] = []
        if journal is not None:
            open_lottery_trades.extend(
                [t.to_dict() for t in journal.book.trades if t.status == "open"]
            )
        elif insights and isinstance(insights, dict):
            open_lottery_trades.extend(insights.get("open_positions") or [])
        # Also fold paper ledger opens (0DTE-style) for SELL NOW
        if isinstance(ledger, dict):
            seen_c = {str(t.get("contract")) for t in open_lottery_trades if t.get("contract")}
            for t in ledger.get("trades") or []:
                if t.get("status") == "open" and str(t.get("contract") or "") not in seen_c:
                    open_lottery_trades.append(t)

        lottery = build_lottery_board(
            explosive,
            quotes=quotes,
            scores=scan.get("scores") or [],
            open_trades=open_lottery_trades,
            min_lottery_score=float(actions_cfg.get("lottery_min_score", 62)),
            min_confirms=int(actions_cfg.get("lottery_min_confirms", 4)),
        )

        from odte_scanner.challenge import build_challenge_board
        from odte_scanner.data.universe import liquid_universe
        from odte_scanner.echo import build_echo_board

        focus_size = scan.get("focus_size") or len(scan.get("tickers") or [])
        liquid_size = len(liquid_universe())

        echo = {}
        try:
            echo = build_echo_board(
                scores=scan.get("scores") or [],
                candidates=refreshed,
                quotes=quotes,
                aliases=aliases,
                insights=insights if isinstance(insights, dict) else None,
                journal_sync=journal_sync if isinstance(journal_sync, dict) else None,
                actions=actions,
                lottery=lottery,
                max_symbols=int(actions_cfg.get("echo_max_symbols", 6)),
                max_dte=int((cfg.get("options") or {}).get("max_dte", 5)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("echo board unavailable: %s", exc)
            echo = {
                "error": str(exc),
                "dark_pool": {"available": False, "reason": "echo board failed to build"},
                "disclaimer": "Echo Desk temporarily unavailable.",
            }

        challenge = {}
        try:
            from odte_scanner.challenge.tracker import ChallengeTracker

            ch_path = Path(actions_cfg.get("challenge_ledger_path", "outputs/challenge_ledger.json"))
            if not ch_path.is_absolute():
                ch_path = ROOT / ch_path
            tracker = ChallengeTracker(
                ch_path,
                starting_cash=float(actions_cfg.get("challenge_start_usd", 1000)),
            )
            # Pre-evaluate opens so board sees HOLD/EXIT
            for t in tracker.open_trades():
                tracker.evaluate_open(t, mark=t.mark, quote=quotes.get(t.symbol))
            tracker.save()

            challenge = build_challenge_board(
                win_table=win_table if isinstance(win_table, dict) else None,
                scores=scan.get("scores") or [],
                quotes=quotes,
                aliases=aliases,
                open_trades=[t.to_dict() for t in tracker.book.trades],
                start_usd=float(actions_cfg.get("challenge_start_usd", 1000)),
                target_usd=float(actions_cfg.get("challenge_target_usd", 1_000_000)),
                flips=int(actions_cfg.get("challenge_flips", 12)),
                max_tickets=int(actions_cfg.get("challenge_max_tickets", 8)),
                fetch_contracts=bool(actions_cfg.get("challenge_fetch_contracts", True)),
                fetch_earnings=bool(actions_cfg.get("challenge_fetch_earnings", True)),
            )
            sync = tracker.sync_from_tickets(
                challenge.get("tickets") or [],
                quotes=quotes,
                auto_enter=bool(actions_cfg.get("challenge_auto_enter", True)),
                auto_exit=bool(actions_cfg.get("challenge_auto_exit", True)),
                max_open=int(actions_cfg.get("challenge_max_open", 1)),
            )
            challenge["sync"] = sync
            challenge["book"] = sync.get("book") or tracker.book.to_dict()
            # Rebuild statuses after sync so UI matches ledger
            challenge = build_challenge_board(
                win_table=win_table if isinstance(win_table, dict) else None,
                scores=scan.get("scores") or [],
                quotes=quotes,
                aliases=aliases,
                open_trades=[t.to_dict() for t in tracker.book.trades],
                start_usd=float(actions_cfg.get("challenge_start_usd", 1000)),
                target_usd=float(actions_cfg.get("challenge_target_usd", 1_000_000)),
                flips=int(actions_cfg.get("challenge_flips", 12)),
                max_tickets=int(actions_cfg.get("challenge_max_tickets", 8)),
                fetch_contracts=False,  # keep second pass cheap
                fetch_earnings=False,  # reuse cache from first pass
            )
            challenge["sync"] = sync
            challenge["book"] = tracker.book.to_dict()
        except Exception as exc:  # noqa: BLE001
            logger.warning("challenge board unavailable: %s", exc)
            challenge = {"error": str(exc), "tickets": [], "disclaimer": "Challenge board unavailable."}

        return jsonify(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "session": scan.get("session_weekday"),
                "universe_mode": scan.get("universe_mode"),
                "universe_size": scan.get("universe_size"),
                "focus_size": focus_size,
                "liquid_size": liquid_size,
                "scores": scan.get("scores") or [],
                "horizons": scan.get("horizons") or {},
                "action_cards": scan.get("action_cards") or {},
                "quality_gates": scan.get("quality_gates") or {},
                "call_candidates": refreshed,
                "explosive": explosive,
                "lottery": lottery,
                "echo": echo,
                "challenge": challenge,
                "watch": {"quotes": quotes},
                "ledger": ledger,
                "actions": actions,
                "hist_win_gate": actions.get("hist_win_gate"),
                "insights": insights,
                "journal_sync": journal_sync,
                "win_rates": win_table,
            }
        )

    @app.post("/api/scan")
    def trigger_scan():
        if not scan_lock.acquire(blocking=False):
            return jsonify({"ok": False, "error": "scan already running"}), 409

        mode = request.args.get("mode") or "focus"

        def _job():
            try:
                cmd = [
                    sys.executable,
                    "-m",
                    "odte_scanner",
                    "-c",
                    cfg_path,
                    "scan",
                    "--no-paper",
                ]
                if mode in ("liquid", "screener", "all"):
                    cmd.extend(["--universe", mode])
                subprocess.run(cmd, cwd=str(ROOT), check=False)
            finally:
                scan_lock.release()

        threading.Thread(target=_job, daemon=True).start()
        return jsonify({"ok": True, "started": True, "mode": mode})

    return app


def run_ui(host: str = "0.0.0.0", port: int = 8787, config_path: str | None = None) -> None:
    app = create_app(config_path)
    logger.info("Signal Desk UI at http://%s:%s", host if host != "0.0.0.0" else "127.0.0.1", port)
    app.run(host=host, port=port, debug=False, use_reloader=False)
