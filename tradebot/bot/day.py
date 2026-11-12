"""All-day trading loop: buy breakouts, sell on stop/target/failed move/time limit, repeat.

  python -m bot.day --auto      # what the schedule calls: trades until the session ends
  python -m bot.day --dry-run   # one scan: show what it would buy now, place nothing
  python -m bot.day --exit      # sell everything now

GitHub jobs can run at most 6 hours, so the day is two sessions: morning (until
12:45 ET) then afternoon (until 15:58 ET). The morning session starts the
afternoon one itself when it finishes. Open positions stay protected across the
hand-off because their stop-loss and target live at Alpaca.
"""
import argparse
from datetime import datetime, time, timedelta, timezone
import logging
import math
import os
import time as _time

import requests
from alpaca.data.enums import DataFeed
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MarketMoversRequest, MostActivesRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetOrdersRequest, GetPortfolioHistoryRequest, LimitOrderRequest,
                                     ReplaceOrderRequest, StopLossRequest, TakeProfitRequest)

from . import scoring
from .broker import Broker
from .config import env, load_config
from .journal import Journal
from .main import gather_signals
from .signals.intraday import ET, OPEN, evaluate, features, stop_distance

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


# ---------------- market data (module-level so tests can swap them) ----------------
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


def _bars(b: Broker, syms: list[str], tf: TimeFrame, start: datetime, end: datetime | None = None) -> dict:
    out = {}
    for i in range(0, len(syms), 50):
        df = b.data.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms[i:i + 50], timeframe=tf,
                                                    start=start, end=end, feed=DataFeed.IEX)).df
        if df.empty:
            continue
        for s, g in df.groupby(level=0):
            out[s] = g.droplevel(0).sort_index()
    return out


def prior_bars(b: Broker, syms: list[str]) -> dict:
    """5-min bars for the previous ~6 sessions (volume baseline + yesterday's close)."""
    today_open = datetime.combine(datetime.now(ET).date(), OPEN, tzinfo=ET)
    return _bars(b, syms, TimeFrame(5, TimeFrameUnit.Minute), today_open - timedelta(days=9), today_open)


def today_bars(b: Broker, syms: list[str]) -> dict:
    """1-min bars since today's open."""
    return _bars(b, syms, TimeFrame(1, TimeFrameUnit.Minute), datetime.combine(datetime.now(ET).date(), OPEN, tzinfo=ET))


def live_price(b: Broker, sym: str) -> float:
    t = b.data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=sym, feed=DataFeed.IEX))
    return float(t[sym].price)


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
    def __init__(self, cfg: dict, broker=None, journal=None, clock=None, sleep=None):
        self.cfg, self.c = cfg, cfg["day"]
        self.b = broker or Broker(cfg)
        self.j = journal or Journal(cfg["journal_path"])
        self.clock = clock or (lambda: datetime.now(ET))
        self.sleep = sleep or _time.sleep
        self.mode = "LIVE" if self.b.live else "PAPER"
        self.seen = {}            # symbol -> last P/L% seen, to notice stop/target exits
        self.plans = {}           # symbol -> {"entry", "stop", "target", "opened", "be_done"}
        self.no_rebuy = set()     # symbols that lost or failed today
        self.skip_until = {}      # symbol -> time, after an order that didn't fill
        self.prior = {}           # symbol -> prior-days bars (fetched once per symbol)
        self.syms, self.syms_at = [], None
        self.last_exit = self.last_scan = None
        self.follow = None
        self.peak = None
        self.trades = 0

    # ----- data -----
    def _features(self, syms: list[str], today: dict | None = None) -> dict:
        need = [s for s in syms if s not in self.prior]
        if need:
            self.prior.update(prior_bars(self.b, need))
            for s in need:
                self.prior.setdefault(s, None)
        today = today if today is not None else today_bars(self.b, syms)
        out = {}
        for s, df in today.items():
            if self.prior.get(s) is not None:
                f = features(df, self.prior[s], self.c["or_minutes"], self.c["base_minutes"], self.c["atr_bars"])
                if f:
                    out[s] = f
        return out

    # ----- orders -----
    def _oco(self, sym: str, qty: float, stop: float, target: float):
        return self.b.trading.submit_order(LimitOrderRequest(
            symbol=sym, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC, order_class=OrderClass.OCO,
            take_profit=TakeProfitRequest(limit_price=round(target, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop, 2))))

    def _buy_capped(self, sym: str, qty: int, limit: float) -> tuple[float, float]:
        """Limit buy that won't pay more than `limit`. Returns (filled_qty, avg_fill_price)."""
        o = self.b.trading.submit_order(LimitOrderRequest(
            symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY, limit_price=round(limit, 2)))
        waited = 0.0
        while waited < self.c["fill_timeout_sec"]:
            o = self.b.trading.get_order_by_id(o.id)
            st = str(o.status).lower()
            if (st.endswith("filled") and not st.endswith("partially_filled")) or \
                    any(st.endswith(x) for x in ("canceled", "rejected", "expired")):
                break
            self.sleep(1)
            waited += 1
        else:
            try:
                self.b.trading.cancel_order_by_id(o.id)
            except Exception:  # noqa: BLE001  (may have filled at the last second)
                pass
            o = self.b.trading.get_order_by_id(o.id)
        return float(o.filled_qty or 0), float(o.filled_avg_price or 0)

    def _move_stop(self, sym: str, new_stop: float, qty: float, target: float) -> bool:
        orders = self.b.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym], nested=True))
        for o in orders:
            for leg in [o] + list(getattr(o, "legs", None) or []):
                if getattr(leg, "stop_price", None) and str(getattr(leg, "side", "")).lower().endswith("sell"):
                    try:
                        self.b.trading.replace_order_by_id(leg.id, ReplaceOrderRequest(stop_price=round(new_stop, 2)))
                        return True
                    except Exception:  # noqa: BLE001  fall back to re-placing the pair
                        pass
        self.b.trading.cancel_orders()
        self._oco(sym, qty, new_stop, target)
        return True

    def _exit_price(self, sym: str) -> float:
        try:
            for o in self.b.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[sym],
                                                                limit=5, nested=True)):
                for leg in [o] + list(getattr(o, "legs", None) or []):
                    if str(getattr(leg, "side", "")).lower().endswith("sell") and getattr(leg, "filled_avg_price", None):
                        return float(leg.filled_avg_price)
        except Exception:  # noqa: BLE001
            pass
        return 0.0

    def _sell(self, sym: str, pos: dict, why: str) -> None:
        log.info("SELL %s x%g (%+.2f%%): %s", sym, pos["qty"], pos["pl_pct"] * 100, why)
        oid = self.b.close(sym)
        self.j.order(sym, "sell", pos["qty"], pos["value"] / max(pos["qty"], 1e-9), None, None, why, oid, False)
        self._closed(sym, pos["pl_pct"])

    def _closed(self, sym: str, pl_pct: float) -> None:
        if pl_pct < 0:
            self.no_rebuy.add(sym)
        self.plans.pop(sym, None)
        self.seen.pop(sym, None)
        self.last_exit = self.clock()

    def _plan_from_journal(self, sym: str, pos: dict) -> dict:
        row = self.j.db.execute("SELECT price, stop, target, ts FROM orders WHERE ticker=? AND side='buy' AND dry_run=0 "
                                "ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
        entry = pos.get("entry") or (row[0] if row else 0)
        stop = row[1] if row and row[1] else entry * (1 - self.c["max_stop_pct"] / 100)
        target = row[2] if row and row[2] else entry + self.c["reward_risk"] * (entry - stop)
        opened = datetime.fromisoformat(row[3]).astimezone(ET) if row else self.clock()
        return {"entry": entry, "stop": stop, "target": target, "opened": opened, "be_done": False}

    # ----- one tick -----
    def manage(self, pos: dict) -> None:
        for sym in list(self.seen):                     # vanished = closed by its stop or target at Alpaca
            if sym not in pos:
                pl = self.seen[sym]
                px = self._exit_price(sym)
                why = "target hit" if pl > 0 else "stop hit"
                log.info("%s closed: %s (%+.2f%%)%s", sym, why, pl * 100, f" @ ${px:.2f}" if px else "")
                self.j.order(sym, "sell", 0, px, None, None, why, "", False)
                self._closed(sym, pl)
        if not pos:
            return
        feats = self._features(list(pos))
        now = self.clock()
        for sym, p in pos.items():
            self.seen[sym] = p["pl_pct"]
            plan = self.plans.setdefault(sym, self._plan_from_journal(sym, p))
            price = p["value"] / max(p["qty"], 1e-9)       # Alpaca's live mark
            r = plan["entry"] - plan["stop"]
            held = (now - plan["opened"]).total_seconds() / 60
            f = feats.get(sym)

            if (f and self.c["exit_below_vwap"] and held >= self.c["vwap_exit_after_min"]
                    and f["last_close"] < f["vwap"] and price < plan["entry"] + self.c["breakeven_at_r"] * r):
                self._sell(sym, p, f"1-min close under VWAP ${f['vwap']:.2f}, move failed")
                self.no_rebuy.add(sym)
                continue
            if held >= self.c["time_stop_min"] and price < plan["entry"] + self.c["time_stop_gain_r"] * r:
                self._sell(sym, p, f"no progress in {self.c['time_stop_min']} min")
                continue
            if not plan["be_done"] and r > 0 and price >= plan["entry"] + self.c["breakeven_at_r"] * r:
                self._move_stop(sym, plan["entry"], p["qty"], plan["target"])
                plan["be_done"] = True
                log.info("%s up %.2fR: stop moved to break-even $%.2f", sym, self.c["breakeven_at_r"], plan["entry"])

    def scan(self) -> list[dict]:
        now = self.clock()
        if self.follow is None:
            self.follow = (scoring.follow_scores(gather_signals(self.cfg), self.cfg["signals"]["weights"])
                           if self.c["follow_bonus"] > 0 else {})
        if not self.syms_at or (now - self.syms_at).total_seconds() >= self.c["universe_refresh_sec"]:
            self.syms, self.syms_at = universe(self.b, self.c), now
        syms = [s for s in self.syms if s not in self.no_rebuy and self.skip_until.get(s, now) <= now]
        picks = []
        for s, f in self._features(syms).items():
            ok, score, why = evaluate(f, self.c)
            if not ok:
                continue
            reasons = [f"intraday: {why}"]
            if s in self.follow:
                score = min(1.0, score + self.c["follow_bonus"] * self.follow[s]["follow"])
                reasons += self.follow[s]["reasons"]
            if score >= self.c["min_score"]:
                picks.append({"ticker": s, "score": round(score, 3), "follow": self.follow.get(s, {}).get("follow", 0),
                              "trend": score, "reasons": reasons, "f": f})
        picks.sort(key=lambda p: p["score"], reverse=True)
        log.info("Scanned %d: %d setup(s)%s", len(syms), len(picks),
                 f", best {picks[0]['ticker']} ({picks[0]['score']:.2f})" if picks else "")
        return picks

    def buy(self, picks: list[dict], a: dict, dry: bool) -> bool:
        c = self.c
        for p in picks[:5]:
            sym, f = p["ticker"], p["f"]
            if not self.b.tradable(sym):
                continue
            live = live_price(self.b, sym)
            if not live or live <= f["level"]:
                log.info("skip %s: live price $%.2f back under the breakout $%.2f", sym, live or 0, f["level"])
                continue
            if (live / f["level"] - 1) * 100 > c["max_extension_pct"]:
                log.info("skip %s: ran to $%.2f, too far past the breakout to chase", sym, live)
                continue
            dist = stop_distance(live, f["atr1"], c)
            limit = live * (1 + c["max_slippage_pct"] / 100)
            qty = size(a["equity"], a["buying_power"], limit, limit - dist, c)
            if qty < 1:
                log.info("skip %s: can't afford a share within risk limits", sym)
                continue
            if dry:
                log.info("WOULD BUY %s x%d, cap $%.2f | stop ~$%.2f | target ~$%.2f | score %.2f",
                         sym, qty, limit, live - dist, live + c["reward_risk"] * dist, p["score"])
                for r in p["reasons"]:
                    log.info("    - %s", r)
                return True

            got, fill = self._buy_capped(sym, qty, limit)
            if got <= 0:
                log.info("skip %s: no fill at or below $%.2f within %ds (price ran)", sym, limit, c["fill_timeout_sec"])
                self.skip_until[sym] = self.clock() + timedelta(minutes=10)
                continue
            stop, target = round(fill - dist, 2), round(fill + c["reward_risk"] * dist, 2)
            try:
                self._oco(sym, got, stop, target)
            except Exception as e:  # noqa: BLE001  never sit in a position without a stop
                log.error("couldn't place stop/target for %s (%s): selling immediately", sym, e)
                self.b.close(sym)
                self.j.order(sym, "sell", got, fill, None, None, "safety: stop order failed", "", False)
                continue
            log.info("BUY %s x%g filled @ $%.2f (cap $%.2f) | stop $%.2f | target $%.2f | risk $%.2f | score %.2f",
                     sym, got, fill, limit, stop, target, got * (fill - stop), p["score"])
            for r in p["reasons"]:
                log.info("    - %s", r)
            self.j.order(sym, "buy", got, fill, stop, target, f"day score {p['score']}", "", False)
            self.j.signal(p, True)
            self.plans[sym] = {"entry": fill, "stop": stop, "target": target, "opened": self.clock(), "be_done": False}
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
        self.manage(self.b.positions())
        pos = self.b.positions()

        if now.time() >= _hm(self.c["flatten_at_et"]):
            if pos:
                self.flatten()
            return
        if len(pos) >= self.c["max_open_positions"] or not in_window(now, self.c["entry_window_et"]):
            return
        if self.last_exit and now - self.last_exit < timedelta(minutes=self.c["cooldown_min"]):
            return
        if self.last_scan and (now - self.last_scan).total_seconds() < self.c["scan_every_sec"]:
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

    def run(self, until: time) -> None:
        a = self.b.account()
        self.j.equity(a["equity"], a["cash"], 0, self.mode)
        log.info("=== SESSION START | %s | equity $%.2f | runs until %s ET ===", self.mode, a["equity"], until)
        while self.clock().time() < until:
            if not self.b.market_open():
                if self.clock().time() < time(9, 31):   # started a few minutes early: wait for the bell
                    self.sleep(30)
                    continue
                log.info("Market closed.")
                break
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001  one bad tick never kills the session
                log.warning("tick error: %s", e)
            self.sleep(self.c["tick_sec"])
        a = self.b.account()
        self.j.equity(a["equity"], a["cash"], sum(p["value"] for p in self.b.positions().values()), self.mode)
        log.info("=== SESSION END | %d trade(s) | day P/L $%+.2f ===", self.trades, a["equity"] - a["last_equity"])


def session_end(now: datetime, c: dict) -> time | None:
    """Which session this run belongs to, or None if it started outside trading hours."""
    t = now.time()
    if t < time(9, 0) or t >= time(15, 55):
        return None
    return _hm(c["morning_until_et"]) if t < _hm(c["afternoon_from_et"]) else _hm(c["afternoon_until_et"])


def start_afternoon() -> None:
    """Morning session hands off to the afternoon one (no waiting on GitHub's timer)."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return
    try:
        r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/trade.yml/dispatches",
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                          json={"ref": "main", "inputs": {"dry_run": "false"}}, timeout=20)
        log.info("afternoon session requested (HTTP %s)", r.status_code)
    except Exception as e:  # noqa: BLE001
        log.warning("couldn't request afternoon session: %s (backup timers will start it)", e)


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
        if not t.buy(t.scan(), t.b.account(), dry=True):
            log.info("No setup good enough right now.")
        return
    if args.exit:
        return t.flatten("manual sell-all")
    end = session_end(t.clock(), cfg["day"])
    if end is None:
        log.info("%s ET is outside trading hours, nothing to do.", t.clock().strftime("%H:%M"))
        return
    t.run(end)
    if end == _hm(cfg["day"]["morning_until_et"]):
        start_afternoon()


if __name__ == "__main__":
    main()
