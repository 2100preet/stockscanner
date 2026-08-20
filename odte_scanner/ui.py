from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from odte_scanner.config import load_config
from odte_scanner.signals.actions import build_action_board
from odte_scanner.backtest.win_rates import (
    build_win_rate_table,
    ensure_challenge_win_table,
    load_win_rate_table,
)

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]

PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ZeroLoss — Do Not Miss</title>
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
    .action-card.enter-now:hover { border-color: rgba(180,255,220,.85); }
    .action-card.long { box-shadow: inset 3px 0 0 var(--long); }
    .action-card.wait { box-shadow: inset 3px 0 0 var(--wait); }
    .action-card.short { box-shadow: inset 3px 0 0 var(--short); }
    .action-card.enter-now,
    .pulse-card.must {
      background:
        linear-gradient(165deg, rgba(62,207,142,.55) 0%, rgba(18,72,48,.92) 46%, rgba(8,28,22,.96) 100%);
      border-color: rgba(140,245,198,.7);
      box-shadow: inset 4px 0 0 #8cf5c6, 0 10px 28px rgba(62,207,142,.22);
    }
    .action-card.enter-now .ac-sym,
    .pulse-card.must .pc-sym { color: #f2fff8; }
    .action-card.enter-now .ac-dir,
    .pulse-card.must .pc-tag {
      color: #062016;
      background: var(--long);
      padding: .16rem .42rem;
      border-radius: .32rem;
    }
    .pulse-card.exit {
      background:
        linear-gradient(165deg, rgba(255,107,90,.38) 0%, rgba(72,22,18,.9) 50%, rgba(28,10,8,.96) 100%);
      border-color: rgba(255,160,150,.55);
    }
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
    .pulse-banner {
      display: none; margin: 0 0 .95rem; border-radius: .9rem; overflow: hidden;
      border: 1px solid rgba(62,207,142,.35);
      background:
        linear-gradient(105deg, rgba(62,207,142,.18), rgba(18,28,24,.55) 42%, rgba(255,107,90,.12));
      box-shadow: 0 10px 28px rgba(0,0,0,.28);
    }
    .pulse-banner.active { display: block; animation: fade .3s ease; }
    .pulse-banner .pb-head {
      display: flex; flex-wrap: wrap; gap: .45rem .75rem; align-items: baseline;
      justify-content: space-between; padding: .7rem 1rem .35rem;
      border-bottom: 1px solid var(--line);
    }
    .pulse-banner .pb-title {
      font-family: "Instrument Serif", Georgia, serif; font-size: 1.35rem; letter-spacing: -.02em; margin: 0;
    }
    .pulse-banner .pb-title em { color: var(--long); font-style: italic; }
    .pulse-banner .pb-sub { color: var(--muted); font-size: .72rem; max-width: 36rem; }
    .pulse-banner .pb-grid {
      display: grid; gap: .55rem; padding: .75rem 1rem 1rem;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    }
    .pulse-card {
      border: 1px solid var(--line); border-radius: .7rem; padding: .7rem .8rem;
      background: rgba(8,16,14,.55);
    }
    .pulse-card .pc-top { display: flex; justify-content: space-between; gap: .5rem; align-items: baseline; }
    .pulse-card .pc-sym { font-family: "Instrument Serif", Georgia, serif; font-size: 1.35rem; }
    .pulse-card .pc-tag {
      font-family: "JetBrains Mono", monospace; font-size: .68rem; letter-spacing: .06em;
      font-weight: 500;
    }
    .pulse-card .pc-tag.exit { color: #fff; background: var(--short); padding: .16rem .42rem; border-radius: .32rem; }
    .pulse-card .pc-meta {
      display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .25rem .55rem;
      margin-top: .45rem; font-size: .78rem;
    }
    .pulse-card .pc-meta strong { color: var(--ink); font-weight: 600; }
    .pulse-card .pc-meta span { color: var(--muted); }
    .pulse-card .pc-why { margin: .4rem 0 0; color: var(--muted); font-size: .72rem; line-height: 1.35; }
    .pulse-empty { padding: .85rem 1rem 1rem; color: var(--muted); font-size: .8rem; }
    #alertToasts {
      position: fixed; right: 1rem; bottom: 1rem; z-index: 80;
      display: flex; flex-direction: column; gap: .45rem; max-width: min(360px, 92vw);
      pointer-events: none;
    }
    .alert-toast {
      pointer-events: auto;
      border: 1px solid var(--line); border-radius: .65rem; padding: .65rem .75rem;
      background: rgba(8,16,14,.92); box-shadow: 0 8px 28px rgba(0,0,0,.35);
      font-size: .78rem; line-height: 1.35;
    }
    .alert-toast.buy { box-shadow: inset 3px 0 0 var(--long), 0 8px 28px rgba(0,0,0,.35); }
    .alert-toast.sell { box-shadow: inset 3px 0 0 var(--short), 0 8px 28px rgba(0,0,0,.35); }
    .alert-toast.watch { box-shadow: inset 3px 0 0 var(--accent), 0 8px 28px rgba(0,0,0,.35); }
    .alert-toast strong { display: block; font-size: .86rem; margin-bottom: .15rem; }
    .alert-toast .at-meta { color: var(--muted); font-family: "JetBrains Mono", monospace; font-size: .7rem; }
    html[data-theme="light"] {
      --bg: #f3f6f4; --panel: rgba(255,255,255,.88); --ink: #122018; --muted: #5a6d65;
      --line: rgba(18,32,24,.12); --long: #0d8a56; --short: #c43c32; --wait: #a87410;
      --accent: #1a7a68;
    }
    html[data-theme="light"] body {
      background:
        radial-gradient(900px 480px at 8% -5%, rgba(13,138,86,.12), transparent 55%),
        radial-gradient(700px 420px at 95% 5%, rgba(26,122,104,.08), transparent 50%),
        linear-gradient(165deg, #f8fbf9, var(--bg) 40%, #e8eeea);
    }
    html[data-theme="light"] .action-card,
    html[data-theme="light"] .metric,
    html[data-theme="light"] .pulse-card { background: var(--panel); }
    html[data-theme="light"] .action-card.enter-now,
    html[data-theme="light"] .pulse-card.must {
      background: linear-gradient(165deg, rgba(13,138,86,.32), rgba(232,255,244,.98) 52%);
      border-color: rgba(13,138,86,.55);
      box-shadow: inset 4px 0 0 var(--long), 0 8px 20px rgba(13,138,86,.12);
    }
    html[data-theme="light"] .action-card.enter-now .ac-dir,
    html[data-theme="light"] .pulse-card.must .pc-tag {
      color: #fff;
      background: var(--long);
    }
    html[data-theme="light"] .pulse-card.exit {
      background: linear-gradient(165deg, rgba(196,60,50,.22), rgba(255,244,242,.98) 52%);
      border-color: rgba(196,60,50,.45);
    }
    html[data-theme="light"] .alert-toast { background: rgba(255,255,255,.94); color: var(--ink); }
    .zl-tape { font-size: .8rem; }
    .zl-tape tbody tr:hover { background: rgba(62,207,142,.06); }
    .badge.dnm { background: rgba(62,207,142,.22); color: var(--long); letter-spacing: .06em; }
    .tabs button[data-tab="zeroloss"].active,
    .tabs button[data-tab="nowboard"].active { color: var(--long); }
    .now-desk { font-family: "Instrument Serif", Georgia, serif; font-size: 1.05rem; font-weight: 400; margin: 1rem 0 .5rem; }
    .now-desk .status { margin-left: .4rem; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1 class="brand">Zero<em>Loss</em></h1>
    <p class="lede">Miss-prevention desk — catch <strong>gap + volume + news</strong> names the old hist-win gate hid (MRNA +177% on 2026-08-19 was never even in the universe). This is <strong>not</strong> an only-winners indicator and it will not get a half-million back. Binary biotech can gap both ways. Paper research only.</p>
    <div class="toolbar">
      <button class="primary" id="btnScan">Scan focus</button>
      <button id="btnScanCatalyst">Scan catalyst</button>
      <button id="btnScanWide">Scan liquid universe</button>
      <button id="btnScanMl6">Scan ML6</button>
      <button id="btnRefresh">Reload</button>
      <a class="pill" id="pagesScanHelp" href="https://github.com/2100preet/zeroloss/actions/workflows/signal-desk-pages.yml" target="_blank" rel="noopener" style="display:none;text-decoration:none">Run scan on GitHub Actions</a>
      <button type="button" id="btnTheme" title="Toggle light / dark">Light mode</button>
      <button type="button" id="btnAlerts" title="Alerts: ENTER NOW on hist-gated BUY, EXIT on SELL, WATCH on Do-not-miss (not a buy)">Enable alerts</button>
      <span class="pill" id="session">—</span>
      <span class="pill" id="universePill">—</span>
      <span class="status" id="counts"></span>
      <span class="status" id="updated">Loading…</span>
    </div>
    <div id="mustTradeBanner" class="pulse-banner" aria-live="polite"></div>
    <div id="loadNote" class="loading" style="display:none;margin-bottom:.6rem"></div>
    <div id="alertToasts" aria-live="assertive"></div>

    <nav class="tabs" id="tabs">
      <button class="active" data-tab="zeroloss">ZeroLoss</button>
      <button data-tab="nowboard">BUY / SELL NOW</button>
      <button data-tab="overview">Overview</button>
      <button data-tab="odte">0DTE</button>
      <button data-tab="odte1k">0DTE $1K</button>
      <button data-tab="powerhour">Power Hour</button>
      <button data-tab="explosive">Explosive</button>
      <button data-tab="weekly">1 Week</button>
      <button data-tab="swing">Swing 1–3M</button>
      <button data-tab="ml6">ML6 Neocloud</button>
      <button data-tab="echo">Flow Desk</button>
      <button data-tab="challenge">$1k→$1M</button>
      <button data-tab="screener">Screener</button>
      <button data-tab="journal">Journal</button>
    </nav>

    <section class="tabpane active" id="tab-zeroloss">
      <div class="metric-row" id="zlMetrics"></div>
      <p class="lede" id="zlNote" style="margin-top:0"></p>
      <h2>Pinned watch — MP / USAR / PFE / NKE / MRNA</h2>
      <p class="lede" style="margin-top:0;font-size:.76rem">Always listed, even on a quiet day. Quiet ≠ buy.</p>
      <div id="zlPinned" class="empty">—</div>
      <h2>Do not miss — WATCH, not ENTER NOW</h2>
      <p class="lede" style="margin-top:0;font-size:.78rem">
        This is a <strong>miss-prevention flag</strong> (gap ≥8%, day ≥12%, or ≥5× volume — MRNA’s Phase 3 class).
        It does <strong>not</strong> mean enter now. Enable Alerts will ping <strong>WATCH</strong> for these names, not ENTER.
        ENTER NOW only fires on hist-gated BUY NOW tickets (hist win ≥80%, strike rate shown on the card).
      </p>
      <div class="cards" id="zlDoNotMiss"></div>
      <div class="panel">
        <h2>Catalyst / unusual tape</h2>
        <div id="zlTape" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Unusual options flow</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          Bullflow-style tape from Yahoo chain snapshots — delayed, not OPRA, not affiliated with bullflow.io.
          Sweeps / dark-pool prints need a paid OPRA feed this repo does not have.
        </p>
        <div id="zlFlow" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Recommended tickets — ENTER / EXIT / P&amp;L</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          One card per recommendation: entry ask, exit bid, profit % and $ P&amp;L (1 contract).
          Radar HOT is not a ticket. Empty P&amp;L means the desk has not paper-filled yet.
        </p>
        <div class="metric-row" id="zlTicketMetrics"></div>
        <div id="zlTickets" class="empty">—</div>
      </div>
      <p class="lede" id="zlDisclaimer" style="font-size:.72rem"></p>
    </section>

    <section class="tabpane" id="tab-nowboard">
      <h2>BUY NOW / SELL NOW — all desks</h2>
      <p class="lede">Live option BUY NOW / SELL NOW, tagged by desk (0DTE, 1 Week, Swing 1–3M, Explosive, ML6, Challenge). A ticker needs a chain <em>and</em> hist win ≥80% (n≥5) to show as BUY NOW. Quality names that clear hist but have no contract yet are listed as SETUP — not a buy. Radar HOT and Do-not-miss WATCH stay off this list.</p>
      <div class="metric-row" id="nowBoardMetrics"></div>
      <p class="lede" id="nowBoardNote" style="margin-top:0;font-size:.76rem"></p>
      <h2>BUY NOW</h2>
      <div id="nowBoardBuy" class="empty">—</div>
      <h2>SELL NOW</h2>
      <div id="nowBoardSell" class="empty">—</div>
      <h2>WAIT — chain on board, hist gate blocked</h2>
      <div id="nowBoardWait" class="empty">—</div>
      <h2>SETUP — hist-eligible quality, no option ticket yet</h2>
      <p class="lede" style="margin-top:0;font-size:.76rem">These underlyings cleared quality + hist win. They are <strong>not</strong> BUY NOW until a call/put contract is on the snapshot.</p>
      <div id="nowBoardSetup" class="empty">—</div>
      <div class="panel">
        <h2>All rows</h2>
        <div id="nowBoardTable" class="empty">—</div>
      </div>
    </section>

    <section class="tabpane" id="tab-overview">
      <div class="metric-row" id="perfCards"></div>
      <p class="lede" id="insightSummary"></p>
      <p class="lede" id="winLegend" style="font-size:.78rem">
        <strong>Hist win</strong> = % of past quality signals where the underlying finished green over the horizon.
        <strong>n</strong> = sample size (how many of those signals). Small n (e.g. 1–5) means the % is fragile.
        <strong>Strike rate ≥1%</strong> = how often the underlying ripped ≥1% after the signal (better proxy for call payoff than plain win%).
        <strong>BUY NOW gate</strong>: only symbols with hist win ≥80% and n≥5 are promoted (see hist-win gate card).
      </p>
      <div class="metric-row" id="histWinGate"></div>
      <div class="panel" id="redFlagPanel">
        <h2>Red Flag — VolSignals 0DTE framework (proxy)</h2>
        <p class="lede" style="margin-top:0">Customer upside call-hedging / dealer short positioning + charm into the close can cap rallies. Blocks index 0DTE long calls when active.</p>
        <div id="redFlagBody" class="empty">Loading Red Flag…</div>
      </div>
      <div class="panel" id="freeDealerPanel">
        <h2>Free dealer / vol cockpit</h2>
        <p class="lede" style="margin-top:0">Keyless feeds: CBOE SPX GEX, SqueezeMetrics DIX/GEX, VIX term (VIX1D/VIX3M/VVIX/SKEW). Delayed / modeled — not VS3D.</p>
        <div id="freeDealerBody" class="empty">Loading free feeds…</div>
      </div>
      <h2>Top action cards</h2>
      <div class="cards" id="overviewCards"></div>
      <div class="panel">
        <h2>Lottery desk — BUY / SELL now</h2>
        <p class="lede" style="margin-top:0">Convex 0DTE/1DTE tickets gated by tape, liquidity, session timing, and multi-algo quality — not a blind list.</p>
        <div id="explosiveMini" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Live board (options)</h2>
        <p class="lede" id="exitCriteriaNote" style="margin-top:0;font-size:.76rem">
          SELL NOW fires only after a paper ENTRY. Every BUY/WAIT shows an EXIT plan (TP / SL / 15:45 ET clock).
        </p>
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
      <p class="lede">Gap-and-go, breakout, volume thrust, VIX regime. Win% = next session green after quality signal. Strike rate = ≥1% / ≥2% underlying rip rate.
        Paper <strong>BUY NOW / SELL NOW</strong> fills update cash, equity, and P&amp;L on each ENTRY / EXIT.</p>
      <p class="lede" style="font-size:.76rem;margin-top:.35rem">
        <strong>How 0DTE score is calculated:</strong> weighted average (0–100) of algos —
        gap &amp; go / breakout (1.5), volume thrust (1.4), relative strength + VIX (1.2), squeeze (1.1),
        MACD (1.0), RSI (0.9), EMA stack (0.8). A <em>confirm</em> = bullish algo with score ≥65.
        <strong>Quality</strong> (needed for gated BUY NOW) = ensemble ≥72 <em>and</em> ≥3 confirms.
        Soft scores ~55–71 can still appear on the chase / convex lane as <em>BUY — bit risky</em>.
      </p>
      <div class="panel" id="redFlagOdtePanel"><div id="redFlagOdte" class="empty">—</div></div>
      <div class="cards" id="cards0dte"></div>
      <div class="panel"><div id="table0dte" class="empty"></div></div>
      <div class="panel">
        <h2>0DTE paper journal — ENTRY / EXIT / P&amp;L</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          Auto fills from BUY NOW → SELL NOW. Cash before→after and realized P&amp;L on every flip (full ledger on Journal tab).
        </p>
        <div id="odteJournal" class="empty">No 0DTE journal fills yet.</div>
      </div>
      <div class="panel">
        <h2>Recommendation log — 0DTE entry / exit / P&amp;L</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          P&amp;L from BUY NOW ask → SELL NOW bid (1 contract). Same logger as Journal, filtered to 0DTE.
        </p>
        <div class="metric-row" id="odteRecLogMetrics"></div>
        <div id="odteRecLog" class="empty">—</div>
      </div>
    </section>

    <section class="tabpane" id="tab-odte1k">
      <h2>0DTE $1K Challenge — Green Friday · ORB15 puts</h2>
      <p class="lede">
        Separate from the swing <strong>$1k→$1M</strong> path. Paper sleeve starts at <strong>$1,000</strong>,
        sizes ~<strong>$850</strong> (~85%), max <strong>2 trades/day</strong>.
        Full focus sleeve: <strong>SPY · QQQ · IWM · TSLA · NVDA · NBIS · AAPL · SLV · SPCX · NOW</strong> + the rest of the focus list.
        Playbook: Green Friday + <strong>break/hold ORB15 Low</strong> (or retest) → <strong>PUT NOW</strong>
        (actionable entry — Paper ENTER on a live host). Surfaces conflict when call “safe zone” still likes index calls.
      </p>
      <div class="metric-row" id="odte1kMetrics"></div>
      <div class="cards" id="odte1kPrimary"></div>
      <div class="panel">
        <h2>ORB15 levels (09:30–09:45 ET)</h2>
        <div id="odte1kOrb" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>PUT NOW / EXIT / WATCH</h2>
        <div id="odte1kActions" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Sleeve book — cash · equity · 2× progress</h2>
        <div id="odte1kBook" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Playbook rules</h2>
        <div id="odte1kRules" class="empty">—</div>
      </div>
      <p class="lede" id="odte1kDisclaimer" style="font-size:.72rem"></p>
    </section>

    <section class="tabpane" id="tab-powerhour">
      <h2>Power Hour — LONG / SHORT · 15m VWAP</h2>
      <p class="lede">
        Prep <strong>14:30</strong> · Power hour <strong>15:00–16:00 ET</strong>.
        Named playbooks: <strong>NU · NVDA · CAPR · ETON · HTFL · GOOGL · NXPI</strong> (+ <strong>TSLA</strong> and the full focus sleeve).
        Each row shows whether to go <strong>LONG or SHORT</strong>, the 15-minute trigger, and the risk line / stop.
      </p>
      <div class="metric-row" id="powerHourMetrics"></div>
      <div class="cards" id="powerHourPrimary"></div>
      <div class="panel">
        <h2>Named playbooks — trigger · risk</h2>
        <div id="powerHourSpecial" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>LONG now</h2>
        <div id="powerHourLong" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>SHORT now</h2>
        <div id="powerHourShort" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>WATCH / WAIT — full sleeve</h2>
        <div id="powerHourWatch" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Rules</h2>
        <div id="powerHourRules" class="empty">—</div>
      </div>
      <p class="lede" id="powerHourDisclaimer" style="font-size:.72rem"></p>
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
        <h2>Discord-style lottery radar (SPY / QQQ wings)</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          Separate lane for cheap near-money 0DTE calls (~$0.15–$2.50) like discretionary Discord alerts.
          <strong>RADAR HOT</strong> ≠ BUY NOW — no hist-win gate, no auto journal fill.
        </p>
        <div class="metric-row" id="radarMetrics"></div>
        <div id="radarHot" class="empty">No RADAR HOT wings yet.</div>
        <div class="status" style="margin:.75rem 0 .4rem">WATCH / COOL</div>
        <div id="radarWatch" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Chase / high-convexity — BUY bit risky</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          Far-OTM or already-ripping 0DTE/1DTE calls the main desk skips (anti-chase + hist-win).
          <strong>BUY — BIT RISKY</strong> = discretionary / size small — <em>not</em> gated Options BUY NOW and not auto-journaled.
          Example pattern: MU far wing that 10× after the rip.
        </p>
        <div class="metric-row" id="chaseMetrics"></div>
        <div id="chaseBuyRisky" class="empty">No BUY — BIT RISKY tickets yet.</div>
        <div class="status" style="margin:.75rem 0 .4rem">WATCH CONVEX</div>
        <div id="chaseWatch" class="empty">—</div>
        <p class="lede" id="chaseScoreNote" style="font-size:.72rem;margin-top:.5rem"></p>
      </div>
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
        <h2>Recommendation log — entry / exit / P&amp;L</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          Persistent history of lottery WAIT / BUY NOW / SELL NOW. WAIT shows the recommended ask while gated;
          P&amp;L (1ct) locks when BUY NOW ask → SELL NOW bid both fire.
        </p>
        <div class="metric-row" id="lotteryRecLogMetrics"></div>
        <div id="lotteryRecLog" class="empty">—</div>
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
      <div class="panel">
        <h2>Recommendation log — weekly entry / exit / P&amp;L</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          Weekly BUY NOW → SELL NOW history with estimated P&amp;L (1 contract).
        </p>
        <div class="metric-row" id="weeklyRecLogMetrics"></div>
        <div id="weeklyRecLog" class="empty">—</div>
      </div>
    </section>

    <section class="tabpane" id="tab-swing">
      <h2>Swing — 1 to 3 months</h2>
      <p class="lede">Stage analysis, trend structure, medium RS, dip buys. Win% / strike rate ≈ 21-session (~1 month) and 42-session (~2mo) forward returns.
        Near-term earnings (today / this week / next week) shared with the challenge desk below.</p>
      <div class="cards" id="cardsSwing"></div>
      <div class="panel">
        <h2>Earnings near you — today / this week / next week</h2>
        <div id="swingEarningsWatch" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Recommendation log — swing entry / exit / P&amp;L</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          Swing BUY NOW → SELL NOW history with estimated P&amp;L (1 contract).
        </p>
        <div class="metric-row" id="swingRecLogMetrics"></div>
        <div id="swingRecLog" class="empty">—</div>
      </div>
    </section>


    <section class="tabpane" id="tab-ml6">
      <h2>ML6 — earnings-catalyst neocloud / AI infra</h2>
      <p class="lede" id="ml6Purpose">
        Beaten-down AI / neocloud / data-center earnings upside (NBIS/CRWV style).
        <strong>Not</strong> the 0DTE technical ensemble. Do <strong>not</strong> auto BUY on the report alone —
        prefer WAIT until confirmed reaction (open/hold above key level, or AH high/VWAP acceptance).
      </p>
      <div class="panel" id="ml6BottomLine" style="margin-bottom:1rem">
        <h2>Bottom-line rules</h2>
        <div id="ml6Rules" class="empty">Loading ML6 rules…</div>
      </div>
      <div class="metric-row" id="ml6Metrics"></div>
      <div class="cards" id="ml6Primary"></div>
      <div class="panel">
        <h2>BUY NOW — ML6 automation</h2>
        <p class="lede" style="margin-top:0">Reaction-gated only. Paper journal auto-enters when enabled (weekly/swing calls).</p>
        <div id="ml6Buy" class="empty">No ML6 BUY NOW — waiting for post-print acceptance.</div>
      </div>
      <div class="panel">
        <h2>SELL NOW — ML6 automation</h2>
        <div id="ml6Sell" class="empty">No open ML6 exits.</div>
      </div>
      <div class="panel">
        <h2>WAIT / WATCH (gated)</h2>
        <div id="ml6Wait" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>ML6 board</h2>
        <div id="ml6Board" class="empty">Run Scan ML6 to populate.</div>
      </div>
    </section>

    <section class="tabpane" id="tab-challenge">
      <h2>$1,000 → $1,000,000 challenge</h2>
      <p class="lede">
        Path includes a <strong>4-month → $500k</strong> pace (prefer <strong>weekly-style</strong> tickets)
        on the way to $1M. Sure-shot hist filter (prefer <strong>100% hist win</strong>, else ≥80% n≥5).
        Status: <strong>ENTRY · HOLD · EXIT</strong>. After each Paper ENTER/EXIT the sleeve
        <strong>cash &amp; equity balance</strong> updates so you know where you are.
        <em>Hist 100% ≠ guaranteed future wins — options can go to zero.</em>
      </p>
      <div class="metric-row" id="challengeMetrics"></div>
      <div class="cards" id="challengePrimary"></div>
      <div class="panel">
        <h2>Earnings near you — today / this week / next week (+ DRAM sleeve)</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          Scans hist-eligible names plus DRAM/memory (DRAM, MU, WDC, STX, AMAT…) and focus list.
          Pre-print → LEAP/WAIT; post-print → prefer continuation.
        </p>
        <div id="challengeEarningsWatch" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Challenge update — ENTRY / HOLD / EXIT</h2>
        <div id="challengeStatus" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Open sleeve &amp; closed flips</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          Paper sleeve updates on <strong>Paper ENTER / EXIT</strong>. Each open flip shows strike, expiry,
          target profit %, strike-rates, and exact EXIT rules.
        </p>
        <div id="challengeBook" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>When to ENTER / EXIT</h2>
        <div id="challengePlan" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Compounding path &amp; hold periods</h2>
        <div id="challengePath" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Recommended tickets — side · strike · expiry · hold · status</h2>
        <div id="challengeTickets" class="empty">—</div>
      </div>
      <div class="panel">
        <h2>Recommendation log — entry / exit / P&amp;L</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          Keeps challenge ENTRY / EXIT history even when a name drops off today’s ticket list.
          P&amp;L (1ct) = EXIT bid − ENTRY ask × 100 when both prices were on the pulse.
        </p>
        <div class="metric-row" id="challengeRecLogMetrics"></div>
        <div id="challengeRecLog" class="empty">—</div>
      </div>
      <p class="lede" id="challengeDisclaimer" style="font-size:.72rem"></p>
    </section>

    <section class="tabpane" id="tab-echo">
      <h2>Flow Desk — unusual options + GEX + dark pool</h2>
      <p class="lede">
        Bullflow-inspired layout: filterable unusual options flow, dealer GEX walls, and dark-pool ATS context.
        Built from Yahoo chain snapshots + <a href="https://www.finra.org/filing-reporting/otc-transparency" target="_blank" rel="noopener" style="color:var(--accent)">FINRA OTC Transparency</a>
        — <strong>not</strong> OPRA time &amp; sales and <strong>not affiliated</strong> with Bullflow / Trade Echo.
      </p>
      <div class="metric-row" id="echoMetrics"></div>
      <div class="panel">
        <h2>Cortex briefing</h2>
        <p class="lede" id="echoCortex" style="margin:0">—</p>
      </div>
      <div class="echo-grid" style="margin-top:1rem">
        <div class="panel" style="margin:0">
          <h2>Unusual options flow</h2>
          <p class="lede" style="margin-top:0;font-size:.76rem">
            Golden / Unusual / Aggressive from premium, volume, and vol/OI — filter like a flow tape.
          </p>
          <div class="playbook" id="flowFilters" style="margin:.35rem 0 .55rem;flex-wrap:wrap">
            <button type="button" class="tag active" data-flow="all">All</button>
            <button type="button" class="tag" data-flow="golden">Golden</button>
            <button type="button" class="tag" data-flow="unusual">Unusual</button>
            <button type="button" class="tag" data-flow="calls">Calls</button>
            <button type="button" class="tag" data-flow="puts">Puts</button>
            <button type="button" class="tag" data-flow="vol_gt_oi">Vol&gt;OI</button>
            <button type="button" class="tag" data-flow="earnings">Earnings</button>
          </div>
          <div id="echoFlow" class="empty">—</div>
        </div>
        <div class="panel" style="margin:0">
          <h2>DealerEdge (GEX)</h2>
          <p class="lede" style="margin-top:0;font-size:.76rem">Call/put walls, flip, HVL from OI + BS gamma proxy.</p>
          <div id="echoGex" class="empty">—</div>
        </div>
      </div>
      <div class="panel">
        <h2>Dark pool (FINRA ATS)</h2>
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
      <p class="lede">
        Liquid optionable universe (~200 names) — <strong>not</strong> the full US market (Yahoo rate limits).
        Sort by <strong>earnings</strong>, <strong>volume</strong>, or <strong>score</strong>.
        Earnings cache warms across refreshes until coverage is complete.
        Use <strong>Scan liquid</strong> to refresh scores/hist.
      </p>
      <div class="metric-row" id="marketMetrics"></div>
      <div class="playbook" id="screenerSort" style="margin:.4rem 0 .6rem">
        <button type="button" class="tag active" data-sort="earnings">Earnings</button>
        <button type="button" class="tag" data-sort="volume">Volume</button>
        <button type="button" class="tag" data-sort="score">Score</button>
      </div>
      <div id="screener" class="empty">Loading market board…</div>
    </section>

    <section class="tabpane" id="tab-journal">
      <h2>Journal &amp; insights</h2>
      <p class="lede">
        0DTE / weekly <strong>BUY NOW → SELL NOW</strong> paper fills. After each ENTRY / EXIT the sleeve
        <strong>cash &amp; equity balance</strong> and <strong>P&amp;L %</strong> update — same discipline as the challenge desk.
      </p>
      <div class="panel">
        <h2>Paper journal (auto BUY/SELL NOW fills)</h2>
        <div id="journal" class="empty">No journal trades yet.</div>
      </div>
      <div class="panel">
        <h2>Balance after ENTRY / EXIT (CST)</h2>
        <div id="journalBalanceLog" class="empty">No ENTRY/EXIT yet — balance updates after BUY NOW / SELL NOW fills.</div>
      </div>
      <div class="panel">
        <h2>Webull auto-trade bridge</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          Routes lottery / 0DTE / weekly / swing / challenge option tickets to Webull by desk type.
          Live gate: <strong>100% hist-win</strong> (n≥3) — historical filter only, <em>not</em> a future guarantee.
          With <code>auto_sync: true</code>, each refresh stages BUY/SELL into the order ledger (preview/dry-run by default).
          Set env <code>WEBULL_APP_KEY</code> / <code>WEBULL_APP_SECRET</code> / <code>WEBULL_ACCOUNT_ID</code>
          and <code>live_trading.enabled</code> + <code>dry_run: false</code> for OpenAPI submits.
        </p>
        <div class="toolbar" style="margin:.4rem 0">
          <button type="button" class="primary" id="btnWebullSync">Sync → Webull (dry-run / live)</button>
          <a class="pill" id="webullHelp" href="https://developer.webull.com/apis/docs/sdk.md" target="_blank" rel="noopener">OpenAPI docs</a>
        </div>
        <div class="metric-row" id="webullMetrics"></div>
        <div id="webullHowTo" class="lede" style="font-size:.74rem;margin:.35rem 0 .55rem"></div>
        <h3 style="margin:.6rem 0 .35rem;font-size:.9rem">BUY / SELL order history</h3>
        <div id="webullOrders" class="empty">No Webull orders staged yet.</div>
        <p class="lede" id="webullDisclaimer" style="font-size:.72rem;margin-top:.5rem"></p>
      </div>
      <div class="panel">
        <h2>Recommendation logger — all sections</h2>
        <p class="lede" style="margin-top:0;font-size:.76rem">
          Cross-desk history: lottery · challenge · 0DTE · weekly · swing.
          P&amp;L (1ct) = <strong>SELL NOW bid − BUY NOW / ENTRY ask</strong> × 100 — priced only when both pulses had marks.
          Clock flatten without a live mark is a <em>lapse</em> (not a win/loss). Same panels live under each desk tab.
        </p>
        <div class="metric-row" id="recLogMetrics"></div>
        <div id="recLogAll" class="empty">—</div>
      </div>
    </section>

    <footer>
      ZeroLoss flags names you must not miss. It does not pick only winning stocks.
      Hist-win ≥80% hid movers; the catalyst sleeve (MRNA, BNTX, XBI, …) is always scanned.
      Win% is underlying direction, not option P&amp;L. Research only — not affiliated with Bullflow, Signa, Intellectia, or Trade Echo.
    </footer>
  </div>
  <script>
    let DATA = {};
    const fmt = (n, d=2) => (n==null || Number.isNaN(Number(n))) ? "—" : Number(n).toFixed(d);
    const pctClass = (n) => (n||0) >= 0 ? "up" : "down";
    /** Format ISO/UTC timestamps in US Central (CST/CDT). */
    function fmtCST(iso, withSeconds=false) {
      if (iso == null || iso === "") return "—";
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) {
        const s = String(iso);
        return s.length >= 16 ? s.slice(0, 16) : s;
      }
      const opts = {
        timeZone: "America/Chicago",
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        hour12: true,
        timeZoneName: "short",
      };
      if (withSeconds) opts.second = "2-digit";
      try {
        return new Intl.DateTimeFormat("en-US", opts).format(d);
      } catch (_) {
        return d.toISOString();
      }
    }

    function allTickets() {
      const rec = DATA.rec_log || {};
      const out = [];
      const push = (rows) => { (rows || []).forEach(r => { if (r) out.push(r); }); };
      push(rec.open_recs); push(rec.closed_recs);
      const by = rec.by_section || {};
      Object.keys(by).forEach(k => { push(by[k].open_recs); push(by[k].closed_recs); });
      const ins = DATA.insights || {};
      (ins.open_trades || []).forEach(t => out.push({
        symbol: t.symbol, contract: t.contract, strike: t.strike, expiry: t.expiry,
        entry_price: t.entry_ask, exit_price: t.exit_bid, pnl_usd: t.pnl_usd,
        profit_pct: t.profit_pct, status: t.status || "open",
        headline: t.entry_reason, reason: t.exit_reason, section: "journal",
        recommended_at: t.entered_at, closed_at: t.exited_at, right: t.right,
      }));
      (ins.closed_trades || []).forEach(t => out.push({
        symbol: t.symbol, contract: t.contract, strike: t.strike, expiry: t.expiry,
        entry_price: t.entry_ask, exit_price: t.exit_bid, pnl_usd: t.pnl_usd,
        profit_pct: t.profit_pct, status: t.status || "closed",
        headline: t.entry_reason, reason: t.exit_reason, section: "journal",
        recommended_at: t.entered_at, closed_at: t.exited_at, right: t.right,
      }));
      return out;
    }

    function ticketFor(sym, contract) {
      const u = String(sym || "").toUpperCase();
      const c = String(contract || "");
      const rows = allTickets();
      let hit = c ? rows.find(r => String(r.symbol||"").toUpperCase()===u && String(r.contract||"")===c) : null;
      if (!hit) hit = rows.find(r => String(r.symbol||"").toUpperCase()===u);
      return hit || {};
    }

    function ticketLines(sym, row) {
      const t = ticketFor(sym, row && row.contract);
      const entry = t.entry_price ?? row?.ask ?? row?.entry_ask ?? row?.entry;
      const exitPx = t.exit_price ?? row?.bid ?? row?.exit_bid;
      const planIn = row?.enter_plan || t.headline || t.reason || (entry != null ? `BUY ask $${fmt(entry,2)}` : "No fill yet");
      const planOut = row?.exit_plan || t.reason || t.close_reason || "TP / SL / 15:45 ET clock — no live EXIT until an ENTRY exists";
      const pnl = t.pnl_usd;
      const pct = t.profit_pct;
      const st = t.status || (entry != null && exitPx == null ? "open" : (pnl != null ? "closed" : "unfilled"));
      return { entry, exitPx, planIn, planOut, pnl, pct, status: st, recommended_at: t.recommended_at || t.entered_at };
    }

    function ticketHtml(sym, row) {
      const t = ticketLines(sym, row || {});
      const w = winLookup(sym, row?.dte_bucket || row?.horizon || "0dte");
      const enterAt = row?.signaled_at_cst || fmtCST(row?.signaled_at || row?.recommended_at || t.recommended_at);
      const sr = w.hit1==null ? "—" : `${fmt(w.hit1,0)}% ≥1%` + (w.hit2==null?"":` / ${fmt(w.hit2,0)}% ≥2%`);
      const pnlTxt = t.pnl==null && t.pct==null ? "unfilled" : `${t.pct==null?"—":fmt(t.pct,1)+"%"}${t.pnl==null?"":" · $"+fmt(t.pnl,2)}`;
      return `<div class="ac-meta" style="margin-top:.4rem">
        <div>ENTER<strong>${t.entry==null?"—":"$"+fmt(t.entry,2)}</strong></div>
        <div>EXIT mark<strong>${t.exitPx==null?"—":"$"+fmt(t.exitPx,2)}</strong></div>
        <div>P&amp;L (1ct)<strong class="${t.pnl==null&&t.pct==null?"":pctClass(t.pct??t.pnl)}">${pnlTxt}</strong></div>
        <div>Status<strong>${t.status}</strong></div>
        <div>Strike rate<strong>${sr}</strong></div>
        <div>ENTER time (CST)<strong>${enterAt}</strong></div>
      </div>
      <p class="pc-why"><strong>ENTER:</strong> ${t.planIn}</p>
      <p class="pc-why"><strong>EXIT:</strong> ${t.planOut}</p>`;
    }

    const NOW_DESK_ORDER = ["0DTE", "1 Week", "Swing 1–3M", "Explosive", "ML6", "Challenge", "0DTE $1K", "Options"];

    function horizonDesk(row, fallback) {
      const b = String(row.dte_bucket || row.horizon || row.hold_style || row.style || "").toLowerCase();
      const dte = row.dte == null ? null : Number(row.dte);
      if (b.includes("0dte") || b === "0d") return "0DTE";
      if (b.includes("week") || b === "1w" || b === "1wk") return "1 Week";
      if (b.includes("month") || b.includes("leap") || b.includes("swing") || b === "1m") return "Swing 1–3M";
      if (dte != null && !Number.isNaN(dte)) {
        if (dte <= 1) return "0DTE";
        if (dte <= 10) return "1 Week";
        return "Swing 1–3M";
      }
      return fallback || "Options";
    }

    function collectNowBoard() {
      const buys = [];
      const sells = [];
      const waits = [];
      const setups = [];
      const seen = new Set();
      const add = (row, side, desk) => {
        if (!row || !row.symbol) return;
        const key = [
          side,
          desk,
          String(row.symbol).toUpperCase(),
          row.contract || "",
          row.expiry || "",
          row.strike ?? "",
          row.right || "",
        ].join("|");
        if (seen.has(key)) return;
        seen.add(key);
        const item = Object.assign({}, row, { _desk: desk, _side: side });
        if (side === "BUY") buys.push(item);
        else if (side === "SELL") sells.push(item);
        else if (side === "WAIT") waits.push(item);
        else setups.push(item);
      };
      const acts = DATA.actions || {};
      (acts.buy_now || []).forEach(r => add(r, "BUY", horizonDesk(r, "Options")));
      (acts.sell_now || []).forEach(r => add(r, "SELL", horizonDesk(r, "Options")));
      (acts.wait || []).slice(0, 16).forEach(r => add(r, "WAIT", horizonDesk(r, "Options")));
      const lot = DATA.lottery || {};
      (lot.buy_now || []).forEach(r => add(r, "BUY", "Explosive"));
      (lot.sell_now || []).forEach(r => add(r, "SELL", "Explosive"));
      (lot.wait || []).slice(0, 8).forEach(r => add(r, "WAIT", "Explosive"));
      const ml = (DATA.ml6 && DATA.ml6.actions) || {};
      (ml.buy_now || []).forEach(r => add(r, "BUY", "ML6"));
      (ml.sell_now || []).forEach(r => add(r, "SELL", "ML6"));
      (ml.wait || []).forEach(r => add(r, "WAIT", "ML6"));
      const ch = DATA.challenge || {};
      (ch.entry || []).forEach(r => add(Object.assign({}, r, { action: r.action || "BUY_NOW" }), "BUY", "Challenge"));
      (ch.exit || []).forEach(r => add(Object.assign({}, r, { action: r.action || "SELL_NOW" }), "SELL", "Challenge"));
      (ch.hold || []).forEach(r => add(Object.assign({}, r, { action: r.action || "WAIT" }), "WAIT", "Challenge"));
      const k1 = DATA.odte_1k || {};
      (k1.put_now || []).forEach(r => add(Object.assign({}, r, { action: "BUY_NOW", right: r.right || "P" }), "BUY", "0DTE $1K"));
      (k1.exit_now || k1.exit || []).forEach(r => add(r, "SELL", "0DTE $1K"));
      const ac = DATA.action_cards || {};
      const histOk = (sym, hz) => {
        const w = winLookup(sym, hz);
        return w.pct != null && w.pct >= 80 && (w.n || 0) >= 5;
      };
      [
        ["0dte_quality", "0dte", "0DTE"],
        ["weekly_quality", "weekly", "1 Week"],
        ["swing_quality", "swing", "Swing 1–3M"],
      ].forEach(([key, hz, desk]) => {
        (ac[key] || []).forEach(t => {
          if (!t || !t.quality || !histOk(t.symbol, t.horizon || hz)) return;
          add(Object.assign({}, t, {
            action: "SETUP",
            dte_bucket: t.horizon || hz,
            detail: t.detail || "Quality underlying with hist win ≥80% — no option contract on this snapshot, so not BUY NOW.",
          }), "SETUP", desk);
        });
      });
      return { buys, sells, waits, setups };
    }

    function nowBoardCard(r) {
      const buy = r._side === "BUY";
      const wait = r._side === "WAIT";
      const setup = r._side === "SETUP";
      const right = String(r.right || "C").toUpperCase() === "P" ? "PUT" : "CALL";
      const win = Number(r.win_pct ?? r.hist_win_pct);
      const n = Number(r.win_samples ?? r.hist_samples);
      const gated = buy && win >= 80 && (Number.isNaN(n) || n >= 3);
      const cls = buy ? (gated ? "enter-now" : "long") : (wait || setup ? "wait" : "short");
      const label = buy ? (gated ? "ENTER NOW" : "BUY NOW") : (setup ? "SETUP · not BUY" : (wait ? "WAIT" : "SELL NOW"));
      const strike = r.strike == null ? "—" : `${fmt(r.strike, Number(r.strike) % 1 ? 2 : 0)}${right === "PUT" ? "p" : "c"}`;
      const px = buy ? (r.ask ?? r.entry_ask) : (r.bid ?? r.mark ?? r.ask ?? r.exit_bid);
      const when = r.signaled_at_cst || fmtCST(r.signaled_at || r.recommended_at);
      const w = winLookup(r.symbol, r.dte_bucket || r.horizon || "0dte");
      const sr = w.hit1 == null ? "—" : `${fmt(w.hit1,0)}% ≥1%` + (w.hit2 == null ? "" : ` / ${fmt(w.hit2,0)}% ≥2%`);
      return `<article class="action-card ${cls}">
        <div class="ac-top">
          <div class="ac-sym">${r.symbol} <span class="tag">${r._desk}</span>${r.hold_style ? ` <span class="tag">${r.hold_style}</span>` : ""} <span class="tag">${right}</span></div>
          <div class="ac-dir ${buy && gated ? "" : (buy ? "long" : (wait || setup ? "wait" : "short"))}">${label}</div>
        </div>
        <div class="ac-conf">${r._desk} · ${when && when !== "—" ? when : "time —"}</div>
        <div class="ac-meta">
          <div>Strike / expiry<strong>${strike} · ${r.expiry || "—"}${r.dte != null ? ` (${r.dte}DTE)` : ""}</strong></div>
          <div>${buy ? "Ask" : "Bid"}<strong>${px == null ? "—" : "$" + fmt(px, 2)}</strong></div>
          <div>Hist win<strong>${Number.isNaN(win) ? "—" : fmt(win, 0) + "%"}</strong></div>
          <div>Strike rate ≥1%<strong>${sr}</strong></div>
          ${levelsMeta(r)}
        </div>
        <p class="why" style="margin:.45rem 0 0">${r.detail || r.headline || r.exit_plan || r.recommend_reason || ""}</p>
        ${ticketHtml(r.symbol, r)}
      </article>`;
    }

    function renderNowGroups(el, rows, emptyMsg) {
      if (!el) return;
      if (!rows.length) {
        el.innerHTML = `<div class="empty">${emptyMsg}</div>`;
        return;
      }
      const g = {};
      rows.forEach(r => {
        const d = r._desk || "Options";
        (g[d] = g[d] || []).push(r);
      });
      const order = NOW_DESK_ORDER.filter(d => g[d]).concat(Object.keys(g).filter(d => !NOW_DESK_ORDER.includes(d)));
      el.innerHTML = order.map(desk => `
        <div class="now-desk">${desk} <span class="status">${g[desk].length}</span></div>
        <div class="cards">${g[desk].map(nowBoardCard).join("")}</div>
      `).join("");
    }

    function renderNowBoard() {
      let buys = [], sells = [], waits = [], setups = [];
      try {
        ({ buys, sells, waits, setups } = collectNowBoard());
      } catch (err) {
        const note = document.getElementById("nowBoardNote");
        if (note) note.textContent = "BUY/SELL board failed to render: " + (err && err.message ? err.message : err);
        return;
      }
      const m = (k, v, cls = "") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      const metrics = document.getElementById("nowBoardMetrics");
      const byDesk = {};
      [...buys, ...sells, ...waits, ...setups].forEach(r => { byDesk[r._desk] = (byDesk[r._desk] || 0) + 1; });
      if (metrics) {
        metrics.innerHTML = [
          m("BUY NOW", buys.length, buys.length ? "up" : ""),
          m("SELL NOW", sells.length, sells.length ? "down" : ""),
          m("WAIT", waits.length),
          m("SETUP", setups.length),
          ...NOW_DESK_ORDER.filter(d => byDesk[d]).map(d => m(d, byDesk[d])),
        ].join("");
      }
      const note = document.getElementById("nowBoardNote");
      if (note) {
        if (!buys.length && !sells.length) {
          note.textContent = waits.length
            ? `No BUY NOW this snapshot (need hist ≥80% and score in the buy band). ${waits.length} WAIT ticket(s) with contracts are listed below.`
            : "No option BUY NOW / SELL NOW on this snapshot. Pages only pulls a few quality chains. SETUP below is hist-eligible tape, not a buy ticket.";
        } else {
          note.textContent = "";
        }
      }
      renderNowGroups(document.getElementById("nowBoardBuy"), buys, "No BUY NOW — need an option contract plus hist win ≥80% (n≥5).");
      renderNowGroups(document.getElementById("nowBoardSell"), sells, "No SELL NOW across desks this snapshot.");
      renderNowGroups(document.getElementById("nowBoardWait"), waits, "No WAIT tickets. These appear when a chain exists but hist win / tape has not cleared BUY NOW.");
      renderNowGroups(document.getElementById("nowBoardSetup"), setups, "No hist-eligible quality underlyings in this snapshot.");
      const table = document.getElementById("nowBoardTable");
      if (table) {
        const all = [...buys, ...sells, ...waits, ...setups];
        if (!all.length) table.innerHTML = `<div class="empty">Empty board — wait for the next Actions publish, or run a live Flask scan with option chains.</div>`;
        else table.innerHTML = `<table class="zl-tape"><thead><tr>
          <th>Side</th><th>Desk</th><th>Symbol</th><th>Asked (CST)</th><th>Contract</th><th>Px</th><th>Hist win</th><th>Strike rate</th><th>Why</th>
        </tr></thead><tbody>${all.map(r => {
          const buy = r._side === "BUY";
          const side = r._side === "BUY" ? "BUY NOW" : (r._side === "SELL" ? "SELL NOW" : r._side);
          const badge = buy ? "buy" : (r._side === "SELL" ? "sell" : "wait");
          const right = String(r.right || "C").toUpperCase() === "P" ? "p" : "c";
          const px = buy ? (r.ask ?? r.entry_ask) : (r.bid ?? r.mark ?? r.ask);
          const w = winLookup(r.symbol, r.dte_bucket || r.horizon || "0dte");
          const sr = w.hit1 == null ? "—" : `${fmt(w.hit1,0)}%`;
          const win = r.win_pct ?? r.hist_win_pct ?? w.pct;
          return `<tr>
            <td><span class="badge ${badge}">${side}</span></td>
            <td><span class="tag">${r._desk}</span></td>
            <td><strong>${r.symbol}</strong></td>
            <td class="mono">${r.signaled_at_cst || fmtCST(r.signaled_at || r.recommended_at)}</td>
            <td class="mono">${r.strike == null ? "—" : fmt(r.strike, 2) + right} ${r.expiry || ""}</td>
            <td class="mono">${px == null ? "—" : "$" + fmt(px, 2)}</td>
            <td class="mono">${win == null ? "—" : fmt(win, 0) + "%"}</td>
            <td class="mono">${sr}</td>
            <td class="why">${r.detail || r.headline || r.exit_plan || ""}</td>
          </tr>`;
        }).join("")}</tbody></table>`;
      }
    }

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
      const row = (table.symbols || {})[String(symbol||"").toUpperCase()] || {};
      let key = String(hz || "0dte").toLowerCase();
      // Alias challenge / UI labels onto win-rate buckets (1 month ≈ monthly / leap)
      if (key === "1w" || key === "week" || key === "1wk") key = "weekly";
      if (["1m","1mo","1-month","1_month","month","monthly","leap"].includes(key)) key = "monthly";
      if (key.includes("swing")) key = "swing";
      if (key.includes("week")) key = "weekly";
      let s = row[key] || {};
      // Older caches: monthly falls back to swing strike-rate
      if ((!s || s.hit_1pct == null) && key === "monthly") s = row.swing || s;
      if ((!s || s.hit_1pct == null) && key === "swing") s = row.monthly || s;
      return { pct: s.win_pct, n: s.trades || 0, hit1: s.hit_1pct, hit2: s.hit_2pct, hz: key };
    }

    function wallLookup(symbol) {
      const m = (DATA.walls_by_symbol || {})[String(symbol || "").toUpperCase()] || {};
      return m;
    }
    function spotLookup(symbol, row) {
      const r = row || {};
      const direct = r.spot ?? r.live_last ?? r.live_spot ?? r.entry_spot ?? r.last_price ?? r.entry;
      if (direct != null && direct !== "" && !Number.isNaN(Number(direct))) return Number(direct);
      const sym = String(symbol || r.symbol || "").toUpperCase();
      if (!sym) return null;
      const sc = (DATA.scores || []).find(s => String(s.symbol || "").toUpperCase() === sym);
      if (sc) {
        const v = sc.last_price ?? sc.entry;
        if (v != null && !Number.isNaN(Number(v))) return Number(v);
      }
      const q = ((DATA.watch || {}).quotes || {})[sym] || {};
      if (q.last != null && !Number.isNaN(Number(q.last))) return Number(q.last);
      // Challenge / market boards sometimes carry spot under aliases
      for (const bucket of ["entry", "hold", "exit", "tickets"]) {
        const rows = ((DATA.challenge || {})[bucket]) || [];
        const hit = rows.find(x => String(x.symbol || "").toUpperCase() === sym && x.spot != null);
        if (hit) return Number(hit.spot);
      }
      return null;
    }
    /** Spot (scan) + call/put walls + soft EXIT — always shown on recommend tiles. */
    function levelsMeta(t) {
      const row = t || {};
      const sym = String(row.symbol || "").toUpperCase();
      const walls = wallLookup(sym);
      const callW = row.call_wall ?? walls.call_wall;
      const putW = row.put_wall ?? walls.put_wall;
      let soft = row.soft_exit ?? walls.soft_exit;
      if (soft == null && callW != null && !Number.isNaN(Number(callW))) {
        soft = Number(callW) - Number(row.wall_buffer_usd ?? walls.wall_buffer_usd ?? 0.10);
      }
      const spot = spotLookup(sym, row);
      return `
          <div>Spot (scan)<strong>${spot==null?"—":"$"+fmt(spot,2)}</strong></div>
          <div title="Max call OI ≥ spot">Call wall<strong class="up">${callW==null?"—":fmt(callW,2)}</strong></div>
          <div title="Max put OI ≤ spot">Put wall<strong class="down">${putW==null?"—":fmt(putW,2)}</strong></div>
          <div title="Take profit on underlying before OI wall">Soft EXIT<strong class="up">${soft==null?"—":"$"+fmt(soft,2)}</strong></div>`;
    }
    function wallMeta(t) {
      // Back-compat alias — prefer levelsMeta on recommend tiles
      return levelsMeta(t);
    }
    function cardHTML(t, hz) {
      const long = t.quality || (t.ensemble_score||0) >= 70;
      const conf = Math.round(t.ensemble_score||0);
      const w = winLookup(t.symbol, hz);
      const walls = wallLookup(t.symbol);
      const dir = long ? "LONG" : "WAIT";
      const winLabel = w.pct==null ? "—" : `${fmt(w.pct,0)}%`;
      const nLabel = w.pct==null ? "—" : `${w.n} samples`;
      const strikeRate = w.hit1==null ? "—" : `${fmt(w.hit1,0)}% ≥1%` + (w.hit2==null?"":` / ${fmt(w.hit2,0)}% ≥2%`);
      const softHint = walls.exit_hint || walls.wall_exit_hint || "";
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
          ${levelsMeta({...t, ...walls})}
        </div>
        <p class="why" style="margin:.55rem 0 0">${(t.reasons||[]).filter(r=>!r.includes("/")).slice(0,4).join(" · ")||"—"}</p>
        ${softHint?`<p class="why" style="margin:.35rem 0 0"><strong>Wall EXIT:</strong> ${softHint}</p>`:""}
        ${ticketHtml(t.symbol, t)}
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
        <th>Action</th><th>Symbol</th><th>Asked (CST)</th><th>Side</th><th>Strike</th><th>Expiry</th><th>ENTER</th><th>EXIT</th><th>P&amp;L</th><th>Why / EXIT plan</th>
      </tr></thead><tbody>${rows.map(r=>{
        const a=(r.action||"WAIT").replace("_"," ");
        const cls=(r.action||"WAIT").toLowerCase().split("_")[0];
        const side=(r.right||"C")==="P"?"PUT":"CALL";
        const when = (r.action==="BUY_NOW"||r.action==="SELL_NOW")
          ? (r.signaled_at_cst || fmtCST(r.signaled_at) || "—")
          : "—";
        const tk = ticketLines(r.symbol, r);
        const why=[r.detail||"", r.exit_plan||tk.planOut||""].filter(Boolean).join(" · ");
        const pnl = tk.pnl==null && tk.pct==null ? "—" : `${tk.pct==null?"—":fmt(tk.pct,1)+"%"}${tk.pnl==null?"":" · $"+fmt(tk.pnl,2)}`;
        return `<tr>
          <td><span class="badge ${cls}">${a}</span></td>
          <td><strong>${r.symbol}</strong></td>
          <td class="mono">${when}</td>
          <td class="mono">${side}</td>
          <td class="mono">${r.strike==null?"—":fmt(r.strike,2)}${(r.right||"C")==="P"?"p":"c"}</td>
          <td class="mono">${r.expiry||"—"} <span class="status">DTE ${r.dte??"—"}</span></td>
          <td class="mono">${tk.entry==null?"—":"$"+fmt(tk.entry,2)}</td>
          <td class="mono">${tk.exitPx==null?"—":"$"+fmt(tk.exitPx,2)}</td>
          <td class="mono ${tk.pnl==null&&tk.pct==null?"":pctClass(tk.pct??tk.pnl)}">${pnl}</td>
          <td class="why">${why}</td>
        </tr>`;
      }).join("")}</tbody></table>`;
    }

    function lotteryCard(r) {
      const act = (r.action||"WAIT");
      const kind = act.startsWith("BUY") ? "long" : act.startsWith("SELL") ? "short" : "wait";
      const enter = act === "BUY_NOW" && (Number(r.win_pct ?? r.hist_win_pct) >= 80) && (Number(r.win_samples ?? r.hist_samples) >= 3);
      const tags = (r.playbook||[]).slice(0,6).map(p => `<span class="tag">${p}</span>`).join("");
      return `<article class="action-card ${enter ? "enter-now" : kind}">
        <div class="ac-top">
          <div class="ac-sym">${r.symbol}</div>
          <div class="ac-dir ${enter ? "" : kind}">${enter ? "ENTER NOW" : act.replaceAll("_"," ")}</div>
        </div>
        <div class="ac-conf">Strength ${fmt(r.strength,0)} · ${r.confirms||0} confirms${r.best_mult!=null?` · ~${fmt(r.best_mult,0)}× upside`:""}${(r.action==="BUY_NOW"||r.action==="SELL_NOW")?` · asked ${r.signaled_at_cst||fmtCST(r.signaled_at)||"—"}`:""}</div>
        <div class="bar"><i style="width:${Math.min(100,r.strength||0)}%"></i></div>
        <div class="ac-meta">
          <div>Strike<strong>${r.strike==null?"—":fmt(r.strike,2)+"c"}</strong></div>
          <div>Ask / Bid<strong>${fmt(r.ask,2)} / ${fmt(r.bid,2)}</strong></div>
          <div>@+3% / +5%<strong>${fmt(r.mult_at_3pct,1)}× / ${fmt(r.mult_at_5pct,1)}×</strong></div>
          <div>Lottery score<strong>${fmt(r.lottery_score,0)}</strong></div>
          <div>Tape 5m / 15m<strong>${r.mom_5m_pct==null?"—":fmt(r.mom_5m_pct,2)+"%"} / ${r.mom_15m_pct==null?"—":fmt(r.mom_15m_pct,2)+"%"}</strong></div>
          <div>Unreal%<strong>${r.option_unrealized_pct==null?"—":fmt(r.option_unrealized_pct,0)+"%"}</strong></div>
          ${levelsMeta(r)}
        </div>
        <p class="why" style="margin:.55rem 0 0">${r.detail||r.headline||"—"}</p>
        ${ticketHtml(r.symbol, r)}
        <div class="playbook">${tags}</div>
      </article>`;
    }

    function lotteryActionRows(rows) {
      if (!rows || !rows.length) return `<div class="empty">None right now.</div>`;
      return `<table><thead><tr>
        <th>Action</th><th>Symbol</th><th>Asked (CST)</th><th>Contract</th><th>Ask/Bid</th><th>@+3%</th><th>Strength</th><th>Why</th>
      </tr></thead><tbody>${rows.map(r=>{
        const a=(r.action||"WAIT").replaceAll("_"," ");
        const cls=(r.action||"WAIT").toLowerCase().split("_")[0];
        const when = (r.action==="BUY_NOW"||r.action==="SELL_NOW")
          ? (r.signaled_at_cst || fmtCST(r.signaled_at) || "—")
          : "—";
        return `<tr>
          <td><span class="badge ${cls}">${a}</span></td>
          <td><strong>${r.symbol}</strong></td>
          <td class="mono">${when}</td>
          <td class="mono">${r.strike==null?"—":fmt(r.strike,2)+"c"} ${r.expiry||""} <span class="status">DTE ${r.dte??"—"}</span></td>
          <td class="mono">${fmt(r.ask,2)} / ${fmt(r.bid,2)}</td>
          <td class="mono up">${fmt(r.mult_at_3pct,1)}×</td>
          <td class="mono">${fmt(r.strength,0)}</td>
          <td class="why">${r.detail||""}${(r.vetoes&&r.vetoes.length)?` · veto: ${r.vetoes.slice(0,2).join("; ")}`:""}</td>
        </tr>`;
      }).join("")}</tbody></table>`;
    }

    function radarCard(r) {
      const act = r.action || "RADAR_WATCH";
      const cls = act === "RADAR_HOT" ? "long" : (act === "RADAR_COOL" ? "short" : "wait");
      const tag = act.replace("RADAR_", "");
      return `<article class="action-card ${cls}">
        <div class="ac-top">
          <div class="ac-sym">${r.symbol} <span class="tag">radar</span></div>
          <div class="ac-dir ${cls}">${tag}</div>
        </div>
        <div class="ac-meta">
          <div>Strike / expiry<strong>${r.strike==null?"—":fmt(r.strike,2)} · ${r.expiry||"—"}</strong></div>
          <div>Ask / spot<strong>$${fmt(r.ask,2)} · $${fmt(r.spot,2)}</strong></div>
          <div>OTM %<strong>${r.moneyness_pct==null?"—":fmt(r.moneyness_pct,2)+"%"}</strong></div>
          <div>~1% / 2% mult<strong>${r.mult_at_1pct==null?"—":fmt(r.mult_at_1pct,1)+"×"} / ${r.mult_at_2pct==null?"—":fmt(r.mult_at_2pct,1)+"×"}</strong></div>
          <div>Confirms<strong>${r.confirms??0}</strong></div>
          <div>Contract<strong class="mono" style="font-size:.72rem">${r.contract||"—"}</strong></div>
          ${levelsMeta(r)}
        </div>
        <p class="why" style="margin:.45rem 0 0">${r.detail||r.headline||""}</p>
      </article>`;
    }

    function chaseCard(r) {
      const risky = (r.action||"") === "BUY_RISKY";
      const watch = (r.action||"") === "WATCH_CONVEX";
      const cls = risky ? "short" : "wait";
      const dir = risky ? "BUY — BIT RISKY" : ((r.action||"CHASE").replaceAll("_"," "));
      return `<article class="action-card ${cls}">
        <div class="ac-top">
          <div class="ac-sym">${r.symbol} <span class="tag">${r.risk_tag||"chase"}</span></div>
          <div class="ac-dir ${cls}">${dir}</div>
        </div>
        <div class="ac-meta">
          <div>Strike / expiry<strong>${r.strike==null?"—":fmt(r.strike,2)+"c"} · ${r.expiry||"—"}</strong></div>
          <div>Ask / spot<strong>$${fmt(r.ask,2)} · $${fmt(r.spot,2)}</strong></div>
          <div>OTM %<strong>${r.moneyness_pct==null?"—":fmt(r.moneyness_pct,2)+"%"}</strong></div>
          <div>Live / 5m<strong>${r.live_change_pct==null?"—":fmt(r.live_change_pct,2)+"%"}${r.mom_5m_pct==null?"":" · "+fmt(r.mom_5m_pct,2)+"%"}</strong></div>
          <div>Mult +3% / +5%<strong>${fmt(r.mult_at_3pct,1)}× / ${fmt(r.mult_at_5pct,1)}×</strong></div>
          <div>0DTE score<strong>${r.ensemble_score==null?"—":fmt(r.ensemble_score,0)}</strong></div>
          <div>Confirms<strong>${r.confirms??0}</strong></div>
          <div>Contract<strong class="mono" style="font-size:.72rem">${r.contract||"—"}</strong></div>
          ${levelsMeta(r)}
        </div>
        <p class="why" style="margin:.45rem 0 0">${r.detail||r.headline||""}</p>
        ${risky?`<p class="why" style="margin:.25rem 0 0"><strong>Risk:</strong> Not hist-gated BUY NOW — size small; premium can go to zero.</p>`:""}
      </article>`;
    }


    function renderFreeDealer(fd) {
      const el = document.getElementById("freeDealerBody");
      if (!el) return;
      if (!fd || !fd.ok) {
        el.innerHTML = `<div class="empty">Free feeds unavailable.</div>`;
        return;
      }
      const spx = fd.spx_gex || {};
      const sm = fd.squeezemetrics || {};
      const vol = (fd.vol_term || {}).levels || {};
      const lvl = (k) => (vol[k] && vol[k].last != null) ? fmt(vol[k].last, 2) : "—";
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      const summary = (fd.summary || []).map(s => `<li>${s}</li>`).join("");
      el.innerHTML = `
        <div class="metric-row">
          ${m("SPX GEX", spx.regime||"—", spx.regime==="SHORT_GAMMA"?"down":"up")}
          ${m("Call wall", spx.call_wall??"—")}
          ${m("Flip", spx.zero_gamma_flip??"—")}
          ${m("DIX", sm.dix??"—", sm.bias==="SUPPORTIVE"?"up":"")}
          ${m("SM GEX $B", sm.gex_billions??"—")}
          ${m("VIX", lvl("VIX"))}
          ${m("VIX1D", lvl("VIX1D"))}
          ${m("SKEW", lvl("SKEW"))}
        </div>
        <ul class="why" style="padding-left:1.1rem;margin:.6rem 0">${summary}</ul>
        <p class="why" style="font-size:.72rem;color:var(--muted)">${fd.disclaimer||""}</p>
      `;
    }

    function renderRedFlag(rf) {
      const body = document.getElementById("redFlagBody");
      const odte = document.getElementById("redFlagOdte");
      if (!rf) {
        const empty = `<div class="empty">Red Flag unavailable — run a scan.</div>`;
        if (body) body.innerHTML = empty;
        if (odte) odte.innerHTML = empty;
        return;
      }
      const st = rf.state || "NEUTRAL";
      const cls = st === "RED_FLAG" ? "down" : (st === "SUPPORTIVE" ? "up" : "");
      const badgeCls = st === "RED_FLAG" ? "sell" : (st === "SUPPORTIVE" ? "buy" : "hold");
      const strikes = (rf.resistance_strikes||[]).slice(0,4).map(s =>
        `<span class="tag">$${s.strike} · OI ${s.open_interest}</span>`
      ).join(" ");
      const rules = (rf.bottom_line_rules||[]).map(r =>
        `<li><strong>${r.ticker}</strong> (${r.when}): ${r.text}</li>`
      ).join("");
      const html = `<article class="action-card ${st==="RED_FLAG"?"wait":"long"}">
        <div class="ac-top">
          <div class="ac-sym">${rf.symbol||"SPY"}</div>
          <div class="ac-dir ${cls||"wait"}"><span class="badge ${badgeCls}">${st.replace(/_/g," ")}</span></div>
        </div>
        <div class="ac-conf">Score ${fmt(rf.score,1)} · charm ${rf.charm_pressure||"—"} · expiry ${rf.expiry||"—"} · spot $${fmt(rf.spot,2)}</div>
        <div class="bar"><i style="width:${Math.min(100, rf.score||50)}%"></i></div>
        <p class="why" style="max-width:none">${(rf.reasons||[]).slice(0,5).join(" · ")||"—"}</p>
        <p class="why" style="max-width:none;margin-top:.4rem"><strong>Equilibrium / call-wall:</strong> ${rf.equilibrium_strike?`$${rf.equilibrium_strike}`:"—"} ${strikes}</p>
        <p class="why" style="max-width:none;margin-top:.4rem">${rf.strategy_hint||""}</p>
        ${rf.cboe_gex && rf.cboe_gex.ok ? `<p class="why" style="max-width:none;margin-top:.4rem"><strong>CBOE SPX GEX:</strong> ${rf.cboe_gex.regime} · net ${fmt(rf.cboe_gex.net_gex/1e9,2)}B · call wall ${rf.cboe_gex.call_wall??"—"} · flip ${rf.cboe_gex.zero_gamma_flip??"—"} · contracts ${rf.cboe_gex.contracts_used??"—"}</p>` : ""}
        <p class="why" style="max-width:none;margin-top:.4rem;font-size:.72rem;color:var(--muted)">${rf.volsignals_note||""}</p>
        ${rf.block_0dte_long_calls ? `<p class="why down" style="margin-top:.5rem"><strong>Gate active:</strong> index 0DTE long calls blocked until Red Flag clears.</p>` : ""}
        <div class="panel" style="margin-top:.8rem">
          <h2 style="font-size:1rem;margin-bottom:.4rem">Bottom-line earnings watch (ML6)</h2>
          <ul class="why" style="padding-left:1.1rem;margin:0">${rules}</ul>
        </div>
      </article>`;
      if (body) body.innerHTML = html;
      if (odte) odte.innerHTML = html;
    }

    function renderMl6(ml6) {
      const rulesEl = document.getElementById("ml6Rules");
      const boardEl = document.getElementById("ml6Board");
      const metricsEl = document.getElementById("ml6Metrics");
      const purpose = document.getElementById("ml6Purpose");
      const buyEl = document.getElementById("ml6Buy");
      const sellEl = document.getElementById("ml6Sell");
      const waitEl = document.getElementById("ml6Wait");
      const primaryEl = document.getElementById("ml6Primary");
      if (!ml6 || (!ml6.watchlist && !ml6.bottom_line_rules)) {
        if (rulesEl) rulesEl.innerHTML = `<div class="empty">No ML6 data — click Scan ML6.</div>`;
        if (boardEl) boardEl.innerHTML = `<div class="empty">No ML6 board yet.</div>`;
        return;
      }
      if (purpose && ml6.purpose) purpose.textContent = ml6.purpose;
      const rules = ml6.bottom_line_rules || [];
      if (rulesEl) {
        rulesEl.innerHTML = rules.length ? rules.map(r => {
          const st = (r.status||"WATCH").replace(/_/g," ");
          const cls = r.status==="WAIT_FOR_CONFIRMATION" ? "wait" : (r.status==="BUY_ONLY_IF_ACCEPTED" ? "long" : "wait");
          return `<article class="action-card ${cls}" style="margin-bottom:.55rem">
            <div class="ac-top">
              <div class="ac-sym">${r.ticker}</div>
              <div class="ac-dir ${cls}">${st}</div>
            </div>
            <div class="ac-conf">${r.headline||""}</div>
            <p class="why" style="max-width:none;margin:.35rem 0 0">${r.rule||""}</p>
          </article>`;
        }).join("") : `<div class="empty">No bottom-line rules.</div>`;
      }

      const acts = ml6.actions || {};
      const c = Object.assign({}, ml6.counts || {}, acts.counts || {});
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      if (metricsEl) {
        metricsEl.innerHTML = [
          m("BUY NOW", c.buy_now??0, (c.buy_now||0)>0?"up":""),
          m("SELL NOW", c.sell_now??0, (c.sell_now||0)>0?"down":""),
          m("WAIT", c.wait??0),
          m("WATCH", c.watch??0),
          m("Names", c.names??(ml6.watchlist||[]).length),
          m("Liquid OK", c.liquidity_ok??0),
        ].join("");
      }

      const actionRows = (rows) => {
        if (!rows || !rows.length) return `<div class="empty">None right now.</div>`;
        return `<table><thead><tr>
          <th>Action</th><th>Symbol</th><th>Asked (CST)</th><th>Contract</th><th>Ask/Bid</th><th>Strength</th><th>Why</th>
        </tr></thead><tbody>${rows.map(r=>{
          const a=(r.action||"WAIT").replaceAll("_"," ");
          const cls=(r.action||"WAIT").toLowerCase().split("_")[0];
          const when = r.signaled_at_cst || fmtCST(r.signaled_at) || "—";
          return `<tr>
            <td><span class="badge ${cls}">${a}</span></td>
            <td><strong>${r.symbol}</strong></td>
            <td class="mono">${when}</td>
            <td class="mono">${r.strike==null?"—":fmt(r.strike,2)+"c"} ${r.expiry||""} <span class="status">DTE ${r.dte??"—"}</span></td>
            <td class="mono">${fmt(r.ask,2)} / ${fmt(r.bid,2)}</td>
            <td class="mono">${fmt(r.strength,0)}</td>
            <td class="why">${r.detail||""}</td>
          </tr>`;
        }).join("")}</tbody></table>`;
      };

      const actionable = [...(acts.sell_now||[]), ...(acts.buy_now||[])].slice(0,4);
      if (primaryEl) {
        primaryEl.innerHTML = actionable.length
          ? actionable.map(r => {
              const enter = (r.action||"").startsWith("BUY");
              const kind = enter ? "enter-now" : "wait";
              const when = r.signaled_at_cst || fmtCST(r.signaled_at);
              return `<article class="action-card ${kind}">
                <div class="ac-top"><div class="ac-sym">${r.symbol}</div>
                <div class="ac-dir ${enter ? "" : "wait"}">${enter ? "ENTER NOW" : (r.action||"").replaceAll("_"," ")}</div></div>
                <div class="ac-conf">Strength ${fmt(r.strength,0)} · score ${fmt(r.score,0)} · asked ${when||"—"}</div>
                <p class="why" style="max-width:none">${r.detail||r.headline||""}</p>
              </article>`;
            }).join("")
          : `<div class="empty">${acts.note||"No ML6 BUY/SELL cleared — reaction gate still on."}</div>`;
      }
      if (buyEl) buyEl.innerHTML = actionRows(acts.buy_now||[]);
      if (sellEl) sellEl.innerHTML = actionRows(acts.sell_now||[]);
      if (waitEl) waitEl.innerHTML = actionRows([...(acts.wait||[]), ...(acts.watch||[]).slice(0,8)]);

      const rows = ml6.watchlist || [];
      if (!boardEl) return;
      if (!rows.length) {
        boardEl.innerHTML = `<div class="empty">Empty ML6 watchlist.</div>`;
        return;
      }
      const statusBadge = (st) => {
        const s = st||"WATCH";
        const cls = s.includes("SELL") ? "sell" : (s.includes("BUY") ? "buy" : (s.includes("WAIT") ? "wait" : "hold"));
        return `<span class="badge ${cls}">${String(s).replace(/_/g," ")}</span>`;
      };
      boardEl.innerHTML = `<table><thead><tr>
        <th>Ticker</th><th>Earnings</th><th>Theme</th><th>Score</th><th>Gate</th><th>Trade</th><th>Asked (CST)</th><th>DD%</th><th>Last</th><th>Detail</th>
      </tr></thead><tbody>${rows.map(r => `<tr>
        <td><strong>${r.symbol}</strong></td>
        <td class="mono">${r.earnings_label||r.earnings_date||"—"}</td>
        <td><span class="tag">${(r.theme||"").split(",")[0]||"—"}</span></td>
        <td class="mono">${fmt(r.ensemble_score||r.score,1)}</td>
        <td>${statusBadge(r.status)}</td>
        <td>${statusBadge(r.trade_action||"—")}</td>
        <td class="mono">${r.buy_now_at_cst || ((r.trade_action||"").includes("BUY") ? fmtCST(r.buy_now_at) : "—")}</td>
        <td class="mono">${r.drawdown_pct==null?"—":fmt(r.drawdown_pct,1)+"%"}</td>
        <td class="mono">${r.last_price==null?"—":fmt(r.last_price,2)}</td>
        <td class="why">${r.trade_detail||r.gate||r.action||""}</td>
      </tr>`).join("")}</tbody></table>`;
    }

    function renderRadar(radar) {
      const metrics = document.getElementById("radarMetrics");
      const hotEl = document.getElementById("radarHot");
      const watchEl = document.getElementById("radarWatch");
      const rad = radar || {};
      const c = rad.counts || {};
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      if (metrics) {
        metrics.innerHTML = [
          m("RADAR HOT", c.hot||0, (c.hot||0)>0?"up":""),
          m("WATCH", c.watch||0),
          m("COOL", c.cool||0),
          m("Wings scanned", c.tickets||(rad.tickets||[]).length||0),
        ].join("");
      }
      if (hotEl) {
        const hot = rad.hot || [];
        hotEl.innerHTML = hot.length
          ? `<div class="cards">${hot.map(radarCard).join("")}</div>`
          : `<div class="empty">No RADAR HOT wings — waiting for cheap SPY/QQQ near-money tape/reclaim.</div>`;
      }
      if (watchEl) {
        const gated = [...(rad.watch||[]), ...(rad.cool||[]).slice(0,4)];
        watchEl.innerHTML = gated.length
          ? `<div class="cards">${gated.map(radarCard).join("")}</div><p class="lede" style="margin-top:.5rem;font-size:.76rem">${rad.note||""}</p>`
          : `<div class="empty">${rad.note||"Radar idle."}</div>`;
      }
    }

    function renderChaseRadar(chase) {
      const metrics = document.getElementById("chaseMetrics");
      const buyEl = document.getElementById("chaseBuyRisky");
      const watchEl = document.getElementById("chaseWatch");
      const noteEl = document.getElementById("chaseScoreNote");
      const ch = chase || {};
      const c = ch.counts || {};
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      if (metrics) {
        metrics.innerHTML = [
          m("BUY — BIT RISKY", c.buy_risky||0, (c.buy_risky||0)>0?"down":""),
          m("WATCH CONVEX", c.watch||0),
          m("COOL", c.cool||0),
          m("Wings scanned", c.all||(ch.tickets||[]).length||0),
        ].join("");
      }
      if (buyEl) {
        const rows = ch.buy_risky || [];
        buyEl.innerHTML = rows.length
          ? `<div class="cards">${rows.map(chaseCard).join("")}</div>`
          : `<div class="empty">No BUY — BIT RISKY tickets — waiting for ripping tape + convex wings.</div>`;
      }
      if (watchEl) {
        const gated = [...(ch.watch||[]), ...(ch.cool||[]).slice(0,4)];
        watchEl.innerHTML = gated.length
          ? `<div class="cards">${gated.map(chaseCard).join("")}</div><p class="lede" style="margin-top:.5rem;font-size:.76rem">${ch.note||""}</p>`
          : `<div class="empty">${ch.note||"Chase lane idle."}</div>`;
      }
      if (noteEl) noteEl.textContent = ch.score_note || "";
    }

    function fillRecLogMetrics(metricsId, board) {
      const metrics = document.getElementById(metricsId);
      if (!metrics || !board) return;
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      metrics.innerHTML = [
        m("Open", board.open??0),
        m("Closed", board.closed??0),
        m("Wins", board.wins??0, "up"),
        m("Losses", board.losses??0, "down"),
        m("Scratch/lapse", (board.scratches||0)+(board.lapsed||0)),
        m("Closed P&L", board.closed_pnl_usd==null?"—":`$${fmt(board.closed_pnl_usd,2)}`, pctClass(board.closed_pnl_usd)),
      ].join("");
    }

    function renderRecLog(elId, board, emptyMsg, metricsId) {
      const el = document.getElementById(elId);
      if (!el) return;
      if (metricsId) fillRecLogMetrics(metricsId, board || {});
      const open = (board && board.open_recs) || [];
      const closed = (board && board.closed_recs) || [];
      if (!open.length && !closed.length) {
        el.innerHTML = `<div class="empty">${emptyMsg||"No recommendations logged yet."}</div>`;
        return;
      }
      const strikeTxt = (r) => {
        if (r.strike == null) return "—";
        const side = (r.right || "C") === "P" ? "p" : "c";
        return `${Number(r.strike).toFixed(Number(r.strike) % 1 ? 2 : 0)}${side}`;
      };
      const pnlTone = (r) => {
        if (r.status === "lapsed" || r.profit_pct == null) return "";
        return r.profit_pct > 0 ? "long" : (r.profit_pct < 0 ? "short" : "");
      };
      const cardOpen = (r) => `<article class="action-card enter-now">
        <div class="ac-top">
          <div class="ac-sym">${r.symbol} <span class="tag">${r.section||""}</span></div>
          <div class="ac-dir">${r.open_action||"ENTER NOW"}${r.on_board?"":" · OFF BOARD"}</div>
        </div>
        <div class="ac-meta">
          <div>Strike / expiry<strong>${strikeTxt(r)} · ${r.expiry||"—"}${r.dte!=null?` (${r.dte}DTE)`:""}</strong></div>
          <div>Entry ask<strong>${r.entry_price==null?"—":"$"+fmt(r.entry_price,2)}</strong></div>
          <div>ENTER time (CST)<strong>${fmtCST(r.recommended_at)}</strong></div>
          <div>Strike rate ≥1%<strong>${(() => { const w=winLookup(r.symbol, r.horizon||r.dte_bucket||"0dte"); return w.hit1==null?"—":fmt(w.hit1,0)+"%"; })()}</strong></div>
          <div>Last seen (CST)<strong>${fmtCST(r.last_recommended_at||r.recommended_at)}</strong></div>
          <div>Contract<strong class="mono" style="font-size:.72rem">${r.contract||"—"}</strong></div>
          <div>Status<strong>${r.on_board?"On board":"Off board"}</strong></div>
          ${levelsMeta(r)}
        </div>
        <p class="why" style="margin:.45rem 0 0">${r.headline||r.reason||""}</p>
      </article>`;
      const cardClosed = (r) => {
        const tone = pnlTone(r);
        const dir = r.status === "lapsed" ? "LAPSE" : (r.close_action||"EXIT");
        const pnlLabel = r.status === "lapsed" ? "—" : (r.pnl_usd==null?"—":"$"+fmt(r.pnl_usd,2));
        const pctLabel = r.status === "lapsed" ? "lapse" : (r.profit_pct==null?"—":fmt(r.profit_pct,1)+"%");
        return `<article class="action-card ${tone||""}">
        <div class="ac-top">
          <div class="ac-sym">${r.symbol} <span class="tag">${r.section||""}</span></div>
          <div class="ac-dir ${tone||""}">${dir}</div>
        </div>
        <div class="ac-meta">
          <div>Strike / expiry<strong>${strikeTxt(r)} · ${r.expiry||"—"}</strong></div>
          <div>In ask → out bid<strong>${r.entry_price==null?"—":"$"+fmt(r.entry_price,2)} → ${r.exit_price==null?"—":"$"+fmt(r.exit_price,2)}</strong></div>
          <div>Entry (CST)<strong>${fmtCST(r.recommended_at)}</strong></div>
          <div>Exit (CST)<strong>${fmtCST(r.closed_at)}</strong></div>
          <div>Strike rate ≥1%<strong>${(() => { const w=winLookup(r.symbol, r.horizon||r.dte_bucket||"0dte"); return w.hit1==null?"—":fmt(w.hit1,0)+"%"; })()}</strong></div>
          <div>Profit %<strong class="${r.profit_pct==null?"":pctClass(r.profit_pct)}">${pctLabel}</strong></div>
          <div>P&amp;L (1ct)<strong class="${r.pnl_usd==null?"":pctClass(r.pnl_usd)}">${pnlLabel}</strong></div>
          ${levelsMeta(r)}
        </div>
        <p class="why" style="margin:.45rem 0 0">${r.exit_reason||r.reason||r.headline||""}</p>
      </article>`;
      };
      let html = "";
      if (board && board.pnl_note) {
        html += `<p class="lede" style="margin:.1rem 0 .55rem;font-size:.74rem">${board.pnl_note}</p>`;
      }
      if (open.length) {
        html += `<div class="status" style="margin:.2rem 0 .45rem">OPEN / STILL TRACKING (${open.length}) · times in CST</div>
          <div class="cards">${open.map(cardOpen).join("")}</div>`;
      }
      if (closed.length) {
        html += `<div class="status" style="margin:.9rem 0 .45rem">CLOSED / LAPSED — EXIT / P&amp;L (${closed.length}) · times in CST</div>
          <div class="cards">${closed.map(cardClosed).join("")}</div>`;
      }
      el.innerHTML = html;
    }

    function renderRecLogAll(rec) {
      const allEl = document.getElementById("recLogAll");
      if (!rec) {
        if (allEl) allEl.innerHTML = `<div class="empty">Recommendation log empty.</div>`;
        return;
      }
      fillRecLogMetrics("recLogMetrics", rec);
      const by = rec.by_section || {};
      // Prefer combined all list
      renderRecLog("recLogAll", {
        open_recs: (rec.open_recs || (rec.all||[]).filter(r=>r.status==="open")),
        closed_recs: (rec.closed_recs || (rec.all||[]).filter(r=>r.status==="closed"||r.status==="lapsed")),
        pnl_note: rec.pnl_note,
      }, "No recommendations logged yet — appear after BUY NOW / ENTRY pulses.");
      // Section panels (each desk tab has its own metrics + cards)
      renderRecLog("lotteryRecLog", by.lottery || rec.lottery, "No lottery recommendations logged yet.", "lotteryRecLogMetrics");
      renderRecLog("challengeRecLog", by.challenge || rec.challenge, "No challenge recommendations logged yet.", "challengeRecLogMetrics");
      renderRecLog("odteRecLog", by.odte || rec.odte, "No 0DTE recommendations logged yet.", "odteRecLogMetrics");
      renderRecLog("weeklyRecLog", by.weekly || rec.weekly, "No weekly recommendations logged yet.", "weeklyRecLogMetrics");
      renderRecLog("swingRecLog", by.swing || rec.swing, "No swing recommendations logged yet.", "swingRecLogMetrics");
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
      const rec = (DATA.rec_log && DATA.rec_log.by_section && DATA.rec_log.by_section.lottery) || (DATA.rec_log && DATA.rec_log.lottery);
      renderRecLog("lotteryRecLog", rec, "No lottery recommendations logged yet.", "lotteryRecLogMetrics");
    }

    function renderChallenge(ch) {
      const metrics = document.getElementById("challengeMetrics");
      const primaryEl = document.getElementById("challengePrimary");
      const pathEl = document.getElementById("challengePath");
      const ticketsEl = document.getElementById("challengeTickets");
      const statusEl = document.getElementById("challengeStatus");
      const bookEl = document.getElementById("challengeBook");
      const planEl = document.getElementById("challengePlan");
      const disc = document.getElementById("challengeDisclaimer");
      const earnWatchEl = document.getElementById("challengeEarningsWatch");
      const swingEarnEl = document.getElementById("swingEarningsWatch");
      if (!ch || !Object.keys(ch).length) {
        if (ticketsEl) ticketsEl.innerHTML = `<div class="empty">Challenge board loading…</div>`;
        return;
      }
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      const path = ch.path || {};
      const pace = ch.pace || {};
      const paceM = pace.milestone || {};
      const c = ch.counts || {};
      // Prefer ledger book (always written by API); sync.book is fallback
      const book = (ch.book && (ch.book.trades || ch.book.cash!=null)) ? ch.book
                 : ((ch.sync && ch.sync.book) || {});
      const sync = ch.sync || {};
      const holdLbl = (t) => t.hold_approx_label || (t.hold_ideal_days!=null?`≈${t.hold_ideal_days}d (${t.hold_min_days||"?"}–${t.hold_max_days||"?"}d)`:(t.hold_period_label||"—"));
      const spotBadge = (t) => {
        const src=t.spot_source||"none";
        if (src==="live") return `<span class="badge buy">LIVE</span>`;
        if (src==="cache") return `<span class="badge wait">CACHE</span>`;
        if (src==="scan") return `<span class="badge skip">SCAN</span>`;
        return `<span class="badge sell">NO SPOT</span>`;
      };
      if (metrics) metrics.innerHTML = [
        m("Sleeve cash", book.cash!=null?`$${Number(book.cash).toLocaleString(undefined,{maximumFractionDigits:0})}`:"—"),
        m("Sleeve equity", book.equity!=null?`$${Number(book.equity).toLocaleString(undefined,{maximumFractionDigits:0})}`:`$${(ch.start_usd||1000).toLocaleString()}`),
        m("→ $500k", book.milestone_500k_pct!=null?`${fmt(book.milestone_500k_pct,2)}%`:(book.equity!=null?`${fmt((book.equity/500000)*100,2)}%`:"—"), "up"),
        m("→ $1M", book.progress_pct!=null?`${fmt(book.progress_pct,3)}%`:"—", "up"),
        m("4mo need / flip", paceM.pct_per_flip==null?"—":`+${fmt(paceM.pct_per_flip,0)}%`, "up"),
        m("Classic need / flip", path.pct_per_flip==null?"—":`+${fmt(path.pct_per_flip,0)}%`),
        m("4mo fits", `${c.fits_4mo_500k||0} / weekly ${c.weekly_pace||0}`),
        m("ENTRY / HOLD / EXIT", `${c.entry||0} / ${c.hold||0} / ${c.exit||0}`),
        m("Closed flips", `${book.flips_closed||0} (W${book.wins||0}/L${book.losses||0})`),
        m("Earn today / week", `${c.earn_today||0} / ${c.earn_this_week||0}`),
      ].join("");

      const earnBadge = (t) => {
        const w = t.earnings_window||t.window||"none";
        const b = t.bucket||"";
        let core = `<span class="badge skip">—</span>`;
        if (w==="post_earnings"||b==="post") core = `<span class="badge buy">POST-EARN</span>`;
        else if (w==="earnings_day"||b==="today") core = `<span class="badge sell">TODAY</span>`;
        else if (b==="this_week") core = `<span class="badge wait">THIS WEEK</span>`;
        else if (b==="next_week") core = `<span class="badge wait">NEXT WEEK</span>`;
        else if (w==="pre_earnings") core = `<span class="badge wait">PRE-EARN</span>`;
        else if (w==="earnings_soon"||b==="soon") core = `<span class="badge skip">SOON</span>`;
        const darling = t.darling ? ` <span class="tag">darling</span>` : "";
        const sess = t.earnings_session ? ` <span class="tag">${String(t.earnings_session).toUpperCase()}</span>` : "";
        return core + darling + sess;
      };
      const renderEarnWatch = (el) => {
        if (!el) return;
        const rows = ch.earnings_watch || [];
        if (!rows.length) {
          el.innerHTML = `<div class="empty">No near-term earnings in watch universe yet (cache warming / fetch limit).</div>`;
          return;
        }
        el.innerHTML = `<table><thead><tr>
          <th>When</th><th>Symbol</th><th>Days</th><th>Date</th><th>Bias</th><th>Note</th>
        </tr></thead><tbody>${rows.slice(0,24).map(r=>{
          const bias = r.strategy_bias||"—";
          const cls = bias==="prefer_post"?"buy":(bias==="caution_pre"||bias==="avoid_short_premium"?"sell":"wait");
          const days = (r.bucket==="post"||r.window==="post_earnings")
            ? (r.days_since_earnings!=null?r.days_since_earnings+"d since":"—")
            : (r.days_to_earnings!=null?r.days_to_earnings+"d to":(r.days_since_earnings!=null?r.days_since_earnings+"d since":"—"));
          const name = r.company_name ? `<div class="why">${r.company_name}</div>` : "";
          return `<tr>
            <td>${earnBadge(r)}</td>
            <td><strong>${r.symbol}</strong>${name}</td>
            <td class="mono">${days}</td>
            <td class="mono">${r.next_earnings||r.last_earnings||"—"}</td>
            <td><span class="badge ${cls}">${bias}</span>${r.prefer_leap?` <span class="tag">LEAP</span>`:""}</td>
            <td class="why">${r.label||""}</td>
          </tr>`;
        }).join("")}</tbody></table>`;
      };
      renderEarnWatch(earnWatchEl);
      renderEarnWatch(swingEarnEl);
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
          const enter = act === "ENTRY";
          const kind = act==="EXIT"?"short":(enter?"enter-now":(act==="HOLD"?"long":"wait"));
          primaryEl.innerHTML = `<article class="action-card ${kind}">
            <div class="ac-top">
              <div class="ac-sym">${t0.symbol} <span class="tag">${t0.right==="P"?"PUT":"CALL"}</span> <span class="tag">${(t0.market_cap_tier||"").replace("_","/")}</span> ${earnBadge(t0)} ${spotBadge(t0)}${t0.fits_4mo_500k?` <span class="badge buy">4MO $500k</span>`:""}</div>
              <div class="ac-dir ${enter ? "" : kind}">${enter ? "ENTER NOW" : act} · ${tier.toUpperCase()}</div>
            </div>
            <div class="ac-conf">Hist win ${fmt(t0.hist_win_pct,0)}% · n=${t0.hist_samples} · <strong>approx hold ${holdLbl(t0)}</strong></div>
            <div class="bar"><i style="width:${Math.min(100,t0.hist_win_pct||0)}%"></i></div>
            <div class="ac-meta">
              <div>Approx hold<strong>${holdLbl(t0)}</strong></div>
              <div>Strike / expiry<strong>${t0.strike==null?"—":fmt(t0.strike,2)} · ${t0.expiry||"—"}</strong></div>
              <div>Day vol / OI<strong>${t0.volume==null?"—":Number(t0.volume).toLocaleString()} / ${t0.open_interest==null?"—":Number(t0.open_interest).toLocaleString()}</strong></div>
              <div>DTE / contract<strong>${t0.dte??"—"}d · ${t0.contract||"pending"}</strong></div>
              <div>Opt ${t0.mark_source||"price"} → tgt<strong>${(t0.ask??t0.option_last)==null?"—":"$"+fmt(t0.ask??t0.option_last,2)} → ${t0.target_ask==null?"—":"$"+fmt(t0.target_ask,2)}</strong></div>
              <div>Strike rate ≥1%/≥2%<strong>${t0.hit_1pct==null?"—":fmt(t0.hit_1pct,0)+"%"} / ${t0.hit_2pct==null?"—":fmt(t0.hit_2pct,0)+"%"}</strong></div>
              ${levelsMeta(t0)}
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
              <th>Status</th><th>Side</th><th>Symbol</th><th>Strike</th><th>Call/Put wall</th><th>Soft EXIT</th><th>Vol/OI</th><th>Opt $</th><th>Hist</th><th>Strike rate</th><th>Hold</th><th>Why</th>
            </tr></thead><tbody>${rows.map(t=>{
              const a=(t.action||"WAIT");
              const cls=a==="EXIT"?"sell":(a==="ENTRY"?"buy":(a==="HOLD"?"hold":"wait"));
              const mark=t.ask??t.option_last;
              const liqBad=(Number(t.volume||0)<=0 && Number(t.open_interest||0)<5000);
              const hz = t.horizon || t.dte_bucket || (t.hold_style) || "swing";
              const w = (t.hit_1pct!=null) ? {hit1:t.hit_1pct, hit2:t.hit_2pct} : winLookup(t.symbol, hz);
              const sr = w.hit1==null ? "—" : `${fmt(w.hit1,0)}% ≥1%` + (w.hit2==null?"":` / ${fmt(w.hit2,0)}% ≥2%`);
              return `<tr>
                <td><span class="badge ${cls}">${a}</span></td>
                <td class="mono">${t.right==="P"?"PUT":"CALL"}</td>
                <td><strong>${t.symbol}</strong> ${spotBadge(t)} ${earnBadge(t)}</td>
                <td class="mono"><strong>${t.strike==null?"—":fmt(t.strike,2)}</strong><div class="why">${t.expiry||""}</div></td>
                <td class="mono"><span class="up">${t.call_wall==null?"—":fmt(t.call_wall,2)}</span> / <span class="down">${t.put_wall==null?"—":fmt(t.put_wall,2)}</span></td>
                <td class="mono up"><strong>${t.soft_exit==null?"—":"$"+fmt(t.soft_exit,2)}</strong><div class="why">${t.primary_wall_side||""} wall${t.wall_buffer_usd!=null?" −$"+fmt(t.wall_buffer_usd,2):""}</div></td>
                <td class="mono ${liqBad?"down":"up"}">${t.volume==null?"—":Number(t.volume).toLocaleString()} / ${t.open_interest==null?"—":Number(t.open_interest).toLocaleString()}</td>
                <td class="mono"><strong>${mark==null?"—":"$"+fmt(mark,2)}</strong><div class="why">${t.mark_source||""}${t.target_ask!=null?" → $"+fmt(t.target_ask,2):""}</div></td>
                <td class="mono up"><strong>${t.hist_win_pct==null?"—":fmt(t.hist_win_pct,0)+"%"}</strong><div class="why">n=${t.hist_samples??"—"}</div></td>
                <td class="mono" title="Underlying ≥1% / ≥2% after signal (~1 month for leap/swing)"><strong>${sr}</strong></td>
                <td class="mono"><strong>${holdLbl(t)}</strong></td>
                <td class="why">${t.recommend_reason||t.status_detail||t.thesis||""}${t.wall_exit_hint?`<div><strong>Wall:</strong> ${t.wall_exit_hint}</div>`:""}</td>
              </tr>`;
            }).join("")}</tbody></table>`;
      }

      if (bookEl) {
        const open=(book.trades||[]).filter(t=>t.status==="open");
        const closed=(book.trades||[]).filter(t=>t.status==="closed").slice(-8).reverse();
        const cash = book.cash!=null?book.cash:(ch.start_usd||1000);
        const equity = book.equity!=null?book.equity:cash;
        const syncNote = (sync.entered&&sync.entered.length)?` · paper entered ${sync.entered.join(", ")}`:"";
        const syncExit = (sync.exited&&sync.exited.length)?` · paper exited ${sync.exited.join(", ")}`:"";
        bookEl.innerHTML = `
          <div class="ac-meta" style="margin-bottom:.5rem">
            <div>Cash<strong>$${Number(cash).toLocaleString(undefined,{maximumFractionDigits:0})}</strong></div>
            <div>Equity<strong>$${Number(equity).toLocaleString(undefined,{maximumFractionDigits:0})}</strong></div>
            <div>Open flips<strong>${book.open_trades!=null?book.open_trades:open.length}</strong></div>
            <div>Closed flips<strong>${book.flips_closed||0}</strong></div>
            <div>Win/Loss<strong>${book.wins||0}/${book.losses||0}</strong></div>
          </div>
          <p class="why" style="margin:.2rem 0 .55rem">Sleeve ledger${syncNote}${syncExit || " · refresh after Paper ENTER"}</p>
          ${open.length?open.map(t=>{
            const tgtPct = t.target_profit_pct!=null?t.target_profit_pct:(t.target_premium_mult!=null?((t.target_premium_mult-1)*100):null);
            const tgtAsk = t.target_ask!=null?t.target_ask:(t.entry_ask!=null&&t.target_premium_mult!=null?t.entry_ask*t.target_premium_mult:null);
            return `<article class="action-card ${t.last_action==="EXIT"?"short":"long"}" style="margin-bottom:.65rem">
              <div class="ac-top">
                <div class="ac-sym">${t.symbol} <span class="tag">${t.right==="P"?"PUT":"CALL"}</span>
                  <span class="badge ${t.last_action==="EXIT"?"sell":"hold"}">${t.last_action||"HOLD"}</span></div>
                <div class="ac-dir ${t.last_action==="EXIT"?"short":"long"}">${t.hold_approx_label||`max ${t.hold_max_days||"—"}d`}</div>
              </div>
              <div class="ac-meta">
                <div>Entered (CST)<strong>${fmtCST(t.entered_at)}</strong></div>
                <div>Strike / expiry<strong>${t.strike==null?"—":fmt(t.strike,2)} · ${t.expiry||"—"}</strong></div>
                <div>Contract / DTE<strong>${t.contract||"—"} · ${t.dte_at_entry??"—"}d</strong></div>
                <div>Entry → mark<strong>$${fmt(t.entry_ask,2)} → $${fmt(t.mark,2)}</strong></div>
                <div>Unrealized / target<strong class="${pctClass(t.unrealized_pct)}">${t.unrealized_pct==null?"—":fmt(t.unrealized_pct,1)+"%"} / ${tgtPct==null?"—":"+"+fmt(tgtPct,0)+"%"}</strong></div>
                <div>Target ask<strong>${tgtAsk==null?"—":"$"+fmt(tgtAsk,2)}</strong></div>
                <div>Strike rate ≥1%/≥2%<strong>${t.hit_1pct==null?"—":fmt(t.hit_1pct,0)+"%"} / ${t.hit_2pct==null?"—":fmt(t.hit_2pct,0)+"%"}</strong></div>
                <div>Hist win<strong>${t.hist_win_pct==null?"—":fmt(t.hist_win_pct,0)+"%"} (n=${t.hist_samples??"—"})</strong></div>
                <div>Held / max<strong>${t.hold_days==null?"0":fmt(t.hold_days,1)}d / ${t.hold_max_days||"—"}d</strong></div>
                <div>Contracts / cost<strong>${t.contracts||1} · $${fmt(t.cost,0)}</strong></div>
                <div>Cash before → after<strong>$${fmt(t.cash_before,0)} → $${fmt(t.cash_after,0)}</strong></div>
                <div>Balance (equity)<strong class="up">$${fmt(t.equity_after!=null?t.equity_after:book.equity,0)}</strong></div>
                ${levelsMeta(t)}
              </div>
              <p class="why" style="margin:.45rem 0 0"><strong>EXIT plan:</strong> ${t.exit_plan||t.last_action_detail||"—"}</p>
              <p class="why" style="margin:.25rem 0 0"><strong>ENTER was:</strong> ${t.enter_plan||t.entry_reason||"—"}</p>
              ${t.balance_note?`<p class="why" style="margin:.25rem 0 0"><strong>Balance:</strong> ${t.balance_note}</p>`:""}
              <div class="playbook" style="margin-top:.45rem">
                <button type="button" class="tag" data-ch-exit="${t.id}">Paper EXIT now</button>
              </div>
            </article>`;
          }).join(""):`<div class="empty">No open challenge flip — click <strong>Paper ENTER</strong> on an ENTRY ticket below.</div>`}
          ${closed.length?`<div class="status" style="margin-top:.7rem">CLOSED · times in CST</div><div class="cards">${closed.map(t=>`<article class="action-card ${((t.profit_pct||0)>=0)?"long":"short"}">
            <div class="ac-top">
              <div class="ac-sym">${t.symbol} <span class="tag">${t.right==="P"?"PUT":"CALL"}</span></div>
              <div class="ac-dir ${((t.profit_pct||0)>=0)?"long":"short"}">EXIT</div>
            </div>
            <div class="ac-meta">
              <div>Entry (CST)<strong>${fmtCST(t.entered_at)}</strong></div>
              <div>Exit (CST)<strong>${fmtCST(t.exited_at||t.closed_at)}</strong></div>
              <div>Strike / expiry<strong>${t.strike==null?"—":fmt(t.strike,2)} · ${t.expiry||"—"}</strong></div>
              <div>In → out $<strong>$${fmt(t.entry_ask,2)} → $${fmt(t.exit_bid,2)}</strong></div>
              <div>P&amp;L % / $<strong class="${pctClass(t.profit_pct)}">${t.profit_pct==null?"—":fmt(t.profit_pct,1)+"%"} · ${t.pnl_usd==null?"—":"$"+fmt(t.pnl_usd,2)}</strong></div>
              <div>Held<strong>${t.hold_days==null?"—":fmt(t.hold_days,1)+"d"}</strong></div>
              <div>Cash before → after<strong>$${fmt(t.cash_before,0)} → $${fmt(t.cash_after,0)}</strong></div>
              <div>Balance after EXIT<strong class="up">$${fmt(t.equity_after!=null?t.equity_after:t.cash_after,0)}</strong></div>
              ${levelsMeta(t)}
            </div>
            <p class="why" style="margin:.45rem 0 0">${t.exit_reason||""}</p>
            ${t.balance_note?`<p class="why" style="margin:.25rem 0 0"><strong>Balance:</strong> ${t.balance_note}</p>`:""}
          </article>`).join("")}</div>`:""}`;
        bookEl.querySelectorAll("[data-ch-exit]").forEach(btn=>{
          btn.addEventListener("click", async ()=>{
            btn.textContent = "Exiting…";
            try {
              const r = await fetch("/api/challenge/exit", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({trade_id: btn.getAttribute("data-ch-exit")})});
              const j = await r.json();
              if (!r.ok) throw new Error(j.error||"exit failed");
              await loadAll();
            } catch(e){ btn.textContent = "EXIT failed"; alert(e.message||e); }
          });
        });
      }

      if (planEl) {
        const focus = ch.primary || (ch.tickets||[])[0];
        if (!focus) planEl.innerHTML = `<div class="empty">No ticket plan yet.</div>`;
        else {
          const mark = focus.ask??focus.option_last;
          planEl.innerHTML = `<div class="ac-meta">
              <div>Symbol / side<strong>${focus.symbol} ${focus.right==="P"?"PUT":"CALL"}</strong></div>
              <div>Status<strong>${focus.action||"—"}</strong></div>
              <div>Strike / expiry<strong>${focus.strike==null?"—":fmt(focus.strike,2)} · ${focus.expiry||"—"} (${focus.dte??"—"}d)</strong></div>
              <div>Opt mark → target<strong>${mark==null?"—":"$"+fmt(mark,2)} → ${focus.target_ask==null?"—":"$"+fmt(focus.target_ask,2)} (+${fmt(focus.target_profit_pct,0)}%)</strong></div>
              <div>Strike rate ≥1%/≥2%<strong>${focus.hit_1pct==null?"—":fmt(focus.hit_1pct,0)+"%"} / ${focus.hit_2pct==null?"—":fmt(focus.hit_2pct,0)+"%"}</strong></div>
              <div>Approx hold<strong>${holdLbl(focus)}</strong></div>
              ${levelsMeta(focus)}
            </div>
            <p class="why" style="margin:.55rem 0 0"><strong>ENTER:</strong> ${focus.enter_plan||"—"}</p>
            <p class="why" style="margin:.35rem 0 0"><strong>EXIT:</strong> ${focus.exit_plan||"—"}</p>
            ${(focus.action==="ENTRY" && focus.ask!=null && focus.contract)?`<div class="playbook" style="margin-top:.55rem"><button type="button" class="tag" id="chEnterPrimary">Paper ENTER this ticket</button></div>`:""}`;
          const ent = document.getElementById("chEnterPrimary");
          if (ent) ent.addEventListener("click", async ()=>{
            ent.textContent = "Entering…";
            try {
              const r = await fetch("/api/challenge/enter", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({symbol: focus.symbol, right: focus.right})});
              const j = await r.json();
              if (!r.ok) throw new Error(j.error||"enter failed");
              await loadAll();
            } catch(e){ ent.textContent = "ENTER failed"; alert(e.message||e); }
          });
        }
      }

      if (pathEl) {
        const paths = ch.paths || [];
        const hp = ch.hold_periods || {};
        const sched = pace.schedule || path.schedule || [];
        const balLog = book.balance_log || [];
        pathEl.innerHTML = `
          <p class="lede" style="margin-top:0"><strong>4-month → $500k pace:</strong> ${pace.note||"—"}</p>
          <p class="lede" style="margin-top:.35rem">${path.note||""}</p>
          <div class="playbook" style="margin-bottom:.55rem">
            ${["weekly","swing","leap"].map(k=>{
              const h=hp[k]||{};
              return `<span class="tag">${k}: ${h.label||"—"}</span>`;
            }).join("")}
            <span class="tag">Prefer weekly for 4mo/$500k</span>
          </div>
          <div class="status" style="margin:.2rem 0 .4rem">4mo compound schedule (weekly ~${pace.ideal_hold_days||8}d holds)</div>
          <table><thead><tr><th>Flip</th><th>Months</th><th>Equity</th><th>Milestone</th></tr></thead>
          <tbody>${(sched.slice(0,16)).map(s=>`<tr>
            <td class="mono">${s.flip}</td>
            <td class="mono">${s.months_elapsed==null?"—":fmt(s.months_elapsed,1)}</td>
            <td class="mono up"><strong>$${Number(s.equity||0).toLocaleString()}</strong></td>
            <td class="why">${s.hit_target?"$1M":(s.hit_milestone?"$500k":"—")}</td>
          </tr>`).join("")||`<tr><td colspan="4" class="empty">No pace schedule</td></tr>`}</tbody></table>
          <div class="status" style="margin:.75rem 0 .4rem">Classic path flip counts</div>
          <table><thead><tr><th>Flips</th><th>Need / flip</th><th>Multiple / flip</th></tr></thead>
          <tbody>${paths.map(p=>`<tr>
            <td class="mono">${p.flips}</td>
            <td class="mono up"><strong>+${fmt(p.pct_per_flip,0)}%</strong></td>
            <td class="mono">${fmt(p.mult_per_flip,2)}×</td>
          </tr>`).join("")}</tbody></table>
          <div class="status" style="margin:.75rem 0 .4rem">Balance after ENTRY / EXIT (CST)</div>
          ${balLog.length?`<table><thead><tr><th>When</th><th>Action</th><th>Sym</th><th>Cash before</th><th>Cash after</th><th>Equity after</th><th>P&amp;L</th></tr></thead>
            <tbody>${balLog.slice().reverse().slice(0,20).map(e=>`<tr>
              <td class="mono">${fmtCST(e.at)}</td>
              <td><span class="badge ${e.action==="EXIT"?"sell":"buy"}">${e.action}</span></td>
              <td><strong>${e.symbol||"—"}</strong></td>
              <td class="mono">$${fmt(e.cash_before,0)}</td>
              <td class="mono"><strong>$${fmt(e.cash_after,0)}</strong></td>
              <td class="mono up">$${fmt(e.equity_after,0)}</td>
              <td class="mono ${pctClass(e.pnl_usd)}">${e.pnl_usd==null?"—":"$"+fmt(e.pnl_usd,2)}</td>
            </tr>`).join("")}</tbody></table>`:`<div class="empty">No ENTRY/EXIT yet — balance updates after Paper ENTER / EXIT.</div>`}
          <ul class="lede" style="font-size:.76rem">${(ch.rules||[]).map(r=>`<li>${r}</li>`).join("")}</ul>`;
      }

      const tickets = ch.tickets || [];
      if (ticketsEl) {
        if (!tickets.length) ticketsEl.innerHTML = `<div class="empty">No tickets.</div>`;
        else ticketsEl.innerHTML = `<table><thead><tr>
          <th>Status</th><th>Side</th><th>Symbol</th><th>Strike</th><th>Call/Put wall</th><th>Soft EXIT</th><th>Vol / OI</th><th>Opt price</th><th>Hist</th><th>Strike rate</th><th>Hold</th><th>Reason</th>
        </tr></thead><tbody>${tickets.map(t=>{
          const a=t.action||"WAIT";
          const cls=a==="EXIT"?"sell":(a==="ENTRY"?"buy":(a==="HOLD"?"hold":"wait"));
          const mark = t.ask!=null?t.ask:t.option_last;
          const markLbl = t.mark_source==="last"?"last":(t.mark_source==="ask"?"ask":(mark!=null?"mark":"zone"));
          const vol=t.volume, oi=t.open_interest;
          const liqBad = (vol==null && oi==null) || (Number(vol||0)<=0 && Number(oi||0)<5000) || (Number(vol||0)<25 && Number(oi||0)<200);
          const hz = t.horizon || t.dte_bucket || t.hold_style || "swing";
          const w = (t.hit_1pct!=null) ? {hit1:t.hit_1pct, hit2:t.hit_2pct} : winLookup(t.symbol, hz);
          const sr = w.hit1==null ? "—" : `${fmt(w.hit1,0)}% ≥1%` + (w.hit2==null?"":` / ${fmt(w.hit2,0)}% ≥2%`);
          return `<tr>
          <td><span class="badge ${cls}">${a}</span></td>
          <td class="mono">${t.right==="P"?"PUT":"CALL"}</td>
          <td><strong>${t.symbol}</strong> ${spotBadge(t)} ${earnBadge(t)}${t.fits_4mo_500k?` <span class="badge buy">4MO $500k</span>`:""}${t.pace_style==="weekly"?` <span class="tag">weekly pace</span>`:""}<div class="why">spot ${t.spot==null?"—":"$"+fmt(t.spot,2)}</div></td>
          <td class="mono"><strong>${t.strike==null?"—":fmt(t.strike,2)}</strong><div class="why">${t.expiry||"—"} · ${t.dte==null?"":t.dte+"d"}</div></td>
          <td class="mono"><span class="up">${t.call_wall==null?"—":fmt(t.call_wall,2)}</span> / <span class="down">${t.put_wall==null?"—":fmt(t.put_wall,2)}</span></td>
          <td class="mono up"><strong>${t.soft_exit==null?"—":"$"+fmt(t.soft_exit,2)}</strong><div class="why">${t.wall_exit_hint||""}</div></td>
          <td class="mono ${liqBad?"down":"up"}"><strong>${vol==null?"—":Number(vol).toLocaleString()}</strong><div class="why">OI ${oi==null?"—":Number(oi).toLocaleString()}${liqBad?" · illiquid":""}</div></td>
          <td class="mono"><strong>${mark==null?"—":"$"+fmt(mark,2)}</strong><div class="why">${markLbl}${t.target_ask!=null?" → tgt $"+fmt(t.target_ask,2):""}</div></td>
          <td class="mono up"><strong>${fmt(t.hist_win_pct,0)}%</strong><div class="why">n=${t.hist_samples}</div></td>
          <td class="mono" title="Underlying ≥1% / ≥2% rip rate over ~1 month (leap) or swing window"><strong>${sr}</strong></td>
          <td class="mono"><strong>${holdLbl(t)}</strong></td>
          <td class="why">${t.recommend_reason||t.status_detail||""}
            <div style="margin-top:.2rem"><strong>ENTER:</strong> ${t.enter_plan||"—"}</div>
            <div><strong>EXIT:</strong> ${t.exit_plan||"—"}</div>
            ${(t.action==="ENTRY"&&t.ask!=null&&t.contract)?`<button type="button" class="tag" data-ch-enter="${t.symbol}|${t.right||"C"}">Paper ENTER</button>`:""}
          </td>
        </tr>`;
        }).join("")}</tbody></table>`;
        ticketsEl.querySelectorAll("[data-ch-enter]").forEach(btn=>{
          btn.addEventListener("click", async ()=>{
            const [sym, right] = (btn.getAttribute("data-ch-enter")||"").split("|");
            btn.textContent = "Entering…";
            try {
              const r = await fetch("/api/challenge/enter", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({symbol: sym, right})});
              const j = await r.json();
              if (!r.ok) throw new Error(j.error||"enter failed");
              await loadAll();
            } catch(e){ btn.textContent = "ENTER failed"; alert(e.message||e); }
          });
        });
      }
      if (disc) disc.textContent = ch.disclaimer || "";
      const rec = (DATA.rec_log && DATA.rec_log.by_section && DATA.rec_log.by_section.challenge) || (DATA.rec_log && DATA.rec_log.challenge);
      renderRecLog("challengeRecLog", rec, "No challenge recommendations logged yet.", "challengeRecLogMetrics");
    }

    function renderOdte1k(board) {
      const metrics = document.getElementById("odte1kMetrics");
      const primaryEl = document.getElementById("odte1kPrimary");
      const orbEl = document.getElementById("odte1kOrb");
      const actEl = document.getElementById("odte1kActions");
      const bookEl = document.getElementById("odte1kBook");
      const rulesEl = document.getElementById("odte1kRules");
      const disc = document.getElementById("odte1kDisclaimer");
      const b = board || {};
      if (!Object.keys(b).length) {
        if (actEl) actEl.innerHTML = `<div class="empty">0DTE $1K board loading…</div>`;
        return;
      }
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      const book = b.book || {};
      const c = b.counts || {};
      const equity = book.equity!=null ? book.equity : b.equity;
      const cash = book.cash!=null ? book.cash : b.cash;
      if (metrics) metrics.innerHTML = [
        m("Sleeve cash", cash!=null?`$${Number(cash).toLocaleString(undefined,{maximumFractionDigits:0})}`:"—"),
        m("Sleeve equity", equity!=null?`$${Number(equity).toLocaleString(undefined,{maximumFractionDigits:0})}`:"—", (b.doubled||book.doubled)?"up":""),
        m("→ 2× ($2k)", `${fmt(b.progress_2x_pct??book.progress_2x_pct,1)}%`, (b.doubled||book.doubled)?"up":""),
        m("Size / trade", b.position_size_usd!=null?`$${fmt(b.position_size_usd,0)}`:"—"),
        m("Trades today", `${b.trades_today??c.trades_today??0} / ${b.max_trades_per_day??2}`),
        m("PUT NOW", c.put_now??0, (c.put_now||0)>0?"up":""),
        m("Names", c.names??(b.symbols||[]).length??0),
        m("ORB ready", c.orb_ready??0),
        m("Green Friday", b.green_friday?"YES":"no", b.green_friday?"up":""),
        m("Call conflict", b.call_safe_zone_conflict?"YES":"no", b.call_safe_zone_conflict?"down":""),
      ].join("");

      const p0 = b.primary;
      if (primaryEl) {
        if (!p0) primaryEl.innerHTML = `<div class="empty">No ORB15 signal yet.</div>`;
        else {
          const act = p0.action||"WAIT";
          const kind = act==="EXIT"?"short":(act==="PUT_NOW"||act==="HOLD"?"long":"wait");
          primaryEl.innerHTML = `<article class="action-card ${kind}">
            <div class="ac-top">
              <div class="ac-sym">${p0.symbol} <span class="tag">PUT</span>
                ${p0.green_friday?`<span class="badge buy">GREEN FRIDAY</span>`:""}
                ${p0.call_safe_zone_conflict?`<span class="badge sell">CALL CONFLICT</span>`:""}
              </div>
              <div class="ac-dir ${kind}">${String(act).replaceAll("_"," ")}</div>
            </div>
            <div class="ac-conf">Strength ${fmt(p0.strength,0)} · ORB Low ${p0.orb_low==null?"—":"$"+fmt(p0.orb_low,2)} · High ${p0.orb_high==null?"—":"$"+fmt(p0.orb_high,2)}
              ${p0.signaled_at_cst?` · asked ${p0.signaled_at_cst}`:""}</div>
            <div class="ac-meta">
              <div>Spot<strong>${p0.spot==null?"—":"$"+fmt(p0.spot,2)}</strong></div>
              <div>Strike / expiry<strong>${p0.strike==null?"—":fmt(p0.strike,2)+"p"} · ${p0.expiry||"—"}</strong></div>
              <div>Ask / Bid<strong>${fmt(p0.ask,2)} / ${fmt(p0.bid,2)}</strong></div>
              <div>Size / cts<strong>${p0.position_size_usd==null?"—":"$"+fmt(p0.position_size_usd,0)} · ${p0.contracts??"—"}</strong></div>
              <div>Break / hold<strong>${p0.broke_orb_low?"YES":"—"} / ${p0.holds_below_low?"YES":"—"}</strong></div>
              <div>Retest<strong>${p0.retest_orb_low?"YES":"—"}</strong></div>
            </div>
            <p class="why" style="margin:.55rem 0 0">${p0.detail||""}</p>
            ${(act==="PUT_NOW" && p0.ask!=null)?`<div class="playbook" style="margin-top:.45rem"><button type="button" class="tag" id="odte1kEnter">Paper ENTER put</button></div>`:""}
          </article>`;
          const ent = document.getElementById("odte1kEnter");
          if (ent) ent.addEventListener("click", async ()=>{
            ent.textContent = "Entering…";
            try {
              if (window.SIGNAL_DESK_STATIC) { alert("Paper 0DTE $1K needs a live host"); return; }
              const r = await fetch("/api/odte1k/enter", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({symbol: p0.symbol})});
              const j = await r.json();
              if (!r.ok) throw new Error(j.error||"enter failed");
              await loadAll();
            } catch(e){ ent.textContent = "ENTER failed"; alert(e.message||e); }
          });
        }
      }

      if (orbEl) {
        const orb = b.orb || {};
        const rows = Object.keys(orb).map(sym => orb[sym]);
        orbEl.innerHTML = !rows.length ? `<div class="empty">No ORB15 levels.</div>` : `<table><thead><tr>
          <th>Symbol</th><th>Status</th><th>ORB High</th><th>ORB Low</th><th>Bars</th><th>Note</th>
        </tr></thead><tbody>${rows.slice(0,40).map(r=>{
          const st=r.status||"—";
          const cls=st==="ready"?"buy":(st==="proxy"?"wait":(st==="forming"?"wait":"skip"));
          return `<tr>
          <td><strong>${r.symbol}</strong></td>
          <td><span class="badge ${cls}">${st}</span></td>
          <td class="mono up"><strong>${r.high==null?"—":"$"+fmt(r.high,2)}</strong></td>
          <td class="mono down"><strong>${r.low==null?"—":"$"+fmt(r.low,2)}</strong></td>
          <td class="mono">${r.bars??"—"}</td>
          <td class="why">${r.note||""}</td>
        </tr>`;
        }).join("")}</tbody></table>${rows.length>40?`<p class="lede" style="font-size:.72rem">Showing 40 / ${rows.length} names — full list in PUT NOW / WATCH table.</p>`:""}`;
      }

      if (actEl) {
        const rows = [...(b.exit_now||[]), ...(b.put_now||[]), ...(b.hold||[]), ...(b.watch||[])];
        actEl.innerHTML = !rows.length ? `<div class="empty">No signals.</div>` : `<table><thead><tr>
          <th>Action</th><th>Symbol</th><th>Asked (CST)</th><th>ORB L/H</th><th>Spot</th><th>Strike</th><th>Ask</th><th>Why</th>
        </tr></thead><tbody>${rows.map(r=>{
          const a=(r.action||"WAIT");
          const cls=a==="EXIT"||a==="PUT_NOW"?(a==="EXIT"?"sell":"buy"):(a==="HOLD"?"hold":"wait");
          return `<tr>
            <td><span class="badge ${cls}">${String(a).replaceAll("_"," ")}</span>${r.call_safe_zone_conflict?` <span class="badge sell">CALL CONFLICT</span>`:""}</td>
            <td><strong>${r.symbol}</strong>${r.green_friday?` <span class="tag">GF</span>`:""}</td>
            <td class="mono">${r.signaled_at_cst||fmtCST(r.signaled_at)||"—"}</td>
            <td class="mono">${r.orb_low==null?"—":"$"+fmt(r.orb_low,2)} / ${r.orb_high==null?"—":"$"+fmt(r.orb_high,2)}</td>
            <td class="mono">${r.spot==null?"—":"$"+fmt(r.spot,2)}</td>
            <td class="mono">${r.strike==null?"—":fmt(r.strike,2)+"p"}</td>
            <td class="mono">${fmt(r.ask,2)}</td>
            <td class="why">${r.detail||""}</td>
          </tr>`;
        }).join("")}</tbody></table>`;
      }

      if (bookEl) {
        const open=(book.trades||[]).filter(t=>t.status==="open");
        const closed=(book.trades||[]).filter(t=>t.status==="closed").slice(-8).reverse();
        bookEl.innerHTML = `
          <div class="ac-meta" style="margin-bottom:.5rem">
            <div>Start<strong>$${Number(b.starting_cash||book.starting_cash||1000).toLocaleString()}</strong></div>
            <div>Cash<strong>$${Number(cash||0).toLocaleString(undefined,{maximumFractionDigits:0})}</strong></div>
            <div>Equity<strong>$${Number(equity||0).toLocaleString(undefined,{maximumFractionDigits:0})}</strong></div>
            <div>W/L<strong>${book.wins||0}/${book.losses||0}</strong></div>
            <div>Today<strong>${book.trades_today??b.trades_today??0} / ${b.max_trades_per_day||2}</strong></div>
          </div>
          ${open.length?open.map(t=>`<article class="action-card long" style="margin-bottom:.55rem">
            <div class="ac-top"><div class="ac-sym">${t.symbol} PUT</div><div class="ac-dir long">OPEN</div></div>
            <div class="ac-meta">
              <div>Entered (CST)<strong>${fmtCST(t.entered_at)}</strong></div>
              <div>Entry → mark<strong>$${fmt(t.entry_ask,2)} → $${fmt(t.mark,2)}</strong></div>
              <div>Unreal%<strong class="${pctClass(t.unrealized_pct)}">${t.unrealized_pct==null?"—":fmt(t.unrealized_pct,1)+"%"}</strong></div>
              <div>ORB Low<strong>${t.orb_low==null?"—":"$"+fmt(t.orb_low,2)}</strong></div>
              <div>Cost<strong>$${fmt(t.cost,0)}</strong></div>
            </div>
            <div class="playbook" style="margin-top:.4rem"><button type="button" class="tag" data-odte1k-exit="${t.id}">Paper EXIT</button></div>
          </article>`).join(""):`<div class="empty">No open 0DTE $1K flip — Paper ENTER on PUT NOW.</div>`}
          ${closed.length?`<div class="status" style="margin-top:.6rem">CLOSED</div><div class="cards">${closed.map(t=>`<article class="action-card ${((t.profit_pct||0)>=0)?"long":"short"}">
            <div class="ac-top"><div class="ac-sym">${t.symbol}</div><div class="ac-dir">EXIT</div></div>
            <div class="ac-meta">
              <div>P&amp;L<strong class="${pctClass(t.profit_pct)}">${t.profit_pct==null?"—":fmt(t.profit_pct,1)+"%"} · $${fmt(t.pnl_usd,2)}</strong></div>
              <div>In → out<strong>$${fmt(t.entry_ask,2)} → $${fmt(t.exit_bid,2)}</strong></div>
            </div>
          </article>`).join("")}</div>`:""}`;
        bookEl.querySelectorAll("[data-odte1k-exit]").forEach(btn=>{
          btn.addEventListener("click", async ()=>{
            btn.textContent = "Exiting…";
            try {
              if (window.SIGNAL_DESK_STATIC) { alert("Paper 0DTE $1K needs a live host"); return; }
              const r = await fetch("/api/odte1k/exit", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({trade_id: btn.getAttribute("data-odte1k-exit")})});
              const j = await r.json();
              if (!r.ok) throw new Error(j.error||"exit failed");
              await loadAll();
            } catch(e){ btn.textContent = "EXIT failed"; alert(e.message||e); }
          });
        });
      }

      if (rulesEl) {
        rulesEl.innerHTML = `<ul class="lede" style="font-size:.78rem">${(b.playbook||[]).map(r=>`<li>${r}</li>`).join("")}</ul>`;
      }
      if (disc) disc.textContent = b.disclaimer || "";
    }

    function renderPowerHour(board) {
      const metrics = document.getElementById("powerHourMetrics");
      const primaryEl = document.getElementById("powerHourPrimary");
      const specialEl = document.getElementById("powerHourSpecial");
      const longEl = document.getElementById("powerHourLong");
      const shortEl = document.getElementById("powerHourShort");
      const watchEl = document.getElementById("powerHourWatch");
      const rulesEl = document.getElementById("powerHourRules");
      const disc = document.getElementById("powerHourDisclaimer");
      const b = board || {};
      if (!Object.keys(b).length) {
        if (longEl) longEl.innerHTML = `<div class="empty">Power Hour board loading…</div>`;
        return;
      }
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      const c = b.counts || {};
      const phase = b.session_phase || "—";
      if (metrics) metrics.innerHTML = [
        m("Phase", String(phase).replace("_"," "), phase==="power_hour"?"up":""),
        m("LONG", c.long??0, (c.long||0)>0?"up":""),
        m("SHORT", c.short??0, (c.short||0)>0?"down":""),
        m("WATCH", c.watch??0),
        m("Names", c.names??0),
        m("QQQ ≥ VWAP", b.qqq_above_vwap===true?"YES":(b.qqq_above_vwap===false?"NO":"—"), b.qqq_above_vwap?"up":""),
      ].join("");

      const sideBadge = (a) => {
        const cls = a==="LONG"?"buy":(a==="SHORT"?"sell":"wait");
        return `<span class="badge ${cls}">${a||"WAIT"}</span>`;
      };
      const rowTable = (rows, emptyMsg) => {
        if (!rows || !rows.length) return `<div class="empty">${emptyMsg}</div>`;
        return `<table><thead><tr>
          <th>Side</th><th>Symbol</th><th>Last</th><th>VWAP</th><th>vs VWAP</th><th>15m%</th><th>Stop</th><th>Trigger</th><th>Risk line</th><th>Asked (CST)</th>
        </tr></thead><tbody>${rows.map(r=>`<tr>
          <td>${sideBadge(r.action)}</td>
          <td><strong>${r.symbol}</strong>${r.special?` <span class="tag">playbook</span>`:""}</td>
          <td class="mono">${r.last==null?"—":"$"+fmt(r.last,2)}</td>
          <td class="mono">${r.vwap==null?"—":"$"+fmt(r.vwap,2)}</td>
          <td class="mono ${pctClass(r.vs_vwap_pct)}">${r.vs_vwap_pct==null?"—":fmt(r.vs_vwap_pct,2)+"%"}</td>
          <td class="mono ${pctClass(r.mom_15m_pct)}">${r.mom_15m_pct==null?"—":fmt(r.mom_15m_pct,2)+"%"}</td>
          <td class="mono">${r.stop==null?"—":"$"+fmt(r.stop,2)}</td>
          <td class="why">${r.trigger||r.detail||""}</td>
          <td class="why">${r.risk_line||""}</td>
          <td class="mono">${r.signaled_at_cst||fmtCST(r.signaled_at)||"—"}</td>
        </tr>`).join("")}</tbody></table>`;
      };

      const p0 = b.primary;
      if (primaryEl) {
        if (!p0) primaryEl.innerHTML = `<div class="empty">No power-hour signal yet.</div>`;
        else {
          const kind = p0.action==="LONG"?"long":(p0.action==="SHORT"?"short":"wait");
          primaryEl.innerHTML = `<article class="action-card ${kind}">
            <div class="ac-top">
              <div class="ac-sym">${p0.symbol} ${sideBadge(p0.action)} ${p0.special?`<span class="tag">named playbook</span>`:""}</div>
              <div class="ac-dir ${kind}">${p0.action||"WAIT"} · ${String(p0.session_phase||"").replace("_"," ")}</div>
            </div>
            <div class="ac-conf">Strength ${fmt(p0.strength,0)} · last ${p0.last==null?"—":"$"+fmt(p0.last,2)} · VWAP ${p0.vwap==null?"—":"$"+fmt(p0.vwap,2)}
              ${p0.signaled_at_cst?` · asked ${p0.signaled_at_cst}`:""}</div>
            <p class="why" style="margin:.55rem 0 0"><strong>Trigger:</strong> ${p0.trigger||""}</p>
            <p class="why" style="margin:.35rem 0 0"><strong>Risk:</strong> ${p0.risk_line||""}</p>
            <p class="why" style="margin:.35rem 0 0">${p0.detail||""}</p>
          </article>`;
        }
      }

      if (specialEl) {
        const rules = b.special_rules || {};
        const specialRows = (b.special||[]).length ? (b.special||[]) : Object.keys(rules).map(sym => ({
          symbol: sym, action: "WAIT", trigger: rules[sym].trigger, risk_line: rules[sym].risk, special: true
        }));
        specialEl.innerHTML = rowTable(specialRows, "No named playbooks.");
      }
      if (longEl) longEl.innerHTML = rowTable(b.long||[], "No LONG setups right now.");
      if (shortEl) shortEl.innerHTML = rowTable(b.short||[], "No SHORT setups right now.");
      if (watchEl) watchEl.innerHTML = rowTable((b.watch||[]).slice(0,40), "Empty watch list.");
      if (rulesEl) rulesEl.innerHTML = `<ul class="lede" style="font-size:.78rem">${(b.playbook||[]).map(r=>`<li>${r}</li>`).join("")}</ul>`;
      if (disc) disc.textContent = b.disclaimer || "";
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

    let FLOW_FILTER = "all";
    const EARNINGS_FLOW_SYMS = new Set();

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
        if (flowEl) flowEl.innerHTML = `<div class="empty">Flow Desk not loaded — wait for snapshot refresh.</div>`;
        return;
      }
      // Refresh earnings symbol set from market/challenge for Earnings filter
      EARNINGS_FLOW_SYMS.clear();
      ((DATA.market && DATA.market.by_earnings) || []).forEach(r => { if (r.symbol) EARNINGS_FLOW_SYMS.add(r.symbol); });
      ((DATA.challenge && DATA.challenge.earnings_watch) || []).forEach(r => { if (r.symbol) EARNINGS_FLOW_SYMS.add(r.symbol); });

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
        cortexEl.innerHTML = `<strong style="color:var(--ink)">${cx.headline||"Flow briefing"}</strong><br/>`
          + (cx.summary || "")
          + ((cx.bullets||[]).length ? `<br/><span class="status">${cx.bullets.join(" · ")}</span>` : "");
      }

      const filterBar = document.getElementById("flowFilters");
      if (filterBar && !filterBar._wired) {
        filterBar._wired = true;
        filterBar.querySelectorAll("[data-flow]").forEach(btn => {
          btn.addEventListener("click", () => {
            FLOW_FILTER = btn.getAttribute("data-flow") || "all";
            filterBar.querySelectorAll("[data-flow]").forEach(b => b.classList.toggle("active", b === btn));
            renderEcho(DATA.echo || {});
          });
        });
      }

      const printsAll = (echo.option_flow && echo.option_flow.prints) || [];
      const prints = printsAll.filter(p => {
        const flags = p.flags || [];
        if (FLOW_FILTER === "golden") return p.tier === "golden";
        if (FLOW_FILTER === "unusual") return p.tier === "unusual" || p.tier === "golden";
        if (FLOW_FILTER === "calls") return p.right === "C";
        if (FLOW_FILTER === "puts") return p.right === "P";
        if (FLOW_FILTER === "vol_gt_oi") return flags.includes("vol_gt_oi");
        if (FLOW_FILTER === "earnings") return EARNINGS_FLOW_SYMS.has(p.symbol);
        return true;
      });
      if (flowEl) {
        if (!prints.length) {
          flowEl.innerHTML = `<div class="empty">No prints for filter “${FLOW_FILTER}” — try All / Unusual, or wait for chain volume.</div>`;
        } else {
          flowEl.innerHTML = `<table><thead><tr>
            <th>Tier</th><th>Sym</th><th>Side</th><th>Strike</th><th>DTE</th><th>Vol</th><th>OI</th><th>Vol/OI</th><th>Premium</th><th>Sent</th><th>Flags</th><th>Score</th>
          </tr></thead><tbody>${prints.slice(0,24).map(p=>{
            const flags = (p.flags||[]).filter(f=>f!==p.tier).slice(0,3).join(" · ") || "—";
            const earn = EARNINGS_FLOW_SYMS.has(p.symbol) ? ` <span class="tag">earn</span>` : "";
            return `<tr>
              <td><span class="badge ${p.tier||"aggressive"}">${(p.tier||"").toUpperCase()}</span></td>
              <td><strong>${p.symbol}</strong>${earn}</td>
              <td class="mono">${p.right}</td>
              <td class="mono">${fmt(p.strike,2)}</td>
              <td class="mono">${p.dte??"—"}</td>
              <td class="mono">${p.volume??"—"}</td>
              <td class="mono">${p.open_interest??"—"}</td>
              <td class="mono">${p.vol_oi_ratio==null?"—":fmt(p.vol_oi_ratio,2)}</td>
              <td class="mono">$${fmt((p.premium_notional||0)/1000,1)}k</td>
              <td><span class="badge ${p.sentiment==="bullish"?"buy":(p.sentiment==="bearish"?"sell":"wait")}">${p.sentiment||"—"}</span></td>
              <td class="why">${flags}</td>
              <td class="mono ${p.flow_score>=0?"up":"down"}">${fmt(p.flow_score,0)}</td>
            </tr>`;
          }).join("")}</tbody></table>
          <p class="lede" style="font-size:.72rem;margin:.4rem 0 0">${echo.option_flow.note||""} Showing ${Math.min(24, prints.length)} / ${prints.length} filtered · Yahoo snapshot ≠ OPRA tape.</p>`;
        }
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
        const closed = mir.closed || [];
        const perf = mir.performance || {};
        mirrorEl.innerHTML = `
          <div class="ac-meta" style="margin-bottom:.5rem">
            <div>Win rate<strong>${perf.win_rate_pct==null?"—":fmt(perf.win_rate_pct,1)+"%"}</strong></div>
            <div>Open<strong>${open.length}</strong></div>
            <div>Closed<strong>${closed.length}</strong></div>
            <div>Mode<strong>${mir.mode||"paper"}</strong></div>
          </div>
          ${open.length?`<table><thead><tr><th>Sym</th><th>Side</th><th>Entry</th><th>Mark</th><th>Unreal%</th></tr></thead>
          <tbody>${open.slice(0,8).map(t=>`<tr>
            <td><strong>${t.symbol}</strong></td>
            <td class="mono">${t.right||"C"}</td>
            <td class="mono">$${fmt(t.entry_ask,2)}</td>
            <td class="mono">$${fmt(t.mark,2)}</td>
            <td class="mono ${pctClass(t.unrealized_pct)}">${t.unrealized_pct==null?"—":fmt(t.unrealized_pct,1)+"%"}</td>
          </tr>`).join("")}</tbody></table>`:`<div class="empty">No open mirrored paper trades.</div>`}
          ${closed.length?`<div class="status" style="margin:.7rem 0 .35rem">CLOSED / EXIT</div>
          <table><thead><tr><th>Sym</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&amp;L%</th><th>Exit (CST)</th></tr></thead>
          <tbody>${closed.slice(0,8).map(t=>`<tr>
            <td><strong>${t.symbol}</strong></td>
            <td class="mono">${t.right||"C"}</td>
            <td class="mono">$${fmt(t.entry_ask,2)}</td>
            <td class="mono">$${fmt(t.exit_bid,2)}</td>
            <td class="mono ${pctClass(t.profit_pct)}">${t.profit_pct==null?"—":fmt(t.profit_pct,1)+"%"}</td>
            <td class="mono">${fmtCST(t.exited_at||t.closed_at)}</td>
          </tr>`).join("")}</tbody></table>`:""}
          <p class="lede" style="font-size:.72rem;margin:.4rem 0 0">${mir.note||""}</p>`;
      }
      if (disc) disc.textContent = echo.disclaimer || "";
    }

    let SCREENER_SORT = "earnings";
    function renderScreener(horizons, market) {
      const el = document.getElementById("screener");
      const metrics = document.getElementById("marketMetrics");
      const sortEl = document.getElementById("screenerSort");
      const mkt = market || DATA.market || {};
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      const c = mkt.counts || {};
      if (metrics) metrics.innerHTML = [
        m("Universe", mkt.universe_size||DATA.liquid_size||"—"),
        m("Earn cached", `${mkt.earnings_classified??"—"} / ${mkt.universe_size||"—"}`),
        m("Vol cached", mkt.volume_cached??"—"),
        m("Today / week / next", `${c.today||0} / ${c.this_week||0} / ${c.next_week||0}`),
        m("Post / soon", `${c.post||0} / ${c.soon||0}`),
      ].join("");
      if (sortEl && !sortEl.dataset.bound) {
        sortEl.dataset.bound = "1";
        sortEl.querySelectorAll("[data-sort]").forEach(btn=>{
          btn.addEventListener("click", ()=>{
            SCREENER_SORT = btn.getAttribute("data-sort")||"earnings";
            sortEl.querySelectorAll("[data-sort]").forEach(b=>b.classList.toggle("active", b===btn));
            renderScreener(DATA.horizons, DATA.market);
          });
        });
      }
      const earnBadge = (t) => {
        const w = t.earnings_window||t.window||"none";
        const b = t.bucket||"";
        let core = `<span class="badge skip">—</span>`;
        if (w==="post_earnings"||b==="post") core = `<span class="badge buy">POST</span>`;
        else if (w==="earnings_day"||b==="today") core = `<span class="badge sell">TODAY</span>`;
        else if (b==="this_week") core = `<span class="badge wait">THIS WK</span>`;
        else if (b==="next_week") core = `<span class="badge wait">NEXT WK</span>`;
        else if (w==="pre_earnings") core = `<span class="badge wait">PRE</span>`;
        else if (w==="earnings_soon"||b==="soon") core = `<span class="badge skip">SOON</span>`;
        const darling = t.darling ? ` <span class="tag">darling</span>` : "";
        const sess = t.earnings_session ? ` <span class="tag">${String(t.earnings_session).toUpperCase()}</span>` : "";
        return core + darling + sess;
      };
      let rows = [];
      if (SCREENER_SORT === "volume") rows = mkt.by_volume || [];
      else if (SCREENER_SORT === "score") rows = mkt.by_score || [];
      else rows = mkt.by_earnings || mkt.earnings_watch || [];
      // Fallback: old score merge if market board empty
      if (!rows.length) {
        const hz = horizons || {};
        const merge = {};
        ["0dte","weekly","swing"].forEach(h => {
          (hz[h]||[]).forEach(t => {
            if (!merge[t.symbol]) merge[t.symbol] = { symbol: t.symbol, last: t.last_price, swing_score: t.ensemble_score, quality: t.quality };
            if (h==="swing") { merge[t.symbol].swing_score = t.ensemble_score; merge[t.symbol].quality = t.quality; }
          });
        });
        rows = Object.values(merge).sort((a,b)=>(b.swing_score||0)-(a.swing_score||0));
      }
      if (!rows.length) {
        el.innerHTML = `<div class="empty">No screener data yet — wait for snapshot / run Scan liquid. ${mkt.note||""}</div>`;
        return;
      }
      el.innerHTML = `<p class="lede" style="margin-top:0;font-size:.72rem">${mkt.note||""} Soft EXIT = $0.10 before call/put OI wall when available.</p>
        <table><thead><tr>
          <th>Earn</th><th>Symbol</th><th>Last</th><th>Day vol</th><th>Rel vol</th><th>Call/Put wall</th><th>Soft EXIT</th><th>Swing</th><th>Bias</th>
        </tr></thead><tbody>${rows.slice(0,80).map(r=>{
          const walls = wallLookup(r.symbol);
          const callW = r.call_wall??walls.call_wall;
          const putW = r.put_wall??walls.put_wall;
          const soft = r.soft_exit??walls.soft_exit;
          const bias = r.strategy_bias||"—";
          const bcls = bias==="prefer_post"?"buy":(bias==="caution_pre"||bias==="avoid_short_premium"?"sell":"wait");
          return `<tr>
            <td>${earnBadge(r)}</td>
            <td><strong>${r.symbol}</strong>${r.in_focus?` <span class="tag">focus</span>`:""}${r.company_name?`<div class="why">${r.company_name}</div>`:""}</td>
            <td class="mono">${r.last==null?"—":fmt(r.last,2)}<div class="why ${pctClass(r.change_pct)}">${r.change_pct==null?"":fmt(r.change_pct,1)+"%"}</div></td>
            <td class="mono">${r.day_volume==null?"—":Number(r.day_volume).toLocaleString()}</td>
            <td class="mono ${Number(r.rel_volume||0)>=1.5?"up":""}">${r.rel_volume==null?"—":fmt(r.rel_volume,2)+"×"}</td>
            <td class="mono"><span class="up">${callW==null?"—":fmt(callW,2)}</span> / <span class="down">${putW==null?"—":fmt(putW,2)}</span></td>
            <td class="mono up"><strong>${soft==null?"—":"$"+fmt(soft,2)}</strong></td>
            <td class="mono ${r.quality?"up":""}">${r.swing_score==null?"—":fmt(r.swing_score,0)}</td>
            <td><span class="badge ${bcls}">${bias}</span><div class="why">${r.wall_exit_hint||walls.exit_hint||r.earnings_label||""}</div></td>
          </tr>`;
        }).join("")}</tbody></table>`;
    }

    function renderWebull(wb) {
      const metrics = document.getElementById("webullMetrics");
      const ordersEl = document.getElementById("webullOrders");
      const disc = document.getElementById("webullDisclaimer");
      const howEl = document.getElementById("webullHowTo");
      if (!metrics) return;
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      const st = wb.status || wb.broker || {};
      const act = wb.activity || {};
      const counts = act.counts || {};
      metrics.innerHTML = [
        m("Bridge", st.enabled ? (st.dry_run?"DRY-RUN":"LIVE") : (st.dry_run?"PREVIEW":"OFF"), st.enabled?(st.dry_run?"wait":"up"):"down"),
        m("Auto sync", wb.auto_sync?"on":"off", wb.auto_sync?"up":""),
        m("Ready live", st.ready_live?"yes":"no", st.ready_live?"up":""),
        m("SDK", st.sdk_available?"installed":"missing"),
        m("Keys", (st.has_app_key&&st.has_app_secret&&st.has_account_id)?"set":"env needed"),
        m("Perfect gate", wb.require_perfect_hist===false?"off":`${wb.min_hist_win_pct??100}% n≥${wb.min_hist_win_samples??3}`),
        m("BUY / SELL logged", `${counts.buys??0} / ${counts.sells??0}`),
        m("Last sync in", wb.submitted_n??0, "up"),
        m("Last sync skip", wb.skipped_n??0),
      ].join("");
      if (disc) disc.textContent = wb.disclaimer || st.disclaimer || "";
      if (howEl) {
        const steps = act.how_to_verify || wb.how_to_verify || [];
        howEl.innerHTML = steps.length
          ? `<strong>How to verify auto BUY/SELL:</strong><ol style="margin:.25rem 0 0;padding-left:1.1rem">${steps.map(s=>`<li>${String(s).replace(/^\\d+\\.\\s*/, "")}</li>`).join("")}</ol>`
          : "";
      }
      const rows = (act.orders || wb.recent || wb.orders || []);
      if (!rows.length) {
        ordersEl.innerHTML = `<div class="empty">No Webull BUY/SELL history yet. With auto_sync on, refresh the desk after a BUY NOW / SELL NOW pulse — or tap Sync. Paper ENTRY/EXIT is on the Journal cards above / Recommendation logger below.</div>`;
        return;
      }
      ordersEl.innerHTML = `<table><thead><tr>
        <th>When (CST)</th><th>Desk</th><th>Side</th><th>Symbol</th><th>Contract</th><th>Limit</th><th>Hist</th><th>Status</th><th>Broker id</th><th>Webull</th>
      </tr></thead><tbody>
      ${rows.slice(0,40).map(o=>`<tr>
        <td class="mono">${fmtCST(o.updated_at||o.created_at)}</td>
        <td class="tag">${o.desk||"—"}</td>
        <td class="mono"><strong>${o.action||"—"}</strong></td>
        <td><strong>${o.symbol}</strong> ${o.right||"C"}</td>
        <td class="mono">${o.contract||((o.strike!=null?o.strike:"")+" "+(o.expiry||""))}</td>
        <td class="mono">${o.limit_price==null?"—":"$"+fmt(o.limit_price,2)}</td>
        <td class="mono">${o.hist_win_pct==null?"—":fmt(o.hist_win_pct,0)+"%"}${(o.hist_samples!=null?` n=${o.hist_samples}`:"")}</td>
        <td><span class="badge ${o.status==="dry_run"||o.status==="submitted"?"buy":(o.status==="skipped"?"wait":"skip")}">${o.status||"—"}</span>
          <div class="why">${o.error||o.reason||(o.meta&&o.meta.note)||""}</div></td>
        <td class="mono" style="font-size:.72rem">${o.broker_order_id||"—"}</td>
        <td>${o.deep_link?`<a href="${o.deep_link}" target="_blank" rel="noopener">Open</a>`:"—"}</td>
      </tr>`).join("")}
      </tbody></table>`;
    }

    async function syncWebull() {
      const btn = document.getElementById("btnWebullSync");
      if (btn) { btn.disabled = true; btn.textContent = "Syncing…"; }
      try {
        const res = await fetch("/api/webull/sync", { method: "POST" });
        const body = await res.json();
        DATA.webull = body;
        renderWebull(body);
        const note = document.getElementById("loadNote");
        if (note) {
          note.style.display = "block";
          note.textContent = `Webull sync: ${body.submitted_n||0} staged/dry-run, ${body.skipped_n||0} skipped (100% gate)`;
        }
      } catch (e) {
        alert("Webull sync failed: " + (e.message||e));
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = "Sync → Webull (dry-run / live)"; }
      }
    }

    function renderInsights(ins) {
      const cards = document.getElementById("perfCards");
      const sum = document.getElementById("insightSummary");
      const journal = document.getElementById("journal");
      const balEl = document.getElementById("journalBalanceLog");
      if (!ins) {
        if (cards) cards.innerHTML="";
        if (sum) sum.textContent="";
        if (balEl) balEl.innerHTML = `<div class="empty">No ENTRY/EXIT yet.</div>`;
        return;
      }
      const p = ins.performance || {};
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      cards.innerHTML = [
        m("Journal win rate", p.win_rate_pct==null?"—":`${fmt(p.win_rate_pct,1)}%`),
        m("Avg profit%", p.avg_profit_pct==null?"—":`${fmt(p.avg_profit_pct,1)}%`, pctClass(p.avg_profit_pct)),
        m("Realized P&L", p.realized_pnl_usd==null?"—":`$${fmt(p.realized_pnl_usd,2)}`, pctClass(p.realized_pnl_usd)),
        m("Cash", p.cash==null?"—":`$${fmt(p.cash,0)}`),
        m("Equity", p.equity==null?"—":`$${fmt(p.equity,0)}`, pctClass(p.return_pct)),
        m("Account", p.return_pct==null?"—":`${fmt(p.return_pct,2)}%`, pctClass(p.return_pct)),
        m("Open / closed", `${p.open_trades||0} / ${p.closed_trades||0}`),
        m("W / L", `${p.wins||0} / ${p.losses||0}`),
      ].join("");
      sum.textContent = ins.summary || "";
      const open = ins.open_positions||[], closed = ins.closed_trades||[];
      let html="";
      if (open.length) {
        html += `<div class="status" style="margin:.3rem 0 .45rem">OPEN · times in CST</div>
          <div class="cards">${open.map(t=>`<article class="action-card wait">
            <div class="ac-top">
              <div class="ac-sym">${t.symbol} ${t.dte_bucket?`<span class="tag">${t.dte_bucket}</span>`:""}</div>
              <div class="ac-dir wait">OPEN</div>
            </div>
            <div class="ac-meta">
              <div>Entered (CST)<strong>${fmtCST(t.entered_at)}</strong></div>
              <div>Strike / expiry<strong>${t.strike==null?"—":fmt(t.strike,2)} · ${t.expiry||"—"}</strong></div>
              <div>Entry → mark<strong>$${fmt(t.entry_ask,2)} → $${fmt(t.mark,2)}</strong></div>
              <div>Unreal % / $<strong class="${pctClass(t.unrealized_pct)}">${t.unrealized_pct==null?"—":fmt(t.unrealized_pct,1)+"%"} · ${t.unrealized_pnl_usd==null?"—":"$"+fmt(t.unrealized_pnl_usd,2)}</strong></div>
              <div>Contracts / cost<strong>${t.contracts||1} · $${fmt(t.cost,0)}</strong></div>
              <div>Cash before → after<strong>$${fmt(t.cash_before,0)} → $${fmt(t.cash_after,0)}</strong></div>
              <div>Balance (equity)<strong class="up">$${fmt(t.equity_after!=null?t.equity_after:p.equity,0)}</strong></div>
              <div>Contract<strong class="mono" style="font-size:.72rem">${t.contract||"—"}</strong></div>
              ${levelsMeta(t)}
            </div>
            <p class="why" style="margin:.45rem 0 0">${t.entry_reason||""}</p>
            ${t.balance_note?`<p class="why" style="margin:.25rem 0 0"><strong>Balance:</strong> ${t.balance_note}</p>`:""}
          </article>`).join("")}</div>`;
      }
      if (closed.length) {
        html += `<div class="status" style="margin:.9rem 0 .45rem">CLOSED · EXIT / P&amp;L · times in CST</div>
          <div class="cards">${closed.map(t=>`<article class="action-card ${((t.profit_pct||0)>=0)?"long":"short"}">
            <div class="ac-top">
              <div class="ac-sym">${t.symbol} ${t.dte_bucket?`<span class="tag">${t.dte_bucket}</span>`:""}</div>
              <div class="ac-dir ${((t.profit_pct||0)>=0)?"long":"short"}">EXIT</div>
            </div>
            <div class="ac-meta">
              <div>Entry (CST)<strong>${fmtCST(t.entered_at)}</strong></div>
              <div>Exit (CST)<strong>${fmtCST(t.exited_at||t.closed_at)}</strong></div>
              <div>Strike / expiry<strong>${t.strike==null?"—":fmt(t.strike,2)} · ${t.expiry||"—"}</strong></div>
              <div>In → out $<strong>$${fmt(t.entry_ask,2)} → $${fmt(t.exit_bid,2)}</strong></div>
              <div>Profit % / P&amp;L<strong class="${pctClass(t.profit_pct)}">${fmt(t.profit_pct,1)}% · $${fmt(t.pnl_usd,2)}</strong></div>
              <div>Hold<strong>${fmt(t.hold_minutes,0)}m</strong></div>
              <div>Cash before → after<strong>$${fmt(t.cash_before,0)} → $${fmt(t.cash_after,0)}</strong></div>
              <div>Balance after EXIT<strong class="up">$${fmt(t.equity_after!=null?t.equity_after:t.cash_after,0)}</strong></div>
              <div>Contract<strong class="mono" style="font-size:.72rem">${t.contract||"—"}</strong></div>
              ${levelsMeta(t)}
            </div>
            <p class="why" style="margin:.45rem 0 0">${t.exit_reason||""}</p>
            ${t.balance_note?`<p class="why" style="margin:.25rem 0 0"><strong>Balance:</strong> ${t.balance_note}</p>`:""}
          </article>`).join("")}</div>`;
      }
      journal.innerHTML = html || `<div class="empty">No journal trades yet.</div>`;

      if (balEl) {
        const balLog = ins.balance_log || [];
        balEl.innerHTML = balLog.length
          ? `<table><thead><tr><th>When</th><th>Action</th><th>Sym</th><th>Bucket</th><th>Cash before</th><th>Cash after</th><th>Equity after</th><th>P&amp;L</th></tr></thead>
            <tbody>${balLog.slice().reverse().slice(0,24).map(e=>`<tr>
              <td class="mono">${fmtCST(e.at)}</td>
              <td><span class="badge ${e.action==="EXIT"?"sell":"buy"}">${e.action}</span></td>
              <td><strong>${e.symbol||"—"}</strong></td>
              <td class="mono">${e.dte_bucket||"—"}</td>
              <td class="mono">$${fmt(e.cash_before,0)}</td>
              <td class="mono"><strong>$${fmt(e.cash_after,0)}</strong></td>
              <td class="mono up">$${fmt(e.equity_after,0)}</td>
              <td class="mono ${pctClass(e.pnl_usd)}">${e.pnl_usd==null?"—":"$"+fmt(e.pnl_usd,2)}${e.profit_pct==null?"":" · "+fmt(e.profit_pct,1)+"%"}</td>
            </tr>`).join("")}</tbody></table>`
          : `<div class="empty">No ENTRY/EXIT yet — balance updates after BUY NOW / SELL NOW fills.</div>`;
      }

      const odteJ = document.getElementById("odteJournal");
      if (odteJ) {
        const is0 = (t) => {
          const b = String(t.dte_bucket||"").toLowerCase();
          return !b || b === "0dte" || b === "0" || b === "1dte";
        };
        const oOpen = open.filter(is0);
        const oClosed = closed.filter(is0);
        let ohtml = "";
        if (oOpen.length) {
          ohtml += `<div class="status" style="margin:.2rem 0 .4rem">OPEN 0DTE</div><div class="cards">${oOpen.map(t=>`<article class="action-card wait">
            <div class="ac-top"><div class="ac-sym">${t.symbol}</div><div class="ac-dir wait">ENTRY</div></div>
            <div class="ac-meta">
              <div>Entered (CST)<strong>${fmtCST(t.entered_at)}</strong></div>
              <div>Strike / expiry<strong>${t.strike==null?"—":fmt(t.strike,2)} · ${t.expiry||"—"}</strong></div>
              <div>Entry → mark<strong>$${fmt(t.entry_ask,2)} → $${fmt(t.mark,2)}</strong></div>
              <div>Unreal % / $<strong class="${pctClass(t.unrealized_pct)}">${t.unrealized_pct==null?"—":fmt(t.unrealized_pct,1)+"%"} · ${t.unrealized_pnl_usd==null?"—":"$"+fmt(t.unrealized_pnl_usd,2)}</strong></div>
              <div>Cash before → after<strong>$${fmt(t.cash_before,0)} → $${fmt(t.cash_after,0)}</strong></div>
              <div>Balance (equity)<strong class="up">$${fmt(t.equity_after!=null?t.equity_after:p.equity,0)}</strong></div>
              ${levelsMeta(t)}
            </div>
            ${t.balance_note?`<p class="why" style="margin:.35rem 0 0"><strong>Balance:</strong> ${t.balance_note}</p>`:""}
          </article>`).join("")}</div>`;
        }
        if (oClosed.length) {
          ohtml += `<div class="status" style="margin:.75rem 0 .4rem">CLOSED 0DTE · P&amp;L</div><div class="cards">${oClosed.map(t=>`<article class="action-card ${((t.profit_pct||0)>=0)?"long":"short"}">
            <div class="ac-top"><div class="ac-sym">${t.symbol}</div><div class="ac-dir ${((t.profit_pct||0)>=0)?"long":"short"}">EXIT</div></div>
            <div class="ac-meta">
              <div>Entry (CST)<strong>${fmtCST(t.entered_at)}</strong></div>
              <div>Exit (CST)<strong>${fmtCST(t.exited_at||t.closed_at)}</strong></div>
              <div>In → out $<strong>$${fmt(t.entry_ask,2)} → $${fmt(t.exit_bid,2)}</strong></div>
              <div>Profit % / P&amp;L<strong class="${pctClass(t.profit_pct)}">${fmt(t.profit_pct,1)}% · $${fmt(t.pnl_usd,2)}</strong></div>
              <div>Cash before → after<strong>$${fmt(t.cash_before,0)} → $${fmt(t.cash_after,0)}</strong></div>
              <div>Balance after EXIT<strong class="up">$${fmt(t.equity_after!=null?t.equity_after:t.cash_after,0)}</strong></div>
              ${levelsMeta(t)}
            </div>
            ${t.balance_note?`<p class="why" style="margin:.35rem 0 0"><strong>Balance:</strong> ${t.balance_note}</p>`:""}
          </article>`).join("")}</div>`;
        }
        odteJ.innerHTML = ohtml || `<div class="empty">No 0DTE journal fills yet — appear after BUY NOW / SELL NOW.</div>`;
      }
    }

    function renderMustTradeBanner() {
      const el = document.getElementById("mustTradeBanner");
      if (!el) return;
      const acts = DATA.actions || {};
      const lot = DATA.lottery || {};
      const ch = DATA.challenge || {};
      const must = [];
      const exits = [];
      const seenMust = new Set();
      const seenExit = new Set();

      const pushMust = (row, desk) => {
        if (!row) return;
        const sym = String(row.symbol || "").toUpperCase();
        if (!sym) return;
        const strike = row.strike;
        const expiry = row.expiry || "—";
        const key = `${sym}|${strike}|${expiry}|${row.contract || ""}`;
        if (seenMust.has(key)) return;
        seenMust.add(key);
        const win = row.win_pct ?? row.hist_win_pct;
        const n = row.win_samples ?? row.hist_samples;
        const hit1 = row.hit_1pct;
        must.push({
          desk,
          symbol: sym,
          strike,
          expiry,
          dte: row.dte,
          ask: row.ask,
          contract: row.contract,
          win_pct: win,
          win_samples: n,
          hit_1pct: hit1,
          hit_2pct: row.hit_2pct,
          certainty: row.certainty_tier,
          score: row.score ?? row.ensemble_score ?? row.lottery_score ?? row.strength,
          detail: row.headline || row.detail || row.recommend_reason || row.thesis || "",
          exit_plan: row.exit_plan || "",
          right: row.right || "C",
          spot: row.spot ?? row.live_last ?? row.live_spot ?? row.entry_spot ?? row.last_price,
          call_wall: row.call_wall,
          put_wall: row.put_wall,
          soft_exit: row.soft_exit,
          wall_buffer_usd: row.wall_buffer_usd,
          signaled_at: row.signaled_at,
          signaled_at_cst: row.signaled_at_cst,
          recommended_at: row.recommended_at || row.signaled_at,
          dte_bucket: row.dte_bucket || row.horizon,
          enter_plan: row.enter_plan || row.headline,
        });
      };
      const pushExit = (row, desk) => {
        if (!row) return;
        const sym = String(row.symbol || "").toUpperCase();
        if (!sym) return;
        const strike = row.strike;
        const expiry = row.expiry || "—";
        const key = `${sym}|${strike}|${expiry}|${row.contract || row.id || ""}`;
        if (seenExit.has(key)) return;
        seenExit.add(key);
        exits.push({
          desk,
          symbol: sym,
          strike,
          expiry,
          dte: row.dte,
          ask: row.bid ?? row.mark ?? row.ask ?? row.exit_bid,
          bid: row.bid ?? row.exit_bid ?? row.mark,
          entry_ask: row.entry_ask ?? row.entry,
          contract: row.contract,
          win_pct: row.win_pct ?? row.hist_win_pct,
          win_samples: row.win_samples ?? row.hist_samples,
          hit_1pct: row.hit_1pct,
          profit_pct: row.profit_pct ?? row.option_unrealized_pct ?? row.unrealized_pct,
          pnl_usd: row.pnl_usd ?? row.unrealized_pnl_usd,
          exited_at: row.exited_at || row.closed_at,
          entered_at: row.entered_at,
          detail: row.exit_plan || row.wall_exit_hint || row.detail || row.headline || row.last_action_detail || "Exit now",
          right: row.right || "C",
          spot: row.spot ?? row.live_last ?? row.live_spot ?? row.entry_spot ?? row.last_price,
          call_wall: row.call_wall,
          put_wall: row.put_wall,
          soft_exit: row.soft_exit,
          wall_buffer_usd: row.wall_buffer_usd,
        });
      };

      (acts.buy_now || []).filter(r => r.headline || r.exit_plan || r.detail).forEach(r => pushMust(r, "Options"));
      (lot.buy_now || []).filter(r => (Number(r.win_pct ?? r.hist_win_pct) >= 80) && (Number(r.win_samples ?? r.hist_samples) >= 3)).forEach(r => pushMust(r, "Explosive"));
      (ch.entry || []).forEach(r => pushMust(r, "Challenge"));
      must.sort((a, b) => {
        const tier = (t) => t === "perfect" ? 0 : t === "elite" ? 1 : 2;
        const tw = (Number(b.win_pct) || 0) - (Number(a.win_pct) || 0);
        if (tier(a.certainty) !== tier(b.certainty)) return tier(a.certainty) - tier(b.certainty);
        if (tw) return tw;
        return (Number(b.hit_1pct) || 0) - (Number(a.hit_1pct) || 0);
      });

      (acts.sell_now || []).forEach(r => pushExit(r, "Options"));
      (acts.just_exited || []).forEach(r => pushExit(r, "Options"));
      (acts.recent_exits || []).forEach(r => pushExit(r, "Options"));
      (lot.sell_now || []).forEach(r => pushExit(r, "Explosive"));
      (ch.exit || []).forEach(r => pushExit(r, "Challenge"));
      (ch.just_exited || []).forEach(r => pushExit(r, "Challenge"));

      const topMust = must.slice(0, 4);
      const topExit = exits.slice(0, 4);
      el.classList.add("active");
      if (!topMust.length && !topExit.length) {
        el.innerHTML = `
          <div class="pb-head">
            <div>
              <h2 class="pb-title">Must trade <em>sure-shot</em> + exits</h2>
              <div class="pb-sub">No hist-gated BUY NOW and no live EXIT this snapshot. Radar HOT wings (cheap SPY/DIA) are <strong>not</strong> sure-shots and are not shown here — open the Radar tab. Empty ENTER/EXIT means nothing was paper-filled.</div>
            </div>
            <div class="status">MUST 0 · EXIT 0</div>
          </div>
          <div class="pulse-empty">Waiting on hist-win ≥80% BUY NOW, or an open ticket that hits TP / SL / 15:45 ET.</div>`;
        return;
      }

      const card = (row, kind) => {
        const tag = kind === "must" ? "ENTER NOW" : "EXIT NOW";
        const tk = ticketLines(row.symbol, row);
        const strikeTxt = row.strike == null ? "—" : `${Number(row.strike).toFixed(Number(row.strike) % 1 ? 2 : 0)}${(row.right || "C") === "P" ? "p" : "c"}`;
        const dteTxt = row.dte == null ? "" : ` · ${row.dte}DTE`;
        const winTxt = row.win_pct == null ? "—" : `${fmt(row.win_pct, 0)}%`;
        const nTxt = row.win_samples == null ? "" : ` n=${row.win_samples}`;
        const srTxt = row.hit_1pct == null ? "—" : `${fmt(row.hit_1pct, 0)}%`;
        const enterTxt = tk.entry == null ? "—" : `$${fmt(tk.entry, 2)}`;
        const exitTxt = kind === "exit"
          ? (row.ask == null && tk.exitPx == null ? "—" : `$${fmt(row.ask ?? tk.exitPx, 2)}`)
          : (tk.exitPx == null ? "—" : `$${fmt(tk.exitPx, 2)}`);
        const pnlTxt = (tk.pct == null && tk.pnl == null && row.profit_pct == null && row.pnl_usd == null)
          ? "unfilled"
          : `${fmt(tk.pct ?? row.profit_pct, 1)}%${(tk.pnl ?? row.pnl_usd)==null?"":" · $"+fmt(tk.pnl ?? row.pnl_usd, 2)}`;
        return `<div class="pulse-card ${kind}">
          <div class="pc-top">
            <div class="pc-sym">${row.symbol}</div>
            <div class="pc-tag ${kind}">${tag} · ${row.desk}</div>
          </div>
          <div class="pc-meta">
            <div><span>Strike</span><br><strong>${strikeTxt}</strong></div>
            <div><span>Expiry</span><br><strong>${row.expiry || "—"}${dteTxt}</strong></div>
            <div><span>Hist win</span><br><strong class="up">${winTxt}</strong><span>${nTxt}</span></div>
            <div><span>Strike rate ≥1%</span><br><strong>${srTxt}</strong></div>
            <div><span>ENTER</span><br><strong>${enterTxt}</strong></div>
            <div><span>ENTER time (CST)</span><br><strong>${row.signaled_at_cst || fmtCST(row.signaled_at || row.recommended_at)}</strong></div>
            <div><span>EXIT</span><br><strong>${exitTxt}</strong></div>
            <div><span>P&amp;L (1ct)</span><br><strong class="${pnlTxt==="unfilled"?"":pctClass(tk.pct ?? row.profit_pct ?? tk.pnl)}">${pnlTxt}</strong></div>
            <div><span>Contract</span><br><strong class="mono" style="font-size:.72rem">${row.contract || "—"}</strong></div>
            ${levelsMeta(row)}
          </div>
          <p class="pc-why"><strong>ENTER:</strong> ${tk.planIn}</p>
          <p class="pc-why"><strong>EXIT:</strong> ${row.exit_plan || tk.planOut}</p>
        </div>`;
      };

      const grid = [
        ...topMust.map(r => card(r, "must")),
        ...topExit.map(r => card(r, "exit")),
      ].join("");

      el.innerHTML = `
        <div class="pb-head">
          <div>
            <h2 class="pb-title">Must trade <em>sure-shot</em> + exits</h2>
            <div class="pb-sub">Only hist-gated BUY NOW and live EXIT tickets. Each card shows ENTER, EXIT, and P&amp;L. Radar HOT is not a sure-shot. Hist edge ≠ guaranteed future profit.</div>
          </div>
          <div class="status">MUST ${topMust.length} · EXIT ${topExit.length}</div>
        </div>
        <div class="pb-grid">${grid}</div>`;
    }

    function renderZeroLoss(zl) {
      const metrics = document.getElementById("zlMetrics");
      const dnm = document.getElementById("zlDoNotMiss");
      const tape = document.getElementById("zlTape");
      const flow = document.getElementById("zlFlow");
      const note = document.getElementById("zlNote");
      const disc = document.getElementById("zlDisclaimer");
      if (!zl || !Object.keys(zl).length) {
        if (dnm) dnm.innerHTML = `<div class="empty">No ZeroLoss board yet — tap Scan focus or Scan catalyst.</div>`;
        return;
      }
      const m = (k,v,cls="") => `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
      const c = zl.counts || {};
      if (metrics) metrics.innerHTML = [
        m("Scanned", c.scanned||0),
        m("Do not miss", c.do_not_miss||0, (c.do_not_miss||0)>0?"up":""),
        m("Catalyst", c.catalyst||0),
        m("Tape", c.tape||0),
        m("Flow prints", (zl.flow_prints||[]).length),
      ].join("");
      if (note) note.textContent = zl.purpose ? (zl.purpose + " " + (zl.mrna_note||"")) : (zl.mrna_note||"");
      if (disc) disc.textContent = zl.disclaimer || "";

      const rowCard = (r) => {
        const ch = r.live_change_pct != null ? r.live_change_pct : r.day_change_pct;
        const cls = (ch||0) >= 0 ? "up" : "down";
        const lane = String(r.lane||"").replaceAll("_"," ");
        return `<div class="action-card wait">
          <div class="ac-top">
            <div class="ac-sym">${r.symbol}</div>
            <div class="ac-dir ${cls}"><span class="badge dnm">WATCH · not ENTER</span></div>
          </div>
          <div class="ac-conf">Miss score ${Math.round(Number(r.miss_score||0)*100)}</div>
          <div class="bar"><i style="width:${Math.min(100, Number(r.miss_score||0)*100)}%"></i></div>
          <div class="ac-meta">
            <div>Gap<strong class="${pctClass(r.gap_pct)}">${r.gap_pct==null?"—":fmt(r.gap_pct,1)+"%"}</strong></div>
            <div>Day<strong class="${pctClass(ch)}">${ch==null?"—":fmt(ch,1)+"%"}</strong></div>
            <div>Rel vol<strong>${r.rel_volume==null?"—":fmt(r.rel_volume,1)+"x"}</strong></div>
            <div>Last<strong class="mono">${r.live_last||r.last||"—"}</strong></div>
          </div>
          <p class="pc-why">${r.why||""}</p>
          <p class="pc-why">${r.risk||""}</p>
        </div>`;
      };

      const pinEl = document.getElementById("zlPinned");
      const pinRows = zl.pinned || [];
      if (pinEl) {
        if (!pinRows.length) pinEl.innerHTML = `<div class="empty">Pinned names not in this snapshot.</div>`;
        else pinEl.innerHTML = `<table class="zl-tape"><thead><tr>
          <th>Sym</th><th>Lane</th><th>Gap</th><th>Day</th><th>Vol</th><th>Why</th>
        </tr></thead><tbody>${pinRows.map(r => `<tr>
          <td><strong>${r.symbol}</strong></td>
          <td><span class="badge ${r.lane==="DO_NOT_MISS"?"dnm":(r.lane==="CATALYST"?"unusual":"wait")}">${r.lane}</span></td>
          <td class="mono ${pctClass(r.gap_pct)}">${r.gap_pct==null?"—":fmt(r.gap_pct,1)+"%"}</td>
          <td class="mono ${pctClass(r.day_change_pct)}">${r.day_change_pct==null?"—":fmt(r.day_change_pct,1)+"%"}</td>
          <td class="mono">${r.rel_volume==null?"—":fmt(r.rel_volume,1)+"x"}</td>
          <td class="why">${r.why||""}</td>
        </tr>`).join("")}</tbody></table>`;
      }

      const dnmRows = zl.do_not_miss || [];
      if (dnm) dnm.innerHTML = dnmRows.length
        ? dnmRows.map(rowCard).join("")
        : `<div class="empty">No gap/volume bombs this session. That is normal — the desk is for misses, not a daily buy list.</div>`;

      const tapeRows = [...(zl.catalyst||[]), ...(zl.tape||[])].slice(0, 16);
      if (tape) {
        if (!tapeRows.length) tape.innerHTML = `<div class="empty">Quiet tape in the catalyst sleeve.</div>`;
        else tape.innerHTML = `<table class="zl-tape"><thead><tr>
          <th>Sym</th><th>Lane</th><th>Gap</th><th>Day</th><th>Vol</th><th>Why</th>
        </tr></thead><tbody>${tapeRows.map(r => `<tr>
          <td><strong>${r.symbol}</strong></td>
          <td><span class="badge ${r.lane==="CATALYST"?"unusual":"wait"}">${r.lane}</span></td>
          <td class="mono ${pctClass(r.gap_pct)}">${r.gap_pct==null?"—":fmt(r.gap_pct,1)+"%"}</td>
          <td class="mono ${pctClass(r.day_change_pct)}">${r.day_change_pct==null?"—":fmt(r.day_change_pct,1)+"%"}</td>
          <td class="mono">${r.rel_volume==null?"—":fmt(r.rel_volume,1)+"x"}</td>
          <td class="why">${r.why||""}</td>
        </tr>`).join("")}</tbody></table>`;
      }

      const prints = zl.flow_prints || [];
      if (flow) {
        if (!prints.length) flow.innerHTML = `<div class="empty">No unusual options prints in this snapshot (Yahoo chain, not OPRA). Open Flow Desk for filters.</div>`;
        else flow.innerHTML = `<table class="zl-tape"><thead><tr>
          <th>Tier</th><th>Sym</th><th>Side</th><th>Strike</th><th>Prem</th><th>Score</th>
        </tr></thead><tbody>${prints.slice(0,20).map(p => `<tr>
          <td><span class="badge ${p.tier||"aggressive"}">${(p.tier||"").toUpperCase()}</span></td>
          <td><strong>${p.symbol}</strong></td>
          <td class="mono ${p.right==="C"?"up":"down"}">${p.right}</td>
          <td class="mono">${fmt(p.strike,2)}</td>
          <td class="mono">${p.premium_notional!=null?Math.round(p.premium_notional).toLocaleString():"—"}</td>
          <td class="mono">${fmt(p.flow_score,1)}</td>
        </tr>`).join("")}</tbody></table>`;
      }

      const rec = DATA.rec_log || {};
      const tixEl = document.getElementById("zlTickets");
      if (tixEl) {
        renderRecLog("zlTickets", rec, "No paper tickets yet — ENTER/EXIT/P&L appear after a hist-gated BUY NOW fill, not from Radar HOT.", "zlTicketMetrics");
      }
    }

    function paint() {
      renderMustTradeBanner();
      maybeFireTradeAlerts();
      renderZeroLoss(DATA.zeroloss || {});
      renderNowBoard();
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
      const hr = acts.hold_rules || {};
      const exitCrit = document.getElementById("exitCriteriaNote");
      if (exitCrit) {
        const bits = hr.exit_criteria || [];
        exitCrit.innerHTML = bits.length
          ? `<strong>EXIT rules:</strong> ${bits.join(" · ")} <span class="status">${hr.note||""}</span>`
          : (hr.note || "");
      }
      renderExplosive(DATA.explosive || [], DATA.lottery || {});
      renderRedFlag(DATA.red_flag);
      renderFreeDealer(DATA.free_dealer);
      renderMl6(DATA.ml6 || { watchlist: ((DATA.horizons||{}).ml6||[]), bottom_line_rules: (DATA.ml6&&DATA.ml6.bottom_line_rules)||[] });
      renderRadar(DATA.radar || {});
      renderChaseRadar(DATA.chase_radar || DATA.convex_risk || {});
      renderEcho(DATA.echo || {});
      renderDarkpoolMini(DATA.echo || {});
      renderChallenge(DATA.challenge || {});
      renderOdte1k(DATA.odte_1k || {});
      renderPowerHour(DATA.power_hour || {});
      renderScreener(hz, DATA.market || {});
      renderInsights(DATA.insights);
      renderRecLogAll(DATA.rec_log || {});
      renderWebull(DATA.webull || {});
      document.getElementById("session").textContent = (DATA.session||"—") + " · " + (DATA.universe_mode||"focus");
      const uniPill = document.getElementById("universePill");
      if (uniPill) {
        uniPill.textContent = `Scanning ${DATA.universe_size??"—"} tickers` +
          (DATA.focus_size!=null ? ` · focus ${DATA.focus_size}` : "") +
          (DATA.liquid_size!=null ? ` · liquid ${DATA.liquid_size}` : "");
      }
      document.getElementById("updated").textContent = "Updated " + fmtCST(DATA.generated_at, true);
      const c = acts.counts || {};
      const lc = (DATA.lottery && DATA.lottery.counts) || {};
      const rc = (DATA.radar && DATA.radar.counts) || {};
      document.getElementById("counts").textContent =
        `BUY ${c.buy_now||0} · SELL ${c.sell_now||0} · WAIT ${c.wait||0} · LOTTO B/S ${lc.buy_now||0}/${lc.sell_now||0} · ML6 B/S ${(DATA.ml6&&DATA.ml6.actions&&DATA.ml6.actions.counts&&DATA.ml6.actions.counts.buy_now)||0}/${(DATA.ml6&&DATA.ml6.actions&&DATA.ml6.actions.counts&&DATA.ml6.actions.counts.sell_now)||0} · RADAR HOT ${rc.hot||0}`;
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
        // Snapshot can take 1–3 min (Yahoo quotes + earnings warm); don't abort early
        const t = setTimeout(() => ctrl.abort(), 180000);
        const res = await fetch("/api/snapshot", { signal: ctrl.signal });
        clearTimeout(t);
        if (!res.ok) throw new Error("HTTP " + res.status);
        DATA = await res.json();
        paint();
        const n = (DATA.scores||[]).length;
        const focus = DATA.focus_size ?? 0;
        if (!n && !focus) {
          note.style.display = "block";
          note.textContent = "No scan yet — tap Scan focus (or Scan liquid) to load data.";
        } else {
          note.style.display = "none";
        }
      } catch (e) {
        note.textContent = "Load failed: " + (e.message||e);
      }
    }

    async function runScan(mode) {
      const btn = mode==="liquid" ? document.getElementById("btnScanWide")
        : (mode==="ml6" ? document.getElementById("btnScanMl6")
        : (mode==="catalyst" || mode==="zeroloss" ? document.getElementById("btnScanCatalyst") : document.getElementById("btnScan")));
      if (!btn) return;
      btn.disabled = true;
      const label = btn.textContent;
      btn.textContent = "Scanning…";
      const note = document.getElementById("loadNote");
      note.style.display = "block";
      try {
        const start = await fetch("/api/scan?mode=" + encodeURIComponent(mode||"focus"), { method: "POST" });
        const body = await start.json().catch(() => ({}));
        if (!start.ok) {
          note.textContent = body.error || ("Scan failed HTTP " + start.status);
          return;
        }
        // Poll until latest scan appears / scan lock frees (liquid can take several minutes)
        const maxWaitMs = mode==="liquid" ? 12*60*1000 : (mode==="ml6" ? 4*60*1000 : 6*60*1000);
        const startedAt = Date.now();
        let ready = false;
        while (Date.now() - startedAt < maxWaitMs) {
          const elapsed = Math.round((Date.now() - startedAt)/1000);
          note.textContent = `Scanning ${mode||"focus"}… ${elapsed}s (Yahoo can be slow)`;
          await new Promise(r => setTimeout(r, 5000));
          try {
            const st = await fetch("/api/scan_status");
            const info = await st.json();
            if (info && info.ready) { ready = true; break; }
            if (info && info.running === false && info.has_scan) { ready = true; break; }
          } catch (_) { /* keep waiting */ }
        }
        if (!ready) note.textContent = "Scan still running — tap Reload in a minute.";
        await loadAll();
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    }

    document.getElementById("btnRefresh").onclick = loadAll;
    (function themeInit() {
      const apply = (mode) => {
        document.documentElement.setAttribute("data-theme", mode);
        try { localStorage.setItem("zerolossTheme", mode); } catch (_) {}
        const btn = document.getElementById("btnTheme");
        if (btn) btn.textContent = mode === "light" ? "Dark mode" : "Light mode";
      };
      let mode = "dark";
      try { mode = localStorage.getItem("zerolossTheme") || "dark"; } catch (_) {}
      apply(mode === "light" ? "light" : "dark");
      const btn = document.getElementById("btnTheme");
      if (btn) btn.onclick = () => apply(document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light");
    })();
    document.getElementById("btnScan").onclick = () => runScan("focus");
    const btnCat = document.getElementById("btnScanCatalyst");
    if (btnCat) btnCat.onclick = () => runScan("catalyst");
    document.getElementById("btnScanWide").onclick = () => runScan("liquid");
    const btnMl6 = document.getElementById("btnScanMl6");
    if (btnMl6) btnMl6.onclick = () => runScan("ml6");
    const btnWb = document.getElementById("btnWebullSync");
    if (btnWb) btnWb.onclick = syncWebull;
    (function applyStaticHost() {
      if (!window.SIGNAL_DESK_STATIC) return;
      const scan = document.getElementById("btnScan");
      if (scan) {
        scan.textContent = "Reload snapshot";
        scan.onclick = () => loadAll();
      }
      ["btnScanCatalyst", "btnScanWide", "btnScanMl6"].forEach((id) => {
        const b = document.getElementById(id);
        if (b) b.style.display = "none";
      });
      const help = document.getElementById("pagesScanHelp");
      if (help) help.style.display = "inline-flex";
    })();

    // --- Browser BUY/SELL alerts (calls & puts) ---
    const ALERT_KEY = "signalDeskAlertsOn";
    const ALERT_SEEN = "signalDeskAlertSeen";
    let alertsEnabled = localStorage.getItem(ALERT_KEY) === "1";
    let alertSeen = new Set();
    try {
      const raw = sessionStorage.getItem(ALERT_SEEN);
      if (raw) JSON.parse(raw).forEach(k => alertSeen.add(k));
    } catch (_) {}
    let alertPrimed = alertSeen.size > 0;

    function saveAlertSeen() {
      try {
        sessionStorage.setItem(ALERT_SEEN, JSON.stringify([...alertSeen].slice(-200)));
      } catch (_) {}
    }

    function syncAlertButton() {
      const btn = document.getElementById("btnAlerts");
      if (!btn) return;
      const perm = (typeof Notification !== "undefined") ? Notification.permission : "unsupported";
      if (alertsEnabled && perm === "granted") {
        btn.textContent = "Alerts on";
        btn.classList.add("alerts-on");
      } else if (alertsEnabled && perm === "denied") {
        btn.textContent = "Alerts blocked";
        btn.classList.remove("alerts-on");
      } else {
        btn.textContent = "Enable alerts";
        btn.classList.remove("alerts-on");
      }
    }

    async function enableTradeAlerts() {
      if (typeof Notification === "undefined") {
        window.alert("This browser does not support notifications. Keep the desk tab open for on-page toasts only.");
        alertsEnabled = true;
        localStorage.setItem(ALERT_KEY, "1");
        syncAlertButton();
        return;
      }
      if (Notification.permission === "denied") {
        window.alert("Notifications are blocked for this site. Allow them in browser settings, then click Enable alerts again.");
        syncAlertButton();
        return;
      }
      if (Notification.permission !== "granted") {
        const p = await Notification.requestPermission();
        if (p !== "granted") {
          window.alert("Permission not granted — on-page toasts will still show while this tab is open.");
        }
      }
      alertsEnabled = true;
      localStorage.setItem(ALERT_KEY, "1");
      syncAlertButton();
      // Seed current board so only *new* recommendations alert after enable
      collectTradeAlerts().forEach(a => alertSeen.add(a.key));
      alertPrimed = true;
      saveAlertSeen();
      const now = collectTradeAlerts();
      const nEnter = now.filter(a => a.kind === "buy").length;
      const nExit = now.filter(a => a.kind === "sell").length;
      const nWatch = now.filter(a => a.kind === "watch").length;
      pushToast({
        kind: nEnter ? "buy" : "watch",
        title: "Alerts armed — keep this tab open",
        body: `On the board now: ${nEnter} ENTER NOW (hist-gated BUY, with strike rate + ENTER time) · ${nExit} EXIT NOW · ${nWatch} WATCH (Do-not-miss, not a buy). New names after this will toast. Existing ones will not re-fire.`,
      });
    }

    function disableTradeAlerts() {
      alertsEnabled = false;
      localStorage.setItem(ALERT_KEY, "0");
      syncAlertButton();
    }

    function collectTradeAlerts() {
      const out = [];
      const strikeRate = (row) => {
        const w = winLookup(row.symbol, row.dte_bucket || row.horizon || "0dte");
        if (w.hit1 == null) return null;
        return `strike rate ≥1% ${fmt(w.hit1,0)}%` + (w.hit2==null?"":` / ≥2% ${fmt(w.hit2,0)}%`);
      };
      const when = (row) => row.signaled_at_cst || fmtCST(row.signaled_at || row.recommended_at);
      const push = (row, side, desk) => {
        if (!row || !row.symbol) return;
        const act = String(row.action || side || "").toUpperCase();
        const isBuy = /BUY|ENTRY/.test(act) || side === "BUY";
        const isSell = /SELL|EXIT/.test(act) || side === "SELL";
        if (!isBuy && !isSell) return;
        const right = String(row.right || "C").toUpperCase() === "P" ? "PUT" : "CALL";
        const key = [
          isBuy ? "BUY" : "SELL",
          desk,
          String(row.symbol).toUpperCase(),
          right,
          row.contract || "",
          row.expiry || "",
          row.strike ?? "",
        ].join("|");
        const px = isBuy ? (row.ask ?? row.entry_ask) : (row.bid ?? row.mark ?? row.ask ?? row.exit_bid);
        out.push({
          key,
          kind: isBuy ? "buy" : "sell",
          title: `${isBuy ? "ENTER NOW" : "EXIT NOW"} ${row.symbol} ${right}`,
          body: [
            desk,
            when(row) && when(row) !== "—" ? `ENTER time ${when(row)}` : null,
            row.expiry || null,
            row.strike != null ? `${Number(row.strike)}${right === "PUT" ? "p" : "c"}` : null,
            px != null ? `@ $${Number(px).toFixed(2)}` : null,
            (row.win_pct ?? row.hist_win_pct) != null ? `hist win ${fmt(row.win_pct ?? row.hist_win_pct, 0)}%` : null,
            strikeRate(row),
            row.dte != null ? `${row.dte}DTE` : (row.dte_bucket || null),
            row.detail || row.headline || row.exit_plan || "",
          ].filter(Boolean).join(" · "),
        });
      };
      const pushWatch = (row) => {
        if (!row || !row.symbol) return;
        const key = `WATCH|DNM|${String(row.symbol).toUpperCase()}|${row.lane||""}|${row.day_change_pct??""}`;
        out.push({
          key,
          kind: "watch",
          title: `WATCH ${row.symbol} — not ENTER`,
          body: [
            "Do not miss tape",
            row.why || "",
            row.gap_pct != null ? `gap ${fmt(row.gap_pct,1)}%` : null,
            row.day_change_pct != null ? `day ${fmt(row.day_change_pct,1)}%` : null,
            "This is not a buy ticket",
          ].filter(Boolean).join(" · "),
        });
      };
      const acts = DATA.actions || {};
      (acts.buy_now || []).filter(r => r.headline || r.exit_plan || r.detail).forEach(r => push(r, "BUY", "Options"));
      (acts.sell_now || []).forEach(r => push(r, "SELL", "Options"));
      (acts.just_exited || []).forEach(r => push({...r, action: "SELL_NOW"}, "SELL", "Options"));
      (acts.recent_exits || []).forEach(r => push({...r, action: "SELL_NOW"}, "SELL", "Options"));
      const lot = DATA.lottery || {};
      (lot.buy_now || []).filter(r => (Number(r.win_pct ?? r.hist_win_pct) >= 80) && (Number(r.win_samples ?? r.hist_samples) >= 3)).forEach(r => push(r, "BUY", "Lottery"));
      (lot.sell_now || []).forEach(r => push(r, "SELL", "Lottery"));
      const ch = DATA.challenge || {};
      (ch.entry || []).forEach(r => push({...r, action: "ENTRY"}, "BUY", "Challenge"));
      (ch.exit || []).forEach(r => push({...r, action: "EXIT"}, "SELL", "Challenge"));
      (ch.just_exited || []).forEach(r => push({...r, action: "EXIT"}, "SELL", "Challenge"));
      ((DATA.zeroloss || {}).do_not_miss || []).forEach(pushWatch);
      return out;
    }

    function pushToast(alert) {
      const host = document.getElementById("alertToasts");
      if (!host) return;
      const el = document.createElement("div");
      el.className = `alert-toast ${alert.kind || "buy"}`;
      el.innerHTML = `<strong>${alert.title}</strong><div>${alert.body || ""}</div><div class="at-meta">ZeroLoss · keep tab open for alerts</div>`;
      host.prepend(el);
      setTimeout(() => el.remove(), 12000);
    }

    function beepAlert(kind) {
      try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        const ctx = new Ctx();
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.type = "sine";
        o.frequency.value = kind === "sell" ? 440 : (kind === "watch" ? 520 : 660);
        g.gain.value = 0.04;
        o.connect(g); g.connect(ctx.destination);
        o.start();
        setTimeout(() => { o.stop(); ctx.close(); }, 180);
      } catch (_) {}
    }

    function fireBrowserNotification(alert) {
      if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
      try {
        const n = new Notification(alert.title, {
          body: alert.body,
          tag: alert.key.slice(0, 100),
          renotify: true,
        });
        setTimeout(() => n.close(), 12000);
      } catch (_) {}
    }

    function maybeFireTradeAlerts() {
      const alerts = collectTradeAlerts();
      if (!alertPrimed) {
        // First paint: remember current set, don't spam
        alerts.forEach(a => alertSeen.add(a.key));
        alertPrimed = true;
        saveAlertSeen();
        return;
      }
      if (!alertsEnabled) {
        alerts.forEach(a => alertSeen.add(a.key));
        saveAlertSeen();
        return;
      }
      const fresh = alerts.filter(a => !alertSeen.has(a.key));
      fresh.slice(0, 6).forEach(a => {
        alertSeen.add(a.key);
        pushToast(a);
        beepAlert(a.kind);
        fireBrowserNotification(a);
      });
      alerts.forEach(a => alertSeen.add(a.key));
      saveAlertSeen();
    }

    const btnAlerts = document.getElementById("btnAlerts");
    if (btnAlerts) {
      syncAlertButton();
      btnAlerts.onclick = () => {
        if (alertsEnabled) disableTradeAlerts();
        else enableTradeAlerts();
      };
    }

    let _loading = false;
    const _origLoad = loadAll;
    loadAll = async function() {
      if (_loading) return;
      _loading = true;
      try { await _origLoad(); } finally { _loading = false; }
    };
    loadAll();
    // Snapshot is expensive (Yahoo); refresh once a minute, never overlap
    setInterval(loadAll, 60000);
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


def _snapshot_offline() -> bool:
    """Disk-only snapshot for GitHub Pages export (no live Yahoo fan-out)."""
    if os.environ.get("SIGNAL_DESK_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    try:
        return str(request.args.get("offline") or "").strip().lower() in {"1", "true", "yes", "on"}
    except RuntimeError:
        return False


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

        offline = _snapshot_offline()
        scan = _read_json(ROOT / "outputs" / "latest_scan.json") or {}
        watch = _read_json(ROOT / "outputs" / "watch" / "latest_watch.json")
        ledger_path = Path(cfg.get("paper_trading", {}).get("ledger_path", "outputs/paper_ledger.json"))
        if not ledger_path.is_absolute():
            ledger_path = ROOT / ledger_path
        ledger = _read_json(ledger_path)
        quotes = dict((watch or {}).get("quotes") or {})

        merged: list[dict] = []
        for key in (
            "option_candidates",
            "call_candidates_0dte",
            "call_candidates_weekly",
            "call_candidates",
            "put_candidates_0dte",
            "put_candidates_weekly",
            "put_candidates",
        ):
            for c in scan.get(key) or []:
                merged.append(dict(c))
        deduped: list[dict] = []
        seen: set[str] = set()
        for item in merged:
            key = item.get("contract") or f"{item.get('symbol')}-{item.get('right')}-{item.get('expiry')}-{item.get('strike')}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        board_rows = deduped[:20]
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
        # Prefer scan/disk win rates — rebuilding here blocks the UI for minutes
        if not win_table:
            try:
                win_table = load_win_rate_table() or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("win rates unavailable: %s", exc)
                win_table = {}

        # Challenge needs hist rates across mid/small + darlings — not just focus scan names
        try:
            from odte_scanner.challenge.million import _eligible_rows

            max_ch_tix = int(actions_cfg.get("challenge_max_tickets", 8))
            eligible_n = len(_eligible_rows(win_table if isinstance(win_table, dict) else None))
            # Pages/offline: never rebuild hist tables from Yahoo — that hung the first deploy.
            if (not offline) and eligible_n < max_ch_tix:
                win_table = ensure_challenge_win_table(
                    win_table if isinstance(win_table, dict) else None,
                    config_path=config_path,
                    max_age_hours=24.0,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("challenge win table ensure failed: %s", exc)

        # Challenge-eligible + DRAM/memory sleeve need live/cache quotes (often outside focus)
        challenge_syms: list[str] = []
        dram_syms: list[str] = []
        try:
            from odte_scanner.challenge.million import _eligible_rows
            from odte_scanner.data.universe import challenge_hist_universe, dram_memory_universe, liquid_universe

            # Only pull challenge/DRAM live quotes when we already have scan scores —
            # otherwise empty first paint waits minutes on Yahoo and the UI aborts.
            has_scan_scores = bool(scan.get("scores"))
            # Cap live quote fan-out — full challenge/DRAM sleeves make snapshot >3 min
            if has_scan_scores:
                challenge_syms = [
                    str(r["symbol"])
                    for r in _eligible_rows(win_table if isinstance(win_table, dict) else None)[
                        : max(12, int(actions_cfg.get("challenge_max_tickets", 8)) + 4)
                    ]
                ]
                if len(challenge_syms) < 4:
                    challenge_syms = sorted(
                        set(challenge_syms) | set(challenge_hist_universe()[:12])
                    )
            dram_syms = dram_memory_universe()[:4] if has_scan_scores else []
            # Aliases for full liquid universe (earnings/volume board — no extra quotes)
            for s in liquid_universe():
                aliases.setdefault(s, resolve_yahoo_symbol(s, cfg))
        except Exception:  # noqa: BLE001
            challenge_syms = []
            dram_syms = []
        quote_syms = (
            []
            if offline
            else sorted(set(syms[:8]) | set(challenge_syms[:12]) | set(dram_syms))
        )
        for s in quote_syms:
            aliases.setdefault(s, resolve_yahoo_symbol(s, cfg))

        def _uq(sym: str):
            return sym, fetch_live_quote(sym, yahoo_symbol=aliases.get(sym))

        if quote_syms:
            with ThreadPoolExecutor(max_workers=8) as pool:
                for sym, q in pool.map(_uq, quote_syms):
                    if q:
                        quotes[sym] = q.to_dict()

        refreshed: list[dict] = []

        def _refresh(item: dict) -> dict:
            # Use scan-time option fields; live chain refresh is too slow for UI paint
            out = dict(item)
            out["quote_stale"] = True
            sym = str(item.get("symbol"))
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

        jcfg = cfg.get("journal") or {}
        insights = None
        journal_sync = None
        journal = None
        journal_opens: list[dict] = []
        marks: dict[str, float] = {}
        if jcfg.get("enabled", True):
            from odte_scanner.options.live_chain import fetch_live_option_quote
            from odte_scanner.trading.journal import SignalJournal

            jpath = Path(jcfg.get("path", "outputs/signal_journal.json"))
            if not jpath.is_absolute():
                jpath = ROOT / jpath
            journal = SignalJournal(
                jpath, starting_cash=float(jcfg.get("starting_cash", 5000))
            )
            # Always create an empty journal file so Pages export can copy it
            if not jpath.exists():
                journal.save()
            # Mark open journal calls FIRST so TP/SL / SELL NOW see live premium
            open_syms_for_quotes: list[str] = []
            if not offline:
                for t in journal.book.trades:
                    if t.status != "open":
                        continue
                    open_syms_for_quotes.append(t.symbol)
                    aliases.setdefault(t.symbol, resolve_yahoo_symbol(t.symbol, cfg))
                    if t.expiry and t.strike is not None:
                        opt_right = "put" if str(getattr(t, "right", "C") or "C").upper() == "P" else "call"
                        q = fetch_live_option_quote(
                            t.symbol,
                            t.expiry,
                            float(t.strike),
                            yahoo_symbol=aliases.get(t.symbol) or resolve_yahoo_symbol(t.symbol, cfg),
                            right=opt_right,
                        )
                        if q:
                            if q.bid > 0 and q.ask > 0:
                                marks[t.contract] = (q.bid + q.ask) / 2
                            elif q.bid > 0:
                                marks[t.contract] = q.bid
                            elif q.ask > 0:
                                marks[t.contract] = q.ask
                # Underlying tape for open positions (exit on dumps / soft wall)
                for sym in sorted(set(open_syms_for_quotes)):
                    if sym in quotes:
                        continue
                    try:
                        lq = fetch_live_quote(sym, yahoo_symbol=aliases.get(sym))
                        if lq:
                            quotes[sym] = lq.to_dict()
                    except Exception:  # noqa: BLE001
                        pass
            if marks:
                journal.mark_open(marks)
            # Enrich open rows with bid=mark for decide_exit premium P&L
            journal_opens = []
            for t in journal.book.trades:
                if t.status != "open":
                    continue
                row = t.to_dict()
                if row.get("mark") is not None:
                    row["bid"] = row.get("bid") or row["mark"]
                    row["entry"] = row.get("entry_ask")
                journal_opens.append(row)

        red_flag_snapshot = scan.get("red_flag")
        rf_cfg = cfg.get("red_flag") or {}
        if rf_cfg.get("enabled", True) and not offline:
            try:
                from odte_scanner.signals.red_flag import analyze_red_flag

                rf_sym = str(rf_cfg.get("symbol") or (cfg.get("regime") or {}).get("spy") or "SPY")
                red_flag_snapshot = analyze_red_flag(
                    rf_sym,
                    yahoo_symbol=rf_cfg.get("yahoo_symbol")
                    or resolve_yahoo_symbol(rf_sym, cfg),
                    otm_min_pct=float(rf_cfg.get("otm_min_pct", 0.15)),
                    otm_max_pct=float(rf_cfg.get("otm_max_pct", 2.5)),
                    min_oi=int(rf_cfg.get("min_oi", 500)),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Red Flag live refresh failed: %s", exc)

        free_dealer = None
        if not offline:
            try:
                from odte_scanner.signals.free_feeds import build_free_dealer_cockpit

                free_dealer = build_free_dealer_cockpit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Free dealer cockpit failed: %s", exc)
                free_dealer = {"ok": False, "error": str(exc)}
        else:
            free_dealer = scan.get("free_dealer") or {"ok": False, "error": "offline"}

        # Pages snapshot has no 5m tape. buy_score 72 left hist-gated MSTR (score 65) in WAIT.
        pages_buy_score = float(
            actions_cfg.get("wait_score", 62) if offline else actions_cfg.get("buy_score", 70)
        )

        actions = build_action_board(
            candidates=refreshed,
            scores=scan.get("scores") or [],
            quotes=quotes,
            ledger=ledger if isinstance(ledger, dict) else None,
            journal_opens=journal_opens,
            buy_score=pages_buy_score,
            wait_score=float(actions_cfg.get("wait_score", 62)),
            sell_score=float(actions_cfg.get("sell_score", 48)),
            stop_loss_pct=float(risk.get("stop_loss_pct", 50)),
            take_profit_pct=float(risk.get("take_profit_pct", 80)),
            max_chase_pct=float(actions_cfg.get("max_chase_pct", 2.5)),
            win_rate_table=win_table,
            min_hist_win_pct=float(actions_cfg.get("min_hist_win_pct", 80)),
            min_hist_win_samples=int(actions_cfg.get("min_hist_win_samples", 5)),
            require_hist_win=bool(actions_cfg.get("require_hist_win", True)),
            weekly_max_hold_days=int(actions_cfg.get("weekly_max_hold_days", 7)),
            odte_flatten_et=str(actions_cfg.get("odte_flatten_et") or "15:45"),
            # Pages offline has no live tape — still allow gated BUY so journal/exits can run
            require_live_confirm=not offline,
            red_flag=red_flag_snapshot,
        )

        if journal is not None:
            from odte_scanner.trading.insights import build_insights

            journal_sync = journal.sync_from_actions(
                actions,
                max_risk_usd=float(jcfg.get("max_risk_per_trade_usd", 250)),
                auto_enter=bool(jcfg.get("auto_enter", True)),
                auto_exit=bool(jcfg.get("auto_exit", True)),
            )
            # If we just opened fills, rebuild SELL NOW (TP/SL/clock) against new opens
            if journal_sync and journal_sync.get("entered"):
                journal_opens = []
                for t in journal.book.trades:
                    if t.status != "open":
                        continue
                    row = t.to_dict()
                    if row.get("mark") is not None:
                        row["bid"] = row.get("bid") or row["mark"]
                        row["entry"] = row.get("entry_ask")
                    journal_opens.append(row)
                actions = build_action_board(
                    candidates=refreshed,
                    scores=scan.get("scores") or [],
                    quotes=quotes,
                    ledger=ledger if isinstance(ledger, dict) else None,
                    journal_opens=journal_opens,
                    buy_score=pages_buy_score,
                    wait_score=float(actions_cfg.get("wait_score", 62)),
                    sell_score=float(actions_cfg.get("sell_score", 48)),
                    stop_loss_pct=float(risk.get("stop_loss_pct", 50)),
                    take_profit_pct=float(risk.get("take_profit_pct", 80)),
                    max_chase_pct=float(actions_cfg.get("max_chase_pct", 2.5)),
                    win_rate_table=win_table,
                    min_hist_win_pct=float(actions_cfg.get("min_hist_win_pct", 80)),
                    min_hist_win_samples=int(actions_cfg.get("min_hist_win_samples", 5)),
                    require_hist_win=bool(actions_cfg.get("require_hist_win", True)),
                    weekly_max_hold_days=int(actions_cfg.get("weekly_max_hold_days", 7)),
                    odte_flatten_et=str(actions_cfg.get("odte_flatten_et") or "15:45"),
                    require_live_confirm=not offline,
                    red_flag=red_flag_snapshot,
                )
                more = journal.sync_from_actions(
                    actions,
                    max_risk_usd=float(jcfg.get("max_risk_per_trade_usd", 250)),
                    auto_enter=False,
                    auto_exit=bool(jcfg.get("auto_exit", True)),
                )
                if more.get("exited"):
                    journal_sync = dict(journal_sync)
                    journal_sync["exited"] = list(journal_sync.get("exited") or []) + list(more["exited"])
                    journal_sync["performance"] = more.get("performance") or journal_sync.get("performance")
            # Re-mark after exits so open MTM / equity stay current
            if marks:
                still = {t.contract: marks[t.contract] for t in journal.book.trades if t.status == "open" and t.contract in marks}
                if still:
                    journal.mark_open(still)
            insights = build_insights(journal=journal, actions=actions, win_rates=win_table)
            # Attach just-closed exits onto actions so UI shows EXIT + P&L this cycle
            if journal_sync and journal_sync.get("exited"):
                actions = dict(actions)
                actions["just_exited"] = journal_sync["exited"]
                actions["counts"] = dict(actions.get("counts") or {})
                actions["counts"]["just_exited"] = len(journal_sync["exited"])
            else:
                # Keep closed history visible even when this cycle had no new exits
                closed = (insights or {}).get("closed_trades") or []
                if closed and not (actions.get("just_exited")):
                    actions = dict(actions)
                    actions["recent_exits"] = closed[:8]

        from odte_scanner.options.explosive import build_explosive_board, build_radar_wing_board
        from odte_scanner.signals.lottery import build_lottery_board
        from odte_scanner.signals.radar import build_radar_board

        # Lottery / parabolic 0DTE–1DTE tickets (e.g. cheap calls that can 3×–100× on a rip)
        explosive = build_explosive_board(
            refreshed,
            scores=scan.get("scores") or [],
            quotes=quotes,
            aliases=aliases,
            enrich_live=False,  # live option enrich is too slow for interactive snapshot
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

        # Paper journal also follows lottery BUY/SELL NOW
        if journal is not None and jcfg.get("enabled", True):
            try:
                from odte_scanner.trading.insights import build_insights as _build_insights

                lot_sync = journal.sync_from_actions(
                    {"buy_now": [], "sell_now": [], "buy_now_0dte": [], "buy_now_weekly": []},
                    max_risk_usd=float(jcfg.get("max_risk_per_trade_usd", 250)),
                    auto_enter=bool(jcfg.get("auto_enter", True)),
                    auto_exit=bool(jcfg.get("auto_exit", True)),
                    lottery=lottery,
                )
                if journal_sync is None:
                    journal_sync = lot_sync
                else:
                    journal_sync = dict(journal_sync)
                    journal_sync["entered"] = list(journal_sync.get("entered") or []) + list(
                        lot_sync.get("entered") or []
                    )
                    journal_sync["exited"] = list(journal_sync.get("exited") or []) + list(
                        lot_sync.get("exited") or []
                    )
                    journal_sync["performance"] = lot_sync.get("performance") or journal_sync.get(
                        "performance"
                    )
                if lot_sync.get("exited"):
                    actions = dict(actions)
                    actions["just_exited"] = list(actions.get("just_exited") or []) + list(
                        lot_sync["exited"]
                    )
                    actions["counts"] = dict(actions.get("counts") or {})
                    actions["counts"]["just_exited"] = len(actions["just_exited"])
                insights = _build_insights(journal=journal, actions=actions, win_rates=win_table)
            except Exception as exc:  # noqa: BLE001
                logger.debug("lottery journal sync skipped: %s", exc)

        # Discord-style radar — cheap index wings; does NOT feed BUY NOW / journal
        radar: dict = {"hot": [], "watch": [], "cool": [], "tickets": [], "counts": {}, "note": ""}
        if actions_cfg.get("radar_enabled", True):
            try:
                focus = list(actions_cfg.get("radar_focus") or ["SPY", "QQQ", "IWM", "DIA"])
                radar_tickets = build_radar_wing_board(
                    scores=scan.get("scores") or [],
                    quotes=quotes,
                    aliases=aliases,
                    focus_symbols=focus,
                    candidates=refreshed,
                    min_ask=float(actions_cfg.get("radar_min_ask", 0.15)),
                    max_ask=float(actions_cfg.get("radar_max_ask", 2.50)),
                    otm_pct_max=float(actions_cfg.get("radar_otm_pct_max", 1.50)),
                    enrich_live=bool(actions_cfg.get("radar_enrich_live", True)),
                    per_symbol=3,
                    max_total=18,
                )
                radar = build_radar_board(
                    radar_tickets,
                    quotes=quotes,
                    scores=scan.get("scores") or [],
                    min_ask=float(actions_cfg.get("radar_min_ask", 0.15)),
                    max_ask=float(actions_cfg.get("radar_max_ask", 2.50)),
                    max_otm_pct=float(actions_cfg.get("radar_otm_pct_max", 1.50)),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("radar board unavailable: %s", exc)
                radar = {
                    "error": str(exc),
                    "hot": [],
                    "watch": [],
                    "cool": [],
                    "tickets": [],
                    "counts": {},
                    "note": "Radar temporarily unavailable.",
                }

        # Chase / high-convexity lane — BUY_RISKY / WATCH_CONVEX (not hist-gated BUY NOW)
        chase_radar: dict = {
            "buy_risky": [],
            "watch": [],
            "cool": [],
            "tickets": [],
            "counts": {},
            "note": "",
            "score_note": "",
        }
        if actions_cfg.get("chase_radar_enabled", True):
            try:
                from odte_scanner.options.explosive import build_chase_wing_board
                from odte_scanner.signals.chase_radar import build_chase_board

                chase_tickets = build_chase_wing_board(
                    scores=scan.get("scores") or [],
                    quotes=quotes,
                    aliases=aliases,
                    candidates=refreshed,
                    min_ask=float(actions_cfg.get("chase_min_ask", 0.20)),
                    max_ask=float(actions_cfg.get("chase_max_ask", 12.0)),
                    otm_pct_max=float(actions_cfg.get("chase_otm_pct_max", 8.0)),
                    enrich_live=bool(actions_cfg.get("chase_enrich_live", True)),
                    max_live_symbols=int(actions_cfg.get("chase_max_live_symbols", 8)),
                    per_symbol=2,
                    max_total=16,
                )
                chase_radar = build_chase_board(
                    chase_tickets,
                    quotes=quotes,
                    scores=scan.get("scores") or [],
                    min_ask=float(actions_cfg.get("chase_min_ask", 0.20)),
                    max_ask=float(actions_cfg.get("chase_max_ask", 12.0)),
                    max_otm_pct=float(actions_cfg.get("chase_otm_pct_max", 8.0)),
                    min_mult_at_3pct=float(actions_cfg.get("chase_min_mult_at_3pct", 3.5)),
                    min_mom_5m=float(actions_cfg.get("chase_min_mom_5m", 0.08)),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("chase radar unavailable: %s", exc)
                chase_radar = {
                    "error": str(exc),
                    "buy_risky": [],
                    "watch": [],
                    "cool": [],
                    "tickets": [],
                    "counts": {},
                    "note": "Chase / convex lane temporarily unavailable.",
                    "score_note": "",
                }

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
                max_symbols=int(actions_cfg.get("echo_max_symbols", 8)),
                max_dte=int((cfg.get("options") or {}).get("max_dte", 5)),
                fetch_ladders=False,  # Yahoo ladders block snapshot for minutes
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

            # Seed walls from Echo DealerEdge profiles (already fetched ladders)
            echo_walls: dict[str, dict] = {}
            try:
                from odte_scanner.options.walls import wall_exit_levels

                for p in ((echo.get("dealer_edge") or {}).get("profiles") or []):
                    sym = str(p.get("symbol") or "").upper()
                    if not sym:
                        continue
                    echo_walls[sym] = {
                        **wall_exit_levels(
                            right="C",
                            spot=p.get("spot"),
                            call_wall=p.get("call_wall"),
                            put_wall=p.get("put_wall"),
                            buffer_usd=float(actions_cfg.get("wall_exit_buffer_usd", 0.10)),
                        ),
                        "flip": p.get("flip"),
                        "regime": p.get("regime"),
                        "expiry": p.get("expiry"),
                        "dte": p.get("dte"),
                        "source": "echo_gex",
                    }
            except Exception:  # noqa: BLE001
                echo_walls = {}

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
                # Snapshot must stay interactive — use disk cache only (no Yahoo fan-out)
                fetch_contracts=False,
                fetch_earnings=False,
                earnings_max_fetch=int(actions_cfg.get("challenge_earnings_max_fetch", 36)),
                fetch_walls=False,
                wall_buffer_usd=float(actions_cfg.get("wall_exit_buffer_usd", 0.10)),
                walls_map=echo_walls,
            )
            live_contracts = {
                (str(t.get("symbol")), str(t.get("right") or "C")): t
                for t in (challenge.get("tickets") or [])
                if t.get("ask") is not None or t.get("contract") or t.get("call_wall") is not None
            }
            sync = tracker.sync_from_tickets(
                challenge.get("tickets") or [],
                quotes=quotes,
                auto_enter=bool(actions_cfg.get("challenge_auto_enter", True)),
                auto_exit=bool(actions_cfg.get("challenge_auto_exit", True)),
                max_open=int(actions_cfg.get("challenge_max_open", 1)),
            )
            challenge["sync"] = sync
            challenge["book"] = sync.get("book") or tracker.book.to_dict()
            # Rebuild statuses after sync; keep prior contract fields when present
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
                fetch_contracts=False,
                fetch_earnings=False,
                earnings_max_fetch=int(actions_cfg.get("challenge_earnings_max_fetch", 36)),
                fetch_walls=False,
                wall_buffer_usd=float(actions_cfg.get("wall_exit_buffer_usd", 0.10)),
                walls_map=challenge.get("walls_map") or echo_walls,
            )
            for t in challenge.get("tickets") or []:
                prev = live_contracts.get((str(t.get("symbol")), str(t.get("right") or "C")))
                if not prev:
                    continue
                for key in (
                    "contract",
                    "expiry",
                    "dte",
                    "strike",
                    "ask",
                    "bid",
                    "option_last",
                    "mark_source",
                    "moneyness_pct",
                    "open_interest",
                    "volume",
                    "target_ask",
                    "debit_usd",
                    "contracts_for_bankroll",
                    "call_wall",
                    "put_wall",
                    "call_wall_oi",
                    "put_wall_oi",
                    "primary_wall",
                    "primary_wall_side",
                    "soft_exit",
                    "wall_buffer_usd",
                    "wall_exit_hint",
                    "gex_flip",
                    "gex_regime",
                    "exit_plan",
                    "reasons",
                    "spot",
                    "spot_source",
                    "live_ok",
                    "data_note",
                ):
                    if t.get(key) in (None, "", "zone") and prev.get(key) not in (None, ""):
                        t[key] = prev.get(key)
                # Prefer live ask from first pass when second pass fell back to zone
                if prev.get("ask") is not None and (
                    t.get("ask") is None or t.get("mark_source") == "zone"
                ):
                    t["ask"] = prev.get("ask")
                    t["bid"] = prev.get("bid")
                    t["option_last"] = prev.get("option_last")
                    t["mark_source"] = prev.get("mark_source") or "ask"
                    t["contract"] = prev.get("contract") or t.get("contract")
                    t["expiry"] = prev.get("expiry") or t.get("expiry")
                    t["dte"] = prev.get("dte") if prev.get("dte") is not None else t.get("dte")
                    t["strike"] = prev.get("strike") if prev.get("strike") is not None else t.get("strike")
                    if prev.get("target_ask") is not None:
                        t["target_ask"] = prev.get("target_ask")
            # Refresh counts after merge
            tickets = challenge.get("tickets") or []
            challenge["counts"] = {
                **(challenge.get("counts") or {}),
                "live_ask": sum(1 for t in tickets if t.get("ask") is not None and t.get("mark_source") != "zone"),
                "live_spot": sum(1 for t in tickets if t.get("spot_source") == "live"),
                "cache_spot": sum(1 for t in tickets if t.get("spot_source") == "cache"),
            }
            challenge["entry"] = [t for t in tickets if t.get("action") == "ENTRY"]
            challenge["hold"] = [t for t in tickets if t.get("action") == "HOLD"]
            challenge["exit"] = [t for t in tickets if t.get("action") == "EXIT"]
            # Keep just-closed flips visible this cycle (rebuild drops them from open_map)
            just_closed = [
                t.to_dict()
                for t in tracker.book.trades
                if t.status == "closed" and t.id in set(sync.get("exited") or [])
            ]
            if just_closed:
                challenge["just_exited"] = just_closed
                # Surface as EXIT cards with P&L so the desk isn't ENTER-only after auto-exit
                for t in just_closed:
                    challenge["exit"].append(
                        {
                            "symbol": t.get("symbol"),
                            "right": t.get("right") or "C",
                            "action": "EXIT",
                            "strike": t.get("strike"),
                            "expiry": t.get("expiry"),
                            "contract": t.get("contract"),
                            "ask": t.get("entry_ask"),
                            "bid": t.get("exit_bid"),
                            "mark": t.get("exit_bid"),
                            "entered_at": t.get("entered_at"),
                            "exited_at": t.get("exited_at"),
                            "closed_at": t.get("exited_at"),
                            "profit_pct": t.get("profit_pct"),
                            "pnl_usd": t.get("pnl_usd"),
                            "cash_after": t.get("cash_after"),
                            "equity_after": t.get("equity_after"),
                            "balance_note": t.get("balance_note"),
                            "exit_plan": t.get("exit_reason") or t.get("last_action_detail"),
                            "recommend_reason": t.get("exit_reason") or "Auto EXIT",
                            "reasons": [t.get("exit_reason") or t.get("last_action_detail") or "EXIT"],
                        }
                    )
                challenge["counts"] = {
                    **(challenge.get("counts") or {}),
                    "exit": len(challenge["exit"]),
                    "just_exited": len(just_closed),
                }
            challenge["sync"] = sync
            challenge["book"] = tracker.book.to_dict()
        except Exception as exc:  # noqa: BLE001
            logger.warning("challenge board unavailable: %s", exc)
            challenge = {"error": str(exc), "tickets": [], "disclaimer": "Challenge board unavailable."}

        odte_1k: dict = {}
        try:
            if bool(actions_cfg.get("odte_1k_enabled", True)):
                from odte_scanner.challenge.odte_1k import build_odte_1k_board, resolve_odte_1k_symbols
                from odte_scanner.challenge.odte_1k_tracker import Odte1kTracker

                o1k_path = Path(actions_cfg.get("odte_1k_ledger_path", "outputs/odte_1k_ledger.json"))
                if not o1k_path.is_absolute():
                    o1k_path = ROOT / o1k_path
                o1k_tracker = Odte1kTracker(
                    o1k_path,
                    starting_cash=float(actions_cfg.get("odte_1k_start_usd", 1000)),
                    max_trades_per_day=int(actions_cfg.get("odte_1k_max_trades_per_day", 2)),
                    default_size_usd=float(actions_cfg.get("odte_1k_position_size_usd", 850)),
                )
                o1k_syms = resolve_odte_1k_symbols(
                    actions_cfg.get("odte_1k_symbols"),
                    config=cfg,
                )
                max_q = int(actions_cfg.get("odte_1k_max_quote_fetch", 48))
                # Ensure quotes for ORB symbols (capped — full focus sleeve is large)
                if not offline:
                    for s in o1k_syms[:max_q]:
                        if s not in quotes:
                            aliases.setdefault(s, resolve_yahoo_symbol(s, cfg))
                            try:
                                q = fetch_live_quote(s, yahoo_symbol=aliases.get(s))
                                if q:
                                    quotes[s] = q.to_dict()
                            except Exception:  # noqa: BLE001
                                pass
                odte_1k = build_odte_1k_board(
                    quotes=quotes,
                    red_flag=red_flag_snapshot if isinstance(red_flag_snapshot, dict) else None,
                    actions=actions if isinstance(actions, dict) else None,
                    symbols=o1k_syms,
                    config=cfg,
                    open_trades=[t.to_dict() for t in o1k_tracker.book.trades],
                    book=o1k_tracker.book.to_dict(),
                    starting_cash=float(actions_cfg.get("odte_1k_start_usd", 1000)),
                    position_size_usd=float(actions_cfg.get("odte_1k_position_size_usd", 850)),
                    position_pct=float(actions_cfg.get("odte_1k_position_pct", 0.85)),
                    max_trades_per_day=int(actions_cfg.get("odte_1k_max_trades_per_day", 2)),
                    max_orb_fetch=int(actions_cfg.get("odte_1k_max_orb_fetch", 20)),
                    max_contract_fetch=int(actions_cfg.get("odte_1k_max_contract_fetch", 8)),
                    fetch_bars=bool(actions_cfg.get("odte_1k_fetch_bars", True)) and not offline,
                    fetch_contracts=bool(actions_cfg.get("odte_1k_fetch_contracts", True)) and not offline,
                    flatten_et=str(actions_cfg.get("odte_flatten_et", "15:45")),
                    aliases=aliases,
                )
                # Auto EXIT open puts when board says EXIT
                if bool(actions_cfg.get("odte_1k_auto_exit", True)):
                    for sig in odte_1k.get("exit_now") or []:
                        sym = str(sig.get("symbol") or "")
                        for t in list(o1k_tracker.open_trades()):
                            if t.symbol != sym:
                                continue
                            mark = float(sig.get("bid") or sig.get("ask") or t.mark or t.entry_ask or 0)
                            o1k_tracker.exit_trade(t.id, exit_bid=mark, reason=str(sig.get("detail") or "AUTO EXIT"))
                    odte_1k["book"] = o1k_tracker.book.to_dict()
                    odte_1k["cash"] = odte_1k["book"].get("cash")
                    odte_1k["equity"] = odte_1k["book"].get("equity")
                    odte_1k["doubled"] = odte_1k["book"].get("doubled")
                    odte_1k["progress_2x_pct"] = odte_1k["book"].get("progress_2x_pct")
                    odte_1k["trades_today"] = odte_1k["book"].get("trades_today")
        except Exception as exc:  # noqa: BLE001
            logger.warning("odte_1k board unavailable: %s", exc)
            odte_1k = {"error": str(exc), "put_now": [], "disclaimer": "0DTE $1K board unavailable."}

        power_hour: dict = {}
        try:
            if bool(actions_cfg.get("power_hour_enabled", True)):
                from odte_scanner.signals.power_hour import (
                    build_power_hour_board,
                    resolve_power_hour_symbols,
                )

                ph_syms = resolve_power_hour_symbols(
                    actions_cfg.get("power_hour_symbols"),
                    config=cfg,
                )
                max_q = int(actions_cfg.get("power_hour_max_quote_fetch", 48))
                if not offline:
                    for s in (["QQQ"] + ph_syms)[:max_q]:
                        if s not in quotes:
                            aliases.setdefault(s, resolve_yahoo_symbol(s, cfg))
                            try:
                                q = fetch_live_quote(s, yahoo_symbol=aliases.get(s))
                                if q:
                                    quotes[s] = q.to_dict()
                            except Exception:  # noqa: BLE001
                                pass
                power_hour = build_power_hour_board(
                    quotes=quotes,
                    symbols=ph_syms,
                    config=cfg,
                    fetch_bars=bool(actions_cfg.get("power_hour_fetch_bars", True)) and not offline,
                    max_bar_fetch=int(actions_cfg.get("power_hour_max_bar_fetch", 16)),
                    aliases=aliases,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("power hour board unavailable: %s", exc)
            power_hour = {"error": str(exc), "long": [], "short": [], "disclaimer": "Power Hour board unavailable."}

        market = {}
        try:
            from odte_scanner.market import build_market_board

            market = build_market_board(
                scores=scan.get("scores") or [],
                quotes=quotes,
                aliases=aliases,
                # Earnings cache only — live Yahoo warm on every snapshot starves the UI
                fetch_earnings=False,
                earnings_max_fetch=int(
                    actions_cfg.get(
                        "market_board_earnings_max_fetch",
                        actions_cfg.get("challenge_earnings_max_fetch", 60),
                    )
                ),
                win_table=win_table if isinstance(win_table, dict) else None,
            )
            # Keep challenge earnings watch at least as broad as market board
            if market.get("earnings_watch") and (
                len(market.get("earnings_watch") or [])
                >= len(challenge.get("earnings_watch") or [])
            ):
                challenge["earnings_watch"] = market.get("earnings_watch")
                challenge["earnings_watch_buckets"] = {
                    "today": (market.get("counts") or {}).get("today", 0),
                    "this_week": (market.get("counts") or {}).get("this_week", 0),
                    "next_week": (market.get("counts") or {}).get("next_week", 0),
                    "post": (market.get("counts") or {}).get("post", 0),
                    "soon": (market.get("counts") or {}).get("soon", 0),
                }
                challenge["counts"] = {
                    **(challenge.get("counts") or {}),
                    "earn_today": challenge["earnings_watch_buckets"]["today"],
                    "earn_this_week": challenge["earnings_watch_buckets"]["this_week"],
                    "earn_next_week": challenge["earnings_watch_buckets"]["next_week"],
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("market board unavailable: %s", exc)
            market = {"error": str(exc), "by_earnings": [], "by_volume": [], "by_score": []}

        # Unified walls map for all recommended surfaces (challenge + echo + action cards)
        walls_by_symbol: dict[str, dict] = {}
        try:
            from odte_scanner.options.walls import wall_exit_levels

            for p in ((echo.get("dealer_edge") or {}).get("profiles") or []):
                sym = str(p.get("symbol") or "").upper()
                if not sym:
                    continue
                walls_by_symbol[sym] = {
                    **wall_exit_levels(
                        right="C",
                        spot=p.get("spot"),
                        call_wall=p.get("call_wall"),
                        put_wall=p.get("put_wall"),
                        buffer_usd=float(actions_cfg.get("wall_exit_buffer_usd", 0.10)),
                    ),
                    "flip": p.get("flip"),
                    "regime": p.get("regime"),
                    "exit_hint": None,
                    "source": "echo_gex",
                }
                walls_by_symbol[sym]["exit_hint"] = walls_by_symbol[sym].get("exit_hint")
            for t in (challenge.get("tickets") or []):
                sym = str(t.get("symbol") or "").upper()
                if not sym or t.get("call_wall") is None and t.get("put_wall") is None:
                    continue
                right = str(t.get("right") or "C").upper()
                refreshed_w = wall_exit_levels(
                    right=right,
                    spot=t.get("spot") or t.get("live_last"),
                    call_wall=t.get("call_wall"),
                    put_wall=t.get("put_wall"),
                    buffer_usd=float(actions_cfg.get("wall_exit_buffer_usd", 0.10)),
                )
                walls_by_symbol[sym] = {
                    "call_wall": t.get("call_wall"),
                    "put_wall": t.get("put_wall"),
                    "call_wall_oi": t.get("call_wall_oi"),
                    "put_wall_oi": t.get("put_wall_oi"),
                    "primary_wall": t.get("primary_wall"),
                    "primary_wall_side": t.get("primary_wall_side"),
                    "soft_exit": refreshed_w.get("soft_exit") or t.get("soft_exit"),
                    "wall_buffer_usd": t.get("wall_buffer_usd"),
                    "exit_hint": refreshed_w.get("exit_hint") or t.get("wall_exit_hint"),
                    "wall_exit_hint": refreshed_w.get("exit_hint") or t.get("wall_exit_hint"),
                    "flip": t.get("gex_flip"),
                    "regime": t.get("gex_regime"),
                    "right": right,
                    "source": "challenge",
                }
            # Soft-exit hint for long bias on action-card names using call wall
            for sym, w in list(walls_by_symbol.items()):
                if w.get("soft_exit") is None and w.get("call_wall") is not None:
                    refreshed_w = wall_exit_levels(
                        right="C",
                        spot=None,
                        call_wall=w.get("call_wall"),
                        put_wall=w.get("put_wall"),
                        call_wall_oi=w.get("call_wall_oi"),
                        put_wall_oi=w.get("put_wall_oi"),
                        buffer_usd=float(actions_cfg.get("wall_exit_buffer_usd", 0.10)),
                    )
                    walls_by_symbol[sym] = {**w, **refreshed_w, "exit_hint": refreshed_w.get("exit_hint")}
            # Attach walls onto market board rows for Screener
            for key in ("by_earnings", "by_volume", "by_score"):
                for row in market.get(key) or []:
                    w = walls_by_symbol.get(str(row.get("symbol") or "").upper())
                    if not w:
                        continue
                    row["call_wall"] = w.get("call_wall")
                    row["put_wall"] = w.get("put_wall")
                    row["soft_exit"] = w.get("soft_exit")
                    row["wall_exit_hint"] = w.get("exit_hint") or w.get("wall_exit_hint")
        except Exception as exc:  # noqa: BLE001
            logger.debug("walls_by_symbol merge failed: %s", exc)
            walls_by_symbol = {}

        # Persist recommendation history (lottery / challenge / 0DTE / weekly / swing)
        rec_log_payload: dict = {}
        try:
            from odte_scanner.trading.rec_log import RecommendationLog

            rec_path = Path(actions_cfg.get("rec_log_path", "outputs/recommendation_log.json"))
            if not rec_path.is_absolute():
                rec_path = ROOT / rec_path
            rlog = RecommendationLog(rec_path)
            rlog.sync_all(
                lottery=lottery,
                challenge=challenge,
                actions=actions,
                action_cards=scan.get("action_cards") or {},
                radar=radar,
                journal=journal,
            )
            by_section = {
                "lottery": rlog.board(section="lottery", limit=30),
                "challenge": rlog.board(section="challenge", limit=30),
                "odte": rlog.board(section="odte", limit=30),
                "weekly": rlog.board(section="weekly", limit=30),
                "swing": rlog.board(section="swing", limit=30),
                "radar": rlog.board(section="radar", limit=30),
            }
            rec_log_payload = {
                **rlog.board(limit=50),
                "by_section": by_section,
                "lottery": by_section["lottery"],
                "challenge": by_section["challenge"],
                "odte": by_section["odte"],
                "weekly": by_section["weekly"],
                "swing": by_section["swing"],
                "radar": by_section["radar"],
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("recommendation log unavailable: %s", exc)
            rec_log_payload = {"error": str(exc), "open_recs": [], "closed_recs": [], "by_section": {}}


        ml6 = scan.get("ml6")
        if not ml6:
            ml6 = _read_json(ROOT / "outputs" / "latest_ml6.json")
        if not ml6 and not offline:
            try:
                from odte_scanner.ml6.board import build_ml6_board

                ml6 = build_ml6_board()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ML6 snapshot fallback failed: %s", exc)
                ml6 = {}
        ml6 = ml6 or {}

        # Refresh ML6 BUY/SELL automation with live quotes + open journal trades
        if not offline:
            try:
                from odte_scanner.data.fetcher import fetch_many as _fetch_many
                from odte_scanner.data.live_quotes import fetch_live_quote as _flq
                from odte_scanner.ml6.board import build_ml6_board as _bml6
                from odte_scanner.ml6.watchlist import ml6_tickers as _ml6t

                ml6_syms = _ml6t()
                for s in ml6_syms:
                    aliases.setdefault(s, resolve_yahoo_symbol(s, cfg))
                ml6_quotes: dict = {}
                for sym in ml6_syms:
                    qq = quotes.get(sym)
                    if not qq:
                        lq = _flq(sym, yahoo_symbol=aliases.get(sym))
                        if lq:
                            qq = lq.to_dict() if hasattr(lq, "to_dict") else dict(lq)
                    if qq:
                        ml6_quotes[sym] = qq
                        quotes[sym] = qq
                open_ml6 = []
                if journal is not None:
                    open_ml6 = [t.to_dict() for t in journal.book.trades if t.status == "open"]
                elif isinstance(ledger, dict):
                    open_ml6 = [t for t in (ledger.get("trades") or []) if t.get("status") == "open"]
                hist = _fetch_many(ml6_syms, period="1y", aliases=aliases)
                ml6 = _bml6(
                    hist,
                    quotes=ml6_quotes,
                    symbols=ml6_syms,
                    open_trades=open_ml6,
                    min_buy_score=float((cfg.get("ml6") or {}).get("min_buy_score", 70)),
                    attach_calls=bool((cfg.get("ml6") or {}).get("attach_calls", True)),
                )
                # Paper sync ML6 BUY/SELL like lottery desk
                if journal is not None and jcfg.get("enabled", True) and bool((cfg.get("ml6") or {}).get("auto_trade", True)):
                    from odte_scanner.trading.insights import build_insights as _bi2

                    ml6_sync = journal.sync_from_actions(
                        {"buy_now": [], "sell_now": [], "buy_now_0dte": [], "buy_now_weekly": []},
                        max_risk_usd=float(jcfg.get("max_risk_per_trade_usd", 250)),
                        auto_enter=bool(jcfg.get("auto_enter", True)),
                        auto_exit=bool(jcfg.get("auto_exit", True)),
                        ml6=ml6.get("actions"),
                    )
                    if journal_sync is None:
                        journal_sync = ml6_sync
                    else:
                        journal_sync = dict(journal_sync)
                        journal_sync["entered"] = list(journal_sync.get("entered") or []) + list(
                            ml6_sync.get("entered") or []
                        )
                        journal_sync["exited"] = list(journal_sync.get("exited") or []) + list(
                            ml6_sync.get("exited") or []
                        )
                    if ml6_sync.get("exited") or ml6_sync.get("entered"):
                        insights = _bi2(journal=journal, actions=actions, win_rates=win_table)
                    ml6["journal_sync"] = {
                        "entered": len(ml6_sync.get("entered") or []),
                        "exited": len(ml6_sync.get("exited") or []),
                    }
            except Exception as exc:  # noqa: BLE001
                logger.warning("ML6 live action refresh failed: %s", exc)

        # Persist boards so /api/webull/sync + auto_sync see the same ENTER/EXIT set
        cache_path = ROOT / "outputs" / "ui_snapshot_cache.json"
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "actions": actions,
                        "lottery": lottery,
                        "challenge": challenge,
                        "odte_1k": odte_1k,
                        "radar": radar,
                        "chase_radar": chase_radar,
                    },
                    indent=2,
                    default=str,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("ui snapshot cache write failed: %s", exc)

        webull_payload = _webull_status_payload()
        lt_cfg = cfg.get("live_trading") or {}
        # Auto-stage BUY/SELL into Webull ledger (preview/dry-run by default).
        # Also run offline so Pages export captures history after a scan.
        if bool(lt_cfg.get("auto_sync", True)):
            try:
                webull_payload = _run_webull_sync(
                    actions=actions,
                    lottery=lottery,
                    challenge=challenge,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("webull auto_sync failed: %s", exc)
                webull_payload = {**(webull_payload or {}), "auto_sync_error": str(exc)}

        zeroloss = scan.get("zeroloss") if isinstance(scan.get("zeroloss"), dict) else {}
        if zeroloss:
            zeroloss = dict(zeroloss)
            oflow = (echo.get("option_flow") or {}) if isinstance(echo, dict) else {}
            prints = list(oflow.get("prints") or zeroloss.get("flow_prints") or [])
            zeroloss["flow_prints"] = prints[:40]
            counts = dict(zeroloss.get("counts") or {})
            counts["flow_prints"] = len(zeroloss["flow_prints"])
            zeroloss["counts"] = counts
            for row in list(zeroloss.get("do_not_miss") or []) + list(zeroloss.get("all") or []):
                if not isinstance(row, dict):
                    continue
                q = quotes.get(str(row.get("symbol") or "").upper())
                if isinstance(q, dict):
                    if q.get("last"):
                        row["live_last"] = q.get("last")
                    ch = q.get("session_change_pct", q.get("change_pct"))
                    if ch is not None:
                        row["live_change_pct"] = ch
        else:
            from odte_scanner.zeroloss.board import DISCLAIMER as ZL_DISCLAIMER

            oflow = (echo.get("option_flow") or {}) if isinstance(echo, dict) else {}
            zeroloss = {
                "brand": "ZeroLoss",
                "purpose": "Do not miss the tape. Catch gap/volume/news names the hist-win gate hid.",
                "disclaimer": ZL_DISCLAIMER,
                "counts": {
                    "scanned": 0,
                    "do_not_miss": 0,
                    "catalyst": 0,
                    "tape": 0,
                    "flow_prints": len(oflow.get("prints") or []),
                },
                "do_not_miss": [],
                "catalyst": [],
                "tape": [],
                "all": [],
                "flow_prints": list(oflow.get("prints") or [])[:40],
                "mrna_note": "Run Scan focus or Scan catalyst so MRNA-class names are scored.",
            }

        return jsonify(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "offline": offline,
                "host": "github-pages" if offline else "live",
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
                "ml6": ml6,
                "red_flag": red_flag_snapshot,
                "free_dealer": free_dealer,
                "radar": radar,
                "chase_radar": chase_radar,
                "echo": echo,
                "challenge": challenge,
                "odte_1k": odte_1k,
                "power_hour": power_hour,
                "zeroloss": zeroloss,
                "market": market,
                "walls_by_symbol": walls_by_symbol,
                "watch": {"quotes": quotes},
                "ledger": ledger,
                "actions": actions,
                "hist_win_gate": actions.get("hist_win_gate"),
                "insights": insights,
                "journal_sync": journal_sync,
                "win_rates": win_table,
                "rec_log": rec_log_payload,
                "webull": webull_payload,
            }
        )

    def _challenge_tracker():
        from odte_scanner.challenge.tracker import ChallengeTracker

        ch_path = Path(actions_cfg.get("challenge_ledger_path", "outputs/challenge_ledger.json"))
        if not ch_path.is_absolute():
            ch_path = ROOT / ch_path
        return ChallengeTracker(
            ch_path,
            starting_cash=float(actions_cfg.get("challenge_start_usd", 1000)),
        )

    @app.post("/api/challenge/enter")
    def challenge_enter():
        """Paper-enter a challenge ticket by symbol/right (uses latest snapshot board)."""
        from odte_scanner.challenge import build_challenge_board
        from odte_scanner.calendars import resolve_yahoo_symbol
        from odte_scanner.data.live_quotes import fetch_live_quote

        body = request.get_json(silent=True) or {}
        symbol = str(body.get("symbol") or "").upper()
        right = str(body.get("right") or "C").upper()
        if not symbol:
            return jsonify({"ok": False, "error": "symbol required"}), 400

        tracker = _challenge_tracker()
        if tracker.open_trades() and len(tracker.open_trades()) >= int(actions_cfg.get("challenge_max_open", 1)):
            return jsonify({"ok": False, "error": "sleeve already has an open flip — EXIT first"}), 409

        scan = _read_json(ROOT / "outputs" / "latest_scan.json") or {}
        win_table = scan.get("win_rates") or load_win_rate_table() or {}
        alias = resolve_yahoo_symbol(symbol, cfg)
        q = fetch_live_quote(symbol, yahoo_symbol=alias)
        quotes = {symbol: q.to_dict()} if q else {}
        board = build_challenge_board(
            win_table=win_table if isinstance(win_table, dict) else None,
            scores=scan.get("scores") or [],
            quotes=quotes,
            aliases={symbol: alias},
            open_trades=[t.to_dict() for t in tracker.book.trades],
            start_usd=float(actions_cfg.get("challenge_start_usd", 1000)),
            target_usd=float(actions_cfg.get("challenge_target_usd", 1_000_000)),
            flips=int(actions_cfg.get("challenge_flips", 12)),
            max_tickets=int(actions_cfg.get("challenge_max_tickets", 8)),
            fetch_contracts=True,
            fetch_earnings=False,
        )
        ticket = next(
            (
                t
                for t in (board.get("tickets") or [])
                if t.get("symbol") == symbol and str(t.get("right") or "C").upper() == right
            ),
            None,
        )
        if not ticket:
            return jsonify({"ok": False, "error": f"no challenge ticket for {symbol} {right}"}), 404
        # Force ENTRY eligibility for explicit paper enter
        ticket = dict(ticket)
        ticket["action"] = "ENTRY"
        if not ticket.get("ask") or not ticket.get("contract"):
            return jsonify(
                {
                    "ok": False,
                    "error": "need live option ask + contract before ENTER",
                    "ticket": {
                        "symbol": symbol,
                        "strike": ticket.get("strike"),
                        "expiry": ticket.get("expiry"),
                        "ask": ticket.get("ask"),
                        "contract": ticket.get("contract"),
                    },
                }
            ), 409
        trade = tracker.enter(ticket, max_open=int(actions_cfg.get("challenge_max_open", 1)))
        if not trade:
            return jsonify({"ok": False, "error": "enter rejected (cash/contract/open limit)"}), 409
        return jsonify({"ok": True, "trade": trade.to_dict(), "book": tracker.book.to_dict()})

    @app.post("/api/challenge/exit")
    def challenge_exit():
        body = request.get_json(silent=True) or {}
        trade_id = str(body.get("trade_id") or "")
        tracker = _challenge_tracker()
        trade = next((t for t in tracker.open_trades() if t.id == trade_id), None)
        if not trade:
            return jsonify({"ok": False, "error": "open trade not found"}), 404
        mark = float(body.get("exit_bid") or trade.mark or trade.entry_ask or 0)
        # Refresh mark from live chain when possible
        try:
            from odte_scanner.options.yahoo_session import pick_challenge_contract

            if trade.expiry and trade.strike is not None:
                live = pick_challenge_contract(
                    trade.symbol,
                    float(trade.entry_spot or 0) or 1.0,
                    right=trade.right,
                    min_dte=max(1, int(trade.dte_at_entry or 30) - 30),
                    max_dte=int(trade.dte_at_entry or 200) + 60,
                    prefer_dte=int(trade.dte_at_entry or 120),
                )
                if live and live.get("ask"):
                    # Prefer matching strike
                    if abs(float(live.get("strike") or 0) - float(trade.strike)) < 0.02:
                        mark = float(live.get("bid") or live.get("last") or live.get("ask") or mark)
        except Exception:  # noqa: BLE001
            pass
        reason = body.get("reason") or trade.last_action_detail or "Manual paper EXIT"
        out = tracker.exit_trade(trade_id, exit_bid=mark, reason=str(reason))
        if not out:
            return jsonify({"ok": False, "error": "exit failed"}), 409
        return jsonify({"ok": True, "trade": out.to_dict(), "book": tracker.book.to_dict()})

    def _odte1k_tracker():
        from odte_scanner.challenge.odte_1k_tracker import Odte1kTracker

        path = Path(actions_cfg.get("odte_1k_ledger_path", "outputs/odte_1k_ledger.json"))
        if not path.is_absolute():
            path = ROOT / path
        return Odte1kTracker(
            path,
            starting_cash=float(actions_cfg.get("odte_1k_start_usd", 1000)),
            max_trades_per_day=int(actions_cfg.get("odte_1k_max_trades_per_day", 2)),
            default_size_usd=float(actions_cfg.get("odte_1k_position_size_usd", 850)),
        )

    @app.post("/api/odte1k/enter")
    def odte1k_enter():
        from odte_scanner.challenge.odte_1k import build_odte_1k_board

        body = request.get_json(silent=True) or {}
        symbol = str(body.get("symbol") or "SPY").upper()
        tracker = _odte1k_tracker()
        alias = resolve_yahoo_symbol(symbol, cfg)
        q = fetch_live_quote(symbol, yahoo_symbol=alias)
        quotes = {symbol: q.to_dict()} if q else {}
        board = build_odte_1k_board(
            quotes=quotes,
            symbols=[symbol],
            open_trades=[t.to_dict() for t in tracker.book.trades],
            book=tracker.book.to_dict(),
            starting_cash=float(actions_cfg.get("odte_1k_start_usd", 1000)),
            position_size_usd=float(actions_cfg.get("odte_1k_position_size_usd", 850)),
            position_pct=float(actions_cfg.get("odte_1k_position_pct", 0.85)),
            max_trades_per_day=int(actions_cfg.get("odte_1k_max_trades_per_day", 2)),
            fetch_bars=True,
            fetch_contracts=True,
            flatten_et=str(actions_cfg.get("odte_flatten_et", "15:45")),
            aliases={symbol: alias},
        )
        sig = next(
            (s for s in (board.get("put_now") or []) if str(s.get("symbol")) == symbol),
            board.get("primary"),
        )
        if not sig or sig.get("action") != "PUT_NOW":
            return jsonify({"ok": False, "error": "no PUT NOW signal for " + symbol, "board": board}), 409
        if not sig.get("ask"):
            return jsonify({"ok": False, "error": "need live put ask before ENTER", "signal": sig}), 409
        trade = tracker.enter(sig)
        if not trade:
            return jsonify({"ok": False, "error": "enter rejected (day cap / cash / size)"}), 409
        return jsonify({"ok": True, "trade": trade.to_dict(), "book": tracker.book.to_dict()})

    @app.post("/api/odte1k/exit")
    def odte1k_exit():
        body = request.get_json(silent=True) or {}
        trade_id = str(body.get("trade_id") or "")
        tracker = _odte1k_tracker()
        trade = next((t for t in tracker.open_trades() if t.id == trade_id), None)
        if not trade:
            return jsonify({"ok": False, "error": "open trade not found"}), 404
        mark = float(body.get("exit_bid") or trade.mark or trade.entry_ask or 0)
        out = tracker.exit_trade(trade_id, exit_bid=mark, reason=str(body.get("reason") or "Manual paper EXIT"))
        if not out:
            return jsonify({"ok": False, "error": "exit failed"}), 409
        return jsonify({"ok": True, "trade": out.to_dict(), "book": tracker.book.to_dict()})

    @app.get("/api/scan_status")
    def scan_status():
        latest = ROOT / "outputs" / "latest_scan.json"
        has_scan = latest.exists() and latest.stat().st_size > 50
        mtime = latest.stat().st_mtime if has_scan else None
        running = scan_lock.locked()
        return jsonify(
            {
                "running": running,
                "has_scan": has_scan,
                "ready": has_scan and not running,
                "latest_mtime": mtime,
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
                if mode == "ml6":
                    cmd.extend(["--horizon", "ml6"])
                elif mode in ("liquid", "screener", "all", "catalyst", "zeroloss"):
                    cmd.extend(["--universe", mode])
                subprocess.run(cmd, cwd=str(ROOT), check=False)
            finally:
                scan_lock.release()

        threading.Thread(target=_job, daemon=True).start()
        return jsonify({"ok": True, "started": True, "mode": mode})

    def _webull_bundle():
        from odte_scanner.trading.auto_trader import AutoTrader
        from odte_scanner.trading.webull import WebullBroker

        lt = cfg.get("live_trading") or {}
        ledger = Path(lt.get("ledger_path", "outputs/webull_orders.json"))
        if not ledger.is_absolute():
            ledger = ROOT / ledger
        broker = WebullBroker(
            enabled=bool(lt.get("enabled", False)),
            dry_run=bool(lt.get("dry_run", True)),
            region=str(lt.get("region") or "us"),
            sandbox=bool(lt.get("sandbox", True)),
            account_id=lt.get("account_id"),
            app_key=lt.get("app_key"),
            app_secret=lt.get("app_secret"),
            ledger_path=ledger,
        )
        desk_cfg = dict(lt.get("desks") or {})
        if not desk_cfg:
            desk_cfg = {
                "lottery": True,
                "odte": True,
                "weekly": True,
                "swing": True,
                "challenge": True,
            }
        trader = AutoTrader(
            broker,
            require_perfect_hist=bool(lt.get("require_perfect_hist", True)),
            min_hist_win_pct=float(lt.get("min_hist_win_pct", 100)),
            min_hist_win_samples=int(lt.get("min_hist_win_samples", 3)),
            desks=desk_cfg,
            max_contracts=int(lt.get("max_contracts", 1)),
            max_orders_per_sync=int(lt.get("max_orders_per_sync", 3)),
        )
        return broker, trader

    def _webull_status_payload() -> dict:
        try:
            broker, trader = _webull_bundle()
            st = broker.status()
            act = broker.activity(40)
            lt = cfg.get("live_trading") or {}
            return {
                "status": st,
                "broker": st,
                "auto_sync": bool(lt.get("auto_sync", True)),
                "require_perfect_hist": trader.require_perfect_hist,
                "min_hist_win_pct": trader.min_hist_win_pct,
                "min_hist_win_samples": trader.min_hist_win_samples,
                "desks": trader.desks,
                "recent": broker.recent(40),
                "activity": act,
                "how_to_verify": act.get("how_to_verify") or [],
                "submitted_n": 0,
                "skipped_n": 0,
                "disclaimer": st.get("disclaimer"),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("webull status failed: %s", exc)
            return {"error": str(exc), "status": {"enabled": False}, "recent": [], "activity": {}}

    def _run_webull_sync(
        *,
        actions: dict | None = None,
        lottery: dict | None = None,
        challenge: dict | None = None,
    ) -> dict:
        """Route current boards to Webull ledger (preview/dry-run/live)."""
        import copy

        broker, trader = _webull_bundle()
        scan = _read_json(ROOT / "outputs" / "latest_scan.json") or {}
        snap_cache = _read_json(ROOT / "outputs" / "ui_snapshot_cache.json") or {}
        if not isinstance(snap_cache, dict):
            snap_cache = {}
        # Copy — never mutate the live action board (was stuffing raw candidates into buy_now)
        actions = copy.deepcopy(actions if isinstance(actions, dict) else snap_cache.get("actions"))
        lottery = copy.deepcopy(lottery if isinstance(lottery, dict) else snap_cache.get("lottery"))
        challenge = copy.deepcopy(
            challenge if isinstance(challenge, dict) else snap_cache.get("challenge")
        )
        if not actions:
            actions = {"buy_now": [], "sell_now": []}
        if not lottery:
            lottery = {"buy_now": [], "sell_now": []}
        if not challenge:
            challenge = {"entry": [], "exit": [], "tickets": []}
        try:
            from odte_scanner.backtest.win_rates import load_win_rate_table, lookup_win_stats

            wr = scan.get("win_rates") or load_win_rate_table() or {}
            # Only enrich a *private* list for Webull when desk has no actionable BUY/SELL
            wb_actions = copy.deepcopy(actions)
            if not (wb_actions.get("buy_now") or wb_actions.get("sell_now")):
                for c in (scan.get("call_candidates_0dte") or [])[:8]:
                    stats = lookup_win_stats(wr, c.get("symbol"), "0dte")
                    wb_actions.setdefault("buy_now", []).append(
                        {
                            **c,
                            "action": "BUY_NOW",
                            "dte_bucket": "0dte",
                            "hist_win_pct": stats.get("win_pct"),
                            "hist_samples": stats.get("trades"),
                            "win_pct": stats.get("win_pct"),
                            "win_samples": stats.get("trades"),
                            "headline": f"SCAN {c.get('symbol')} call",
                            "detail": "Webull enrich from scan candidate (not desk BUY NOW)",
                        }
                    )
                for c in (scan.get("put_candidates_0dte") or [])[:4]:
                    stats = lookup_win_stats(wr, c.get("symbol"), "0dte")
                    wb_actions.setdefault("buy_now", []).append(
                        {
                            **c,
                            "action": "BUY_NOW",
                            "dte_bucket": "0dte",
                            "right": "P",
                            "hist_win_pct": stats.get("win_pct"),
                            "hist_samples": stats.get("trades"),
                            "win_pct": stats.get("win_pct"),
                            "win_samples": stats.get("trades"),
                            "headline": f"SCAN {c.get('symbol')} put",
                            "detail": "Webull enrich from scan candidate (not desk BUY NOW)",
                        }
                    )
            else:
                wb_actions = actions
        except Exception as exc:  # noqa: BLE001
            logger.debug("webull sync enrich failed: %s", exc)
            wb_actions = actions

        out = trader.sync(actions=wb_actions, lottery=lottery, challenge=challenge)
        lt = cfg.get("live_trading") or {}
        out["auto_sync"] = bool(lt.get("auto_sync", True))
        out["status"] = out.get("broker") or out.get("status")
        return out

    @app.get("/api/webull/status")
    def webull_status():
        return jsonify(_webull_status_payload())

    @app.post("/api/webull/sync")
    def webull_sync():
        """Route current lottery / actions / challenge tickets to Webull (dry-run by default)."""
        return jsonify(_run_webull_sync())

    return app


def run_ui(host: str = "0.0.0.0", port: int = 8787, config_path: str | None = None) -> None:
    app = create_app(config_path)
    logger.info("ZeroLoss UI at http://%s:%s", host if host != "0.0.0.0" else "127.0.0.1", port)
    app.run(host=host, port=port, debug=False, use_reloader=False)
