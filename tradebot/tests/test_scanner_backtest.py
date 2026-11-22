"""Offline tests for the scanner backtest (no network)."""
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from bot.config import load_config
from scanner import backtest as B

C = load_config()["scanner"]
D = date(2026, 11, 17)
ET = B.ET


def bars(prices_by_time: dict, vol: float = 10_000) -> pd.DataFrame:
    """5-min bars from 4:00 to 15:55; price steps from the given {"HH:MM": price} points."""
    idx = pd.date_range(datetime.combine(D, time(4, 0), ET), datetime.combine(D, time(15, 55), ET), freq="5min")
    keys = sorted(prices_by_time)
    px = []
    for t in idx:
        k = [x for x in keys if B._t(x) <= t.time()]
        px.append(prices_by_time[k[-1]] if k else prices_by_time[keys[0]])
    px = np.array(px, dtype=float)
    return pd.DataFrame({"open": px, "high": px * 1.01, "low": px * 0.99, "close": px, "volume": vol}, index=idx)


def cand(rows):
    return pd.DataFrame(rows, columns=["symbol", "prev_close", "avg_vol", "avg_dvol", "high", "next_open"])


def test_prefilter():
    g = cand([("A", 10, 1e6, 1e7, 11, 0), ("B", 10, 1e6, 1e7, 10.2, 0), ("C", 10, 1e6, 1e5, 12, 0),
              ("D", 100, 1e6, 1e8, 120, 0)])
    assert B.prefilter(g, C)["symbol"].tolist() == ["A"]


def test_outcome_horizons():
    b = bars({"04:00": 10, "10:00": 11, "11:00": 12, "15:00": 9})
    i = int(np.flatnonzero(b.index >= datetime.combine(D, time(9, 35), ET))[0])
    o = B.outcome(b, i, next_open=8.0)
    assert o["entry"] == 10
    assert o["30 min"] == pytest.approx(10.0)        # last close before 10:05 is the 10:00 bar at 11
    assert o["11:30"] == pytest.approx(20.0)
    assert o["close"] == pytest.approx(-10.0)
    assert o["next open"] == pytest.approx(-20.0)
    assert o["best"] == pytest.approx(21.2) and o["worst"] == pytest.approx(-10.9)


def test_screen_day_no_lookahead_and_groups():
    # GO: +10% early with heavy volume -> pick. QUIET: +10% but little volume -> control.
    # LATE: only gaps at 11:00 -> not seen at 9:35. NEWSY: news after 9:35 must not count at 9:35.
    b = {"GO": bars({"04:00": 11, "12:00": 12}, vol=40_000), "QUIET": bars({"04:00": 11}, vol=200),
         "LATE": bars({"04:00": 10, "11:00": 13}, vol=40_000), "NEWSY": bars({"04:00": 11}, vol=40_000)}
    c = cand([(s, 10, 100_000, 1e7, 13, 10.5) for s in b])
    news = {"NEWSY": [datetime.combine(D, time(9, 50), ET)], "GO": [datetime.combine(D, time(7, 0), ET)]}
    rows = {r["symbol"]: r for r in B.screen_day(D, c, b, news, {"GO": 12.5}, C, "09:35")}
    assert set(rows) == {"GO", "QUIET", "NEWSY"}
    assert rows["GO"]["group"] == "pick" and rows["GO"]["news"]
    assert rows["QUIET"]["group"] == "control"
    assert rows["NEWSY"]["group"] == "pick" and not rows["NEWSY"]["news"]
    assert rows["GO"]["entry"] == 11 and rows["GO"]["close"] == pytest.approx((12 / 11 - 1) * 100)
    assert rows["GO"]["rank"] == 1 and rows["GO"]["score"] > rows["NEWSY"]["score"]
    later = {r["symbol"]: r for r in B.screen_day(D, c, b, news, {}, C, "11:30")}
    assert "LATE" in later


def test_premarket_scan_enters_at_open():
    b = {"PRE": bars({"04:00": 11, "09:30": 11.5}, vol=40_000)}
    c = cand([("PRE", 10, 100_000, 1e7, 12, 0)])
    r = B.screen_day(D, c, b, {}, {}, C, "09:25")[0]
    assert r["price"] == 11 and r["entry"] == 11.5


def test_summary_verdict_and_report():
    rng = np.random.default_rng(1)
    rows = []
    for k in range(40):
        d = D + timedelta(days=k)
        for j in range(3):
            rows.append({"day": d, "decision": "09:35", "symbol": f"P{j}", "group": "pick", "rank": j + 1,
                         "price": 8, "gap": 8, "rvol": 5, "news": j == 0, "best": 3, "worst": -2,
                         **{h: 1.0 + rng.normal(0, 0.5) for h, _ in B.HORIZONS}})
            rows.append({"day": d, "decision": "09:35", "symbol": f"C{j}", "group": "control", "rank": None,
                         "price": 8, "gap": 8, "rvol": 1, "news": False, "best": 1, "worst": -1,
                         **{h: -0.5 + rng.normal(0, 0.5) for h, _ in B.HORIZONS}})
    df = pd.DataFrame(rows)
    p, c = B.summarize(df[df.group == "pick"], "close"), B.summarize(df[df.group == "control"], "close")
    assert p["n"] == 120 and p["days"] == 40 and p["t_days"] > 2
    assert B.verdict(p, c).startswith("🟢")
    assert B.verdict(c, p).startswith("🔴")
    assert B.verdict({"n": 3}, c).startswith("⚪")
    text = B.report(df, {"start": D, "end": D, "days": 40, "decisions": ["09:35"], "c": C, "spy": 0.05})
    assert "Scan at 09:35" in text and "top 5 by score" in text and "SPY" in text
