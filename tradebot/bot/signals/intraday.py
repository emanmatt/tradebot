"""Intraday breakout signal from 1-minute bars.

Idea: stocks that gapped up and trade on unusually heavy volume, then push to a
fresh high above both the opening range and their last 30 minutes while above
VWAP, tend to keep running for a few minutes. The stop is sized from how much
the stock normally moves per minute, so ordinary wiggles don't knock us out.
"""
from datetime import time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")
OPEN, CLOSE = time(9, 30), time(16, 0)


def to_et(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = df.copy()
    idx = pd.DatetimeIndex(df.index)
    df.index = (idx.tz_localize("UTC") if idx.tz is None else idx).tz_convert(ET)
    return df[(df.index.time >= OPEN) & (df.index.time < CLOSE)].sort_index()


def features(today: pd.DataFrame, prior: pd.DataFrame, or_minutes: int = 15,
             base_minutes: int = 30, atr_bars: int = 15) -> dict | None:
    """today: 1-min bars for the current session. prior: bars (any size) from earlier days."""
    t, p = to_et(today), to_et(prior)
    if t.empty or p.empty:
        return None
    day = t.index[-1].date()
    t = t[t.index.date == day]
    p = p[p.index.date < day]
    if t.empty or p.empty:
        return None

    open_ts = pd.Timestamp.combine(day, OPEN).tz_localize(ET)
    rng = t[t.index < open_ts + timedelta(minutes=or_minutes)]
    after = t[t.index >= open_ts + timedelta(minutes=or_minutes)]
    if rng.empty or len(after) < 2:
        return None

    now_ts = t.index[-1]
    prev_close = float(p["close"].iloc[-1])
    tp = (t["high"] + t["low"] + t["close"]) / 3
    vwap = float((tp * t["volume"]).sum() / max(t["volume"].sum(), 1))

    # relative volume: today's volume so far vs the same stretch of the day on prior days
    past = []
    for d in sorted(set(p.index.date))[-5:]:
        pd_ = p[p.index.date == d]
        past.append(pd_[pd_.index.time <= now_ts.time()]["volume"].sum())
    past = [v for v in past if v > 0]
    rvol = float(t["volume"].sum() / np.mean(past)) if past else 0.0

    # the 30 minutes before the current bar: a fresh breakout clears this range
    base = after[(after.index >= now_ts - timedelta(minutes=base_minutes)) & (after.index < now_ts)]
    if base.empty:
        base = rng

    last = t.tail(atr_bars + 1)
    tr = pd.concat([last["high"] - last["low"], (last["high"] - last["close"].shift()).abs(),
                    (last["low"] - last["close"].shift()).abs()], axis=1).max(axis=1).iloc[1:]
    return {
        "price": float(t["close"].iloc[-1]),
        "or_high": float(rng["high"].max()),
        "or_low": float(rng["low"].min()),
        "vwap": vwap,
        "gap_pct": (float(t["open"].iloc[0]) / prev_close - 1) * 100,
        "rvol": rvol,
        "atr1": float(tr.mean()) if len(tr) else float(t["high"].iloc[-1] - t["low"].iloc[-1]),
        "day_high": float(t["high"].max()),
        "base_high": float(base["high"].max()),
        "base_low": float(base["low"].min()),
        "last_close": float(t["close"].iloc[-2]),   # last finished 1-min bar
        "level": float(max(rng["high"].max(), base["high"].max())),
    }


def stop_distance(price: float, atr1: float, c: dict) -> float:
    """1.5x the stock's normal 1-minute move, kept between min and max % of price."""
    d = c["stop_atr_mult"] * atr1
    return float(np.clip(d, price * c["min_stop_pct"] / 100, price * c["max_stop_pct"] / 100))


def evaluate(f: dict, c: dict) -> tuple[bool, float, str]:
    """Return (passes, score 0..1, reason). Stop/target are set later from the real fill."""
    p, level = f["price"], f["level"]
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
    if f["atr1"] / p * 100 * c["stop_atr_mult"] > c["max_stop_pct"] * 1.5:
        fails.append("too jumpy for a safe stop")
    if fails:
        return False, 0.0, ", ".join(fails)

    score = (0.45 * min(f["rvol"] / (3 * c["min_rvol"]), 1)          # heavier volume
             + 0.25 * min(f["gap_pct"] / 8, 1)                          # stronger gap
             + 0.20 * (1 - ext / c["max_extension_pct"])                # fresher breakout
             + 0.10 * min((p / f["vwap"] - 1) * 100 / 2, 1))            # strength vs VWAP
    reason = (f"breakout over ${level:.2f}, gap {f['gap_pct']:+.1f}%, "
              f"volume {f['rvol']:.1f}x, above VWAP ${f['vwap']:.2f}")
    return True, round(float(np.clip(score, 0, 1)), 3), reason
