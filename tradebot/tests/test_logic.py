"""Offline tests: no network, no API keys.   python -m pytest -q"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from bot import main, risk, scoring
from bot.config import load_config
from bot.signals import congress, funds_13f, insiders, social, trend


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c["journal_path"] = str(tmp_path / "j.sqlite")
    c["cache_dir"] = str(tmp_path)
    return c


def bars(n=260, drift=0.0012, seed=0):
    rng = np.random.default_rng(seed)
    close = 50 * np.cumprod(1 + drift + rng.normal(0, 0.006, n))
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": 1_000_000})


# ---------- trend ----------
def test_uptrend_passes_downtrend_fails(cfg):
    ok, score, _ = trend.evaluate(trend.indicators(bars()), cfg)
    assert ok and 0 < score <= 1
    ok, _, why = trend.evaluate(trend.indicators(bars(drift=-0.002)), cfg)
    assert not ok and "below" in why


# ---------- sources ----------
def test_insider_parse(cfg):
    html = """<table><tr><th>X</th><th>Filing\xa0Date</th><th>Ticker</th><th>Insider Name</th>
    <th>Title</th><th>Trade\xa0Type</th><th>Value</th></tr>
    <tr><td>M</td><td>2026-11-01</td><td>ABC</td><td>Jane</td><td>CEO</td><td>P - Purchase</td><td>+$500,000</td></tr>
    <tr><td>M</td><td>2026-11-01</td><td>ABC</td><td>Bob</td><td>Dir</td><td>P - Purchase</td><td>+$250,000</td></tr>
    <tr><td></td><td>2026-11-01</td><td>XYZ</td><td>Al</td><td>CFO</td><td>S - Sale</td><td>-$900,000</td></tr>
    </table>"""
    out = insiders.parse(html, cfg)
    assert "ABC" in out and "XYZ" not in out
    assert "2 insider" in out["ABC"]["reason"]


def test_congress_net_buying(cfg):
    rows = [
        {"Ticker": "NVDA", "Transaction": "Purchase", "Range": "$15,001 - $50,000",
         "Representative": "A", "ReportDate": "2026-10-20"},
        {"Ticker": "NVDA", "Transaction": "Purchase", "Range": "$50,001 - $100,000",
         "Representative": "B", "ReportDate": "2026-10-25"},
        {"Ticker": "TSLA", "Transaction": "Purchase", "Range": "$15,001 - $50,000",
         "Representative": "A", "ReportDate": "2026-10-20"},
        {"Ticker": "TSLA", "Transaction": "Sale", "Range": "$15,001 - $50,000",
         "Representative": "C", "ReportDate": "2026-10-20"},
        {"Ticker": "OLD", "Transaction": "Purchase", "Range": "$15,001 - $50,000",
         "Representative": "A", "ReportDate": "2025-01-01"},
    ]
    out = congress.parse(rows, cfg, today=datetime(2026, 11, 3))
    assert set(out) == {"NVDA"}


def test_social(cfg):
    rows = [{"ticker": "GME", "mentions": 300, "mentions_24h_ago": 100},
            {"ticker": "AAPL", "mentions": 200, "mentions_24h_ago": 190},
            {"ticker": "TINY", "mentions": 5, "mentions_24h_ago": 1}]
    assert set(social.parse(rows, cfg)) == {"GME"}


def test_13f_diff():
    xml = """<informationTable xmlns="x">
      <infoTable><nameOfIssuer>AAA</nameOfIssuer><cusip>111</cusip><value>50000000</value>
        <shrsOrPrnAmt><sshPrnamt>1000</sshPrnamt></shrsOrPrnAmt></infoTable>
      <infoTable><nameOfIssuer>BBB</nameOfIssuer><cusip>222</cusip><value>90000000</value>
        <shrsOrPrnAmt><sshPrnamt>2000</sshPrnamt></shrsOrPrnAmt></infoTable>
      <infoTable><nameOfIssuer>CCC</nameOfIssuer><cusip>333</cusip><value>90000000</value>
        <shrsOrPrnAmt><sshPrnamt>2000</sshPrnamt></shrsOrPrnAmt><putCall>Put</putCall></infoTable>
    </informationTable>"""
    new = funds_13f.parse_infotable(xml)
    assert "333" not in new                     # options excluded
    old = {"111": {"name": "AAA", "value": 4e7, "shares": 1000}}
    d = funds_13f.diff(new, old, 25, 1e7)
    assert set(d) == {"222"} and d["222"][1] is None   # BBB is new; AAA unchanged


# ---------- scoring ----------
def test_confluence_beats_single_source(cfg):
    w = cfg["signals"]["weights"]
    f = scoring.follow_scores({
        "insiders": {"AAA": {"score": 0.6, "reason": ""}, "BBB": {"score": 0.6, "reason": ""}},
        "congress": {"AAA": {"score": 0.6, "reason": ""}},
        "social": {"CCC": {"score": 0.9, "reason": ""}},
    }, w)
    assert f["AAA"]["follow"] > f["BBB"]["follow"] > f["CCC"]["follow"]


def test_rank_requires_trend(cfg):
    follow = {"AAA": {"follow": 0.9, "sources": ["insiders"], "reasons": []},
              "BBB": {"follow": 0.9, "sources": ["insiders"], "reasons": []}}
    tt = {"AAA": (True, 0.8, "up", {}), "BBB": (False, 0, "down", {})}
    assert [p["ticker"] for p in scoring.rank(follow, tt, cfg)] == ["AAA"]


# ---------- risk ----------
def test_sizing_caps(cfg):
    o = risk.size_order(100_000, 200_000, 0, 100, 2, cfg)
    # risk cap: 1% of 100k / (2*2) = 250 sh, size cap: 10% / 100 = 100 sh -> 100
    assert o["qty"] == 100 and o["stop"] == 96 and o["target"] == 110
    assert risk.size_order(100_000, 200_000, 79_950, 100, 2, cfg) is None  # exposure full
    assert risk.size_order(1_000, 1_000, 0, 500, 5, cfg) is None           # can't afford 1 share


def test_gates(cfg):
    assert risk.account_gates(100_000, 101_000, 101_000, cfg) == []
    assert risk.account_gates(96_000, 100_000, 100_000, cfg)             # -4% day
    assert any("drawdown" in m for m in risk.account_gates(84_000, 84_500, 100_000, cfg))


def test_exits(cfg):
    assert risk.should_exit(31, None, cfg)
    assert risk.should_exit(3, {"close": 9, "sma50": 10}, cfg)
    assert risk.should_exit(3, {"close": 11, "sma50": 10}, cfg) is None


# ---------- end to end with a fake broker ----------
class FakeBroker:
    live = False

    def __init__(self, cfg):
        self.orders = []

    def market_open(self): return True
    def account(self): return {"equity": 100_000, "last_equity": 100_000, "cash": 100_000, "buying_power": 200_000}
    def positions(self): return {}
    def tradable(self, s): return True
    def bars(self, syms): return {s: bars(seed=i) for i, s in enumerate(syms)}
    def last_price(self, s): return 60.0
    def buy_bracket(self, s, q, st, tg): self.orders.append((s, q, st, tg)); return "id-" + s
    def close(self, s): return "x"


def test_full_run(cfg, monkeypatch):
    fb = FakeBroker(cfg)
    monkeypatch.setattr(main, "Broker", lambda c: fb)
    monkeypatch.setattr(main, "gather_signals", lambda c: {
        "insiders": {"AAA": {"score": 0.9, "reason": "3 insiders"}, "BBB": {"score": 0.7, "reason": "2"},
                     "CCC": {"score": 0.8, "reason": "2"}},
        "congress": {"AAA": {"score": 0.6, "reason": "2 members"}}, "funds_13f": {}, "social": {}})
    main.run(cfg, dry_run=False)
    assert len(fb.orders) == cfg["risk"]["max_new_per_day"]
    assert fb.orders[0][0] == "AAA"
    for s, q, stop, tgt in fb.orders:
        assert stop < 60 < tgt and q >= 1
    # second run same day must not buy more
    main.run(cfg, dry_run=False)
    assert len(fb.orders) == cfg["risk"]["max_new_per_day"]
