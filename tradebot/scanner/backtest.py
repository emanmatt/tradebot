"""Backtest for Step 1: did the scanner's picks keep going up after they made the list?

  python -m scanner.backtest --months 3

For every past trading day:
  1. Rebuild the momentum screen exactly as the live scanner would have seen it at a decision time
     (default 9:35 AM ET, also 9:25 pre-market and 10:00): gap vs yesterday's close, volume so far vs normal
     for that time of day, price range, liquidity, news since yesterday's close. Nothing from later in the day
     is used to pick.
  2. "Buy" each pick at the next 5-minute bar's open (9:30 open for the pre-market scan) and measure what
     happened: after 30 min, 60 min, at 11:30, at the close, and the next morning's open.
  3. Compare with a control group: stocks that gapped up just as much but FAILED the volume test.
     If picks don't beat the control group, the volume filter isn't adding anything.

Data: Alpaca's full-market (SIP) history, free once it is 15+ minutes old. The live scanner uses the smaller
free IEX feed, so live numbers will be noisier. The smart money screen can't be backtested yet: the tracker
has only just started collecting.
Costs: results are shown before and after 0.2% round-trip costs (spread + slippage on small caps).
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta
import logging
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from bot.config import ROOT, env, load_config
from . import momentum

log = logging.getLogger("scanner.backtest")
ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
COST_PCT = 0.2
HORIZONS = [("30 min", 30), ("60 min", 60), ("11:30", "11:30"), ("close", "15:55"), ("next open", "next")]


def _t(s: str) -> time:
    return time(*map(int, s.split(":")))


# ======================================================================== pure logic (tested offline)
def prefilter(g: pd.DataFrame, c: dict) -> pd.DataFrame:
    """Stocks that COULD pass at the decision time. Uses the day's high only to decide what to download
    (anything up 5%+ at 9:35 must have a day high 5%+ above yesterday's close), never to pick."""
    return g[(g["prev_close"].between(c["min_price"] * 0.8, c["max_price"] * 1.2))
             & (g["avg_dvol"] >= c["min_avg_dollar_volume"])
             & ((g["high"] / g["prev_close"] - 1) * 100 >= c["min_gap_pct"])]


def outcome(bars: pd.DataFrame, entry_i: int, next_open: float | None) -> dict:
    """Returns (%) from the entry bar's open to each horizon, plus best/worst point before the close."""
    entry = float(bars["open"].iloc[entry_i])
    t0 = bars.index[entry_i]
    out = {"entry": entry}
    after = bars.iloc[entry_i:]
    reg = after[after.index.time <= time(15, 55)]
    for name, h in HORIZONS:
        if h == "next":
            px = next_open
        else:
            cutoff = t0 + timedelta(minutes=h) if isinstance(h, int) else datetime.combine(t0.date(), _t(h), ET)
            win = reg[reg.index < cutoff]
            px = float(win["close"].iloc[-1]) if len(win) and (isinstance(h, int) or cutoff > t0) else None
        out[name] = (px / entry - 1) * 100 if px else None
    if len(reg):
        out["best"] = (float(reg["high"].max()) / entry - 1) * 100
        out["worst"] = (float(reg["low"].min()) / entry - 1) * 100
    return out


def screen_day(d: date, cand: pd.DataFrame, bars: dict, news: dict, next_open: dict, c: dict,
               decision: str) -> list[dict]:
    """Replays the live momentum screen for one day at one decision time. Returns picks and controls."""
    dt = datetime.combine(d, _t(decision), ET)
    rows = []
    for r in cand.itertuples():
        b = bars.get(r.symbol)
        if b is None or b.empty:
            continue
        seen = b[b.index + timedelta(minutes=5) <= dt]            # bars fully finished by the decision time
        if seen.empty:
            continue
        price = float(seen["close"].iloc[-1])
        gap = (price / r.prev_close - 1) * 100
        if not (c["min_price"] <= price <= c["max_price"] and c["min_gap_pct"] <= gap <= c["max_gap_pct"]):
            continue
        rv = momentum.rvol(float(seen["volume"].sum()), r.avg_vol, dt, c["volume_curve"])
        if rv is None:
            continue
        has_news = any(t < dt for t in news.get(r.symbol, []))    # only headlines out before the scan
        entry_from = max(dt, datetime.combine(d, OPEN, ET))
        later = np.flatnonzero(b.index >= entry_from)
        if not len(later):
            continue
        passed = rv >= c["min_rvol"] and (has_news or not c.get("require_news"))
        rec = {"day": d, "decision": decision, "symbol": r.symbol, "group": "pick" if passed else "control",
               "price": price, "gap": gap, "rvol": rv, "news": has_news,
               "score": momentum.score(gap, rv, has_news, c)}
        rec.update(outcome(b, int(later[0]), next_open.get(r.symbol)))
        rows.append(rec)
    picks = sorted((x for x in rows if x["group"] == "pick"), key=lambda x: -x["score"])
    for i, x in enumerate(picks, 1):
        x["rank"] = i
        if i > c["max_watchlist"]:
            x["group"] = "pick (beyond top 20)"
    return rows


def summarize(df: pd.DataFrame, col: str) -> dict:
    v = df[col].dropna()
    if len(v) == 0:
        return {"n": 0}
    days = df.dropna(subset=[col]).groupby("day")[col].mean()       # days are not independent: also judge per day
    t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 2 and v.std() > 0 else 0.0
    td = days.mean() / (days.std(ddof=1) / math.sqrt(len(days))) if len(days) > 2 and days.std() > 0 else 0.0
    return {"n": len(v), "days": len(days), "avg": v.mean(), "median": v.median(), "up": (v > 0).mean() * 100,
            "after_cost": v.mean() - COST_PCT, "t": t, "t_days": td, "pf": _pf(v - COST_PCT)}


def _pf(v: pd.Series) -> float:
    loss = -v[v < 0].sum()
    return float(v[v > 0].sum() / loss) if loss > 0 else float("inf")


def verdict(pick: dict, ctrl: dict) -> str:
    if pick.get("n", 0) < 30:
        return "⚪ Too few picks to judge (need 30+). Run more months."
    beats = ctrl.get("n", 0) >= 10 and pick["avg"] > ctrl["avg"]
    if pick["after_cost"] > 0 and pick["t_days"] >= 2 and beats:
        return "🟢 Picks rose on average after costs, consistently across days, and beat the control group."
    if pick["after_cost"] > 0 and beats:
        return "🟡 Positive after costs and better than the control group, but not consistent enough to trust yet."
    if beats:
        return "🟠 Better than the control group, but not profitable after costs on its own: needs a better entry/exit (Step 2)."
    return "🔴 No edge: picks did not beat similar gappers that failed the volume test."


def report(df: pd.DataFrame, meta: dict) -> str:
    L = [f"# Scanner backtest ({meta['start']} to {meta['end']})", "",
         f"{meta['days']} trading days · full-market data · costs {COST_PCT}% round trip · "
         f"rules from config.yaml `scanner:` (gap ≥{meta['c']['min_gap_pct']}%, volume ≥{meta['c']['min_rvol']}x, "
         f"${meta['c']['min_price']}-${meta['c']['max_price']})", "",
         "**How to read this:** each pick is 'bought' at the open of the next 5-minute bar after the scan. "
         "'avg' is the average % move to that point; 'up' is how often it was higher; 't (days)' above 2 "
         "means the result held up day after day rather than coming from a few lucky days. The control group "
         "gapped up just as much but failed the volume test.", ""]
    for dec in meta["decisions"]:
        sub = df[df["decision"] == dec]
        top = sub[sub["group"] == "pick"]
        ctrl = sub[sub["group"] == "control"]
        L += [f"## Scan at {dec} ET", "",
              f"Picks: {len(top)} ({len(top) / max(meta['days'], 1):.1f} per day) · control: {len(ctrl)}", "",
              "| Hold until | Group | n | avg % | median % | up % | avg after cost % | t | t (days) |",
              "|---|---|---|---|---|---|---|---|---|"]
        for name, _ in HORIZONS:
            for label, g in (("picks", top), ("control", ctrl)):
                s = summarize(g, name)
                if s["n"]:
                    L.append(f"| {name} | {label} | {s['n']} | {s['avg']:+.2f} | {s['median']:+.2f} | {s['up']:.0f} | "
                             f"{s['after_cost']:+.2f} | {s['t']:.1f} | {s['t_days']:.1f} |")
        L += ["", "**Verdict (hold until close):** " + verdict(summarize(top, "close"), summarize(ctrl, "close")), ""]
        if len(top):
            L += ["Best/worst point reached before the close (avg): "
                  f"up {top['best'].mean():+.1f}% / down {top['worst'].mean():+.1f}%. "
                  "A big gap between these means exits (Step 2) decide the result more than the picks do.", ""]
            L += ["**Does the ranking help?** (hold until close)", "", "| Bucket | n | avg % | up % |", "|---|---|---|---|"]
            for label, g in (("top 5 by score", top[top["rank"] <= 5]), ("rank 6-20", top[top["rank"] > 5]),
                             ("with news", top[top["news"]]), ("no news", top[~top["news"]]),
                             ("gap 5-10%", top[top["gap"] < 10]), ("gap 10-20%", top[top["gap"].between(10, 20)]),
                             ("gap 20%+", top[top["gap"] > 20]), ("volume 3-6x", top[top["rvol"] < 6]),
                             ("volume 6-15x", top[top["rvol"].between(6, 15)]), ("volume 15x+", top[top["rvol"] > 15]),
                             ("price under $5", top[top["price"] < 5]), ("price $5+", top[top["price"] >= 5])):
                s = summarize(g, "close")
                if s["n"]:
                    L.append(f"| {label} | {s['n']} | {s['avg']:+.2f} | {s['up']:.0f} |")
            L.append("")
    spy = meta.get("spy")
    if spy is not None:
        L += [f"SPY over the same days, 9:35 to close: avg {spy:+.2f}%/day (the market's own drift, for context).", ""]
    L += ["## Limits", "- Smart-money picks are not included (no history yet).",
          "- The live scanner only sees Alpaca's most-active and top-gainer lists and the smaller IEX feed; "
          "this test sees every stock with full-market data, so it shows the screen's best case.",
          "- No stop-loss or target is applied here: this tests the PICKS. Step 2 adds and tests the exits."]
    return "\n".join(L)


# ======================================================================== data (Alpaca, runs on GitHub)
def five_min(dc, syms: list[str], d: date, feed) -> dict:
    from alpaca.data.enums import Adjustment
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from bot.backtest import _retry
    out = {}
    for i in range(0, len(syms), 200):
        req = StockBarsRequest(symbol_or_symbols=syms[i:i + 200], timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                               start=datetime.combine(d, time(4, 0), ET), end=datetime.combine(d, time(16, 0), ET),
                               feed=feed, adjustment=Adjustment.SPLIT)
        df = _retry(lambda: dc.get_stock_bars(req)).df
        if df.empty:
            continue
        for s, g in df.groupby(level=0):
            g = g.droplevel(0)
            g.index = pd.DatetimeIndex(g.index).tz_convert(ET)
            out[s] = g[~g.index.duplicated()].sort_index()[["open", "high", "low", "close", "volume"]]
    return out


def news_flags(nc, syms: list[str], start: datetime, end: datetime) -> dict:
    from alpaca.data.requests import NewsRequest
    from bot.backtest import _retry
    times = defaultdict(list)
    for i in range(0, len(syms), 50):
        token = None
        for _ in range(5):
            req = NewsRequest(symbols=",".join(syms[i:i + 50]), start=start, end=end, limit=50,
                              exclude_contentless=False, page_token=token)
            res = _retry(lambda: nc.get_news(req))
            for n in res.data.get("news", []):
                for s in n.symbols or []:
                    times[s].append(pd.Timestamp(n.created_at).tz_convert(ET).to_pydatetime())
            token = res.next_page_token
            if not token:
                break
    return dict(times)


def run(months: float, out_dir: Path, decisions: list[str]) -> Path:
    import time as _time
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical.news import NewsClient
    from bot.backtest import _clients, all_symbols, daily_bars
    c = load_config()["scanner"]
    tc, dc = _clients()
    nc = NewsClient(env("ALPACA_API_KEY"), env("ALPACA_SECRET_KEY"))
    end = datetime.now(ET).date() - timedelta(days=1)
    start = end - timedelta(days=int(months * 30.5))
    log.info("Listing symbols...")
    syms = sorted(set(all_symbols(tc)) | {"SPY"})
    log.info("%d symbols. Daily bars (full market)...", len(syms))
    # free plan: full-market data must be 15+ min old, so never ask past yesterday
    daily = daily_bars(dc, syms, start - timedelta(days=45), end, DataFeed.SIP)
    daily = daily.sort_values(["symbol", "day"])
    g = daily.groupby("symbol")
    daily["prev_close"] = g["close"].shift(1)
    daily["next_open"] = g["open"].shift(-1)
    daily["avg_vol"] = g["volume"].transform(lambda v: v.shift(1).rolling(c["baseline_days"], min_periods=10).mean())
    daily["avg_dvol"] = (daily["close"] * daily["volume"]).groupby(daily["symbol"]).transform(
        lambda v: v.shift(1).rolling(c["baseline_days"], min_periods=10).mean())
    by_day = dict(tuple(daily.groupby("day")))
    days = [d for d in sorted(by_day) if start <= d <= end]

    recs, spy_moves = [], []
    for i, d in enumerate(days):
        cand = prefilter(by_day[d].dropna(subset=["prev_close", "avg_vol", "avg_dvol"]), c)
        cand = cand.nlargest(400, "avg_dvol")
        names = cand["symbol"].tolist() + ["SPY"]
        bars = five_min(dc, names, d, DataFeed.SIP)
        prev = max(x for x in by_day if x < d) if any(x < d for x in by_day) else d - timedelta(days=1)
        news = news_flags(nc, cand["symbol"].tolist(), datetime.combine(prev, time(16, 0), ET),
                          datetime.combine(d, _t(max(decisions)), ET)) if len(cand) else {}
        nxt = dict(zip(cand["symbol"], cand["next_open"]))
        for dec in decisions:
            recs += screen_day(d, cand, bars, news, nxt, c, dec)
        s = bars.get("SPY")
        if s is not None and len(s):
            r = s[(s.index.time >= time(9, 35)) & (s.index.time <= time(15, 55))]
            if len(r):
                spy_moves.append((float(r["close"].iloc[-1]) / float(r["open"].iloc[0]) - 1) * 100)
        log.info("  %s: %d candidates, %d picks so far", d, len(cand), sum(x["group"] == "pick" for x in recs))
        _time.sleep(0.2)

    df = pd.DataFrame(recs)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now(ET).strftime("%Y-%m-%d")
    meta = {"start": days[0] if days else start, "end": days[-1] if days else end, "days": len(days),
            "decisions": decisions, "c": c, "spy": float(np.mean(spy_moves)) if spy_moves else None}
    text = report(df, meta) if len(df) else "# Scanner backtest\n\nNo stocks passed on any day."
    path = out_dir / f"scanner-backtest-{tag}.md"
    path.write_text(text)
    if len(df):
        df.to_csv(out_dir / f"scanner-backtest-{tag}.csv", index=False)
    print(text)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=float, default=3)
    ap.add_argument("--at", nargs="*", default=["09:25", "09:35", "10:00"], help="scan times (ET) to test")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    run(a.months, ROOT / "results", a.at)


if __name__ == "__main__":
    main()
