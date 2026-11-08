"""Hedge fund 13F filings straight from SEC EDGAR.

For each followed fund, compares its two most recent 13F-HR filings and flags
new positions or positions that grew by min_increase_pct. CUSIPs are mapped to
tickers with OpenFIGI. Results are cached per filing because 13Fs change only
quarterly.
"""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import requests

from ..config import env

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/"
FIGI = "https://api.openfigi.com/v3/mapping"


def _get(url, ua, **kw):
    r = requests.get(url, headers={"User-Agent": ua}, timeout=30, **kw)
    r.raise_for_status()
    return r


def recent_13f(cik: str, ua: str, n: int = 2) -> list[str]:
    data = _get(SUBMISSIONS.format(cik=cik.zfill(10)), ua).json()
    rec = data["filings"]["recent"]
    accs = [a for a, f in zip(rec["accessionNumber"], rec["form"]) if f == "13F-HR"]
    return accs[:n]


def parse_infotable(xml_text: str) -> dict:
    """Return {cusip: {"name":..., "value":..., "shares":...}} (calls/puts excluded)."""
    root = ET.fromstring(xml_text)
    out = {}
    for it in root.iter():
        if not it.tag.endswith("infoTable"):
            continue
        f = {c.tag.split("}")[-1]: c for c in it.iter()}
        if f.get("putCall") is not None and (f["putCall"].text or "").strip():
            continue
        cusip = (f["cusip"].text or "").strip()
        val = float(f["value"].text or 0)
        sh = float(f["sshPrnamt"].text or 0) if "sshPrnamt" in f else 0
        d = out.setdefault(cusip, {"name": (f["nameOfIssuer"].text or "").strip(), "value": 0, "shares": 0})
        d["value"] += val
        d["shares"] += sh
    return out


def holdings(cik: str, acc: str, ua: str, cache_dir: str) -> dict:
    cache = Path(cache_dir) / f"13f_{cik}_{acc}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    base = ARCHIVE.format(cik_int=int(cik), acc=acc.replace("-", ""))
    idx = _get(base + "index.json", ua).json()
    names = [i["name"] for i in idx["directory"]["item"] if i["name"].lower().endswith(".xml")]
    info = next((n for n in names if "primary_doc" not in n.lower()), None)
    if not info:
        return {}
    h = parse_infotable(_get(base + info, ua).text)
    cache.write_text(json.dumps(h))
    return h


def diff(new: dict, old: dict, min_increase_pct: float, min_usd: float) -> dict:
    """Return {cusip: (name, pct_change or None for new)} for buys worth flagging.

    13F values are reported in whole dollars (since 2023). Growth is measured in
    shares so price moves don't look like buying.
    """
    flagged = {}
    for cusip, h in new.items():
        if h["value"] < min_usd:
            continue
        prev = old.get(cusip)
        if prev is None or prev["shares"] == 0:
            flagged[cusip] = (h["name"], None)
        else:
            pct = (h["shares"] - prev["shares"]) / prev["shares"] * 100
            if pct >= min_increase_pct:
                flagged[cusip] = (h["name"], pct)
    return flagged


def cusips_to_tickers(cusips: list[str], cache_dir: str) -> dict:
    cache = Path(cache_dir) / "cusip_map.json"
    mp = json.loads(cache.read_text()) if cache.exists() else {}
    todo = [c for c in cusips if c not in mp]
    key = env("OPENFIGI_API_KEY")
    headers = {"Content-Type": "application/json", **({"X-OPENFIGI-APIKEY": key} if key else {})}
    step = 100 if key else 10
    for i in range(0, len(todo), step):
        batch = todo[i:i + step]
        r = requests.post(FIGI, headers=headers, timeout=30,
                          json=[{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch])
        r.raise_for_status()
        for c, res in zip(batch, r.json()):
            data = res.get("data") or []
            mp[c] = data[0]["ticker"] if data else None
    cache.write_text(json.dumps(mp))
    return {c: mp.get(c) for c in cusips}


def get(cfg: dict) -> dict:
    c = cfg["signals"]["funds_13f"]
    ua = cfg["sec_user_agent"]
    if "example.com" in ua:
        raise RuntimeError("set sec_user_agent in config.yaml (SEC requires a real contact)")
    per_cusip = {}
    for fund, cik in c["funds"].items():
        accs = recent_13f(cik, ua)
        if len(accs) < 2:
            continue
        new, old = (holdings(cik, a, ua, cfg["cache_dir"]) for a in accs)
        for cusip, (name, pct) in diff(new, old, c["min_increase_pct"], c["min_position_usd"]).items():
            tag = "new" if pct is None else f"+{pct:.0f}%"
            per_cusip.setdefault(cusip, []).append((fund, tag, pct is None))

    tickers = cusips_to_tickers(list(per_cusip), cfg["cache_dir"])
    out = {}
    for cusip, hits in per_cusip.items():
        t = tickers.get(cusip)
        if not t:
            continue
        funds = len(hits)
        new_pos = sum(1 for h in hits if h[2])
        out[t.upper()] = {
            "score": round(min(1.0, 0.35 * funds + 0.15 * new_pos), 3),
            "reason": "; ".join(f"{f} {tag}" for f, tag, _ in hits),
        }
    return out
