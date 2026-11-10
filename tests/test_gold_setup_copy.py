"""Customers see "Gold Setup", never "Sure Winner". Internal identifiers may stay."""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from odte_scanner.signals.sure_winners import build_sure_winner_board
from odte_scanner.ui import PAGE

BANNED = re.compile(r"sure[\s_-]*winners?", re.I)


def _script(page: str) -> str:
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", page, re.S))


def test_page_markup_has_no_sure_winner_copy():
    markup = re.sub(r"<script[^>]*>.*?</script>", "", PAGE, flags=re.S)
    markup = re.sub(r"<style[^>]*>.*?</style>", "", markup, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", markup)  # class names like sure-max are not copy
    assert not BANNED.search(text), BANNED.search(text)
    assert "Gold Setup" in text


def test_page_script_strings_only_use_sure_winner_as_an_identifier():
    js = _script(PAGE)
    literals = re.findall(r"`[^`]*`|\"[^\"\n]*\"|'[^'\n]*'", js)
    hits = [s for s in literals if BANNED.search(s)]
    # Every remaining literal is exactly the internal desk key, mapped by deskLabel() on render.
    # "SURE_WINNER" is the action code; the card label never prints it.
    assert hits and all(s.strip("\"'") in {"Sure Winner", "SURE_WINNER"} for s in hits), hits
    assert "function deskLabel(" in js
    for raw in ("${row.desk}", "${sl.desk}", "${r._desk}", "${o.desk", "<div class=\"now-desk\">${desk}"):
        assert raw not in js, raw
    assert "Gold Setup <em>" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_desk_label_maps_the_internal_key():
    js = _script(PAGE)
    fn = re.search(r"function deskLabel\(d\) \{.*?\n    \}", js, re.S).group(0)
    probe = fn + """
const out = ["Sure Winner", "sure_winner", "SURE-WINNERS", "0DTE", null, "Gold Setup"].map(deskLabel);
console.log(JSON.stringify(out));
"""
    res = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30, check=True)
    assert res.stdout.strip() == '["Gold Setup","Gold Setup","Gold Setup","0DTE","","Gold Setup"]'


def _buy(symbol):
    return {
        "action": "BUY_NOW", "symbol": symbol, "right": "C", "strike": 100, "expiry": "2026-11-13",
        "dte": 4, "ask": 1.0, "contract": f"{symbol}261113C00100000", "hist_win_pct": 100,
        "hist_samples": 5, "win_pct": 100, "win_samples": 5, "ensemble_score": 82,
        "certainty_tier": "perfect", "hit_1pct": 80,
    }


def test_board_copy_says_gold_setup(tmp_path):
    empty = build_sure_winner_board(actions={"buy_now": []}, challenge={"entry": []}, store_path=tmp_path / "a.json")
    assert "Gold Setup" in empty["note"] and not BANNED.search(empty["note"])
    board = build_sure_winner_board(actions={"buy_now": [_buy("GOLD")]}, challenge={"entry": []},
                                    store_path=tmp_path / "b.json")
    assert board["winners"], board["counts"]
    detail = str(board["winners"][0].get("detail") or "")
    assert detail.startswith("Gold Setup #") and not BANNED.search(detail), detail
    assert not BANNED.search(str(board["winners"][0].get("headline") or ""))
