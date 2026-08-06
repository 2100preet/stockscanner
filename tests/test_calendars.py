from odte_scanner.calendars import (
    expiry_tags,
    has_weekday_expiry,
    mon_wed_priority_symbols,
    resolve_universe,
    resolve_yahoo_symbol,
)
from odte_scanner.config import load_config


def test_universe_includes_requested_names():
    cfg = load_config()
    uni = resolve_universe(cfg)
    for sym in (
        "SPY",
        "QQQ",
        "SPX",
        "XSP",
        "GLD",
        "SLV",
        "NVDA",
        "TSLA",
        "GOOGL",
        "AVGO",
        "MU",
        "NFLX",  # Friday weekly kept
    ):
        assert sym in uni


def test_spx_yahoo_alias():
    cfg = load_config()
    assert resolve_yahoo_symbol("SPX", cfg) == "^SPX"
    assert resolve_yahoo_symbol("XSP", cfg) == "^XSP"
    assert resolve_yahoo_symbol("NVDA", cfg) == "NVDA"


def test_expiry_tags():
    cfg = load_config()
    assert "Everyday" in expiry_tags("SPY", cfg)
    assert "Everyday" in expiry_tags("SPX", cfg)
    assert "Mon+Wed" in expiry_tags("MU", cfg)
    assert "Fri-weekly" in expiry_tags("NFLX", cfg)


def test_friday_never_dropped():
    cfg = load_config()
    # Friday weekday=4 is always True for any listed name
    assert has_weekday_expiry("NFLX", 4, cfg)
    assert has_weekday_expiry("SPY", 4, cfg)
    assert has_weekday_expiry("MU", 4, cfg)
    # prefer list in config includes Friday
    assert 4 in cfg["options"]["prefer_expiry_weekdays"]


def test_everyday_tue_thu():
    cfg = load_config()
    assert has_weekday_expiry("SPX", 1, cfg)
    assert has_weekday_expiry("QQQ", 3, cfg)
    assert not has_weekday_expiry("NFLX", 1, cfg)


def test_wednesday_priority_includes_uso_and_spx():
    cfg = load_config()
    pri = mon_wed_priority_symbols(cfg, weekday=2)
    assert "USO" in pri and "SPX" in pri and "AVGO" in pri
    fri = mon_wed_priority_symbols(cfg, weekday=4)
    assert "NFLX" in fri and "SPY" in fri
