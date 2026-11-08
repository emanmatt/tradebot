"""Reddit mention momentum via ApeWisdom's free API.

Social buzz is the noisiest signal (and often marks tops), so it carries the
lowest weight and still has to pass the trend filter.
"""
import requests

URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}"


def fetch(pages: int = 2) -> list:
    rows = []
    for p in range(1, pages + 1):
        r = requests.get(URL.format(page=p), timeout=30)
        r.raise_for_status()
        rows += r.json().get("results", [])
    return rows


def parse(rows: list, cfg: dict) -> dict:
    c = cfg["signals"]["social"]
    out = {}
    for r in rows:
        m = int(r.get("mentions") or 0)
        prev = int(r.get("mentions_24h_ago") or 0)
        if m < c["min_mentions"]:
            continue
        growth = m / max(prev, 1)
        if growth < c["min_growth"]:
            continue
        score = min(1.0, 0.3 + 0.2 * (growth - c["min_growth"]))
        out[str(r["ticker"]).upper()] = {"score": round(score, 3),
                                         "reason": f"{m} mentions ({growth:.1f}x vs 24h)"}
    return out


def get(cfg: dict) -> dict:
    return parse(fetch(), cfg)
