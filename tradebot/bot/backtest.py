"""Replay the day-trading strategy over past months of real minute data.

  python -m bot.backtest --months 3

1. Universe: for each past trading day, the stocks that gapped up at the open and are
   normally liquid (uses only what was knowable at 9:30 that morning).
2. Minute bars for those stocks (full-market SIP data; also IEX, which the live bot uses).
3. Replays the live rules bar by bar: setups, pullback or spike entry, stop/target,
   break-even, time stop, VWAP exit, cooldown, no-revenge rule, daily loss limit, 3:50 flatten.
4. Runs several versions side by side and writes results/backtest-<date>.md + trades CSV.

Fills are modelled conservatively: if a bar touches both the stop and the target,
we assume the stop hit first; stop and market exits lose an extra cent.
"""
import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import logging
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", message="All-NaN slice encountered")
warnings.filterwarnings("ignore", message="Mean of empty slice")

from .config import ROOT, env, load_config

log = logging.getLogger("backtest")
ET = ZoneInfo("America/New_York")
N = 390                                  # 1-minute bars from 9:30 to 15:59
EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "NYSEARCA"}


def idx(hh: int, mm: int) -> int:
    return (hh * 60 + mm) - (9 * 60 + 30)


# ======================= data =======================
def _clients():
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    k, s = env("ALPACA_API_KEY"), env("ALPACA_SECRET_KEY")
    return TradingClient(k, s, paper=True), StockHistoricalDataClient(k, s)


def all_symbols(tc) -> list[str]:
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest
    assets = tc.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY))
    out = [a.symbol for a in assets if a.tradable and str(getattr(a.exchange, "value", a.exchange)) in EXCHANGES
           and a.symbol.isalpha() and len(a.symbol) <= 5]
    return sorted(set(out))


def daily_bars(dc, syms: list[str], start: date, end: date, feed) -> pd.DataFrame:
    from alpaca.data.enums import Adjustment
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    frames = []
    for i in range(0, len(syms), 400):
        df = dc.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=syms[i:i + 400], timeframe=TimeFrame.Day, adjustment=Adjustment.SPLIT,
            start=datetime.combine(start, time(0), ET), end=datetime.combine(end, time(23, 59), ET), feed=feed)).df
        if not df.empty:
            frames.append(df)
        if i and i % 2000 == 0:
            log.info("  daily bars: %d / %d symbols", i, len(syms))
    df = pd.concat(frames).reset_index()
    df["day"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(ET).dt.date
    return df.drop_duplicates(["symbol", "day"])


def pick_candidates(daily: pd.DataFrame, days: list[date], c: dict, top: int) -> dict:
    """Each day's liquid gap-up stocks, ranked by gap. Only uses data known at that day's open."""
    out = {}
    daily = daily.sort_values(["symbol", "day"])
    daily["prev_close"] = daily.groupby("symbol")["close"].shift(1)
    daily["avg_vol"] = daily.groupby("symbol")["volume"].transform(lambda v: v.shift(1).rolling(20, min_periods=10).mean())
    daily["dvol"] = daily["close"] * daily["volume"]
    daily["avg_dvol"] = daily.groupby("symbol")["dvol"].transform(lambda v: v.shift(1).rolling(20, min_periods=10).mean())
    daily["gap"] = (daily["open"] / daily["prev_close"] - 1) * 100
    by_day = dict(tuple(daily.groupby("day")))
    for d in days:
        g = by_day.get(d)
        if g is None:
            continue
        g = g[(g["prev_close"].between(c["min_price"], c["max_price"] * 1.1))
              & (g["gap"].between(c["min_gap_pct"], c["max_gap_pct"]))
              & (g["avg_dvol"] >= c.get("bt_min_avg_dollar_vol", 3e6))]
        g = g.nlargest(top, "gap")
        out[d] = {r.symbol: {"prev_close": r.prev_close, "avg_vol": r.avg_vol} for r in g.itertuples()}
    return out


def minute_grid(dc, syms: list[str], d: date, feed) -> dict:
    """{symbol: DataFrame with exactly 390 rows (9:30..15:59), gaps forward-filled}."""
    from alpaca.data.enums import Adjustment
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    start = datetime.combine(d, time(9, 30), ET)
    df = dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                                            start=start, end=start + timedelta(minutes=N), feed=feed,
                                            adjustment=Adjustment.SPLIT)).df
    out = {}
    if df.empty:
        return out
    grid = pd.date_range(start, periods=N, freq="1min")
    for s, g in df.groupby(level=0):
        g = g.droplevel(0)
        g.index = pd.DatetimeIndex(g.index).tz_convert(ET)
        g = g[~g.index.duplicated()].reindex(grid)
        g["volume"] = g["volume"].fillna(0)
        g["close"] = g["close"].ffill()
        for col in ("open", "high", "low"):
            g[col] = g[col].fillna(g["close"])
        out[s] = g[["open", "high", "low", "close", "volume"]]
    return out


# ======================= features (same rules as the live bot, vectorised) =======================
def prep(g: pd.DataFrame, prev_close: float, avg_vol: float, profile: np.ndarray, c: dict) -> dict | None:
    o, h, l, cl, v = (np.array(g[k], dtype=float) for k in ("open", "high", "low", "close", "volume"))
    valid = ~np.isnan(cl)
    if not valid[:5].any() or not prev_close or not avg_vol:
        return None
    first = int(np.argmax(valid))
    orb = c["or_minutes"]
    if first >= orb:
        return None
    tp = (h + l + cl) / 3
    cumv = np.nancumsum(v)
    vwap = np.nancumsum(np.nan_to_num(tp) * v) / np.maximum(cumv, 1)
    vwap[cumv == 0] = cl[cumv == 0]
    or_high, or_low = np.nanmax(h[:orb]), np.nanmin(l[:orb])
    h_after = h.copy()
    h_after[:orb] = -np.inf
    base_high = pd.Series(h_after).rolling(c["base_minutes"], min_periods=1).max().shift(1).to_numpy()
    base_high = np.where(np.isfinite(base_high), base_high, or_high)
    level = np.maximum(or_high, base_high)
    prev_c = np.concatenate([[cl[first]], cl[:-1]])
    tr = np.nanmax(np.vstack([h - l, np.abs(h - prev_c), np.abs(l - prev_c)]), axis=0)
    atr = pd.Series(tr).rolling(c["atr_bars"], min_periods=1).mean().to_numpy()
    rvol = cumv / np.maximum(avg_vol * profile, 1)
    return {"o": o, "h": h, "l": l, "c": cl, "vwap": vwap, "level": level, "atr": atr, "rvol": rvol,
            "day_high": np.fmax.accumulate(np.nan_to_num(h, nan=-np.inf)),
            "gap": (o[first] / prev_close - 1) * 100, "or_low": or_low}


def signals(f: dict, c: dict) -> tuple[np.ndarray, np.ndarray]:
    """Boolean setup flag and score per minute."""
    p, level = f["c"], f["level"]
    with np.errstate(invalid="ignore", divide="ignore"):
        ext = (p / level - 1) * 100
        ok = ((p >= c["min_price"]) & (p <= c["max_price"]) & (p > level) & (p > f["vwap"])
              & (f["rvol"] >= c["min_rvol"]) & (c["min_gap_pct"] <= f["gap"] <= c["max_gap_pct"])
              & (ext <= c["max_extension_pct"]) & (p >= f["day_high"] * (1 - c["max_pullback_pct"] / 100))
              & (f["atr"] / p * 100 * c["stop_atr_mult"] <= c["max_stop_pct"] * 1.5))
        score = (0.45 * np.minimum(f["rvol"] / (3 * c["min_rvol"]), 1) + 0.25 * min(max(f["gap"], 0) / 8, 1)
                 + 0.20 * (1 - ext / c["max_extension_pct"]) + 0.10 * np.minimum((p / f["vwap"] - 1) * 100 / 2, 1))
    ok &= score >= c["min_score"]
    ok[: c["or_minutes"]] = False
    return np.nan_to_num(ok, nan=False).astype(bool), np.nan_to_num(score)


def stop_dist(price: float, atr: float, c: dict) -> float:
    d = float(np.clip(c["stop_atr_mult"] * atr, price * c["min_stop_pct"] / 100, price * c["max_stop_pct"] / 100))
    return max(d, c.get("min_stop_usd", 0.0))


# ======================= simulation =======================
@dataclass
class Variant:
    name: str
    entry: str = "pullback"          # "pullback" | "spike"
    market_filter: bool = False      # only buy while SPY is above its VWAP and its open
    signal_feed: str = "sip"         # which data the signals are computed from
    overrides: dict = None


def simulate(days: list[date], sig: dict, px: dict, spy_ok: dict, v: Variant, c0: dict,
             start_equity: float = 100.0) -> tuple[list[dict], list[dict]]:
    """sig/px: {day: {symbol: prepped features}} (signals from sig, fills from px)."""
    c = {**c0, **(v.overrides or {})}
    slip = 0.01
    entry_start, entry_end = idx(9, 45), idx(15, 15)
    flatten = idx(*map(int, c["flatten_at_et"].split(":")))
    eq = start_equity
    trades, daily = [], []
    for d in days:
        S, P = sig.get(d, {}), px.get(d, {})
        syms = [s for s in S if s in P]
        flags = {s: signals(S[s], c) for s in syms}
        day_start, blocked, pos = eq, False, None
        cooldown_until, next_scan = -1, 0
        no_rebuy, skip_until = set(), {}
        t = c["or_minutes"]
        while t < N:
            if pos:
                s, F = pos["sym"], P[pos["sym"]]
                k = t
                o, h, l, cl = F["o"][k], F["h"][k], F["l"][k], F["c"][k]
                exit_px = why = None
                if k >= flatten:
                    exit_px, why = cl - slip, "end of day"
                elif o <= pos["stop"]:
                    exit_px, why = o - slip, "stop hit"
                elif l <= pos["stop"]:
                    exit_px, why = pos["stop"] - slip, "stop hit"
                elif h >= pos["target"]:
                    exit_px, why = pos["target"], "target hit"
                else:
                    held = k - pos["bar"]
                    if c["exit_below_vwap"] and held >= c["vwap_exit_after_min"] and cl < F["vwap"][k] \
                            and cl < pos["entry"] + c["breakeven_at_r"] * pos["r"]:
                        exit_px, why = cl - slip, "lost VWAP"
                    elif c.get("time_stop_min") and held >= c["time_stop_min"] \
                            and cl < pos["entry"] + c["time_stop_gain_r"] * pos["r"]:
                        exit_px, why = cl - slip, "time stop"
                pos["hi"], pos["lo"] = max(pos["hi"], h), min(pos["lo"], l)
                if exit_px is None:
                    if not pos["be"] and h >= pos["entry"] + c["breakeven_at_r"] * pos["r"]:
                        pos["stop"], pos["be"] = pos["entry"], True
                    t += 1
                    continue
                if why == "stop hit" and pos["be"]:
                    why = "break-even stop"
                pl = (exit_px - pos["entry"]) * pos["qty"]
                eq += pl
                r = pos["r"]
                trades.append({"day": d.isoformat(), "symbol": s, "entry_time": _hhmm(pos["bar"]),
                               "exit_time": _hhmm(k), "hold_min": k - pos["bar"], "qty": pos["qty"],
                               "entry": round(pos["entry"], 4), "stop0": round(pos["stop0"], 4),
                               "target": round(pos["target"], 4), "exit": round(exit_px, 4), "why": why,
                               "pl": round(pl, 2), "r": round((exit_px - pos["entry"]) / r, 2),
                               "mfe_r": round((pos["hi"] - pos["entry"]) / r, 2),
                               "mae_r": round((pos["lo"] - pos["entry"]) / r, 2), "equity": round(eq, 2)})
                if pl < 0 or why == "lost VWAP":
                    no_rebuy.add(s)
                cooldown_until = k + c["cooldown_min"]
                if eq <= day_start * (1 - c["max_daily_loss_pct"] / 100):
                    blocked = True
                pos = None
                t = k + 1
                continue

            # ---- flat: look for a setup ----
            if blocked or t < entry_start or t > entry_end or t <= cooldown_until or t < next_scan:
                t += 1
                continue
            if v.market_filter and not spy_ok[d][t]:
                t += 1
                continue
            best = None
            for s in syms:
                if s in no_rebuy or skip_until.get(s, -1) >= t:
                    continue
                ok, sc = flags[s]
                if ok[t] and (best is None or sc[t] > best[1]):
                    best = (s, sc[t])
            if not best:
                t += 1
                continue
            s = best[0]
            Fs, Fp = S[s], P[s]
            sig_px = Fs["c"][t]
            if v.entry == "spike":
                e = t + 1
                if e >= N:
                    break
                cap = sig_px * 1.002
                if Fp["o"][e] > cap:
                    skip_until[s] = t + 10
                    t += 1
                    continue
                fill, fill_bar = Fp["o"][e] + slip, e
            else:
                limit = min(math.ceil(Fs["level"][t] * (1 + c["pullback_buffer_pct"] / 100) * 100) / 100, sig_px)
                waited = max(1, c["pullback_wait_sec"] // 60)
                fill = fill_bar = None
                for e in range(t + 1, min(t + 1 + waited, N)):
                    if Fp["l"][e] <= limit:
                        fill, fill_bar = min(Fp["o"][e], limit), e
                        break
                if fill is None:
                    skip_until[s] = t + waited + c["no_pullback_retry_min"]
                    next_scan = t + waited + 1
                    t = next_scan
                    continue
            dist = stop_dist(fill, Fs["atr"][t], c)
            risk = eq * c["risk_per_trade_pct"] / 100
            qty = math.floor(min(risk / dist, eq * c["max_position_pct"] / 100 / fill, eq * 0.95 / fill))
            if qty < 1:
                skip_until[s] = N
                t += 1
                continue
            pos = {"sym": s, "entry": fill, "stop": fill - dist, "stop0": fill - dist, "r": dist,
                   "target": fill + c["reward_risk"] * dist, "qty": qty, "bar": fill_bar, "be": False,
                   "hi": fill, "lo": fill}
            t = fill_bar + 1
        daily.append({"day": d.isoformat(), "equity": round(eq, 2), "pl": round(eq - day_start, 2),
                      "loss_limit": blocked})
    return trades, daily


def _hhmm(i: int) -> str:
    m = 9 * 60 + 30 + i
    return f"{m // 60:02d}:{m % 60:02d}"


# ======================= stats & report =======================
def stats(trades: list[dict], daily: list[dict], start_equity: float = 100.0) -> dict:
    if not trades:
        return {"trades": 0}
    t = pd.DataFrame(trades)
    wins, losses = t[t.pl > 0], t[t.pl < 0]
    eq = pd.Series([start_equity] + [d["equity"] for d in daily])
    dd = ((eq.cummax() - eq) / eq.cummax()).max() * 100
    half = len(t) // 2
    out = {
        "trades": len(t), "per_day": round(len(t) / max(len(daily), 1), 1),
        "win_pct": round(len(wins) / len(t) * 100, 1),
        "avg_win_r": round(wins.r.mean(), 2) if len(wins) else 0.0,
        "avg_loss_r": round(losses.r.mean(), 2) if len(losses) else 0.0,
        "exp_r": round(t.r.mean(), 3),
        "pf": round(wins.pl.sum() / -losses.pl.sum(), 2) if len(losses) and losses.pl.sum() else float("inf"),
        "return_pct": round((eq.iloc[-1] / start_equity - 1) * 100, 1),
        "max_dd_pct": round(dd, 1),
        "loss_limit_days": int(sum(d["loss_limit"] for d in daily)),
        "exp_r_first_half": round(t.r.iloc[:half].mean(), 3) if half else 0.0,
        "exp_r_second_half": round(t.r.iloc[half:].mean(), 3) if half else 0.0,
        "never_up_losers_pct": round((losses.mfe_r <= 0.1).mean() * 100, 0) if len(losses) else 0.0,
        "avg_hold": round(t.hold_min.mean(), 1),
    }
    return {k: (float(v) if isinstance(v, (np.floating, np.integer)) else v) for k, v in out.items()}


def verdict(s: dict) -> str:
    if s.get("trades", 0) < 50:
        return "too few trades to judge"
    if s["exp_r"] > 0.1 and s["pf"] >= 1.2 and s["exp_r_first_half"] > 0 and s["exp_r_second_half"] > 0:
        return "PROMISING (positive in both halves)"
    if s["exp_r"] > 0:
        return "marginal (positive overall but not robust)"
    return "LOSES MONEY"


def report(results: dict, meta: dict) -> str:
    lines = [f"# Backtest: {meta['start']} to {meta['end']}", "",
             f"{meta['days']} trading days, up to {meta['top']} gap-up stocks per day, $100 start, "
             f"current config.yaml rules. Signals/fills on {meta['feed_note']}.", "",
             f"SPY over the same period: {meta['spy_ret']:+.1f}%", "",
             "| Version | Trades | /day | Win % | Avg win R | Avg loss R | Expectancy R | Profit factor "
             "| Return | Max DD | Loss-limit days | 1st half R | 2nd half R | Verdict |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, (s, _, _) in results.items():
        if not s.get("trades"):
            lines.append(f"| {name} | 0 | | | | | | | | | | | | no trades |")
            continue
        lines.append(f"| {name} | {s['trades']} | {s['per_day']} | {s['win_pct']} | {s['avg_win_r']} | "
                     f"{s['avg_loss_r']} | {s['exp_r']:+.3f} | {s['pf']} | {s['return_pct']:+.1f}% | "
                     f"{s['max_dd_pct']}% | {s['loss_limit_days']} | {s['exp_r_first_half']:+.3f} | "
                     f"{s['exp_r_second_half']:+.3f} | {verdict(s)} |")
    lines += ["", "Expectancy R = average result per trade in units of risk (+0.1R = 10% of the risk "
              "won per trade on average). Profit factor = dollars won / dollars lost. A real edge should be "
              "positive in BOTH halves of the period.", ""]
    for name, (s, trades, daily) in results.items():
        if not trades:
            continue
        t = pd.DataFrame(trades)
        lines += [f"## {name}", "",
                  "Exits: " + ", ".join(f"{k} {v}" for k, v in t.why.value_counts().items()),
                  f"Average hold {s['avg_hold']} min. Losers that never went up: {s['never_up_losers_pct']:.0f}%.", ""]
        t["hour"] = t.entry_time.str[:2]
        t["month"] = t.day.str[:7]
        t["px"] = pd.cut(t.entry, [0, 5, 15, 1e9], labels=["<$5", "$5-15", "$15+"])
        for col, title in (("month", "By month"), ("hour", "By entry hour"), ("px", "By share price")):
            g = t.groupby(col, observed=True).agg(n=("r", "size"), win=("pl", lambda x: (x > 0).mean() * 100),
                                                  exp_r=("r", "mean"), pl=("pl", "sum"))
            lines.append(f"{title}: " + "; ".join(f"{i}: {int(r.n)} trades, {r.win:.0f}% win, "
                                                  f"{r.exp_r:+.2f}R, ${r.pl:+.2f}" for i, r in g.iterrows()))
        lines.append("")
    return "\n".join(lines)


VARIANTS = [
    Variant("A. live now: pullback"),
    Variant("B. spike entry", entry="spike"),
    Variant("C. pullback + market filter", market_filter=True),
    Variant("D. spike + market filter", entry="spike", market_filter=True),
    Variant("E. pullback + filter, $5+ stocks", market_filter=True, overrides={"min_price": 5}),
    Variant("F. pullback + filter, 2R target, no time stop", market_filter=True,
            overrides={"reward_risk": 2.0, "time_stop_min": 0}),
    Variant("G. live now, IEX-only signals", signal_feed="iex"),
]


# ======================= main =======================
def run(months: float, top: int, out_dir: Path) -> Path:
    from alpaca.data.enums import DataFeed
    cfg = load_config()
    c = cfg["day"]
    tc, dc = _clients()
    end = datetime.now(ET).date() - timedelta(days=1)
    start = end - timedelta(days=int(months * 30.5))

    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    feed_note = "full-market SIP data"
    sip = DataFeed.SIP
    try:
        dc.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Day, feed=sip,
                                           start=datetime.combine(start, time(0), ET),
                                           end=datetime.combine(end, time(0), ET)))
    except Exception as e:  # noqa: BLE001
        log.warning("SIP history not available on this plan (%s); using IEX for everything", e)
        sip, feed_note = DataFeed.IEX, "IEX data only (SIP not available)"

    log.info("Listing symbols...")
    syms = all_symbols(tc)
    log.info("%d symbols. Daily bars %s..%s", len(syms), start, end)
    daily = daily_bars(dc, sorted(set(syms) | {"SPY"}), start - timedelta(days=45), end, sip)
    spy_d = daily[daily.symbol == "SPY"].drop_duplicates("day").set_index("day").sort_index()
    days = sorted({d for d in spy_d.index if start <= d <= end})
    cands = pick_candidates(daily, days, c, top)
    spy_ret = float((spy_d.loc[days[-1], "close"] / spy_d.loc[days[0], "open"] - 1) * 100)

    log.info("%d trading days; fetching minute bars...", len(days))
    raw_sip, raw_iex, spy_min = {}, {}, {}
    for i, d in enumerate(days):
        want = list(cands.get(d, {})) + ["SPY"]
        try:
            raw_sip[d] = minute_grid(dc, want, d, sip)
            raw_iex[d] = minute_grid(dc, want[:-1], d, DataFeed.IEX) if sip != DataFeed.IEX else raw_sip[d]
        except Exception as e:  # noqa: BLE001
            log.warning("  %s: minute data failed (%s), skipping day", d, e)
            continue
        if "SPY" in raw_sip[d]:
            spy_min[d] = raw_sip[d].pop("SPY")
        if (i + 1) % 10 == 0:
            log.info("  %d / %d days", i + 1, len(days))
    days = [d for d in days if d in spy_min]

    # intraday volume curve (share of a normal day's volume traded by each minute), from SPY
    fr = [np.cumsum(g["volume"].to_numpy()) / max(g["volume"].sum(), 1) for g in spy_min.values()]
    profile = np.maximum(np.mean(fr, axis=0), 0.02)

    spy_ok = {}
    for d, g in spy_min.items():
        tp = (g["high"] + g["low"] + g["close"]) / 3
        vw = (tp * g["volume"]).cumsum() / g["volume"].cumsum().replace(0, np.nan)
        spy_ok[d] = ((g["close"] > vw) & (g["close"] > g["open"].iloc[0])).fillna(False).to_numpy()

    def prepped(raw, scale_to=None):
        """scale_to: when signals come from IEX, shrink the normal-volume baseline to IEX's share
        of that stock's volume (the live bot compares IEX volume with IEX history)."""
        out = {}
        for d in days:
            out[d] = {}
            for s, g in raw.get(d, {}).items():
                info = cands[d].get(s)
                if not info:
                    continue
                avg = info["avg_vol"]
                if scale_to is not None:
                    full = scale_to.get(d, {}).get(s)
                    share = g["volume"].sum() / max(full["volume"].sum(), 1) if full is not None else 0
                    if share <= 0:
                        continue
                    avg *= share
                f = prep(g, info["prev_close"], avg, profile, c)
                if f:
                    out[d][s] = f
        return out

    P_sip = prepped(raw_sip)
    P_iex = prepped(raw_iex, scale_to=raw_sip) if sip != DataFeed.IEX else P_sip
    results = {}
    for v in VARIANTS:
        S = P_iex if v.signal_feed == "iex" else P_sip
        tr, dl = simulate(days, S, P_sip, spy_ok, v, c)
        results[v.name] = (stats(tr, dl), tr, dl)
        log.info("%-45s %s", v.name, results[v.name][0])

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ET).strftime("%Y-%m-%d")
    md = out_dir / f"backtest-{stamp}.md"
    md.write_text(report(results, {"start": days[0], "end": days[-1], "days": len(days), "top": top,
                                   "feed_note": feed_note, "spy_ret": spy_ret}))
    pd.concat([pd.DataFrame(tr).assign(version=name) for name, (_, tr, _) in results.items() if tr]) \
        .to_csv(out_dir / f"backtest-{stamp}-trades.csv", index=False)
    log.info("wrote %s", md)
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=float, default=3)
    ap.add_argument("--top", type=int, default=30, help="gap-up stocks per day to replay")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    run(a.months, a.top, ROOT / "results")


if __name__ == "__main__":
    main()
