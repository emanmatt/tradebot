"""Intraday opening-range breakout signal from 5-minute bars.

Idea: stocks that gap up on unusually heavy volume and then break above their
first-15-minute high, while trading above VWAP, tend to keep running for a while.
We buy the breakout, put the stop under the opening range, and target 2x the risk.
"""
from datetime import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)


def _et(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    idx = pd.DatetimeIndex(df.index)
    df.index = (idx.tz_localize("UTC") if idx.tz is None else idx).tz_convert(ET)
    return df[(df.index.time >= OPEN) & (df.index.time < time(16, 0))]  # regular hours only


def features(bars: pd.DataFrame, or_minutes: int = 15) -> dict | None:
    """bars: several days of 5-min bars (open, high, low, close, volume), UTC or ET index."""
    df = _et(bars)
    if df.empty:
        return None
    days = sorted(set(df.index.date))
    today, prior = days[-1], days[:-1]
    t = df[df.index.date == today]
    or_end = (pd.Timestamp.combine(today, OPEN) + pd.Timedelta(minutes=or_minutes)).time()
    rng = t[t.index.time < or_end]
    after = t[t.index.time >= or_end]
    if len(rng) < max(1, or_minutes // 5) or after.empty or not prior:
        return None

    prev_close = float(df[df.index.date == prior[-1]]["close"].iloc[-1])
    now_t = t.index[-1].time()
    tp = (t["high"] + t["low"] + t["close"]) / 3
    vwap = float((tp * t["volume"]).sum() / max(t["volume"].sum(), 1))

    # relative volume: today's volume so far vs the same time window on prior days
    past = [df[(df.index.date == d) & (df.index.time <= now_t)]["volume"].sum() for d in prior[-5:]]
    avg_past = np.mean([v for v in past if v > 0]) if any(v > 0 for v in past) else 0
    rvol = float(t["volume"].sum() / avg_past) if avg_past else 0.0

    # the last 30 minutes before the current bar: a fresh breakout clears this range
    base = after.iloc[-7:-1] if len(after) >= 3 else rng
    if base.empty:
        base = rng

    last = t.tail(12)
    tr = pd.concat([last["high"] - last["low"], (last["high"] - last["close"].shift()).abs(),
                    (last["low"] - last["close"].shift()).abs()], axis=1).max(axis=1)
    return {
        "price": float(t["close"].iloc[-1]),
        "or_high": float(rng["high"].max()),
        "or_low": float(rng["low"].min()),
        "vwap": vwap,
        "gap_pct": (float(t["open"].iloc[0]) / prev_close - 1) * 100,
        "rvol": rvol,
        "atr5": float(tr.mean()),
        "day_high": float(t["high"].max()),
        "base_high": float(base["high"].max()),
        "base_low": float(base["low"].min()),
        "last_close_5m": float(t["close"].iloc[-2] if len(t) > 1 else t["close"].iloc[-1]),  # last finished bar
    }


def evaluate(f: dict, c: dict) -> tuple[bool, float, str, float, float]:
    """Return (passes, score 0..1, reason, stop, target)."""
    p = f["price"]
    level = max(f["or_high"], f["base_high"])   # must clear both the opening range and the last 30 min
    fails = []
    if not c["min_price"] <= p <= c["max_price"]:
        fails.append("price out of range")
    if p <= level:
        fails.append("no breakout yet")
    if p <= f["vwap"]:
        fails.append("below VWAP")
    if f["rvol"] < c["min_rvol"]:
        fails.append(f"volume only {f['rvol']:.1f}x normal")
    if not c["min_gap_pct"] <= f["gap_pct"] <= c["max_gap_pct"]:
        fails.append(f"gap {f['gap_pct']:+.1f}% out of range")
    ext = (p / level - 1) * 100
    if ext > c["max_extension_pct"]:
        fails.append(f"already {ext:.1f}% past breakout (chasing)")
    if p < f["day_high"] * (1 - c["max_pullback_pct"] / 100):
        fails.append("fading off the high")

    # stop under the range it just broke out of, never closer than min_stop_pct or wider than max_stop_pct
    stop = max(f["base_low"], f["or_low"], p - 2 * f["atr5"])
    dist = max(p - stop, p * c["min_stop_pct"] / 100)
    if dist / p * 100 > c["max_stop_pct"]:
        fails.append(f"stop would be {dist / p * 100:.1f}% away (too risky)")
    if fails:
        return False, 0.0, ", ".join(fails), 0.0, 0.0

    stop = round(p - dist, 2)
    target = round(p + c["reward_risk"] * dist, 2)
    score = (0.45 * min(f["rvol"] / (3 * c["min_rvol"]), 1)          # heavier volume
             + 0.25 * min(f["gap_pct"] / 8, 1)                          # stronger gap
             + 0.20 * (1 - ext / c["max_extension_pct"])                # fresher breakout
             + 0.10 * min((p / f["vwap"] - 1) * 100 / 2, 1))            # strength vs VWAP
    reason = (f"breakout over ${level:.2f}, gap {f['gap_pct']:+.1f}%, "
              f"volume {f['rvol']:.1f}x, above VWAP ${f['vwap']:.2f}")
    return True, round(float(np.clip(score, 0, 1)), 3), reason, stop, target
