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
        req = StockBarsRequest(
            symbol_or_symbols=syms[i:i + 400], timeframe=TimeFrame.Day, adjustment=Adjustment.SPLIT,
            start=datetime.combine(start, time(0), ET), end=datetime.combine(end, time(23, 59), ET), feed=feed)
        df = _retry(lambda: dc.get_stock_bars(req)).df
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


# ======================= suite "new": stocks-in-play ORB, news filter, last-half-hour momentum ========
ORB = {"min_price": 5.0, "min_avg_vol": 1_000_000, "min_atr": 0.50, "min_rvol": 1.0, "top": 20,
       "or_bars": 5, "stop_atr_frac": 0.10, "risk_pct": 1.0, "exit_bar": idx(15, 55), "slip": 0.01,
       "rvol_days": 14, "min_rvol_history": 5,
       "max_alloc_pct": 20.0}            # no borrowing: cap each position at 20% of the account (5 at once)


def daily_features(daily: pd.DataFrame) -> pd.DataFrame:
    """Adds prev_close, 14-day average volume and 14-day ATR, all known BEFORE each day's open."""
    d = daily.sort_values(["symbol", "day"]).copy()
    g = d.groupby("symbol")
    pc = g["close"].shift(1)
    d["prev_close"] = pc
    d["tr"] = np.maximum(d["high"] - d["low"], np.maximum((d["high"] - pc).abs(), (d["low"] - pc).abs()))
    d["avg_vol14"] = g["volume"].transform(lambda v: v.shift(1).rolling(14, min_periods=10).mean())
    d["atr14"] = d.groupby("symbol")["tr"].transform(lambda v: v.shift(1).rolling(14, min_periods=10).mean())
    return d


def _retry(fn, tries: int = 6):
    """Alpaca's free plan allows ~200 requests/min: on 'too many requests' wait and try again."""
    import time as _t
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if i == tries - 1 or not any(k in msg for k in ("429", "too many", "rate limit", "timed out", "503", "502")):
                raise
            log.info("  rate limited, waiting %ds...", 10 * (i + 1))
            _t.sleep(10 * (i + 1))


def first5(dc, syms: list[str], d: date, feed) -> dict:
    """{symbol: (open, close, high, volume)} for the first 5 minutes (9:30-9:34)."""
    from alpaca.data.enums import Adjustment
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    start = datetime.combine(d, time(9, 30), ET)
    out = {}
    for i in range(0, len(syms), 800):
        req = StockBarsRequest(symbol_or_symbols=syms[i:i + 800], timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                               start=start, end=start + timedelta(minutes=4, seconds=59), feed=feed,
                               adjustment=Adjustment.SPLIT)
        df = _retry(lambda: dc.get_stock_bars(req)).df
        if df.empty:
            continue
        for s, g in df.groupby(level=0):
            out[s] = (float(g["open"].iloc[0]), float(g["close"].iloc[-1]), float(g["high"].max()),
                      float(g["volume"].sum()))
    return out


def news_counts(syms: list[str], start: datetime, end: datetime) -> dict:
    """How many news stories mention each symbol between start and end (Benzinga via Alpaca)."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest
    nc = NewsClient(env("ALPACA_API_KEY"), env("ALPACA_SECRET_KEY"))
    ns = _retry(lambda: nc.get_news(NewsRequest(symbols=",".join(syms), start=start, end=end,
                                                exclude_contentless=False)))
    counts = defaultdict(int)
    for n in ns.data.get("news", []):
        for s in n.symbols:
            if s in syms:
                counts[s] += 1
    return dict(counts)


def orb_trade(g: pd.DataFrame, atr: float, cfg: dict, orh: float | None = None,
              green: bool | None = None) -> dict | None:
    """Long-only 5-minute opening-range breakout held to the close (stop = 10% of daily ATR).
    Returns None if the first 5 minutes weren't up or the breakout never came.
    orh/green: when the bot only sees IEX data, the range high and the direction come from IEX,
    while the stop-buy order itself fills against the real (full-market) prices in g."""
    o, h, l, c = (np.array(g[k], dtype=float) for k in ("open", "high", "low", "close"))
    k5 = cfg["or_bars"]
    if green is None:
        if np.isnan(o[0]) or np.isnan(c[k5 - 1]) or c[k5 - 1] <= o[0]:
            return None
    elif not green:
        return None
    if orh is None:
        orh = np.nanmax(h[:k5])
    slip, last = cfg["slip"], cfg["exit_bar"]
    for t in range(k5, last + 1):
        if h[t] >= orh:
            break
    else:
        return None
    gross_fill = max(o[t], orh)
    fill = gross_fill + slip
    dist = cfg["stop_atr_frac"] * atr
    stop = gross_fill - dist
    exit_bar, gross_exit, why = last, c[last], "close"
    if c[t] <= stop:                        # broke out and collapsed within the same minute
        exit_bar, gross_exit, why = t, stop, "stop"
    else:
        for k in range(t + 1, last + 1):
            if o[k] <= stop:
                exit_bar, gross_exit, why = k, o[k], "stop"
                break
            if l[k] <= stop:
                exit_bar, gross_exit, why = k, stop, "stop"
                break
    exit_px = gross_exit - slip
    return {"entry_bar": t, "exit_bar": exit_bar, "fill": fill, "stop": stop, "dist": dist, "exit": exit_px,
            "why": why, "r": (exit_px - fill) / dist, "r_gross": (gross_exit - gross_fill) / dist}


def run_orb(days: list[date], picks: dict, bars: dict, news: dict | None, which: str, cfg: dict,
            start_equity: float = 100.0) -> tuple[list[dict], list[dict]]:
    """which: 'all' | 'news' | 'no_news'. Fractional shares, no borrowing, risk 1% of equity per trade."""
    eq, trades, daily = start_equity, [], []
    for d in days:
        cands = []
        for s, info in picks.get(d, {}).items():
            if which != "all" and news is not None:
                has = news.get(d, {}).get(s, 0) > 0
                if (which == "news") != has:
                    continue
            g = bars.get(d, {}).get(s)
            if g is None:
                continue
            tr = orb_trade(g, info["atr"], cfg, info.get("orh"), info.get("green"))
            if tr:
                cands.append((tr["entry_bar"], -info["rvol"], s, tr))
        cands.sort(key=lambda x: (x[0], x[1]))          # earliest breakout first; ties: most unusual volume
        day_eq, cash, open_pos, pl_day = eq, eq, [], 0.0
        for bar, _, s, tr in cands:
            for p in [p for p in open_pos if p[0] < bar]:      # positions that already closed free their cash
                cash += p[1]
                open_pos.remove(p)
            qty = min(day_eq * cfg["risk_pct"] / 100 / tr["dist"],
                      day_eq * cfg["max_alloc_pct"] / 100 / tr["fill"], cash / tr["fill"])
            if qty * tr["fill"] < 1.0:                            # Alpaca's $1 minimum order
                continue
            cost = qty * tr["fill"]
            pl = (tr["exit"] - tr["fill"]) * qty
            cash -= cost
            open_pos.append((tr["exit_bar"], cost + pl))
            pl_day += pl
            trades.append({"day": d.isoformat(), "symbol": s, "entry_time": _hhmm(tr["entry_bar"]),
                           "exit_time": _hhmm(tr["exit_bar"]), "why": tr["why"], "qty": round(qty, 4),
                           "fill": round(tr["fill"], 4), "stop": round(tr["stop"], 4), "exit": round(tr["exit"], 4),
                           "pl": round(pl, 4), "r": round(tr["r"], 3), "r_gross": round(tr["r_gross"], 3),
                           "rvol": round(picks[d][s]["rvol"], 2), "news": (news or {}).get(d, {}).get(s)})
        eq += pl_day
        daily.append({"day": d.isoformat(), "equity": round(eq, 4), "pl": round(pl_day, 4)})
    return trades, daily


def run_momentum(days: list[date], bars: dict, prev_close: dict, sym: str, slip: float = 0.01,
                 start_equity: float = 100.0) -> tuple[list[dict], list[dict]]:
    """If `sym` is up from yesterday's close at 10:00, buy at 3:30 and sell at the close. Long only."""
    eq, trades, daily = start_equity, [], []
    for d in days:
        g = bars.get(d, {}).get(sym)
        pc = prev_close.get(d, {}).get(sym)
        if g is not None and pc:
            p10, buy, sell = g["close"].iloc[idx(9, 59)], g["open"].iloc[idx(15, 30)], g["close"].iloc[N - 1]
            if p10 > pc and not np.isnan(buy) and not np.isnan(sell):
                ret = (sell - slip) / (buy + slip) - 1
                trades.append({"day": d.isoformat(), "symbol": sym, "first_half_hour_pct": round((p10 / pc - 1) * 100, 2),
                               "buy": round(buy, 2), "sell": round(sell, 2), "ret_pct": round(ret * 100, 3),
                               "pl": round(eq * ret, 4)})
                eq *= 1 + ret
        daily.append({"day": d.isoformat(), "equity": round(eq, 4)})
    return trades, daily


def stats2(trades: list[dict], daily: list[dict], unit: str, start_equity: float = 100.0) -> dict:
    if not trades:
        return {"trades": 0}
    t = pd.DataFrame(trades)
    key = "r" if unit == "R" else "ret_pct"
    eq = pd.Series([start_equity] + [d["equity"] for d in daily])
    wins, losses = t[t.pl > 0], t[t.pl < 0]
    half = len(t) // 2
    out = {"trades": len(t), "per_day": round(len(t) / max(len(daily), 1), 2),
           "win_pct": round(len(wins) / len(t) * 100, 1),
           "avg": round(t[key].mean(), 3), "avg_win": round(wins[key].mean(), 3) if len(wins) else 0.0,
           "avg_loss": round(losses[key].mean(), 3) if len(losses) else 0.0,
           "gross": round(t["r_gross"].mean(), 3) if "r_gross" in t else None,
           "pf": round(wins.pl.sum() / -losses.pl.sum(), 2) if len(losses) and losses.pl.sum() else float("inf"),
           "return_pct": round((eq.iloc[-1] / start_equity - 1) * 100, 1),
           "max_dd_pct": round(((eq.cummax() - eq) / eq.cummax()).max() * 100, 1),
           "first_half": round(t[key].iloc[:half].mean(), 3) if half else 0.0,
           "second_half": round(t[key].iloc[half:].mean(), 3) if half else 0.0}
    return {k: (float(v) if isinstance(v, (np.floating, np.integer)) else v) for k, v in out.items()}


def verdict2(s: dict) -> str:
    if s.get("trades", 0) < 40:
        return "too few trades to judge"
    if s["avg"] > 0 and s["pf"] >= 1.2 and s["first_half"] > 0 and s["second_half"] > 0:
        return "PROMISING (positive in both halves)"
    if s["avg"] > 0 and s["pf"] > 1:
        return "marginal (not robust)"
    return "LOSES MONEY"


def report2(res: dict, meta: dict) -> str:
    L = [f"# New strategies backtest: {meta['start']} to {meta['end']}", "",
         f"{meta['days']} trading days. $100 start, long only, fractional shares, no borrowing "
         f"(max 20% of the account per position). "
         f"Costs: 1 cent per share on every buy and sell. Full-market (SIP) minute data. "
         f"SPY over the period: {meta['spy_ret']:+.1f}%.", "",
         f"Stocks in play per day (avg): {meta['avg_in_play']:.1f}; with overnight news: {meta['news_share']}."
         + (f" IEX-only picks that match the full-market picks: {meta['overlap']:.0f}%." if meta.get("overlap") is not None
            else ""), "",
         "## Opening-range breakout on stocks in play (results in R = multiples of the amount risked)", "",
         "| Version | Trades | /day | Win % | Avg win R | Avg loss R | Avg R after costs | Avg R before costs "
         "| Profit factor | Return | Max DD | 1st half R | 2nd half R | Verdict |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, (s, _, _) in res.items():
        if not name.startswith("ORB"):
            continue
        if not s.get("trades"):
            L.append(f"| {name} | 0 | | | | | | | | | | | | no trades |")
            continue
        L.append(f"| {name} | {s['trades']} | {s['per_day']} | {s['win_pct']} | {s['avg_win']} | {s['avg_loss']} | "
                 f"{s['avg']:+.3f} | {s['gross']:+.3f} | {s['pf']} | {s['return_pct']:+.1f}% | {s['max_dd_pct']}% | "
                 f"{s['first_half']:+.3f} | {s['second_half']:+.3f} | {verdict2(s)} |")
    L += ["", "## Last-half-hour market momentum (results in % per trade)", "",
          "| Version | Trades | Win % | Avg trade | Avg win | Avg loss | Profit factor | Return | Max DD "
          "| 1st half | 2nd half | Verdict |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, (s, _, _) in res.items():
        if not name.startswith("MOM"):
            continue
        if not s.get("trades"):
            L.append(f"| {name} | 0 | | | | | | | | | | no trades |")
            continue
        L.append(f"| {name} | {s['trades']} | {s['win_pct']} | {s['avg']:+.3f}% | {s['avg_win']:+.3f}% | "
                 f"{s['avg_loss']:+.3f}% | {s['pf']} | {s['return_pct']:+.1f}% | {s['max_dd_pct']}% | "
                 f"{s['first_half']:+.3f}% | {s['second_half']:+.3f}% | {verdict2(s)} |")
    for name, (s, tr, _) in res.items():
        if not tr:
            continue
        t = pd.DataFrame(tr)
        t["month"] = t.day.str[:7]
        key = "r" if name.startswith("ORB") else "ret_pct"
        g = t.groupby("month").agg(n=(key, "size"), avg=(key, "mean"), pl=("pl", "sum"))
        L += ["", f"### {name}", "By month: " + "; ".join(f"{m}: {int(r.n)} trades, avg {r.avg:+.3f}, ${r.pl:+.2f}"
                                                         for m, r in g.iterrows())]
        if name.startswith("ORB"):
            L.append("Exits: " + ", ".join(f"{k} {v}" for k, v in t.why.value_counts().items()))
    return "\n".join(L) + "\n"


def run_new(months: float, out_dir: Path, with_news: bool = False, with_iex: bool = True) -> Path:
    from alpaca.data.enums import DataFeed
    import time as _t
    cfg = ORB
    tc, dc = _clients()
    end = datetime.now(ET).date() - timedelta(days=1)
    start = end - timedelta(days=int(months * 30.5))
    sip = DataFeed.SIP

    log.info("Listing symbols...")
    syms = sorted(set(all_symbols(tc)) | {"SPY", "QQQ"})
    log.info("%d symbols. Daily bars...", len(syms))
    daily = daily_features(daily_bars(dc, syms, start - timedelta(days=70), end, sip))
    by_day = dict(tuple(daily.groupby("day")))
    all_days = sorted(by_day)
    days = [d for d in all_days if start <= d <= end]
    warm = [d for d in all_days if d < start][-(cfg["rvol_days"] + 2):]

    # 1) first-5-minute volume for every liquid stock, every day (warm-up days build the baseline)
    hist, hist_iex = defaultdict(list), defaultdict(list)
    picks, picks_iex, n_in_play = {}, {}, []
    for i, d in enumerate(warm + days):
        g = by_day[d]
        ok = g[(g.prev_close >= cfg["min_price"]) & (g.avg_vol14 >= cfg["min_avg_vol"]) & (g.atr14 >= cfg["min_atr"])]
        f5 = first5(dc, ok.symbol.tolist(), d, sip)
        f5i = first5(dc, ok.symbol.tolist(), d, DataFeed.IEX) if with_iex else {}
        _t.sleep(0.2)
        if d in days:
            rows = []
            atr = dict(zip(ok.symbol, ok.atr14))
            for s, (o, c, h, v) in f5.items():
                past = hist[s][-cfg["rvol_days"]:]
                if len(past) < cfg["min_rvol_history"] or o < cfg["min_price"]:
                    continue
                rv = v / max(np.mean(past), 1)
                if rv >= cfg["min_rvol"]:
                    rows.append((rv, s))
            rows.sort(reverse=True)
            picks[d] = {s: {"rvol": rv, "atr": atr[s]} for rv, s in rows[:cfg["top"]]}
            n_in_play.append(len(picks[d]))
            # what a live bot on the free plan sees: IEX volume vs IEX history, IEX range high and direction
            rows_i = []
            for s, (o, c, h, v) in f5i.items():
                past = hist_iex[s][-cfg["rvol_days"]:]
                if len(past) < cfg["min_rvol_history"] or o < cfg["min_price"]:
                    continue
                rv = v / max(np.mean(past), 1)
                if rv >= cfg["min_rvol"]:
                    rows_i.append((rv, s))
            rows_i.sort(reverse=True)
            picks_iex[d] = {s: {"rvol": rv, "atr": atr[s], "orh": f5i[s][2], "green": f5i[s][1] > f5i[s][0]}
                            for rv, s in rows_i[:cfg["top"]]}
        for s, (_, _, _, v) in f5.items():
            hist[s].append(v)
        for s, (_, _, _, v) in f5i.items():
            hist_iex[s].append(v)
        if (i + 1) % 10 == 0:
            log.info("  first-5-min scan: %d / %d days", i + 1, len(warm) + len(days))

    # 2) full minute bars for the stocks in play (+ SPY, QQQ) and 3) overnight news
    bars, news, news_ok = {}, {}, True
    prev_close = {d: {s: float(by_day[d].set_index("symbol").prev_close.get(s, np.nan)) for s in ("SPY", "QQQ")}
                  for d in days}
    for i, d in enumerate(days):
        want = sorted(set(picks.get(d, {})) | set(picks_iex.get(d, {})) | {"SPY", "QQQ"})
        try:
            bars[d] = _retry(lambda: minute_grid(dc, want, d, sip))
        except Exception as e:  # noqa: BLE001
            log.warning("  %s minute bars failed: %s", d, e)
            continue
        if with_news and news_ok and picks.get(d):
            prev_day = all_days[all_days.index(d) - 1]
            try:
                news[d] = news_counts(list(picks[d]), datetime.combine(prev_day, time(16, 0), ET),
                                      datetime.combine(d, time(9, 30), ET))
            except Exception as e:  # noqa: BLE001
                log.warning("news history unavailable (%s): skipping the news split", e)
                news_ok, news = False, None
        if (i + 1) % 10 == 0:
            log.info("  minute bars + news: %d / %d days", i + 1, len(days))
    days = [d for d in days if d in bars]

    if not with_news:
        news = None
    res = {}
    for name, which in (("ORB stocks in play (all)", "all"), ("ORB with overnight news", "news"),
                        ("ORB without news", "no_news")):
        if which != "all" and news is None:
            continue
        tr, dl = run_orb(days, picks, bars, news, which, cfg)
        res[name] = (stats2(tr, dl, "R"), tr, dl)
        log.info("%-32s %s", name, res[name][0])
    if with_iex:
        tr, dl = run_orb(days, picks_iex, bars, None, "all", cfg)
        res["ORB stocks in play, IEX-only view (live on free plan)"] = (stats2(tr, dl, "R"), tr, dl)
        log.info("ORB IEX view %s", res["ORB stocks in play, IEX-only view (live on free plan)"][0])
        same = [len(set(picks.get(d, {})) & set(picks_iex.get(d, {}))) / max(len(picks.get(d, {})), 1) for d in days]
        overlap = float(np.mean(same)) * 100 if same else 0.0
    else:
        overlap = None
    for sym in ("SPY", "QQQ"):
        tr, dl = run_momentum(days, bars, prev_close, sym)
        res[f"MOM {sym} last half-hour"] = (stats2(tr, dl, "%"), tr, dl)
        log.info("MOM %s %s", sym, res[f"MOM {sym} last half-hour"][0])

    in_play_news = [news[d].get(s, 0) > 0 for d in days if news and d in news for s in picks.get(d, {})]
    spy = daily[daily.symbol == "SPY"].set_index("day")
    meta = {"start": days[0], "end": days[-1], "days": len(days),
            "spy_ret": float((spy.loc[days[-1], "close"] / spy.loc[days[0], "open"] - 1) * 100),
            "avg_in_play": float(np.mean(n_in_play)) if n_in_play else 0.0,
            "news_share": f"{np.mean(in_play_news) * 100:.0f}%" if in_play_news else "not checked",
            "overlap": overlap}
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ET).strftime("%Y-%m-%d")
    md = out_dir / f"new-strategies-{stamp}-{months:g}m.md"
    md.write_text(report2(res, meta))
    frames = [pd.DataFrame(tr).assign(version=name) for name, (_, tr, _) in res.items() if tr]
    if frames:
        pd.concat(frames).to_csv(out_dir / f"new-strategies-{stamp}-{months:g}m-trades.csv", index=False)
    log.info("wrote %s", md)
    return md


# ======================= suite "pack": the day-trader-skill system (Jakeschincariol/day-trader-skill) ========
# Ported rules: liquid US stocks (price >= $5, yesterday's volume >= 1M), optional pre-market watchlist of five
# daily-close setups, 15-minute opening range on 5-minute bars, buy after the first 5-minute bar that CLOSES above
# the range high (9:45-15:30), stop at the range midpoint, target 2R (or 1R), flat at 15:55, max 3 trades a day,
# ~1% of the account risked per trade, no new trades after a -3% day.
PACK = {"min_price": 5.0, "min_prev_vol": 1_000_000, "liquid_top": 200, "tag_top": 50, "or_bars5": 3,
        "last_entry_bar5": 71, "flat_bar5": 77, "max_trades": 3, "risk_pct": 1.0, "max_alloc_pct": 33.0,
        "daily_loss_pct": 3.0, "slip": 0.01, "min_risk_pct": 0.05}
N5 = 78                                      # 5-minute bars 9:30..15:55
SETUPS = ("breakout_20d", "bull_flag", "reclaim_ma20", "oversold", "oversold_bounce")


def wilder_rsi(c: np.ndarray, n: int = 14) -> float | None:
    if len(c) < n + 1:
        return None
    d = np.diff(c)
    g, lo = np.maximum(d, 0), np.maximum(-d, 0)
    ag, al = g[:n].mean(), lo[:n].mean()
    for i in range(n, len(d)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def pack_setups(c: np.ndarray) -> list[str]:
    """The pack's find_setups() on daily closes up to YESTERDAY (oldest first, >= 30)."""
    if len(c) < 30:
        return []
    last, hi20 = c[-1], c[-21:-1].max()
    ma20, ma20_prev = c[-20:].mean(), c[-21:-1].mean()
    run, rng5 = c[-6] / c[-16] - 1, (c[-5:].max() - c[-5:].min()) / last
    r, r_prev = wilder_rsi(c), wilder_rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags


def pack_universe(daily: pd.DataFrame, days: list[date], cfg: dict) -> dict:
    """{day: {symbol: {"dvol": prev-day $ volume, "tags": [...]}}} using only data before each day."""
    d = daily.sort_values(["symbol", "day"])
    out = {day: {} for day in days}
    want = set(days)
    for s, g in d.groupby("symbol"):
        closes, vols, ds = g["close"].to_numpy(float), g["volume"].to_numpy(float), list(g["day"])
        for i, day in enumerate(ds):
            if day not in want or i < 1:
                continue
            pc, pv = closes[i - 1], vols[i - 1]
            if pc < cfg["min_price"] or pv < cfg["min_prev_vol"]:
                continue
            out[day][s] = {"dvol": pc * pv, "tags": pack_setups(closes[max(0, i - 60):i])}
    picked = {}
    for day, m in out.items():
        top = sorted(m, key=lambda s: -m[s]["dvol"])
        keep = set(top[:cfg["liquid_top"]])
        for tag in SETUPS:
            keep |= set([s for s in top if tag in m[s]["tags"]][:cfg["tag_top"]])
        picked[day] = {s: m[s] for s in keep}
    return picked


def five_min_grid(dc, syms: list[str], d: date, feed) -> dict:
    from alpaca.data.enums import Adjustment
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    start = datetime.combine(d, time(9, 30), ET)
    grid = pd.date_range(start, periods=N5, freq="5min")
    out = {}
    for i in range(0, len(syms), 200):
        req = StockBarsRequest(symbol_or_symbols=syms[i:i + 200], timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                               start=start, end=start + timedelta(minutes=5 * N5), feed=feed,
                               adjustment=Adjustment.SPLIT)
        df = _retry(lambda: dc.get_stock_bars(req)).df
        if df.empty:
            continue
        for s, g in df.groupby(level=0):
            g = g.droplevel(0)
            g.index = pd.DatetimeIndex(g.index).tz_convert(ET)
            g = g[~g.index.duplicated()].reindex(grid)
            if g["close"].iloc[:3].isna().any():          # the pack needs every range bar
                continue
            g["close"] = g["close"].ffill()
            for col in ("open", "high", "low"):
                g[col] = g[col].fillna(g["close"])
            out[s] = g[["open", "high", "low", "close"]]
    return out


def pack_trade(g: pd.DataFrame, target_r: float, cfg: dict) -> dict | None:
    o, h, l, c = (np.array(g[k], dtype=float) for k in ("open", "high", "low", "close"))
    k = cfg["or_bars5"]
    oh, ol = h[:k].max(), l[:k].min()
    if not oh > ol:
        return None
    for t in range(k, cfg["last_entry_bar5"] + 1):
        if c[t] > oh:                                      # first 5-minute bar to CLOSE above the range high
            break
    else:
        return None
    e_bar = t + 1                                          # we can only act once that bar has closed
    if e_bar >= cfg["flat_bar5"]:
        return None
    gross_fill = o[e_bar]
    fill = gross_fill + cfg["slip"]
    stop = math.floor((oh + ol) / 2 * 100) / 100           # range midpoint, rounded down (as the pack does)
    risk = fill - stop
    if risk < fill * cfg["min_risk_pct"] / 100:
        return None
    target = round(fill + target_r * risk, 2)
    exit_bar, gross_exit, why = cfg["flat_bar5"], o[cfg["flat_bar5"]], "flat 15:55"
    for b in range(e_bar, cfg["flat_bar5"]):
        if o[b] <= stop and b > e_bar:
            exit_bar, gross_exit, why = b, o[b], "stop"
            break
        if l[b] <= stop:
            exit_bar, gross_exit, why = b, min(stop, o[b]), "stop"
            break
        if h[b] >= target:
            exit_bar, gross_exit, why = b, target, "target"
            break
    exit_px = gross_exit - (cfg["slip"] if why != "target" else 0)
    return {"signal_bar": t, "entry_bar": e_bar, "exit_bar": exit_bar, "fill": fill, "stop": stop, "risk": risk,
            "target": target, "exit": exit_px, "why": why, "r": (exit_px - fill) / risk,
            "r_gross": (gross_exit - gross_fill) / (gross_fill - stop) if gross_fill > stop else 0.0}


def run_pack(days: list[date], uni: dict, bars: dict, tag: str | None, target_r: float, cfg: dict,
             start_equity: float = 100.0) -> tuple[list[dict], list[dict]]:
    """tag None = all liquid names; 'any' = any watchlist setup; else one setup."""
    eq, trades, daily = start_equity, [], []
    for d in days:
        cands = []
        for s, info in uni.get(d, {}).items():
            if tag == "any" and not info["tags"]:
                continue
            if tag not in (None, "any") and tag not in info["tags"]:
                continue
            g = bars.get(d, {}).get(s)
            if g is None:
                continue
            tr = pack_trade(g, target_r, cfg)
            if tr:
                cands.append((tr["entry_bar"], -info["dvol"], s, tr))
        cands.sort(key=lambda x: (x[0], x[1]))           # earliest trigger first; ties: most traded
        day_eq, cash, open_pos, realized, n = eq, eq, [], 0.0, 0
        for bar, _, s, tr in cands:
            if n >= cfg["max_trades"]:
                break
            for p in [p for p in open_pos if p[0] < bar]:
                cash += p[1]
                realized += p[2]
                open_pos.remove(p)
            if realized <= -day_eq * cfg["daily_loss_pct"] / 100:
                break
            qty = min(day_eq * cfg["risk_pct"] / 100 / tr["risk"], day_eq * cfg["max_alloc_pct"] / 100 / tr["fill"],
                      cash / tr["fill"])
            if qty * tr["fill"] < 1.0:
                continue
            pl = (tr["exit"] - tr["fill"]) * qty
            cash -= qty * tr["fill"]
            open_pos.append((tr["exit_bar"], qty * tr["fill"] + pl, pl))
            n += 1
            trades.append({"day": d.isoformat(), "symbol": s, "entry_time": f"{9 + (30 + 5 * bar) // 60:02d}:"
                           f"{(30 + 5 * bar) % 60:02d}", "why": tr["why"], "qty": round(qty, 4),
                           "fill": round(tr["fill"], 4), "stop": tr["stop"], "target": tr["target"],
                           "exit": round(tr["exit"], 4), "pl": round(pl, 4), "r": round(tr["r"], 3),
                           "r_gross": round(tr["r_gross"], 3), "tags": ",".join(uni[d][s]["tags"])})
        eq += sum(t["pl"] for t in trades if t["day"] == d.isoformat())
        daily.append({"day": d.isoformat(), "equity": round(eq, 4)})
    return trades, daily


PACK_VARIANTS = [("PACK ORB, top-200 liquid, 2R", None, 2.0), ("PACK ORB, top-200 liquid, 1R", None, 1.0),
                 ("PACK ORB, any watchlist setup, 2R", "any", 2.0)] + \
                [(f"PACK ORB, {t}, 2R", t, 2.0) for t in SETUPS]


def run_pack_suite(months: float, out_dir: Path) -> Path:
    from alpaca.data.enums import DataFeed
    cfg = PACK
    tc, dc = _clients()
    end = datetime.now(ET).date() - timedelta(days=1)
    start = end - timedelta(days=int(months * 30.5))
    sip = DataFeed.SIP
    log.info("Listing symbols...")
    syms = sorted(set(all_symbols(tc)) | {"SPY"})
    daily = daily_bars(dc, syms, start - timedelta(days=100), end, sip)
    spy = daily[daily.symbol == "SPY"].drop_duplicates("day").set_index("day").sort_index()
    days = sorted(d for d in spy.index if start <= d <= end)
    uni = pack_universe(daily[daily.symbol != "SPY"], days, cfg)
    log.info("%d days, ~%d stocks/day; fetching 5-minute bars...", len(days), int(np.mean([len(u) for u in uni.values()])))
    bars = {}
    for i, d in enumerate(days):
        try:
            bars[d] = five_min_grid(dc, sorted(uni[d]), d, sip)
        except Exception as e:  # noqa: BLE001
            log.warning("  %s failed: %s", d, e)
        if (i + 1) % 10 == 0:
            log.info("  %d / %d days", i + 1, len(days))
    days = [d for d in days if d in bars]
    res = {}
    for name, tag, tr_ in PACK_VARIANTS:
        tr, dl = run_pack(days, uni, bars, tag, tr_, cfg)
        res[name] = (stats2(tr, dl, "R"), tr, dl)
        log.info("%-40s %s", name, res[name][0])
    tag_counts = {t: float(np.mean([sum(t in v["tags"] for v in uni[d].values()) for d in days])) for t in SETUPS}
    L = [f"# day-trader-skill pack backtest: {days[0]} to {days[-1]}", "",
         f"{len(days)} trading days. Rules ported from github.com/Jakeschincariol/day-trader-skill: 15-min opening "
         "range on 5-min bars, buy at the next bar's open after the first 5-min CLOSE above the range high "
         "(9:45-15:30), stop at the range midpoint, flat 15:55, max 3 trades/day, 1% risk, no new trades after "
         "a -3% day. $100, long only, fractional shares, max 33% per position, 1 cent per share each way. "
         f"SPY over the period: {float((spy.loc[days[-1], 'close'] / spy.loc[days[0], 'open'] - 1) * 100):+.1f}%.", "",
         "Watchlist names per day (avg, before the top-50 cap): "
         + ", ".join(f"{t} {v:.0f}" for t, v in tag_counts.items()), "",
         "| Version | Trades | /day | Win % | Avg win R | Avg loss R | Avg R after costs | Avg R before costs "
         "| Profit factor | Return | Max DD | 1st half R | 2nd half R | Verdict |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, (s_, _, _) in res.items():
        if not s_.get("trades"):
            L.append(f"| {name} | 0 | | | | | | | | | | | | no trades |")
            continue
        L.append(f"| {name} | {s_['trades']} | {s_['per_day']} | {s_['win_pct']} | {s_['avg_win']} | "
                 f"{s_['avg_loss']} | {s_['avg']:+.3f} | {s_['gross']:+.3f} | {s_['pf']} | {s_['return_pct']:+.1f}% | "
                 f"{s_['max_dd_pct']}% | {s_['first_half']:+.3f} | {s_['second_half']:+.3f} | {verdict2(s_)} |")
    for name, (s_, tr, _) in res.items():
        if tr:
            t = pd.DataFrame(tr)
            g = t.groupby(t.day.str[:7]).agg(n=("r", "size"), avg=("r", "mean"), pl=("pl", "sum"))
            L += ["", f"### {name}", "Exits: " + ", ".join(f"{k} {v}" for k, v in t.why.value_counts().items()),
                  "By month: " + "; ".join(f"{m}: {int(r.n)} trades, {r.avg:+.3f}R, ${r.pl:+.2f}" for m, r in g.iterrows())]
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ET).strftime("%Y-%m-%d")
    md = out_dir / f"pack-{stamp}-{months:g}m.md"
    md.write_text("\n".join(L) + "\n")
    frames = [pd.DataFrame(tr).assign(version=n) for n, (_, tr, _) in res.items() if tr]
    if frames:
        pd.concat(frames).to_csv(out_dir / f"pack-{stamp}-{months:g}m-trades.csv", index=False)
    log.info("wrote %s", md)
    return md


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
    ap.add_argument("--top", type=int, default=30, help="gap-up stocks per day to replay (classic suite)")
    ap.add_argument("--suite", default="robust", choices=["robust", "swing", "all", "new", "pack", "classic"],
                    help="robust = IBS on ~19 funds and rule variants; "
                         "swing = 1-5 day strategies on ~10 years of daily data; all = new + pack; "
                         "new = stocks-in-play ORB, news split, last-half-hour momentum; "
                         "pack = the day-trader-skill system; classic = the old bot")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    if a.suite == "robust":
        from .swing import run_robust
        tc, dc = _clients()
        end = datetime.now(ET).date() - timedelta(days=1)
        run_robust(dc, date(2016, 11, 1), end, ROOT / "results", retry=_retry)
    if a.suite == "swing":
        from .swing import run_swing
        tc, dc = _clients()
        end = datetime.now(ET).date() - timedelta(days=1)
        start = max(date(2016, 11, 1), end - timedelta(days=int(a.months * 30.5)))   # 200-day avg needs warm-up
        run_swing(dc, all_symbols(tc), start, end, ROOT / "results", retry=_retry)
    if a.suite in ("all", "pack"):
        run_pack_suite(a.months, ROOT / "results")
    if a.suite in ("all", "new"):
        run_new(a.months, ROOT / "results")
    if a.suite == "classic":
        run(a.months, a.top, ROOT / "results")


if __name__ == "__main__":
    main()
