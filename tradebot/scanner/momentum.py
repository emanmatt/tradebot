"""Step 1a - momentum screen: gap up, unusual volume for the time of day, price range, liquidity, news."""
from __future__ import annotations

from datetime import datetime, time
import math


def _t(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def day_fraction(now_et: datetime, curve: list) -> float:
    """Share of a normal day's volume that has usually traded by `now_et` (linear between config points)."""
    t = now_et.hour * 60 + now_et.minute
    pts = [(_t(a).hour * 60 + _t(a).minute, float(b)) for a, b in curve]
    if t <= pts[0][0]:
        return pts[0][1]
    for (t0, f0), (t1, f1) in zip(pts, pts[1:]):
        if t <= t1:
            return f0 + (f1 - f0) * (t - t0) / (t1 - t0)
    return pts[-1][1]


def rvol(today_volume: float, avg_volume: float, now_et: datetime, curve: list) -> float | None:
    """Volume so far vs what a normal day has traded by this time. None if there's no baseline yet."""
    expected = avg_volume * max(day_fraction(now_et, curve), 0.01)  # floor avoids divide-by-~0 at 4 AM
    if not avg_volume or expected <= 0:
        return None
    return today_volume / expected


def score(gap_pct: float, rv: float, has_news: bool, c: dict) -> float:
    """0..1. Each part is capped, so one huge number can't carry a weak setup."""
    w = c["weights"]
    g = min(max(gap_pct, 0) / c["gap_full_pct"], 1)
    r = min(max(math.log(max(rv, 1)) / math.log(c["rvol_full"]), 0), 1)
    return round(w["gap"] * g + w["rvol"] * r + w["news"] * (1 if has_news else 0), 3)


def screen(data, c: dict, now_et: datetime) -> tuple[list[dict], list[dict]]:
    """Returns (passed, rejected). Every candidate gets a plain-English reason either way."""
    tradable = data.assets(c["exchanges"])
    found = data.movers(c["universe_top"])
    for s in c.get("extra_symbols") or []:
        found.setdefault(s.upper(), "your extra list")
    cands = [s for s in found if s in tradable]
    rejected = [{"symbol": s, "why": "not tradable on a main exchange"} for s in found if s not in tradable]

    snaps = data.snapshots(cands)
    in_range = []
    for s in cands:
        x = snaps.get(s)
        if not x or not x["prev_close"]:
            rejected.append({"symbol": s, "why": "no price data"})
            continue
        gap = (x["price"] / x["prev_close"] - 1) * 100
        x["gap_pct"] = gap
        if not c["min_price"] <= x["price"] <= c["max_price"]:
            rejected.append({"symbol": s, "why": f"price ${x['price']:.2f} outside ${c['min_price']}-${c['max_price']}"})
        elif not c["min_gap_pct"] <= gap <= c["max_gap_pct"]:
            rejected.append({"symbol": s, "why": f"gap {gap:+.1f}% (need {c['min_gap_pct']}%+)"})
        else:
            in_range.append(s)

    avg = data.avg_daily_volume(in_range, c["baseline_days"])
    vol = data.volume_today(in_range)
    news = data.news(in_range, c["news_lookback_hours"]) if in_range else {}
    scale = 1 / c["iex_volume_share"] if c.get("iex_volume_share") else 1
    passed = []
    for s in in_range:
        x = snaps[s]
        a = avg.get(s)
        if not a:
            rejected.append({"symbol": s, "why": "not enough trading history"})
            continue
        dollar_vol = a * x["prev_close"] * scale
        rv = rvol(vol.get(s, 0.0), a, now_et, c["volume_curve"])
        heads = news.get(s, [])
        if dollar_vol < c["min_avg_dollar_volume"]:
            rejected.append({"symbol": s, "why": f"too thin: ~${dollar_vol / 1e6:.1f}M/day traded"})
            continue
        if rv is None or rv < c["min_rvol"]:
            rejected.append({"symbol": s, "why": f"volume only {rv or 0:.1f}x normal (need {c['min_rvol']}x)"})
            continue
        if c.get("require_news") and not heads:
            rejected.append({"symbol": s, "why": "no news catalyst"})
            continue
        reasons = [f"gap {x['gap_pct']:+.1f}%", f"volume {rv:.1f}x normal", f"~${dollar_vol / 1e6:.0f}M/day"]
        if heads:
            reasons.append(f"news: {heads[0]['headline'][:90]}")
        passed.append({"symbol": s, "name": tradable[s]["name"], "price": x["price"], "gap_pct": round(x["gap_pct"], 2),
                       "rvol": round(rv, 2), "news": len(heads), "momentum": score(x["gap_pct"], rv, bool(heads), c),
                       "found_by": found[s], "fractionable": tradable[s]["fractionable"], "reasons": reasons})
    passed.sort(key=lambda p: -p["momentum"])
    return passed, rejected
