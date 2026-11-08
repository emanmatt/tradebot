"""Position sizing and account-level safety gates. Pure functions, easy to test."""
import math


def size_order(equity: float, buying_power: float, exposure: float, entry: float,
               atr: float, cfg: dict) -> dict | None:
    """Shares sized so a stop-out loses at most risk_per_trade_pct of equity.

    Returns {"qty", "stop", "target", "risk_usd"} or None if it can't be sized safely.
    """
    r = cfg["risk"]
    stop_dist = r["stop_atr_mult"] * atr
    if atr <= 0 or entry <= 0 or stop_dist >= entry * 0.5:
        return None
    by_risk = equity * r["risk_per_trade_pct"] / 100 / stop_dist
    by_size = equity * r["max_position_pct"] / 100 / entry
    room = max(0.0, equity * r["max_total_exposure_pct"] / 100 - exposure)
    by_room = room / entry
    by_bp = buying_power * 0.95 / entry
    qty = math.floor(min(by_risk, by_size, by_room, by_bp))
    if qty < 1:
        return None
    stop = round(entry - stop_dist, 2)
    target = round(entry + r["reward_risk"] * stop_dist, 2)
    return {"qty": qty, "stop": stop, "target": target, "risk_usd": round(qty * stop_dist, 2)}


def account_gates(equity: float, last_equity: float, peak_equity: float, cfg: dict) -> list[str]:
    """Reasons to block NEW buys today. Empty list = clear to buy."""
    r = cfg["risk"]
    blocks = []
    if last_equity > 0 and (last_equity - equity) / last_equity * 100 >= r["max_daily_loss_pct"]:
        blocks.append(f"daily loss limit hit ({(equity / last_equity - 1):.1%})")
    if peak_equity > 0 and (peak_equity - equity) / peak_equity * 100 >= r["max_drawdown_pct"]:
        blocks.append(f"max drawdown hit ({(equity / peak_equity - 1):.1%} from peak) - review before resuming")
    return blocks


def should_exit(held_days: int, ind: dict | None, cfg: dict) -> str | None:
    x = cfg["exit"]
    if held_days >= x["max_hold_days"]:
        return f"time stop ({held_days} days)"
    if ind and x["exit_below_sma50"] and ind["close"] < ind["sma50"]:
        return "trend broke (closed below 50d avg)"
    return None
