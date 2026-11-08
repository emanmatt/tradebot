"""Insider open-market purchases (SEC Form 4) via OpenInsider's public screener.

Cluster buys (several insiders buying the same stock) score highest, followed by
large purchases from top executives.
"""
from io import StringIO
import math
import re

import pandas as pd
import requests

URL = (
    "http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd={days}&fdr=&td=0&tdr="
    "&fdlyl=&fdlyh=&daysago=&xp=1&vl={min_k}&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999"
    "&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h=&sortcol=0&cnt=1000&page=1"
)


def _clean_cols(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [re.sub(r"\s+", "_", str(c).replace("\xa0", " ").strip()).lower() for c in df.columns]
    return df


def _money(x) -> float:
    s = re.sub(r"[^\d.\-]", "", str(x))
    try:
        return abs(float(s))
    except ValueError:
        return 0.0


def fetch_html(cfg: dict) -> str:
    c = cfg["signals"]["insiders"]
    url = URL.format(days=c["lookback_days"], min_k=int(c["min_value_usd"] / 1000))
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 tradebot"})
    r.raise_for_status()
    return r.text


def parse(html: str, cfg: dict) -> dict:
    c = cfg["signals"]["insiders"]
    tables = pd.read_html(StringIO(html))
    df = next((_clean_cols(t) for t in tables if "ticker" in [str(x).lower() for x in t.columns]), None)
    if df is None or df.empty:
        return {}
    df = df[df["trade_type"].astype(str).str.startswith("P")]  # purchases only
    df["usd"] = df["value"].map(_money)
    df = df[df["usd"] >= c["min_value_usd"]]
    exec_re = "|".join(c["exec_titles"])

    out = {}
    for tkr, g in df.groupby("ticker"):
        insiders = g["insider_name"].nunique()
        total = g["usd"].sum()
        execs = g["title"].astype(str).str.contains(exec_re, case=False).sum()
        # cluster count dominates, size and seniority add on top
        score = min(1.0, 0.25 * insiders + 0.1 * math.log10(max(total, 1) / 1e5 + 1) * 3 + 0.1 * min(execs, 3))
        out[str(tkr).strip().upper()] = {
            "score": round(score, 3),
            "reason": f"{insiders} insider(s) bought ${total:,.0f}",
        }
    return out


def get(cfg: dict) -> dict:
    return parse(fetch_html(cfg), cfg)
