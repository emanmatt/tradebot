"""All-day trading loop: buy breakouts, sell on stop/target/failed move, repeat.

  python -m bot.day --auto      # what the schedule calls: trades until the session ends
  python -m bot.day --dry-run   # one scan: show what it would buy now, place nothing
  python -m bot.day --exit      # sell everything now

GitHub jobs can run at most 6 hours, so the day is split into two sessions:
morning (until 12:45 ET) and afternoon (until 15:58 ET). Positions carry across
the hand-off safely because their stop-loss and target live at Alpaca.
"""
import argparse
from datetime import datetime, time, timedelta, timezone
import logging
import math
import os
import time as _time

from alpaca.data.enums import DataFeed
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MarketMoversRequest, MostActivesRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest, ReplaceOrderRequest

from . import scoring
from .broker import Broker
from .config import env, load_config
from .journal import Journal
from .main import gather_signals
from .signals.intraday import ET, evaluate, features

log = logging.getLogger("day")

# used only if Alpaca's movers screener is unavailable
FALLBACK = ["AAPL", "AMD", "NVDA", "TSLA", "PLTR", "SOFI", "F", "INTC", "BAC", "T", "PFE", "NIO",
            "RIVN", "LCID", "SNAP", "UBER", "HOOD", "RIOT", "MARA", "AAL", "CCL", "WBD", "KVUE", "GRAB",
            "CLSK", "SOUN", "RKLB", "IONQ", "OPEN", "CHPT", "PLUG", "JOBY", "ACHR", "SMCI", "MU", "COIN"]


def _hm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def in_window(now_et: datetime, window: list[str]) -> bool:
    return _hm(window[0]) <= now_et.time() <= _hm(window[1])


# ---------------- market data helpers ----------------
def universe(b: Broker, c: dict) -> list[str]:
    syms = set()
    try:
        sc = ScreenerClient(env("ALPACA_API_KEY"), env("ALPACA_SECRET_KEY"))
        syms |= {s.symbol for s in sc.get_most_actives(MostActivesRequest(top=c["universe_top"], by="trades")).most_actives}
        syms |= {m.symbol for m in sc.get_market_movers(MarketMoversRequest(top=c["universe_top"])).gainers
                 if c["min_price"] <= m.price <= c["max_price"]}
    except Exception as e:  # noqa: BLE001
        log.warning("movers screener unavailable (%s), using fallback list", e)
    if not syms:
        syms = set(FALLBACK)
    return sorted(s for s in syms if s.isalpha() and len(s) <= 5)


def intraday_bars(b: Broker, syms: list[str]) -> dict:
    start = datetime.now(timezone.utc) - timedelta(days=9)
    out = {}
    for i in range(0, len(syms), 50):
        df = b.data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=syms[i:i + 50], timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start, feed=DataFeed.IEX)).df
        if df.empty:
            continue
        for s, g in df.groupby(level=0):
            out[s] = g.droplevel(0).sort_index()
    return out


def peak_equity(b: Broker, equity: float) -> float:
    try:
        h = b.trading.get_portfolio_history(GetPortfolioHistoryRequest(period="1A", timeframe="1D"))
        return max([e for e in h.equity if e] + [equity])
    except Exception:  # noqa: BLE001
        return equity


def size(equity: float, bp: float, price: float, stop: float, c: dict) -> int:
    risk_usd = equity * c["risk_per_trade_pct"] / 100
    by_risk = risk_usd / max(price - stop, 0.01)
    by_size = equity * c["max_position_pct"] / 100 / price
    by_bp = bp * 0.95 / price
    return math.floor(min(by_risk, by_size, by_bp))


def gates(equity: float, last_equity: float, peak: float, c: dict) -> list[str]:
    blocks = []
    if last_equity > 0 and (last_equity - equity) / last_equity * 100 >= c["max_daily_loss_pct"]:
        blocks.append("daily loss limit hit - no more buys today")
    if peak > 0 and (peak - equity) / peak * 100 >= c["max_drawdown_pct"]:
        blocks.append(f"down {(1 - equity / peak):.0%} from peak - bot halted, review before resuming")
    return blocks


# ---------------- the trader ----------------
class DayTrader:
    def __init__(self, cfg: dict, broker=None, journal=None, clock=None):
        self.cfg, self.c = cfg, cfg["day"]
        self.b = broker or Broker(cfg)
        self.j = journal or Journal(cfg["journal_path"])
        self.clock = clock or (lambda: datetime.now(ET))
        self.mode = "LIVE" if self.b.live else "PAPER"
        self.seen = {}            # symbol -> last P/L% seen, to notice bracket exits
        self.plans = {}           # symbol -> {"entry", "stop", "be_done"}
        self.no_rebuy = set()     # symbols that lost today: no revenge trades
        self.last_exit = None
        self.last_scan = None
        self.follow = None
        self.peak = None
        self.trades = 0

    # ----- helpers -----
    def _plan_from_journal(self, sym: str, pos: dict) -> dict:
        row = self.j.db.execute("SELECT price, stop FROM orders WHERE ticker=? AND side='buy' AND dry_run=0 "
                                "ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
        entry = pos.get("entry") or (row[0] if row else 0)
        stop = row[1] if row and row[1] else entry * (1 - self.c["max_stop_pct"] / 100)
        return {"entry": entry, "stop": stop, "be_done": False}

    def _sell(self, sym: str, pos: dict, why: str) -> None:
        log.info("SELL %s x%g (%+.1f%%): %s", sym, pos["qty"], pos["pl_pct"] * 100, why)
        oid = self.b.close(sym)
        self.j.order(sym, "sell", pos["qty"], pos["value"] / max(pos["qty"], 1e-9), None, None, why, oid, False)
        self._closed(sym, pos["pl_pct"])

    def _closed(self, sym: str, pl_pct: float) -> None:
        if pl_pct < 0:
            self.no_rebuy.add(sym)
        self.plans.pop(sym, None)
        self.seen.pop(sym, None)
        self.last_exit = self.clock()

    def _move_stop(self, sym: str, new_stop: float) -> bool:
        for o in self.b.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym], nested=True)):
            for leg in [o] + list(getattr(o, "legs", None) or []):
                if getattr(leg, "stop_price", None) and str(getattr(leg, "side", "")).lower().endswith("sell"):
                    self.b.trading.replace_order_by_id(leg.id, ReplaceOrderRequest(stop_price=round(new_stop, 2)))
                    return True
        return False

    # ----- one minute of work -----
    def manage(self, pos: dict) -> None:
        # positions that vanished since last tick were closed by their stop or target at Alpaca
        for sym in list(self.seen):
            if sym not in pos:
                pl = self.seen[sym]
                log.info("%s closed by its %s (%+.1f%%)", sym, "target" if pl > 0 else "stop", pl * 100)
                self.j.order(sym, "sell", 0, 0, None, None, "target hit" if pl > 0 else "stop hit", "", False)
                self._closed(sym, pl)
        if not pos:
            return
        bars = intraday_bars(self.b, list(pos))
        for sym, p in pos.items():
            self.seen[sym] = p["pl_pct"]
            plan = self.plans.setdefault(sym, self._plan_from_journal(sym, p))
            f = features(bars[sym], self.c["or_minutes"]) if sym in bars else None
            if not f:
                continue
            price, r = f["price"], plan["entry"] - plan["stop"]
            # failed move: a finished 5-min bar closed back under VWAP -> get out early
            if self.c["exit_below_vwap"] and f["last_close_5m"] < f["vwap"] and price < plan["entry"] + r:
                self._sell(sym, p, f"lost VWAP (${f['vwap']:.2f}), move failed")
                self.no_rebuy.add(sym)          # a failed breakout is done for the day
                continue
            # winner: once up 1R, move the stop to break-even so it can't turn into a loss
            if not plan["be_done"] and r > 0 and price >= plan["entry"] + self.c["breakeven_at_r"] * r:
                if self._move_stop(sym, plan["entry"]):
                    plan["be_done"] = True
                    log.info("%s up %.1fR: stop moved to break-even $%.2f", sym, self.c["breakeven_at_r"], plan["entry"])

    def scan(self) -> list[dict]:
        if self.follow is None:
            self.follow = (scoring.follow_scores(gather_signals(self.cfg), self.cfg["signals"]["weights"])
                           if self.c["follow_bonus"] > 0 else {})
        syms = [s for s in universe(self.b, self.c) if s not in self.no_rebuy]
        picks = []
        for s, df in intraday_bars(self.b, syms).items():
            f = features(df, self.c["or_minutes"])
            if not f:
                continue
            ok, score, why, stop, target = evaluate(f, self.c)
            if not ok:
                continue
            reasons = [f"intraday: {why}"]
            if s in self.follow:
                score = min(1.0, score + self.c["follow_bonus"] * self.follow[s]["follow"])
                reasons += self.follow[s]["reasons"]
            if score >= self.c["min_score"]:
                picks.append({"ticker": s, "score": round(score, 3), "follow": self.follow.get(s, {}).get("follow", 0),
                              "trend": score, "reasons": reasons, "price": f["price"], "stop": stop, "target": target})
        picks.sort(key=lambda p: p["score"], reverse=True)
        log.info("Scanned %d stocks: %d setup(s)%s", len(syms), len(picks),
                 f", best {picks[0]['ticker']} ({picks[0]['score']:.2f})" if picks else "")
        return picks

    def buy(self, picks: list[dict], a: dict, dry: bool) -> bool:
        for p in picks[:5]:
            if not self.b.tradable(p["ticker"]):
                continue
            qty = size(a["equity"], a["buying_power"], p["price"], p["stop"], self.c)
            if qty < 1:
                log.info("skip %s: can't afford a share within risk limits", p["ticker"])
                continue
            log.info("BUY %s x%d @ ~$%.2f | stop $%.2f | target $%.2f | risk $%.2f | score %.2f",
                     p["ticker"], qty, p["price"], p["stop"], p["target"], qty * (p["price"] - p["stop"]), p["score"])
            for r in p["reasons"]:
                log.info("    - %s", r)
            oid = "" if dry else self.b.buy_bracket(p["ticker"], qty, p["stop"], p["target"])
            self.j.order(p["ticker"], "buy", qty, p["price"], p["stop"], p["target"], f"day score {p['score']}", oid, dry)
            self.j.signal(p, True)
            if not dry:
                self.plans[p["ticker"]] = {"entry": p["price"], "stop": p["stop"], "be_done": False}
                self.trades += 1
            return True
        return False

    def flatten(self, why: str = "end of day") -> None:
        self.b.trading.cancel_orders()
        for sym, p in self.b.positions().items():
            self._sell(sym, p, why)

    def tick(self) -> None:
        now = self.clock()
        a = self.b.account()
        pos = self.b.positions()
        self.manage(pos)
        pos = self.b.positions()

        if now.time() >= _hm(self.c["flatten_at_et"]):
            if pos:
                self.flatten()
            return
        if len(pos) >= self.c["max_open_positions"]:
            return
        if not in_window(now, self.c["entry_window_et"]):
            return
        if self.last_exit and now - self.last_exit < timedelta(minutes=self.c["cooldown_min"]):
            return
        if self.last_scan and now - self.last_scan < timedelta(seconds=self.c["scan_every_sec"]):
            return
        if self.peak is None:
            self.peak = peak_equity(self.b, a["equity"])
        self.peak = max(self.peak, a["equity"])
        blocks = gates(a["equity"], a["last_equity"], self.peak, self.c)
        if blocks:
            if not getattr(self, "_warned", False):
                for m in blocks:
                    log.warning("NO NEW BUYS: %s", m)
                    self.j.event("warn", m)
                self._warned = True
            return
        self.last_scan = now
        self.buy(self.scan(), a, dry=False)

    def run(self, until: time, sleep=_time.sleep) -> None:
        a = self.b.account()
        self.j.equity(a["equity"], a["cash"], 0, self.mode)
        log.info("=== SESSION START | %s | equity $%.2f | runs until %s ET ===", self.mode, a["equity"], until)
        while self.clock().time() < until:
            if not self.b.market_open():
                if self.clock().time() < time(9, 31):   # started a few minutes early: wait for the bell
                    sleep(30)
                    continue
                log.info("Market closed.")
                break
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001  one bad minute never kills the session
                log.warning("tick error: %s", e)
            sleep(self.c["tick_sec"])
        a = self.b.account()
        self.j.equity(a["equity"], a["cash"], sum(p["value"] for p in self.b.positions().values()), self.mode)
        log.info("=== SESSION END | %d trade(s) | day P/L $%+.2f ===", self.trades, a["equity"] - a["last_equity"])


def session_end(now: datetime, c: dict) -> time | None:
    """Which session this run belongs to, or None if it started outside trading hours."""
    t = now.time()
    if t < time(9, 0) or t >= time(15, 55):
        return None
    return _hm(c["morning_until_et"]) if t < _hm(c["afternoon_from_et"]) else _hm(c["afternoon_until_et"])


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    for f in ("--auto", "--exit", "--dry-run"):
        g.add_argument(f, action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    cfg = load_config()
    t = DayTrader(cfg)
    if args.dry_run:
        picks = t.scan()
        if not t.buy(picks, t.b.account(), dry=True):
            log.info("No setup good enough right now.")
        return
    if args.exit:
        return t.flatten("manual sell-all")
    end = session_end(t.clock(), cfg["day"])
    if end is None:
        log.info("%s ET is outside trading hours, nothing to do.", t.clock().strftime("%H:%M"))
        return
    t.run(end)


if __name__ == "__main__":
    main()
