"""Step 1 - build the ranked watchlist (momentum screen + smart money screen).

  python -m scanner.watchlist            # scan now, print the list, save it to data/watchlist.sqlite
  python -m scanner.watchlist --push     # ...and send the top 5 to your phone (ntfy)
  python -m scanner.watchlist --force    # scan even outside 4:00 AM - 4:00 PM ET (for testing)

Output: up to 20 stocks, best first, each with the reasons it made the list.
"""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta, timezone
import json
import logging
import os
import sqlite3
from zoneinfo import ZoneInfo

from bot.config import ROOT, load_config
from . import momentum, smart_money

log = logging.getLogger("scanner")
ET = ZoneInfo("America/New_York")

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (ts TEXT, n_picks INTEGER, n_rejected INTEGER, rejected TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS watchlist (ts TEXT, rank INTEGER, symbol TEXT, kind TEXT, score REAL, momentum REAL,
    smart REAL, price REAL, gap_pct REAL, rvol REAL, news INTEGER, reasons TEXT);
"""


def build(data, c: dict, tracker_path: str, now_et: datetime) -> tuple[list[dict], list[dict]]:
    passed, rejected = momentum.screen(data, c, now_et)
    smart = smart_money.recent_buys(tracker_path, c)

    picks = []
    for p in passed:
        s = smart.get(p["symbol"])
        p["smart"] = s["smart"] if s else 0.0
        p["score"] = round(p["momentum"] + c["smart_boost"] * p["smart"], 3)
        p["kind"] = "both" if s else "momentum"
        if s:
            p["reasons"] += s["reasons"]
        picks.append(p)

    # smart-money stocks that did NOT gap: still worth watching, but they get their own limited slots
    have = {p["symbol"] for p in picks}
    others = sorted((sym for sym in smart if sym not in have), key=lambda s: -smart[s]["smart"])
    tradable = data.assets(c["exchanges"])
    others = [s for s in others if s in tradable]
    snaps = data.snapshots(others[: c["smart_slots"] * 4]) if others else {}
    extra = []
    for s in others:
        x = snaps.get(s)
        if not x or not c["min_price"] <= x["price"] <= c["max_price"]:
            if x:
                rejected.append({"symbol": s, "why": f"smart money pick, but price ${x['price']:.2f} out of range"})
            continue
        gap = (x["price"] / x["prev_close"] - 1) * 100 if x.get("prev_close") else 0.0
        extra.append({"symbol": s, "name": tradable[s]["name"], "price": x["price"], "gap_pct": round(gap, 2),
                      "rvol": None, "news": 0, "momentum": 0.0, "smart": smart[s]["smart"],
                      "score": round(c["smart_boost"] * smart[s]["smart"], 3), "kind": "smart money",
                      "fractionable": tradable[s]["fractionable"],
                      "reasons": smart[s]["reasons"] + [f"no gap today ({gap:+.1f}%)"]})
        if len(extra) >= c["smart_slots"]:
            break

    picks.sort(key=lambda p: -p["score"])
    final = picks[: c["max_watchlist"] - len(extra)] + extra
    final.sort(key=lambda p: -p["score"])
    for i, p in enumerate(final, 1):
        p["rank"] = i
    return final, rejected


def save(path: str, picks: list[dict], rejected: list[dict], note: str = "") -> None:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute("INSERT INTO scans VALUES (?,?,?,?,?)", (ts, len(picks), len(rejected), json.dumps(rejected[:300]), note))
    db.executemany("INSERT INTO watchlist VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   [(ts, p["rank"], p["symbol"], p["kind"], p["score"], p["momentum"], p["smart"], p["price"],
                     p["gap_pct"], p["rvol"], p["news"], json.dumps(p["reasons"])) for p in picks])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    db.execute("DELETE FROM watchlist WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM scans WHERE ts < ?", (cutoff,))
    db.commit()
    db.close()


def latest(path: str) -> list[dict]:
    """The most recent saved watchlist (what Step 2 will read)."""
    if not os.path.exists(path):
        return []
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    row = db.execute("SELECT MAX(ts) FROM watchlist").fetchone()
    if not row or not row[0]:
        return []
    cols = ["ts", "rank", "symbol", "kind", "score", "momentum", "smart", "price", "gap_pct", "rvol", "news", "reasons"]
    out = [dict(zip(cols, r)) for r in db.execute("SELECT * FROM watchlist WHERE ts=? ORDER BY rank", (row[0],))]
    for p in out:
        p["reasons"] = json.loads(p["reasons"])
    return out


def fmt(picks: list[dict]) -> str:
    if not picks:
        return "No stocks passed the screen right now."
    lines = [f"{'#':>2} {'TICKER':<6} {'TYPE':<11} {'SCORE':>5} {'PRICE':>8} {'GAP':>7} {'VOL':>6}  WHY"]
    for p in picks:
        rv = f"{p['rvol']:.1f}x" if p.get("rvol") else "-"
        lines.append(f"{p['rank']:>2} {p['symbol']:<6} {p['kind']:<11} {p['score']:>5.2f} {p['price']:>8.2f} "
                     f"{p['gap_pct']:>+6.1f}% {rv:>6}  {'; '.join(p['reasons'])}")
    return "\n".join(lines)


def in_hours(now_et: datetime, window: list[str]) -> bool:
    a, b = (time(*map(int, x.split(":"))) for x in window)
    return now_et.weekday() < 5 and a <= now_et.time() <= b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    cfg = load_config()
    c = cfg["scanner"]
    now_et = datetime.now(ET)
    if not a.force and not in_hours(now_et, c["active_et"]):
        log.info("outside scan hours (%s-%s ET on weekdays); use --force to scan anyway", *c["active_et"])
        return
    from .data import AlpacaData
    tracker_path = cfg["journal_path"].replace("journal.sqlite", "tracker.sqlite")
    picks, rejected = build(AlpacaData(c.get("feed", "iex")), c, tracker_path, now_et)
    save(str(ROOT / c["watchlist_path"]), picks, rejected)
    print(fmt(picks))
    log.info("%d picks, %d rejected (reasons saved)", len(picks), len(rejected))
    if a.push and picks:
        from bot.tracker import push
        body = "\n".join(f"{p['rank']}. {p['symbol']} ${p['price']:.2f} {p['gap_pct']:+.1f}% - {p['reasons'][0]}"
                         for p in picks[:5])
        push(f"Watchlist {now_et:%H:%M}: {len(picks)} stocks", body, "mag")


if __name__ == "__main__":
    main()
