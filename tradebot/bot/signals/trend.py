"""Technical trend filter and score from daily bars."""
import numpy as np
import pandas as pd


def indicators(df: pd.DataFrame) -> dict:
    """df: daily bars with columns open, high, low, close, volume (oldest first)."""
    c, h, l = df["close"], df["high"], df["low"]
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return {
        "close": float(c.iloc[-1]),
        "sma20": float(c.rolling(20).mean().iloc[-1]),
        "sma50": float(c.rolling(50).mean().iloc[-1]),
        "sma200": float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else float("nan"),
        "rsi": float(rsi.fillna(100).iloc[-1]),
        "atr": float(tr.rolling(14).mean().iloc[-1]),
        "ret20": float(c.iloc[-1] / c.iloc[-21] - 1) if len(c) > 21 else 0.0,
        "dollar_vol": float((c * df["volume"]).rolling(20).mean().iloc[-1]),
    }


def evaluate(ind: dict, cfg: dict) -> tuple[bool, float, str]:
    """Return (passes, score 0..1, reason)."""
    t = cfg["trend"]
    fails = []
    if ind["close"] < t["min_price"]:
        fails.append("price too low")
    if ind["dollar_vol"] < t["min_avg_dollar_volume"]:
        fails.append("illiquid")
    if t["require_above_sma50"] and ind["close"] <= ind["sma50"]:
        fails.append("below 50d avg")
    if t["require_sma20_above_sma50"] and ind["sma20"] <= ind["sma50"]:
        fails.append("20d below 50d")
    if not t["rsi_min"] <= ind["rsi"] <= t["rsi_max"]:
        fails.append(f"RSI {ind['rsi']:.0f} out of range")
    if fails:
        return False, 0.0, ", ".join(fails)

    score = 0.4
    if not np.isnan(ind["sma200"]) and ind["close"] > ind["sma200"]:
        score += 0.2                                   # long-term uptrend
    score += float(np.clip(ind["ret20"] / 0.15, 0, 1)) * 0.25   # 20d momentum
    score += 0.15 * (1 - abs(ind["rsi"] - 58) / 20)    # sweet spot, not overbought
    score = float(np.clip(score, 0, 1))
    return True, round(score, 3), f"uptrend, RSI {ind['rsi']:.0f}, 20d {ind['ret20']:+.1%}"
