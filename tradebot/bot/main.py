"""Run the bot.

  python -m bot.main --dry-run   # full pipeline, prints the plan, places no orders
  python -m bot.main --once      # one real run (paper unless live is unlocked)
  python -m bot.main --loop      # stays running, fires daily at schedule.run_time_et
"""
import argparse
from datetime import datetime, timezone
import logging
import time
from zoneinfo import ZoneInfo

from . import risk, scoring
from .broker import Broker
from .config import is_live, load_config
from .journal import Journal
from .signals import congress, funds_13f, insiders, safe, social, trend

log = logging.getLogger("bot")

SOURCES = {"insiders": insiders.get, "congress": congress.get,
           "funds_13f": funds_13f.get, "social": social.get}


def gather_signals(cfg) -> dict:
    return {name: safe(name, fn, cfg) for name, fn in SOURCES.items()}


def trend_table(broker, tickers, cfg) -> dict:
    out = {}
    for sym, df in broker.bars(tickers).items():
        if len(df) < 60:
            continue
        ind = trend.indicators(df)
        ok, score, reason = trend.evaluate(ind, cfg)
        out[sym] = (ok, score, reason, ind)
    return out


def days_held(opened_iso: str | None) -> int:
    if not opened_iso:
        return 0
    return (datetime.now(timezone.utc) - datetime.fromisoformat(opened_iso)).days


def run(cfg: dict, dry_run: bool) -> None:
    j = Journal(cfg["journal_path"])
    b = Broker(cfg)
    mode = "LIVE" if b.live else "PAPER"
    log.info("=== run start | %s%s ===", mode, " | DRY RUN" if dry_run else "")

    if not dry_run and not b.market_open():
        log.info("Market closed, nothing to do.")
        return

    acct = b.account()
    pos = b.positions()
    exposure = sum(p["value"] for p in pos.values())
    j.equity(acct["equity"], acct["cash"], exposure, mode)
    peak = max(j.peak_equity(), acct["equity"])
    log.info("Equity $%.2f | invested $%.2f | %d positions", acct["equity"], exposure, len(pos))

    # ---------- 1. exits ----------
    held_trend = trend_table(b, list(pos), cfg) if pos else {}
    for sym, p in list(pos.items()):
        held = days_held(j.opened(sym))
        opened = j.opened(sym)
        if opened and opened[:10] == datetime.now(timezone.utc).date().isoformat():
            continue  # never sell same day: avoids pattern-day-trader flags
        ind = held_trend.get(sym, (None, None, None, None))[3]
        why = risk.should_exit(held, ind, cfg)
        if why:
            log.info("EXIT %s: %s (P/L %+.1f%%)", sym, why, p["pl_pct"] * 100)
            oid = "" if dry_run else b.close(sym)
            j.order(sym, "sell", p["qty"], ind["close"] if ind else 0, None, None, why, oid, dry_run)
            if not dry_run:
                exposure -= p["value"]
                del pos[sym]

    # ---------- 2. signals ----------
    sources = gather_signals(cfg)
    follow = scoring.follow_scores(sources, cfg["signals"]["weights"])
    cands = sorted((t for t in follow if t not in pos), key=lambda t: follow[t]["follow"], reverse=True)[:75]
    picks = scoring.rank(follow, trend_table(b, cands, cfg), cfg)
    log.info("%d tickers flagged, %d passed trend + score", len(follow), len(picks))

    # ---------- 3. gates ----------
    blocks = risk.account_gates(acct["equity"], acct["last_equity"], peak, cfg)
    if blocks:
        for msg in blocks:
            log.warning("NO NEW BUYS: %s", msg)
            j.event("warn", msg)
    today = datetime.now(timezone.utc).date().isoformat()
    bought_today = j.db.execute("SELECT COUNT(*) FROM entries WHERE substr(opened,1,10)=?", (today,)).fetchone()[0]
    slots = 0 if blocks else max(0, min(cfg["risk"]["max_new_per_day"] - bought_today,
                                        cfg["risk"]["max_positions"] - len(pos)))

    # ---------- 4. entries ----------
    bp = acct["buying_power"]
    for p in picks:
        acted = False
        if slots > 0 and b.tradable(p["ticker"]):
            price = b.last_price(p["ticker"])
            o = risk.size_order(acct["equity"], bp, exposure, price, p["ind"]["atr"], cfg)
            if o:
                log.info("BUY %s x%d @ ~$%.2f | stop $%.2f | target $%.2f | risk $%.0f | score %.2f",
                         p["ticker"], o["qty"], price, o["stop"], o["target"], o["risk_usd"], p["score"])
                for r in p["reasons"]:
                    log.info("    - %s", r)
                oid = "" if dry_run else b.buy_bracket(p["ticker"], o["qty"], o["stop"], o["target"])
                j.order(p["ticker"], "buy", o["qty"], price, o["stop"], o["target"],
                        f"score {p['score']}", oid, dry_run)
                exposure += o["qty"] * price
                bp -= o["qty"] * price
                slots -= 1
                acted = True
        j.signal(p, acted)
    log.info("=== run done ===")


def loop(cfg: dict) -> None:
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    hh, mm = map(int, cfg["schedule"]["run_time_et"].split(":"))
    last = None
    log.info("Waiting for %02d:%02d %s each trading day (Ctrl+C to stop)", hh, mm, tz)
    while True:
        now = datetime.now(tz)
        if now.date() != last and (now.hour, now.minute) >= (hh, mm) and now.weekday() < 5:
            last = now.date()
            try:
                run(cfg, dry_run=False)
            except Exception as e:  # noqa: BLE001
                log.exception("Run failed: %s", e)
        time.sleep(30)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", action="store_true")
    ap.add_argument("--config")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    cfg = load_config(a.config)
    if cfg.get("mode") == "live" and not is_live(cfg):
        log.warning("config says live but CONFIRM_LIVE != YES -> running on PAPER")
    if a.loop:
        loop(cfg)
    else:
        run(cfg, dry_run=a.dry_run)


if __name__ == "__main__":
    main()
