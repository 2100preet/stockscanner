"""Headline classifier for the 📰 News Desk.

Keyword rules on the headline, 8-K item codes, and the Nasdaq EPS surprise.
Built from the Nov 2026 research backtest (/workspace/news-research). Rules,
not a model, so every tag can be traced to a phrase.

False-positive fixes carried over from the research:
* "CRL" only counts as an FDA rejection when written upper-case next to "FDA"
  (Charles River's ticker and lower-case filenames like ``crl-20260924``
  used to match).
* Earnings releases are classified as earnings first, so a quarterly release
  that mentions an FDA approval in its bullets is not an "FDA approval".
* Routine 6-K notices (transaction in own shares, buyback programme progress,
  voting rights, PDMR dealings) are ignored.
* M&A target vs acquirer is decided by where the filer's own name sits
  relative to "acquire"/"acquisition" in the headline.
* Law-firm "shareholder alert" / class-action solicitations are ignored.
"""

from __future__ import annotations

import re
from typing import Any

# Event types that may be alerted. Everything else is recorded at most.
ALERT_TYPES = {
    "EARN_BEAT",
    "EARN_MISS",
    "EARN_RAISE",
    "EARN_CUT",
    "EARN_GAP_DOWN",
    "EARN_GAP_UP",
    "GUIDE_RAISE",
    "GUIDE_CUT",
    "FDA_APPROVAL",
    "FDA_CRL",
    "TRIAL_FAIL",
    "TRIAL_DATA",
    "MA_TARGET",
    "MA_ACQUIRER",
    "OFFERING",
    "CYBER",
    "RESTATEMENT",
    "BANKRUPTCY",
    "EXEC_CHANGE",
}

LABELS = {
    "EARN_BEAT": "Earnings beat",
    "EARN_MISS": "Earnings miss",
    "EARN_RAISE": "Earnings + raised guidance",
    "EARN_CUT": "Earnings + cut guidance",
    "EARN_INLINE": "Earnings in line",
    "EARN_PENDING": "Earnings (EPS surprise pending)",
    "EARN_GAP_DOWN": "Earnings · trading down",
    "EARN_GAP_UP": "Earnings · trading up",
    "EARN_OUT": "Earnings out · little move",
    "GUIDE_RAISE": "Guidance raised",
    "GUIDE_CUT": "Guidance cut",
    "FDA_APPROVAL": "FDA approval",
    "FDA_CRL": "FDA rejection (CRL)",
    "FDA_OTHER": "FDA update",
    "TRIAL_FAIL": "Trial failure",
    "TRIAL_DATA": "Trial data (positive wording)",
    "MA_TARGET": "M&A target",
    "MA_ACQUIRER": "M&A acquirer",
    "MA_CLOSED": "Deal closed",
    "OFFERING": "Stock / convertible offering",
    "OFFERING_CLOSED": "Offering closed",
    "DEBT": "Debt offering",
    "CYBER": "Cybersecurity incident",
    "RESTATEMENT": "Restatement (non-reliance)",
    "BANKRUPTCY": "Bankruptcy",
    "EXEC_CHANGE": "Executive change",
    "BUYBACK": "Buyback",
    "DEBT_TENDER": "Debt tender / redemption",
    "ANALYST": "Analyst rating / price target",
    "OPINION": "Opinion / listicle",
    "DIVIDEND": "Dividend",
    "ROUTINE": "Routine notice",
    "LAWFIRM": "Law-firm solicitation",
    "OTHER": "Other",
}

# Playbook notes from the backtest. They describe what to watch, never an order.
NOTES = {
    "EARN_BEAT": "beat — gap-ups were a coin flip after the open; don't chase",
    "EARN_RAISE": "raised guidance — the move is usually in the gap; don't chase",
    "EARN_MISS": "miss + gap down — watch 9:45 for put setup",
    "EARN_CUT": "cut guidance + gap down — watch 9:45 for put setup",
    "EARN_GAP_DOWN": "earnings + gap down — watch 9:45 for put setup",
    "EARN_GAP_UP": "earnings gap-ups were a coin flip after the open; don't chase",
    "GUIDE_RAISE": "raised guidance — move usually in the gap; don't chase",
    "GUIDE_CUT": "guidance cut — watch 9:45 for put setup",
    "FDA_APPROVAL": "approval — move usually done by the open; don't chase",
    "FDA_CRL": "FDA rejection — gap usually done by the open; don't chase",
    "TRIAL_FAIL": "trial failure — gap usually done by the open; don't chase",
    "TRIAL_DATA": "fade watch — don't chase (positive trial headlines faded in the backtest)",
    "MA_TARGET": "target pinned near deal price — don't chase",
    "MA_ACQUIRER": "acquirer often dips on big deals — watch, don't chase",
    "OFFERING": "puts watch — dilution; wait for 9:45 confirmation",
    "CYBER": "negative headline — watch, don't chase",
    "RESTATEMENT": "accounting restatement — negative; watch, don't chase",
    "BANKRUPTCY": "bankruptcy filing — avoid",
    "EXEC_CHANGE": "CEO/CFO change — context only",
    "DEBT_TENDER": "debt buyback — context only, not a catalyst",
    "ANALYST": "analyst action — context only",
}

BEAT_PCT = 3.0
MISS_PCT = -3.0
# Extended-hours move that counts as a reaction when no EPS surprise is published yet.
MOVE_PCT = 2.0


def earnings_by_move(move_pct: float | None) -> str:
    """Earnings release with no EPS surprise yet: tag it by the extended-hours reaction."""
    if move_pct is None:
        return "EARN_OUT"
    if move_pct <= -MOVE_PCT:
        return "EARN_GAP_DOWN"
    if move_pct >= MOVE_PCT:
        return "EARN_GAP_UP"
    return "EARN_OUT"


def tag(kind: str) -> dict[str, Any]:
    """Type -> the same dict shape classify() returns."""
    direction = -1 if kind in {"EARN_GAP_DOWN", "EARN_MISS", "EARN_CUT", "GUIDE_CUT"} else (
        1 if kind in {"EARN_GAP_UP", "EARN_BEAT", "EARN_RAISE", "GUIDE_RAISE"} else 0)
    return {"type": kind, "direction": direction, "label": LABELS.get(kind, kind),
            "note": NOTES.get(kind), "alert": kind in ALERT_TYPES}

_STOP = {
    "the", "inc", "corp", "corporation", "company", "co", "holdings", "holding", "group",
    "international", "american", "first", "new", "global", "energy", "bank", "united",
    "national", "general", "pharmaceuticals", "therapeutics", "technologies", "plc", "ltd",
    "limited", "sa", "nv", "ag", "se", "lp", "llc", "trust", "the", "us", "usa",
}


def _has(text: str, *patterns: str, flags: int = re.I) -> bool:
    return any(re.search(p, text, flags) for p in patterns)


_BOILER = re.compile(
    r"(exhibit\s*99[\.\d]*|news release|press release|for immediate release|ex-99[\.\d]*)\s+",
    re.I,
)


_HEAD_VERB = re.compile(
    r"\b(reports?|announces?|provides|posts|delivers|enters|completes?|declares?|appoints|names|"
    r"receives?|launches|agrees?|to acquire|to be acquired|acquires|prices|updates?|raises|lowers|"
    r"withdraws|achieves|unveils|signs|files|grants?)\b",
    re.I,
)
_HEAD_CUT = re.compile(r"(\b\d{4}\b|\bDocument\b|\)|:|—|–|\||\bContact\b[^A-Z]*)\s*", re.I)


def headline_of(text: str, limit: int = 350, company: str | None = None) -> str:
    """Strip exhibit boilerplate (addresses, contacts, dates, file names) and return the headline window."""
    window = re.sub(r"\s+", " ", (text or "")[:2000]).strip()
    matches = list(_BOILER.finditer(window[:700]))
    if matches:
        window = window[matches[-1].end():]
    verb = _HEAD_VERB.search(window[:800])
    if verb and verb.start() > 0:
        start = None
        token = name_token(company)
        if token:
            pos = window.lower().rfind(token.lower(), max(0, verb.start() - 120), verb.start())
            if pos >= 0:
                start = pos
                joined = re.search(r"\b\w+\s*(&|and)\s*$", window[max(0, pos - 25):pos])
                if joined:  # "Johnson & Johnson reports" starts at the first Johnson
                    start = max(0, pos - 25) + joined.start()
        if start is None:
            lead = window[max(0, verb.start() - 100): verb.start()]
            cuts = list(_HEAD_CUT.finditer(lead))
            start = max(0, verb.start() - 100) + (cuts[-1].end() if cuts else 0)
            if start > 0 and verb.start() - start > 100:
                start = verb.start()
        window = window[start:]
    return window.strip()[:limit]


_SUBHEAD = re.compile(
    r"\s[•▪●◦■]\s|/PRNewswire/|\(BUSINESS WIRE\)|\(GLOBE NEWSWIRE\)|--\(|\s--\s|"
    r"\s[A-Z][A-Z.&' ]{2,40}(,\s?[A-Z][A-Za-z.]{1,15})*,?\s(\(|[A-Z][a-z]{2,8}\.?\s\d{1,2},\s20\d\d)"
)


def display_headline(headline: str, limit: int = 180) -> str:
    """The title alone: cut at the first sub-head bullet or dateline, then at a word boundary."""
    text = re.sub(r"\s+", " ", headline or "").strip()
    cut = _SUBHEAD.search(text, 20)
    if cut:
        text = text[: cut.start()].rstrip(" ,;:-—")
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def name_token(name: str | None) -> str | None:
    """First distinctive word of a company name ("Ovintiv Inc." -> "Ovintiv")."""
    words = re.findall(r"[A-Za-z][A-Za-z\-&']+", name or "")
    for word in words:
        if word.lower() not in _STOP and len(word) >= 3:
            return word
    return None


def _items(raw: Any) -> set[str]:
    if isinstance(raw, (list, tuple, set)):
        parts = [str(x) for x in raw]
    else:
        parts = re.split(r"[,\s]+", str(raw or ""))
    return {p.strip() for p in parts if re.fullmatch(r"\d\.\d\d", p.strip())}


_GUIDE_FILL = r"(its |the |our |full[- ]year |fiscal |fy\s?20\d\d |annual |20\d\d |q[1-4] |revenue |sales |eps |earnings |financial |adjusted |non-gaap )*"
_GUIDE_UP = (
    r"\b(raises|raised|raising|increases|increased|increasing|lifts|lifted|boosts|boosted|upgrades|upgraded) " + _GUIDE_FILL + r"(guidance|outlook|forecast)\b",
    r"\b(guidance|outlook|forecast) (is |was |has been )?(raised|increased|upgraded)\b",
)
_GUIDE_DOWN = (
    r"\b(lowers|lowered|lowering|reduces|reduced|reducing|cuts|slashes|slashed|withdraws|withdrew|withdrawn|withdrawing|suspends|suspended|suspending) " + _GUIDE_FILL + r"(guidance|outlook|forecast)\b",
    r"\b(guidance|outlook|forecast) (is |was |has been )?(lowered|reduced|cut|withdrawn|suspended)\b",
)


# A guidance change on a cost line (capex, interest, tax) is not a business outlook change.
_COST_LINE = re.compile(
    r"(capital (investments?|expenditures?|spending)|capex|interest expense|tax rate|share count|"
    r"expenses?|costs?|depreciation|amortization|dilution)\W+(\w+\W+){0,4}$",
    re.I,
)


def _guide_hit(window: str, patterns: tuple[str, ...]) -> bool:
    for pat in patterns:
        for m in re.finditer(pat, window, re.I):
            before = window[max(0, m.start() - 60): m.start()]
            inside = m.group(0)
            if _COST_LINE.search(before) or re.search(r"capex|capital (investment|expenditure)|expense|cost", inside, re.I):
                continue
            return True
    return False


def _ma_role(headline: str, company: str | None) -> str | None:
    """'MA_TARGET', 'MA_ACQUIRER', or None when the headline is not a deal."""
    # Blank-check names ("Haymaker Acquisition Corp V") are not deals.
    h1 = re.sub(r"\b(acquisition|merger)s? (corp(oration)?|co|company|holdings?|ltd|limited|inc)\b\.?", "", headline[:220], flags=re.I)
    if _has(h1, r"priority review voucher", r"\bPRV\b"):
        return None  # buying a regulatory voucher is not a company deal
    if _has(h1, r"long stop date", r"outside date", r"extend\w* (the )?(tender|exchange) offer", r"expiration (date )?of (the )?(tender|exchange) offer"):
        return None  # deal housekeeping, not news
    if _has(h1, r"separate trading", r"\bunits?\b.{0,40}\b(begin|commence) trading", r"pricing of .{0,40}initial public offering"):
        return None
    if not _has(h1, r"acqui", r"merger", r"business combination", r"\bagree[sd]? to buy\b", r"\bdeal to buy\b", r"take[- ]private", r"go[- ]private", r"buyout"):
        return None
    if _has(h1, r"\b(complet\w+|clos(es|ed|ing)|consummat\w+)\b.{0,40}(acquisition|merger|sale|transaction)",
            r"(stockholders|shareholders) (vote|approve|approval)", r"approv\w+ by (stockholders|shareholders)"):
        return "MA_CLOSED"
    if _has(h1, r"to be acquired", r"agree[sd]? to be acquired", r"will be acquired", r"to go private",
            r"take[- ]private", r"stockholders will receive \$", r"\$[\d.]+ per share in (an all-)?cash"):
        return "MA_TARGET"
    token = name_token(company)
    verb = re.search(r"acqui\w*|merger|business combination|\bagree[sd]? to buy\b|\bdeal to buy\b|buyout", h1, re.I)
    if token and verb:
        pos = h1.lower().find(token.lower())
        if 0 <= pos < verb.start():
            return "MA_ACQUIRER"
        if pos > verb.start() and re.search(r"(acqui\w*|agree[sd]? to buy|deal to buy|buyout) (of )?.{0,60}" + re.escape(token), h1, re.I):
            return "MA_TARGET"
    if _has(h1, r"(definitive agreement|agreement|agree[sd]?) to (acquire|merge|combine)",
            r"announces? (the )?(strategic )?acquisition of", r"\bto acquire\b"):
        return "MA_ACQUIRER"
    return None


def classify(
    *,
    headline: str,
    text: str = "",
    items: Any = None,
    form: str = "",
    company: str | None = None,
    eps_surprise_pct: float | None = None,
    subjects: Any = None,
) -> dict[str, Any]:
    """Tag one news item. Returns type, direction (+1/-1/0), label, note, alert flag."""
    item_set = _items(items)
    h = re.sub(r"\s+", " ", headline or "").strip()
    h_short = h[:160]  # the title proper; h also carries the sub-heads
    body = re.sub(r"\s+", " ", text or "")[:4000]
    subj = {str(s).strip().upper() for s in (subjects or []) if s}
    kind = "OTHER"
    direction = 0

    def done(k: str, d: int) -> dict[str, Any]:
        return {
            "type": k,
            "direction": d,
            "label": LABELS.get(k, k),
            "note": NOTES.get(k),
            "alert": k in ALERT_TYPES,
        }

    # --- noise first
    # Opinion pieces and listicles from news aggregators ("X vs Y: Which Stock to Buy?").
    filed_results = "2.02" in item_set or "ERN" in subj
    if not filed_results and _has(h_short, r"\b(vs\.?|versus)\s[^:]{1,60}[:?]", r"\bstocks? to (buy|sell|own|watch)\b", r"\bbetter (stock|buy)\b",
            r"\bshould you (buy|sell)\b", r"\btime to (buy|sell)\b", r"(not too late|reasons?) to buy",
            r"here'?s why", r"\bbuy the (stock|dip)\b", r"\bis it (a )?(buy|sell)\b", r"\btop \d+ stocks?\b",
            r"millionaire", r"\bgrowth stocks?\b", r"\bdividend stocks?\b", r"\?\s*$"):
        return done("OPINION", 0)
    if not filed_results and not _has(h_short, r"guidance|outlook|forecast") and _has(
            h_short, r"\banalyst (upgrade|downgrade|rating|action)s?\b", r"\b(upgrade[sd]?|downgrade[sd]?) (to|by|from|at)\b", r"initiates? coverage",
            r"price target", r"\b(outperform|underperform|overweight|underweight|buy|sell|neutral) rating"):
        return done("ANALYST", 0)
    if "ATY" in subj or _has(h, r"shareholder alert", r"investor alert", r"investor notice", r"deadline alert",
                            r"class action", r"securities fraud", r"law firm", r"levi & korsinsky", r"rosen law",
                            r"pomerantz", r"bronstein", r"faruqi", r"glancy", r"robbins geller", r"kessler topaz",
                            r"investors (who|with) .{0,40}losses", r"lead plaintiff"):
        return done("LAWFIRM", 0)
    if str(form).upper().startswith("6-K") and _has(h, r"transaction in own shares", r"share buyback programme",
                                                     r"changes in issued shares", r"director.{0,30}dealing",
                                                     r"pdmr", r"total voting rights", r"form 8\.3",
                                                     r"next day disclosure", r"monthly return of equity",
                                                     r"progress on share buyback"):
        return done("ROUTINE", 0)
    # --- hard 8-K items
    if "1.03" in item_set or _has(h, r"chapter 11", r"voluntary petition"):
        return done("BANKRUPTCY", -1)
    if "4.02" in item_set or _has(h, r"non-reliance on previously issued", r"\brestat(e|es|ement)\b.{0,40}financial"):
        return done("RESTATEMENT", -1)
    if "1.05" in item_set or _has(h, r"cybersecurity incident", r"ransomware", r"data breach"):
        return done("CYBER", -1)
    # --- earnings before anything else (an earnings release can mention FDA news)
    earnings = "2.02" in item_set or "ERN" in subj or _has(
        h,
        r"(first|second|third|fourth|1st|2nd|3rd|4th)[- ]quarter.{0,60}(results|earnings)",
        r"\b(q[1-4]|quarterly|fiscal|full[- ]year)\b.{0,40}(results|earnings)",
        r"financial results",
        r"reports? .{0,50}results",
    )
    if earnings and not _has(h, r"to (report|announce|release|host).{0,40}(results|earnings)",
                             r"(conference call|webcast) (to discuss|for)", r"date for .{0,30}results",
                             r"schedules? .{0,40}(results|earnings)"):
        window = (h + " " + body[:2500])
        up = _guide_hit(window, _GUIDE_UP)
        down = _guide_hit(window, _GUIDE_DOWN)
        if down and _has(window, r"pendency of", r"pending (merger|acquisition|transaction)"):
            down = False  # guidance pulled because of a pending deal, not a business cut
        if up and down:
            up = down = False  # mixed wording: fall back to the EPS surprise
        if down:
            return done("EARN_CUT", -1)
        if up:
            return done("EARN_RAISE", +1)
        if eps_surprise_pct is None:
            return done("EARN_PENDING", 0)
        if eps_surprise_pct >= BEAT_PCT:
            return done("EARN_BEAT", +1)
        if eps_surprise_pct <= MISS_PCT:
            return done("EARN_MISS", -1)
        return done("EARN_INLINE", 0)
    # --- FDA / clinical
    if _has(h, r"complete response letter", r"refusal to file", r"refuse to file") or (
        re.search(r"\bCRL\b", h) and re.search(r"\bFDA\b", h)
    ):
        return done("FDA_CRL", -1)
    if _has(h, r"did not (meet|achieve)", r"failed to (meet|achieve)", r"not statistically significant",
            r"discontinu\w+ (the )?(development|trial|program|study)", r"clinical hold", r"futility"):
        return done("TRIAL_FAIL", -1)
    if _has(h_short, r"(stockholders|shareholders) (vote|approve)", r"approv\w* by (stockholders|shareholders)"):
        return done("MA_CLOSED", 0)  # a shareholder vote, not a drug approval
    approval = _has(
        h_short,
        r"\bFDA\b.{0,40}\bapprov",
        r"approv\w*.{0,40}\b(FDA|food and drug administration)\b",
        r"european commission.{0,30}approv",
        r"(receives?|granted|grants|wins) (full |accelerated |traditional )?(fda |u\.s\. )?approval",
        r"\bapproves?\b",
    )
    if approval and _has(h_short, r"\bFDA\b|food and drug|european commission|\bEMA\b|health canada|\bMHRA\b|\bPMDA\b"):
        if _has(h_short, r"(seek|potential|path(way)? to|submi\w+|file for|filing for|expected|anticipat|pending|accept\w*.{0,30}(review|filing)|pdufa|priority review|breakthrough|fast track|orphan)"):
            return done("FDA_OTHER", 0)
        return done("FDA_APPROVAL", +1)
    if _has(h, r"(met|meets|achieved|achieves) (its |the |both |all )?(primary|co-primary|key secondary)",
            r"positive (topline|top-line|phase|pivotal|results|data|interim)", r"statistically significant",
            r"topline (results|data)", r"phase (1|2|3|i|ii|iii|1b|2b)\b.{0,60}(results|data)",
            r"(demonstrat|show)\w* .{0,40}(improvement|efficacy|response)"):
        return done("TRIAL_DATA", +1)
    # --- deals
    role = _ma_role(h, company)
    if role == "MA_CLOSED":
        return done("MA_CLOSED", 0)
    if role:
        return done(role, +1 if role == "MA_TARGET" else 0)
    # --- financing
    if _has(h_short, r"tender offers? (for|to purchase) .{0,60}\b(notes|debentures|bonds)\b",
            r"offers? to purchase .{0,60}\b(notes|debentures|bonds)\b", r"redemption of .{0,40}\b(notes|debentures)\b",
            r"(consent solicitation|exchange offer)s? .{0,40}\b(notes|debentures)\b"):
        return done("DEBT_TENDER", 0)
    if _has(h, r"\b(closing|closes|closed|completion|completes|completed)\b.{0,40}offering"):
        return done("OFFERING_CLOSED", 0)
    offering = _has(
        h,
        r"(pricing of|prices|proposed|announces|launch\w*|commenc\w*) (an? |its )?(upsized |proposed )?(underwritten |public |registered direct |secondary |follow-on |\$[\d.,]+ (million|billion) )*(public |secondary )?offering",
        r"at[- ]the[- ]market (equity )?(offering|program)",
        r"offering of (\$[\d.,]+ (million|billion) )?(.{0,40})(convertible|common stock|ordinary shares|american depositary)",
        r"registered direct offering",
        r"private placement of (common|ordinary|shares)",
    )
    if offering:
        if _has(h, r"\bnotes\b|debentures|\bbonds\b") and not _has(h, r"convertible|exchangeable"):
            return done("DEBT", 0)
        return done("OFFERING", -1)
    # --- executives
    board_only = _has(h_short, r"\bboard\b|\bdirectors?\b") and not _has(
        h_short, r"(new|next|interim|as) (president and )?(chief executive|chief financial|CEO|CFO)")
    if not board_only and ("5.02" in item_set or _has(h_short, r"\b(appoint|name[sd]?|step(s|ping)? down|resign|retire|depart|succe(ed|ssor)|transition)")) and _has(
        h_short, r"chief executive officer|chief financial officer|\bCEO\b|\bCFO\b"
    ) and _has(h_short, r"\b(appoint\w*|name[sd]?|step(s|ping)? down|resign\w*|retire\w*|depart\w*|succe(ed|ssor|ssion)\w*|transition\w*|hire[sd]?|terminat\w*|leav(e|es|ing)|next)\b"):
        return done("EXEC_CHANGE", 0)
    # --- stand-alone guidance
    if _guide_hit(h, _GUIDE_DOWN) or _has(h, r"preliminary .{0,60}below"):
        return done("GUIDE_CUT", -1)
    if _guide_hit(h, _GUIDE_UP) or _has(h, r"preliminary .{0,60}(above|exceed)"):
        return done("GUIDE_RAISE", +1)
    if _has(h, r"(share|stock) repurchase (program|authorization)", r"buyback"):
        return done("BUYBACK", 0)
    if _has(h, r"dividend"):
        return done("DIVIDEND", 0)
    return done(kind, direction)
