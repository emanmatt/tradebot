"""Congressional stock trades via Quiver Quantitative's API (needs QUIVER_API_KEY).

Net buying (purchases minus sales, by distinct members) drives the score.
"""
from datetime import datetime, timedelta
import re

import requests

from ..config import env

URL = "https://api.quiverquant.com/beta/live/congresstrading"


def _range_low(r) -> float:
    nums = re.findall(r"[\d,]+", str(r))
    return float(nums[0].replace(",", "")) if nums else 0.0


def fetch(cfg: dict) -> list:
    key = env("QUIVER_API_KEY")
    if not key:
        raise RuntimeError("QUIVER_API_KEY not set (congress source disabled)")
    r = requests.get(URL, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    return r.json()


def parse(rows: list, cfg: dict, today: datetime | None = None) -> dict:
    c = cfg["signals"]["congress"]
    cutoff = (today or datetime.utcnow()) - timedelta(days=c["lookback_days"])
    buyers, sellers = {}, {}
    for row in rows:
        tkr = str(row.get("Ticker", "")).strip().upper()
        if not tkr or not tkr.isalpha():
            continue
        date_s = row.get("ReportDate") or row.get("TransactionDate") or ""
        try:
            if datetime.fromisoformat(str(date_s)[:10]) < cutoff:
                continue
        except ValueError:
            continue
        amt = row.get("Amount")
        low = float(amt) if isinstance(amt, (int, float)) else _range_low(row.get("Range", ""))
        if low < c["min_amount_usd"]:
            continue
        who = row.get("Representative", "?")
        tx = str(row.get("Transaction", "")).lower()
        if "purchase" in tx:
            buyers.setdefault(tkr, set()).add(who)
        elif "sale" in tx:
            sellers.setdefault(tkr, set()).add(who)

    out = {}
    for tkr, b in buyers.items():
        net = len(b) - len(sellers.get(tkr, set()))
        if net <= 0:
            continue
        out[tkr] = {"score": round(min(1.0, 0.4 + 0.2 * (net - 1)), 3),
                    "reason": f"{len(b)} member(s) bought, net +{net}"}
    return out


def get(cfg: dict) -> dict:
    return parse(fetch(cfg), cfg)
