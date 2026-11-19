"""Short-term (1-5 day) strategies replayed on ~10 years of daily data.

  python -m bot.backtest --suite swing --months 130

Strategies (all long-only, fractional shares, $100 start, costs on every buy and sell):
  RSI2 stocks     Connors: top-100 most-traded stocks; buy when close > 200-day avg, close < 5-day avg and
                  2-day RSI < 10 (lowest RSI first, 4 slots); sell when the close is back above the 5-day avg.
  RSI2 SPY / QQQ  same rules on the ETF, all-in.
  IBS SPY / QQQ   buy when the close is in the bottom 10% of the day's range, sell when in the top 10%.
                  Two versions: trade at the next open, or at the close (market-on-close).
  Double 7        SPY/QQQ above 200-day avg closes at a 7-day low -> buy at close; sell at a 7-day high close.
  Reversal        each Friday, buy the 10 biggest 1-week losers among the top-100 stocks, sell next Friday.
  Turn of month   SPY: buy 4 days before month end, sell 3 days into the new month.
  Overnight       SPY: buy every close, sell the next open.
  Gap + hold      stocks gapping up 8%+ on 3x volume that close in the top half of the day's range and had not
                  already run up 20%+: buy at the close, sell 1 or 3 days later.
  SPY buy & hold  benchmark.

Caveat: the stock list is today's listed companies (delisted ones are missing), which flatters dip-buying
on individual stocks a little. The ETF strategies don't have that problem.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

log = logging.getLogger("swing")

ETF_COST = 0.0002          # 0.02% per side (SPY/QQQ spreads are ~1 cent on $400-600)
STOCK_COST = 0.0005        # 0.05% per side on the most-traded large stocks
KEEP = ("SPY", "QQQ")
MIN_PRICE, MIN_DVOL = 5.0, 2e7
TOP_N = 100


# ======================= data =======================
def fetch_daily(dc, syms: list[str], start: date, end: date, retry=lambda f: f()) -> dict[str, pd.DataFrame]:
    """Daily bars (split+dividend adjusted). Keeps only liquid names (price >= $5 and >= $20M a day traded)
    plus SPY/QQQ, so ~10 years of the whole market fits in memory."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    out = {}
    for i in range(0, len(syms), 400):
        req = StockBarsRequest(symbol_or_symbols=syms[i:i + 400], timeframe=TimeFrame.Day, adjustment=Adjustment.ALL,
                               start=datetime.combine(start, time(0), et), end=datetime.combine(end, time(23, 59), et),
                               feed=DataFeed.SIP)
        df = retry(lambda: dc.get_stock_bars(req)).df
        if df.empty:
            continue
        for s, g in df.groupby(level=0):
            g = g.droplevel(0)
            g.index = pd.DatetimeIndex(g.index).tz_convert(et).date
            g = g[~pd.Index(g.index).duplicated()][["open", "high", "low", "close", "volume"]].astype("float64")
            dv = (g["close"] * g["volume"]).rolling(20, min_periods=10).mean()
            if s in KEEP or ((g["close"] >= MIN_PRICE) & (dv >= MIN_DVOL)).any():
                out[s] = g
        log.info("  daily bars: %d / %d symbols (%d liquid kept)", min(i + 400, len(syms)), len(syms), len(out))
    return out


# ======================= indicators =======================
def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def indicators(g: pd.DataFrame) -> pd.DataFrame:
    c = g["close"]
    x = g.copy()
    x["sma200"] = c.rolling(200, min_periods=200).mean()
    x["sma5"] = c.rolling(5).mean()
    x["rsi2"] = rsi(c, 2).fillna(50)
    rng = (g["high"] - g["low"]).replace(0, np.nan)
    x["ibs"] = ((c - g["low"]) / rng).fillna(0.5)
    x["low7"] = c.rolling(7).min()
    x["high7"] = c.rolling(7).max()
    x["ret5"] = c / c.shift(5) - 1
    x["ret20"] = c.shift(1) / c.shift(21) - 1
    x["gap"] = g["open"] / c.shift(1) - 1
    x["rvol"] = g["volume"] / g["volume"].rolling(20).mean().shift(1)
    x["dvol20"] = (c * g["volume"]).rolling(20, min_periods=10).mean().shift(1)    # known before the day
    return x


# ======================= generic portfolio simulator =======================
@dataclass
class Spec:
    name: str
    syms: list[str] | None                    # None = top-100 most traded stocks each day
    slots: int
    entry: Callable[[pd.DataFrame], pd.Series]   # signal known at the close
    exit: Callable[[pd.DataFrame], pd.Series] | None = None
    rank: Callable[[pd.DataFrame], pd.Series] | None = None   # lower = better
    enter_at: str = "next_open"               # "next_open" | "close"
    exit_at: str = "next_open"
    max_hold: int | None = None               # sell after this many closes held (0 = next open after a close entry)
    cost: float = STOCK_COST
    entry_days: Callable[[list[date]], set] | None = None    # calendar-based entries (turn of month, Fridays)


def simulate(spec: Spec, ind: dict[str, pd.DataFrame], dates: list[date], universe: dict[date, set],
             start_equity: float = 100.0, cost_mult: float = 1.0) -> tuple[list[dict], pd.Series]:
    """Day loop. At each open: run sells, then buys decided at the previous close. At each close: check
    exits (signal or max holding time) and new entries. Equal-weight slots, never more than the cash we have."""
    cost = spec.cost * cost_mult
    syms = spec.syms or sorted({s for u in universe.values() for s in u})
    syms = [s for s in syms if s in ind]
    sig_e = {s: spec.entry(ind[s]).reindex(dates).fillna(False).to_numpy(bool) for s in syms}
    sig_x = {s: (spec.exit(ind[s]).reindex(dates).fillna(False).to_numpy(bool) if spec.exit
                 else np.zeros(len(dates), bool)) for s in syms}
    rank = {s: (spec.rank(ind[s]).reindex(dates).to_numpy(float) if spec.rank else np.zeros(len(dates)))
            for s in syms}
    px = {s: ind[s][["open", "close"]].reindex(dates).to_numpy(float) for s in syms}
    cal = spec.entry_days(dates) if spec.entry_days else None

    book = {"cash": start_equity}
    pos, trades, curve = {}, [], []
    pend_buy, pend_sell = [], []

    def price(s, i, col):
        v = px[s][i, col]
        if np.isnan(v):                       # no trade that bar: use the last known close
            prev = px[s][:i + 1, 1]
            prev = prev[~np.isnan(prev)]
            v = prev[-1] if len(prev) else np.nan
        return v

    def equity(i, col):
        return book["cash"] + sum(p["qty"] * price(s, i, col) for s, p in pos.items())

    def buy(s, i, col, at):
        px_ = px[s][i, col]
        if np.isnan(px_) or px_ <= 0:
            return False
        spend = min(equity(i, col) / spec.slots, book["cash"])
        if spend < 1.0:
            return False
        book["cash"] -= spend
        pos[s] = {"qty": spend * (1 - cost) / px_, "entry": px_, "i": i, "day": dates[i], "at": at, "spent": spend}
        return True

    def sell(s, i, col):
        p = pos.pop(s)
        px_ = price(s, i, col)
        proceeds = p["qty"] * px_ * (1 - cost)
        book["cash"] += proceeds
        trades.append({"strategy": spec.name, "symbol": s, "entry_day": p["day"].isoformat(),
                       "exit_day": dates[i].isoformat(), "entry": round(p["entry"], 4), "exit": round(float(px_), 4),
                       "ret_pct": round((proceeds / p["spent"] - 1) * 100, 3), "pl": round(proceeds - p["spent"], 4)})

    for i, d in enumerate(dates):
        # ---- open ----
        for s in pend_sell:
            if s in pos:
                sell(s, i, 0)
        pend_sell = []
        for s in pend_buy:
            buy(s, i, 0, "open")
        pend_buy = []
        # ---- close: exits ----
        for s in list(pos):
            p = pos[s]
            closes_held = i - p["i"] + (1 if p["at"] == "open" else 0)
            if closes_held < 1:
                continue
            if (spec.max_hold is not None and spec.max_hold > 0 and closes_held >= spec.max_hold) or sig_x[s][i]:
                if spec.exit_at == "close":
                    sell(s, i, 1)
                else:
                    pend_sell.append(s)
        # ---- close: entries ----
        free = spec.slots - (len(pos) - len(pend_sell))
        if free > 0 and (cal is None or d in cal):
            uni = universe.get(d, set()) if spec.syms is None else set(syms)
            cands = sorted((rank[s][i], s) for s in syms
                           if s in uni and s not in pos and sig_e[s][i] and not np.isnan(px[s][i, 1]))
            for _, s in cands[:free]:
                if spec.enter_at == "close":
                    if buy(s, i, 1, "close") and spec.max_hold == 0:
                        pend_sell.append(s)
                else:
                    pend_buy.append(s)
        curve.append(equity(i, 1))
    return trades, pd.Series(curve, index=pd.Index(dates))


# ======================= the strategies =======================
def month_end_window(dates: list[date], before: int = 4) -> set:
    """Entry days for turn of the month: `before` trading days before each month's last trading day
    (buy at that close, hold 7 closes -> sell at the close of the 3rd trading day of the new month)."""
    last = [i for i in range(len(dates)) if i == len(dates) - 1 or dates[i + 1].month != dates[i].month]
    return {dates[i - before] for i in last if i - before >= 0}


def fridays(dates: list[date]) -> set:
    """Last trading day of each week."""
    return {d for i, d in enumerate(dates) if i == len(dates) - 1 or dates[i + 1].isocalendar()[1] != d.isocalendar()[1]}


def specs() -> list[Spec]:
    rsi2_e = lambda x: (x.close > x.sma200) & (x.close < x.sma5) & (x.rsi2 < 10)  # noqa: E731
    rsi2_x = lambda x: x.close > x.sma5                                              # noqa: E731
    ibs_e = lambda x: x.ibs < 0.1                                                    # noqa: E731
    ibs_x = lambda x: x.ibs > 0.9                                                    # noqa: E731
    d7_e = lambda x: (x.close > x.sma200) & (x.close <= x.low7)                      # noqa: E731
    d7_x = lambda x: x.close >= x.high7                                              # noqa: E731
    gap_e = lambda x: ((x.gap >= 0.08) & (x.rvol >= 3) & (x.ibs >= 0.5) & (x.ret20 < 0.20))  # noqa: E731
    out = [
        Spec("RSI2 stocks (top-100, 4 slots)", None, 4, rsi2_e, rsi2_x, rank=lambda x: x.rsi2),
        Spec("RSI2 SPY", ["SPY"], 1, rsi2_e, rsi2_x, cost=ETF_COST),
        Spec("RSI2 QQQ", ["QQQ"], 1, rsi2_e, rsi2_x, cost=ETF_COST),
        Spec("IBS SPY (next open)", ["SPY"], 1, ibs_e, ibs_x, cost=ETF_COST),
        Spec("IBS QQQ (next open)", ["QQQ"], 1, ibs_e, ibs_x, cost=ETF_COST),
        Spec("IBS SPY (at close)", ["SPY"], 1, ibs_e, ibs_x, enter_at="close", exit_at="close", cost=ETF_COST),
        Spec("IBS QQQ (at close)", ["QQQ"], 1, ibs_e, ibs_x, enter_at="close", exit_at="close", cost=ETF_COST),
        Spec("Double 7 SPY", ["SPY"], 1, d7_e, d7_x, enter_at="close", exit_at="close", cost=ETF_COST),
        Spec("Double 7 QQQ", ["QQQ"], 1, d7_e, d7_x, enter_at="close", exit_at="close", cost=ETF_COST),
        Spec("Weekly reversal (10 losers)", None, 10, lambda x: x.close > 0, None, rank=lambda x: x.ret5,
             enter_at="close", exit_at="close", max_hold=5, entry_days=fridays),
        Spec("Turn of month SPY", ["SPY"], 1, lambda x: x.close > 0, None, enter_at="close", exit_at="close",
             max_hold=7, cost=ETF_COST, entry_days=month_end_window),
        Spec("Overnight SPY", ["SPY"], 1, lambda x: x.close > 0, None, enter_at="close", exit_at="next_open",
             max_hold=0, cost=ETF_COST),
        Spec("Gap + hold 1 day", None, 5, gap_e, None, rank=lambda x: -x.rvol, enter_at="close",
             exit_at="close", max_hold=1),
        Spec("Gap + hold 3 days", None, 5, gap_e, None, rank=lambda x: -x.rvol, enter_at="close",
             exit_at="close", max_hold=3),
    ]
    return out


# ======================= stats & report =======================
def perf(curve: pd.Series, trades: list[dict]) -> dict:
    if curve.empty:
        return {}
    yrs = max((curve.index[-1] - curve.index[0]).days / 365.25, 1e-9)
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / yrs) - 1
    dd = ((curve.cummax() - curve) / curve.cummax()).max()
    t = pd.DataFrame(trades)
    out = {"final": curve.iloc[-1], "cagr": cagr * 100, "max_dd": dd * 100, "trades": len(t)}
    if len(t):
        w, l = t[t.pl > 0], t[t.pl <= 0]
        out.update({"win": len(w) / len(t) * 100, "avg": t.ret_pct.mean(),
                    "pf": w.pl.sum() / -l.pl.sum() if len(l) and l.pl.sum() < 0 else float("inf"),
                    "days_held": (pd.to_datetime(t.exit_day) - pd.to_datetime(t.entry_day)).dt.days.mean()})
    half = curve.index[len(curve) // 2]
    for k, part in (("cagr_1st", curve[curve.index <= half]), ("cagr_2nd", curve[curve.index >= half])):
        y = max((part.index[-1] - part.index[0]).days / 365.25, 1e-9)
        out[k] = ((part.iloc[-1] / part.iloc[0]) ** (1 / y) - 1) * 100
    yearly = curve.groupby([d.year for d in curve.index]).agg(["first", "last"])
    out["years"] = ((yearly["last"] / yearly["first"] - 1) * 100).round(1).to_dict()
    out["beat_spy_years"] = None
    return out


def build_universe(ind: dict[str, pd.DataFrame], dates: list[date]) -> dict[date, set]:
    stocks = [s for s in ind if s not in KEEP]
    dv = pd.DataFrame({s: ind[s]["dvol20"] for s in stocks}).reindex(dates)
    pxc = pd.DataFrame({s: ind[s]["close"].shift(1) for s in stocks}).reindex(dates)
    dv = dv.where(pxc >= MIN_PRICE)
    uni = {}
    for d, row in dv.iterrows():
        top = row.dropna().nlargest(TOP_N)
        uni[d] = set(top.index)
    return uni


def run_swing(dc, symbols: list[str], start: date, end: date, out_dir: Path, retry=lambda f: f()) -> Path:
    warm = start - timedelta(days=320)
    data = fetch_daily(dc, sorted(set(symbols) | set(KEEP)), warm, end, retry)
    ind = {s: indicators(g) for s, g in data.items()}
    spy = ind["SPY"]
    dates = [d for d in spy.index if start <= d <= end]
    uni = build_universe(ind, dates)
    res = {}
    bh = spy["close"].reindex(dates)
    res["SPY buy & hold"] = (perf(bh / bh.iloc[0] * 100, []), [], bh / bh.iloc[0] * 100)
    for sp in specs():
        tr, curve = simulate(sp, ind, dates, uni)
        tr2, curve2 = simulate(sp, ind, dates, uni, cost_mult=2.0)
        p = perf(curve, tr)
        p["cagr_2x_cost"] = perf(curve2, tr2).get("cagr", float("nan"))
        p["exposure"] = None
        res[sp.name] = (p, tr, curve)
        log.info("%-32s CAGR %+.1f%%  DD %.1f%%  trades %d", sp.name, p.get("cagr", 0), p.get("max_dd", 0),
                 p.get("trades", 0))
    return write_report(res, dates, out_dir)


def verdict(p: dict, spy: dict) -> str:
    if p.get("trades", 0) < 30:
        return "too few trades"
    beats = p["cagr"] > 0 and p["cagr_1st"] > 0 and p["cagr_2nd"] > 0 and p.get("cagr_2x_cost", -1) > 0
    risk_adj = p["cagr"] / max(p["max_dd"], 1) > spy["cagr"] / max(spy["max_dd"], 1)
    if beats and risk_adj:
        return "PROMISING (both halves, survives 2x costs, better return per drawdown than SPY)"
    if beats:
        return "positive, but SPY did better per unit of risk"
    return "LOSES / not robust"


def write_report(res: dict, dates: list[date], out_dir: Path) -> Path:
    spy = res["SPY buy & hold"][0]
    L = [f"# Short-term (1-5 day) strategies: {dates[0]} to {dates[-1]}", "",
         f"{len(dates)} trading days (~{len(dates) / 252:.1f} years). $100 start, long only, fractional shares. "
         f"Costs per side: {ETF_COST * 100:.2f}% on SPY/QQQ, {STOCK_COST * 100:.2f}% on stocks; also re-run at 2x costs. "
         "Stock strategies use the 100 most-traded stocks each day (today's listed names only, so delisted "
         "companies are missing - this flatters dip-buying on single stocks a little).", "",
         "| Strategy | $100 becomes | CAGR | Max drawdown | CAGR / DD | Trades | Win % | Avg trade | Profit factor "
         "| Avg days held | 1st half CAGR | 2nd half CAGR | CAGR at 2x costs | Verdict |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, (p, tr, _) in res.items():
        if not p:
            continue
        if name == "SPY buy & hold":
            L.append(f"| **{name}** | ${p['final']:.0f} | {p['cagr']:+.1f}% | {p['max_dd']:.1f}% | "
                     f"{p['cagr'] / max(p['max_dd'], 1):.2f} | - | - | - | - | - | {p['cagr_1st']:+.1f}% | "
                     f"{p['cagr_2nd']:+.1f}% | - | benchmark |")
            continue
        if not p.get("trades"):
            L.append(f"| {name} | $100 | | | | 0 | | | | | | | | no trades |")
            continue
        L.append(f"| {name} | ${p['final']:.0f} | {p['cagr']:+.1f}% | {p['max_dd']:.1f}% | "
                 f"{p['cagr'] / max(p['max_dd'], 1):.2f} | {p['trades']} | {p['win']:.0f} | {p['avg']:+.2f}% | "
                 f"{p['pf']:.2f} | {p['days_held']:.1f} | {p['cagr_1st']:+.1f}% | {p['cagr_2nd']:+.1f}% | "
                 f"{p['cagr_2x_cost']:+.1f}% | {verdict(p, spy)} |")
    L += ["", "CAGR = average yearly growth. CAGR / DD = yearly growth per % of worst drop (higher is better). "
          "A strategy only counts as promising if it made money in both halves of the period, still made money "
          "with double the costs, and earned more per unit of drawdown than just holding SPY.", "",
          "## Year by year (% return)", ""]
    years = sorted({y for p, _, _ in res.values() if p for y in p.get("years", {})})
    L.append("| Strategy | " + " | ".join(str(y) for y in years) + " |")
    L.append("|---|" + "---|" * len(years))
    for name, (p, _, _) in res.items():
        if p and p.get("years"):
            L.append(f"| {name} | " + " | ".join(f"{p['years'].get(y, float('nan')):+.1f}" for y in years) + " |")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    md = out_dir / f"swing-{stamp}.md"
    md.write_text("\n".join(L) + "\n")
    frames = [pd.DataFrame(tr) for _, (_, tr, _) in res.items() if tr]
    if frames:
        pd.concat(frames).to_csv(out_dir / f"swing-{stamp}-trades.csv", index=False)
    pd.DataFrame({n: c for n, (_, _, c) in res.items()}).to_csv(out_dir / f"swing-{stamp}-equity.csv")
    log.info("wrote %s", md)
    return md


# ======================= IBS robustness test =======================
ROBUST_ETFS = {
    "SPY": "S&P 500", "QQQ": "Nasdaq-100", "IWM": "small caps", "DIA": "Dow 30", "MDY": "mid caps",
    "XLK": "tech", "SMH": "semiconductors", "XLF": "financials", "XLV": "healthcare", "XLE": "energy",
    "XLY": "consumer discretionary", "XLP": "consumer staples", "XLI": "industrials", "XLU": "utilities",
    "XLB": "materials", "EFA": "developed intl", "EEM": "emerging markets", "GLD": "gold", "TLT": "long bonds",
}
EQUITY_ETFS = [s for s in ROBUST_ETFS if s not in ("GLD", "TLT")]


def ibs_spec(name, syms, lo=0.10, hi=0.90, at="close", max_hold=None, slots=1):
    return Spec(name, syms, slots, lambda x, lo=lo: x.ibs < lo, lambda x, hi=hi: x.ibs > hi,
                rank=lambda x: x.ibs, enter_at="close" if at == "close" else "next_open",
                exit_at="close" if at == "close" else "next_open", max_hold=max_hold, cost=ETF_COST)


ROBUST_VARIANTS = [
    ("10/90 close", dict()),
    ("10/90 next open", dict(at="next_open")),
    ("15/85 close", dict(lo=0.15, hi=0.85)),
    ("20/80 close", dict(lo=0.20, hi=0.80)),
    ("10/90 max 3 days", dict(max_hold=3)),
]


def ratio(p: dict) -> float:
    return p["cagr"] / max(p["max_dd"], 1) if p else float("nan")


def run_robust(dc, start: date, end: date, out_dir: Path, retry=lambda f: f()) -> Path:
    warm = start - timedelta(days=320)
    data = fetch_daily(dc, sorted(ROBUST_ETFS), warm, end, retry)
    ind = {s: indicators(g) for s, g in data.items()}
    dates = [d for d in ind["SPY"].index if start <= d <= end]
    recent = dates[-504:]                                         # last ~2 years
    rows, grid = [], {}
    for s in ROBUST_ETFS:
        if s not in ind:
            continue
        bh = ind[s]["close"].reindex(dates).ffill()
        bh_p = perf(bh / bh.iloc[0] * 100, [])
        bh_r = ind[s]["close"].reindex(recent).ffill()
        bh_recent = perf(bh_r / bh_r.iloc[0] * 100, [])
        grid[s] = {"bh": bh_p, "bh_recent": bh_recent}
        for vname, kw in ROBUST_VARIANTS:
            sp = ibs_spec(f"IBS {s} {vname}", [s], **kw)
            tr, curve = simulate(sp, ind, dates, {})
            p = perf(curve, tr)
            tr2, curve2 = simulate(sp, ind, dates, {}, cost_mult=2.0)
            p["cagr_2x_cost"] = perf(curve2, tr2).get("cagr", float("nan"))
            trr, cr = simulate(sp, ind, recent, {})
            p["recent"] = perf(cr, trr)
            grid[s][vname] = p
        log.info("%s done", s)

    pools = [("Pool SPY+QQQ (2 slots)", ["SPY", "QQQ"], 2),
             ("Pool 4 broad indexes (SPY QQQ IWM DIA, 2 slots)", ["SPY", "QQQ", "IWM", "DIA"], 2),
             ("Pool all 17 stock ETFs (4 slots)", EQUITY_ETFS, 4)]
    pool_res = {}
    for name, syms, slots in pools:
        sp = ibs_spec(name, syms, slots=slots)
        tr, curve = simulate(sp, ind, dates, {})
        p = perf(curve, tr)
        tr2, curve2 = simulate(sp, ind, dates, {}, cost_mult=2.0)
        p["cagr_2x_cost"] = perf(curve2, tr2).get("cagr", float("nan"))
        trr, cr = simulate(sp, ind, recent, {})
        p["recent"] = perf(cr, trr)
        pool_res[name] = (p, tr)
    return write_robust(grid, pool_res, dates, recent, out_dir)


def write_robust(grid: dict, pools: dict, dates: list[date], recent: list[date], out_dir: Path) -> Path:
    base = "10/90 close"
    eq = [s for s in grid if s in EQUITY_ETFS]
    pos = sum(grid[s][base]["cagr"] > 0 for s in eq)
    beats_ratio = sum(ratio(grid[s][base]) > ratio(grid[s]["bh"]) for s in eq)
    halves = sum(grid[s][base]["cagr_1st"] > 0 and grid[s][base]["cagr_2nd"] > 0 for s in eq)
    costs = sum(grid[s][base]["cagr_2x_cost"] > 0 for s in eq)
    recent_pos = sum(grid[s][base]["recent"].get("cagr", -1) > 0 for s in eq)
    L = [f"# IBS robustness test: {dates[0]} to {dates[-1]}", "",
         f"IBS = where the close sits in the day's range (0 = at the low, 1 = at the high). Base rule: buy when "
         f"IBS < 0.10, sell when IBS > 0.90, trading at the close. $100 per fund, fractional shares, "
         f"{ETF_COST * 100:.2f}% cost per side. 'Recent' = last {len(recent)} trading days "
         f"({recent[0]} to {recent[-1]}).", "",
         "## Scorecard for the base rule on the 17 stock-market funds", "",
         f"- Made money: **{pos} of {len(eq)}**",
         f"- Better return per unit of worst drop than just holding that same fund: **{beats_ratio} of {len(eq)}**",
         f"- Made money in both halves of the period: **{halves} of {len(eq)}**",
         f"- Still made money at double costs: **{costs} of {len(eq)}**",
         f"- Made money in the recent 2 years: **{recent_pos} of {len(eq)}**", "",
         "## Yearly growth (CAGR) for each fund and rule version", "",
         "| Fund | What it is | Buy & hold | Buy & hold worst drop | " + " | ".join(v for v, _ in ROBUST_VARIANTS)
         + " | Base: worst drop | Base: trades | Base: win % | Base: recent 2y | Buy & hold recent 2y |",
         "|---|---|---|---|" + "---|" * len(ROBUST_VARIANTS) + "---|---|---|---|---|"]
    for s, g in grid.items():
        b = g[base]
        L.append(f"| {s} | {ROBUST_ETFS[s]} | {g['bh']['cagr']:+.1f}% | {g['bh']['max_dd']:.0f}% | "
                 + " | ".join(f"{g[v]['cagr']:+.1f}%" for v, _ in ROBUST_VARIANTS)
                 + f" | {b['max_dd']:.0f}% | {b.get('trades', 0)} | {b.get('win', 0):.0f}% | "
                 f"{b['recent'].get('cagr', float('nan')):+.1f}% | {g['bh_recent']['cagr']:+.1f}% |")
    L += ["", "## Pooled versions (one account trading several funds)", "",
          "| Portfolio | $100 becomes | CAGR | Worst drop | CAGR / drop | Trades | Win % | 1st half | 2nd half "
          "| 2x costs | Recent 2y |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    spy = grid["SPY"]["bh"]
    L.append(f"| SPY buy & hold | ${spy['final']:.0f} | {spy['cagr']:+.1f}% | {spy['max_dd']:.0f}% | "
             f"{ratio(spy):.2f} | - | - | {spy['cagr_1st']:+.1f}% | {spy['cagr_2nd']:+.1f}% | - | "
             f"{grid['SPY']['bh_recent']['cagr']:+.1f}% |")
    for name, (p, _) in pools.items():
        L.append(f"| {name} | ${p['final']:.0f} | {p['cagr']:+.1f}% | {p['max_dd']:.0f}% | {ratio(p):.2f} | "
                 f"{p.get('trades', 0)} | {p.get('win', 0):.0f}% | {p['cagr_1st']:+.1f}% | {p['cagr_2nd']:+.1f}% | "
                 f"{p['cagr_2x_cost']:+.1f}% | {p['recent'].get('cagr', float('nan')):+.1f}% |")
    L += ["", "## Year by year, base rule (% return)", ""]
    years = sorted({y for g in grid.values() for y in g[base].get("years", {})})
    L.append("| Fund | " + " | ".join(str(y) for y in years) + " |")
    L.append("|---|" + "---|" * len(years))
    for s, g in grid.items():
        L.append(f"| {s} | " + " | ".join(f"{g[base]['years'].get(y, float('nan')):+.1f}" for y in years) + " |")
    for name, (p, _) in pools.items():
        L.append(f"| {name} | " + " | ".join(f"{p['years'].get(y, float('nan')):+.1f}" for y in years) + " |")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    md = out_dir / f"ibs-robustness-{stamp}.md"
    md.write_text("\n".join(L) + "\n")
    frames = [pd.DataFrame(tr) for _, (_, tr) in pools.items() if tr]
    if frames:
        pd.concat(frames).to_csv(out_dir / f"ibs-robustness-{stamp}-pool-trades.csv", index=False)
    log.info("wrote %s", md)
    return md
