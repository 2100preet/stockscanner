"""Tight corporate-catalyst binary universe (target 50-150 markets).

Keep: CEO changes, M&A, IPO, bankruptcy, FDA, SEC/DOJ probes, earnings beats.
Drop: weekly price-target / price-bin markets, gossip, and contracts whose
YES already trades at or above 90c (near-certain / resolved).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from odte_scanner.polymarket.client import PolymarketClient, PolymarketError, parse_json_list

# Gamma tag ids: Business, Finance, Earnings, Big Tech, NVIDIA
DEFAULT_TAG_IDS: tuple[int, ...] = (107, 120, 1013, 101999, 1330)
DEFAULT_SEARCH_QUERIES: tuple[str, ...] = (
    "NVIDIA",
    "Apple",
    "Tesla",
    "Microsoft",
    "Amazon",
    "Meta",
    "Alphabet",
    "Netflix",
    "AMD",
    "Intel",
)
NEAR_CERTAIN_PRICE = 0.90
DEAD_PRICE = 0.003
MAX_UNIVERSE = 150

CATALYST_RE = re.compile(
    r"\b(ceo|cfo|resign\w*|step(?:s|ping)? down|rejoin\w*|return\w* as|"
    r"fired|oust\w*|replace\w*|successor|acquir\w*|acquisition|merg\w*|buyout|takeover|"
    r"ipo|go(?:es)? public|list(?:ing)? on|bankrupt\w*|chapter 11|default|fda|approv\w*|"
    r"sec\b|doj|investigat\w*|indict\w*|charged|probe|antitrust|lawsuit|sued|settle\w*|"
    r"earnings|beat|guidance|layoff\w*|delist\w*|split)\b",
    re.IGNORECASE,
)
PRICE_BIN_RE = re.compile(
    r"(what will .{0,40}\b(hit|close|trade|reach|be)\b|price of|close (above|below|at|over|under)|"
    r"finish (above|below)|(above|below|over|under) \$|between \$|reach \$|hit \$|dip to|"
    r"\$\s?\d[\d,.]*\s?[-–]\s?\$?\d|up or down|market cap (above|below|over|under)|"
    r"\b(highest|lowest) price|all[- ]time high|this week\?|weekly close|\brange\b)",
    re.IGNORECASE,
)
GOSSIP_RE = re.compile(
    r"\b(divorc\w*|affair|dating|girlfriend|boyfriend|wedding|married|marry|engag\w*|pregnan\w*|"
    r"baby|tweet\w*|post on x|say ['\"]|mention\w*|net worth|richest|time person of the year|"
    r"podcast|interview|outfit|hair|approval rating)\b",
    re.IGNORECASE,
)

# Obvious company / CEO names -> ticker. Longer names first wins.
NAME_TICKERS: dict[str, str] = {
    "nvidia": "NVDA",
    "jensen huang": "NVDA",
    "apple": "AAPL",
    "tim cook": "AAPL",
    "tesla": "TSLA",
    "elon musk": "TSLA",
    "microsoft": "MSFT",
    "satya nadella": "MSFT",
    "amazon": "AMZN",
    "andy jassy": "AMZN",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "sundar pichai": "GOOGL",
    "meta platforms": "META",
    "meta": "META",
    "facebook": "META",
    "mark zuckerberg": "META",
    "netflix": "NFLX",
    "advanced micro devices": "AMD",
    "amd": "AMD",
    "lisa su": "AMD",
    "intel": "INTC",
    "broadcom": "AVGO",
    "palantir": "PLTR",
    "coinbase": "COIN",
    "microstrategy": "MSTR",
    "strategy": "MSTR",
    "boeing": "BA",
    "disney": "DIS",
    "walmart": "WMT",
    "jpmorgan": "JPM",
    "jamie dimon": "JPM",
    "berkshire": "BRK.B",
    "warren buffett": "BRK.B",
    "oracle": "ORCL",
    "salesforce": "CRM",
    "super micro": "SMCI",
    "starbucks": "SBUX",
    "pfizer": "PFE",
    "moderna": "MRNA",
    "eli lilly": "LLY",
    "novo nordisk": "NVO",
    "unitedhealth": "UNH",
    "robinhood": "HOOD",
    "gamestop": "GME",
    "amc": "AMC",
    "uber": "UBER",
    "spacex": "SPCX",
}
# Uppercase words that are never a bare ticker mention in a market question.
_ACRONYMS = frozenset({"CEO", "CFO", "COO", "CTO", "IPO", "SEC", "DOJ", "FDA", "FTC", "USA", "ETF", "GDP", "CPI", "FED", "NYSE", "AGI", "LLM", "EPS"})
_NAME_RE = [(re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE), tick)
            for name, tick in sorted(NAME_TICKERS.items(), key=lambda kv: -len(kv[0]))]


def map_ticker(text: str, focus: Iterable[str] = ()) -> str | None:
    """Best-effort company/CEO -> ticker; then ``$TICK`` / ``(TICK)`` focus symbols."""
    body = str(text or "")
    for rx, tick in _NAME_RE:
        if rx.search(body):
            return tick
    focus_set = {str(f).upper() for f in focus if f}
    for m in re.finditer(r"(?:\$|\()([A-Z]{1,5})(?:\)|\b)", body):
        if m.group(1) in focus_set:
            return m.group(1)
    for word in re.findall(r"\b[A-Z]{3,5}\b", body):
        if word in focus_set and word not in _ACRONYMS:
            return word
    return None


@dataclass
class UniverseMarket:
    condition_id: str
    question: str
    slug: str
    event_slug: str
    event_title: str
    token_ids: list[str]
    outcomes: list[str]
    yes_price: float | None
    ticker: str | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        slug = self.event_slug or self.slug
        return f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"

    def outcome_for_token(self, token_id: str) -> str | None:
        try:
            idx = self.token_ids.index(str(token_id))
        except ValueError:
            return None
        return self.outcomes[idx] if idx < len(self.outcomes) else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "question": self.question,
            "ticker": self.ticker,
            "yes_price": self.yes_price,
            "url": self.url,
        }


def reject_reason(question: str, yes_price: float | None) -> str | None:
    """Why a market is out of the universe, or None when it qualifies."""
    q = str(question or "")
    if not q:
        return "no question"
    if PRICE_BIN_RE.search(q):
        return "price-bin"
    if GOSSIP_RE.search(q):
        return "gossip"
    if not CATALYST_RE.search(q):
        return "not a corporate catalyst"
    if yes_price is not None:
        if yes_price >= NEAR_CERTAIN_PRICE:
            return "near-certain"
        if yes_price <= DEAD_PRICE:
            return "dead"
    return None


def _yes_price(market: dict) -> float | None:
    prices = parse_json_list(market.get("outcomePrices"))
    if prices:
        try:
            return float(prices[0])
        except (TypeError, ValueError):
            pass
    for key in ("lastTradePrice", "bestAsk"):
        try:
            if market.get(key) is not None:
                return float(market[key])
        except (TypeError, ValueError):
            continue
    return None


def markets_from_events(events: Iterable[dict], *, focus: Iterable[str] = ()) -> tuple[list[UniverseMarket], dict[str, int]]:
    """Flatten Gamma events into qualifying binary markets plus reject counts."""
    kept: dict[str, UniverseMarket] = {}
    rejects: dict[str, int] = {}
    focus = list(focus)
    for ev in events:
        if not isinstance(ev, dict) or ev.get("closed"):
            continue
        tag_labels = [str(t.get("label") or "") for t in (ev.get("tags") or []) if isinstance(t, dict)]
        for m in ev.get("markets") or []:
            if not isinstance(m, dict):
                continue
            cid = str(m.get("conditionId") or "")
            if not cid or cid in kept:
                continue
            if m.get("closed") or m.get("active") is False or m.get("enableOrderBook") is False:
                rejects["closed"] = rejects.get("closed", 0) + 1
                continue
            outcomes = [str(o) for o in parse_json_list(m.get("outcomes"))]
            tokens = [str(t) for t in parse_json_list(m.get("clobTokenIds"))]
            if len(outcomes) != 2 or len(tokens) != 2:
                rejects["not binary"] = rejects.get("not binary", 0) + 1
                continue
            question = str(m.get("question") or ev.get("title") or "")
            price = _yes_price(m)
            why = reject_reason(question, price)
            if why:
                rejects[why] = rejects.get(why, 0) + 1
                continue
            kept[cid] = UniverseMarket(
                condition_id=cid,
                question=question,
                slug=str(m.get("slug") or ""),
                event_slug=str(ev.get("slug") or ""),
                event_title=str(ev.get("title") or ""),
                token_ids=tokens,
                outcomes=outcomes,
                yes_price=price,
                ticker=map_ticker(f"{question} {ev.get('title') or ''}", focus),
                tags=tag_labels,
            )
    return list(kept.values()), rejects


def build_universe(
    client: PolymarketClient,
    *,
    tag_ids: Iterable[int] = DEFAULT_TAG_IDS,
    search_queries: Iterable[str] = DEFAULT_SEARCH_QUERIES,
    focus: Iterable[str] = (),
    max_markets: int = MAX_UNIVERSE,
) -> tuple[list[UniverseMarket], dict[str, Any]]:
    """Pull tag feeds + focus searches, filter, cap. Partial failures are tolerated."""
    events: list[dict] = []
    errors: list[str] = []
    for tag in tag_ids:
        try:
            events.extend(client.events_by_tag(int(tag)))
        except PolymarketError as exc:
            errors.append(f"tag {tag}: {exc}")
    for q in search_queries:
        try:
            events.extend(client.public_search(str(q)))
        except PolymarketError as exc:
            errors.append(f"search {q}: {exc}")
    markets, rejects = markets_from_events(events, focus=focus)
    # Prefer mapped tickers, then cheaper YES (where longshot flow matters).
    markets.sort(key=lambda m: (m.ticker is None, m.yes_price if m.yes_price is not None else 1.0))
    capped = markets[: max(1, int(max_markets))]
    return capped, {"events": len(events), "rejects": rejects, "errors": errors, "kept": len(capped)}
