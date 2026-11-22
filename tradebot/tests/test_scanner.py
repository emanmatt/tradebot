"""Offline tests for Step 1 (scanner). No network."""
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from bot.config import load_config
from bot.tracker import Store
from scanner import momentum, smart_money, watchlist
from scanner.watchlist import ET

C = load_config()["scanner"]
NOW = datetime(2026, 11, 23, 9, 15, tzinfo=ET)          # Monday, pre-market


class FakeData:
    """avg volume is IEX shares/day; at 9:15 a normal day has traded ~4.6% of its volume."""
    def __init__(self):
        self.assets_ = {s: {"name": f"{s} Inc", "exchange": "NASDAQ", "fractionable": True}
                        for s in ["GAP", "BIG", "THIN", "SLOW", "PRICY", "FLAT", "NEWS", "INS", "CEO"]}
        self.movers_ = {s: "top gainer" for s in ["GAP", "BIG", "THIN", "SLOW", "PRICY", "FLAT", "NEWS", "OTCX"]}
        self.snaps = {"GAP": (11.0, 10.0), "BIG": (30.0, 20.0), "THIN": (5.5, 5.0), "SLOW": (11, 10),
                      "PRICY": (120, 100), "FLAT": (10.1, 10), "NEWS": (8.8, 8.0), "INS": (40, 40.2), "CEO": (200, 190)}
        self.avg = {"GAP": 100_000, "BIG": 100_000, "THIN": 5_000, "SLOW": 100_000, "NEWS": 50_000}
        self.vol = {"GAP": 30_000, "BIG": 60_000, "THIN": 9_000, "SLOW": 4_000, "NEWS": 20_000}
        self.calls = []

    def assets(self, ex):
        self.calls.append("assets")
        return self.assets_

    def movers(self, top):
        return dict(self.movers_)

    def snapshots(self, syms):
        return {s: {"price": self.snaps[s][0], "prev_close": self.snaps[s][1]} for s in syms if s in self.snaps}

    def avg_daily_volume(self, syms, days):
        return {s: self.avg[s] for s in syms if s in self.avg}

    def volume_today(self, syms):
        return {s: self.vol[s] for s in syms if s in self.vol}

    def news(self, syms, hours):
        return {"NEWS": [{"headline": "NEWS Inc wins FDA approval", "source": "x", "ts": 1}]} if "NEWS" in syms else {}


def test_day_fraction_and_rvol():
    curve = C["volume_curve"]
    assert momentum.day_fraction(datetime(2026, 1, 5, 3, 0), curve) == 0.0
    assert momentum.day_fraction(datetime(2026, 1, 5, 9, 30), curve) == pytest.approx(0.05)
    assert momentum.day_fraction(datetime(2026, 1, 5, 9, 45), curve) == pytest.approx(0.11)
    assert momentum.day_fraction(datetime(2026, 1, 5, 17, 0), curve) == 1.0
    assert momentum.rvol(10_000, 100_000, datetime(2026, 1, 5, 16, 0), curve) == pytest.approx(0.1)
    assert momentum.rvol(5, 0, datetime(2026, 1, 5, 12, 0), curve) is None


def test_score_is_capped():
    assert momentum.score(100, 1000, True, C) == 1.0
    assert momentum.score(0, 0.5, False, C) == 0.0
    assert momentum.score(10, 10, False, C) == pytest.approx(0.35 * 0.5 + 0.40)


def test_momentum_screen_reasons():
    passed, rejected = momentum.screen(FakeData(), C, NOW)
    assert [p["symbol"] for p in passed] == ["NEWS", "BIG", "GAP"]
    why = {r["symbol"]: r["why"] for r in rejected}
    assert "not tradable" in why["OTCX"]
    assert "too thin" in why["THIN"]
    assert "volume only" in why["SLOW"]
    assert "outside" in why["PRICY"]
    assert "gap +1.0%" in why["FLAT"]
    news = passed[0]
    assert news["news"] == 1 and any("FDA" in r for r in news["reasons"])
    assert all(p["rvol"] >= 3 and p["gap_pct"] >= 5 for p in passed)


def test_require_news():
    passed, _ = momentum.screen(FakeData(), {**C, "require_news": True}, NOW)
    assert [p["symbol"] for p in passed] == ["NEWS"]


def tracker_file(tmp_path):
    path = str(tmp_path / "tracker.sqlite")
    st = Store(path)
    now = datetime.now(timezone.utc)
    for who, sym, src, days_ago in [("Smith CEO", "INS", "insiders", 1), ("Jones CFO", "INS", "insiders", 2),
                                    ("Rep. X", "GAP", "congress", 3), ("Old Guy", "OLD", "insiders", 40),
                                    ("Big Fund", "CEO", "funds", 5)]:
        st.add_event({"ts": (now - timedelta(days=days_ago)).isoformat(), "source": src, "who": who, "role": "r",
                      "symbol": sym, "action": "buy", "value_text": "$500,000", "trade_date": "2026-11-10"})
    st.add_event({"source": "insiders", "who": "Seller", "symbol": "INS", "action": "sell"})
    st.commit()
    return path


def test_smart_money(tmp_path):
    s = smart_money.recent_buys(tracker_file(tmp_path), C)
    assert set(s) == {"INS", "GAP", "CEO"}                          # 40-day-old insider buy is too old
    assert s["INS"]["smart"] == pytest.approx(0.6 + 0.2)            # insiders + cluster bonus
    assert any("cluster" in r for r in s["INS"]["reasons"])
    assert smart_money.recent_buys(str(tmp_path / "missing.sqlite"), C) == {}


def test_watchlist_merges_and_saves(tmp_path):
    picks, rejected = watchlist.build(FakeData(), C, tracker_file(tmp_path), NOW)
    kinds = {p["symbol"]: p["kind"] for p in picks}
    assert kinds == {"NEWS": "momentum", "BIG": "momentum", "GAP": "both", "INS": "smart money"}
    assert [p["rank"] for p in picks] == [1, 2, 3, 4]
    gap = next(p for p in picks if p["symbol"] == "GAP")
    assert gap["score"] == pytest.approx(gap["momentum"] + C["smart_boost"] * gap["smart"])
    assert any("congress" in r for r in gap["reasons"])
    assert any(r["symbol"] == "CEO" and "out of range" in r["why"] for r in rejected)   # $200 stock

    path = str(tmp_path / "w.sqlite")
    watchlist.save(path, picks, rejected)
    got = watchlist.latest(path)
    assert [p["symbol"] for p in got] == [p["symbol"] for p in picks] and isinstance(got[0]["reasons"], list)
    assert sqlite3.connect(path).execute("SELECT n_picks FROM scans").fetchone()[0] == 4
    assert "TICKER" in watchlist.fmt(picks) and watchlist.fmt([]).startswith("No stocks")


def test_watchlist_size_limits(tmp_path):
    c = {**C, "max_watchlist": 2, "smart_slots": 1}
    picks, _ = watchlist.build(FakeData(), c, tracker_file(tmp_path), NOW)
    assert len(picks) == 2 and sum(p["kind"] == "smart money" for p in picks) == 1


def test_hours():
    assert watchlist.in_hours(NOW, C["active_et"])
    assert not watchlist.in_hours(NOW.replace(hour=3), C["active_et"])
    assert not watchlist.in_hours(datetime(2026, 11, 22, 10, tzinfo=ET), C["active_et"])   # Sunday
