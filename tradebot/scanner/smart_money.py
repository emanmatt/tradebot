"""Step 1b - smart money screen: recent buys by insiders, Congress and hedge funds.

Reads what the investor tracker (bot/tracker.py) has already collected in data/tracker.sqlite, so the scanner
makes no extra calls to SEC or the House website.

How old is this information? (be honest about it)
  insiders  public within 2 business days of the trade  -> still fresh
  congress  public up to 45 days after the trade         -> often stale
  funds     13F, up to 45 days after quarter end         -> a quarter old
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import sqlite3


def recent_buys(tracker_path: str, c: dict, now: datetime | None = None) -> dict[str, dict]:
    """{symbol: {smart: 0..1, reasons: [...], sources: {...}}} for stocks bought recently by watched investors."""
    if not os.path.exists(tracker_path):
        return {}
    now = now or datetime.now(timezone.utc)
    db = sqlite3.connect(tracker_path)
    out: dict[str, dict] = {}
    w = c["smart_weights"]
    for src, days in c["smart_lookback_days"].items():
        since = (now - timedelta(days=days)).isoformat()
        rows = db.execute("SELECT symbol, who, role, value_text, trade_date, public_date, days_late, note FROM events "
                          "WHERE source=? AND action='buy' AND symbol IS NOT NULL AND ts>=? ORDER BY ts DESC",
                          (src, since)).fetchall()
        for sym, who, role, val, td, pd_, late, note in rows:
            sym = sym.upper()
            d = out.setdefault(sym, {"smart": 0.0, "reasons": [], "sources": {}, "people": set()})
            d["people"].add(who)
            first_for_src = src not in d["sources"]
            d["sources"][src] = d["sources"].get(src, 0) + 1
            if first_for_src:
                d["smart"] += w.get(src, 0)
            if len(d["reasons"]) < 3:
                lag = f", public {late}d later" if late else ""
                d["reasons"].append(f"{src}: {who} ({role or '?'}) bought {val or ''}"
                                    f"{' on ' + td if td else ''}{lag}".replace("  ", " "))
    db.close()
    for d in out.values():
        if d["sources"].get("insiders", 0) >= 2 or len(d["people"]) >= 3:
            d["smart"] += w.get("cluster_bonus", 0)
            d["reasons"].append(f"cluster: {len(d['people'])} different buyers")
        d["smart"] = round(min(d["smart"], 1.0), 3)
        d["people"] = len(d["people"])
    return out
