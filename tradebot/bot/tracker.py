"""Investor tracker: watch disclosed trades and whale wallets, alert your phone, and copy on paper.

  python -m bot.tracker              # one check (what the 15-minute schedule runs)
  python -m bot.tracker --dry-run    # check and print alerts; no orders, no phone pushes, nothing saved
  python -m bot.tracker --test-alert # send one test notification to your phone

Sources (all free):
  insiders   SEC Form 4 "latest filings" feed: CEOs/CFOs/directors buying or selling their own stock
             (public within 2 business days of the trade).
  congress   House Clerk Periodic Transaction Reports (PTRs): members of the House and their families
             (public up to 45 days after the trade). Senate is not included (needs a paid feed).
  funds      SEC 13F filings of chosen hedge funds (quarterly, up to 45 days after quarter end).
  whales     Hyperliquid (crypto) top wallets from its public leaderboard: live fills.

When something new and big enough shows up, it pushes an alert to your phone (ntfy app) and, if it passes
the copy rules, buys a small slice in the Alpaca PAPER account. Copies are sold when the investor sells,
after a holding period, or at a stop-loss.

The House PTR parser is adapted from day-trader-skill (github.com/Jakeschincariol/day-trader-skill, MIT).
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import html
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time as _time
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo

import requests

from .config import ROOT, env, load_config

log = logging.getLogger("tracker")
ET_TZ = ZoneInfo("America/New_York")

DEFAULTS = {
    # ---- what to watch ----
    "congress_members": [],          # [] = every House member; e.g. ["Pelosi", "Khanna"] to copy only these
    "funds": {"Berkshire Hathaway": "0001067983", "Pershing Square": "0001336528",
              "Appaloosa": "0001656456", "Scion Asset Mgmt": "0001649339",
              "Duquesne Family Office": "0001536411"},
    "whale_wallets": [],             # extra Hyperliquid addresses to always watch
    "whale_auto_top": 10,            # plus the best N from the Hyperliquid leaderboard (refreshed daily)
    # ---- alert thresholds ----
    "insider_min_buy_usd": 100_000,
    "insider_min_sell_usd": 1_000_000,
    "congress_min_usd": 15_001,      # lower end of the disclosed range ($1,001-$15,000 trades are ignored)
    "whale_min_notional_usd": 250_000,
    "max_alerts_per_run": 15,
    # ---- paper copy rules ----
    "copy_enabled": True,
    "copy_pct": 5.0,                 # % of the account per copied trade
    "copy_max_stock_positions": 10,
    "copy_max_crypto_positions": 3,
    "copy_insider_min_usd": 250_000, # copy an insider buy this big (or any cluster of 2+ insiders)
    "copy_congress_max_age_days": 30,  # skip congress trades disclosed more than 30 days after the trade
    "copy_fund_top_new": 3,          # copy a fund's 3 biggest NEW positions
    "copy_fund_max_age_days": 7,     # ...only if the 13F was filed in the last 7 days
    "stock_hold_days": 28,           # calendar days, then sell
    "stock_stop_pct": 15.0,
    "crypto_hold_days": 7,
    "crypto_stop_pct": 10.0,
    "min_price": 3.0,
    # ---- how often each source is checked (minutes) ----
    "every_min": {"insiders": 15, "congress": 60, "funds": 360, "whales": 15},
}

SEC_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb=&owner=include"
            "&start={start}&count=100&output=atom")
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
HOUSE_INDEX = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PTR = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
HL_INFO = "https://api.hyperliquid.xyz/info"
HL_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
OPENFIGI = "https://api.openfigi.com/v3/mapping"
NTFY = "https://ntfy.sh/{topic}"


# =====================================================================================================
# storage
# =====================================================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (source TEXT, key TEXT, ts TEXT, PRIMARY KEY (source, key));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, who TEXT,
    role TEXT, symbol TEXT, action TEXT, value_usd REAL, value_text TEXT, trade_date TEXT, public_date TEXT,
    days_late INTEGER, detail TEXT, url TEXT, alerted INTEGER DEFAULT 0, copied INTEGER DEFAULT 0, note TEXT);
CREATE TABLE IF NOT EXISTS copies (id INTEGER PRIMARY KEY AUTOINCREMENT, opened TEXT, source TEXT, who TEXT,
    symbol TEXT, asset_class TEXT, notional REAL, entry_px REAL, status TEXT, closed TEXT, exit_px REAL,
    exit_reason TEXT, pl_pct REAL, event_id INTEGER, order_id TEXT);
"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def seen(self, source: str, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE source=? AND key=?", (source, key)).fetchone() is not None

    def mark(self, source: str, key: str):
        self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?)", (source, key, now_utc().isoformat(timespec="seconds")))

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def add_event(self, e: dict) -> int:
        cols = ["ts", "source", "who", "role", "symbol", "action", "value_usd", "value_text", "trade_date",
                "public_date", "days_late", "detail", "url", "note"]
        cur = self.db.execute(f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                              [e.get("ts", now_utc().isoformat(timespec="seconds"))] + [e.get(c) for c in cols[1:]])
        return cur.lastrowid

    def commit(self):
        self.db.commit()


# =====================================================================================================
# http
# =====================================================================================================
class Http:
    def __init__(self, sec_ua: str):
        self.s = requests.Session()
        self.sec_ua = sec_ua
        self._last_sec = 0.0

    def get(self, url: str, sec: bool = False, timeout: int = 60, **kw) -> requests.Response:
        headers = {"User-Agent": self.sec_ua if sec else "tradebot-tracker/1.0 (personal research)"}
        if sec:   # SEC fair access: max 10 requests/second; we stay well under
            wait = 0.15 - (_time.monotonic() - self._last_sec)
            if wait > 0:
                _time.sleep(wait)
            self._last_sec = _time.monotonic()
        for attempt in range(4):
            r = self.s.get(url, headers=headers, timeout=timeout, **kw)
            if r.status_code in (429, 503) or (sec and r.status_code == 403 and attempt < 2):
                _time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        return r

    def post_json(self, url: str, body: dict, timeout: int = 30, headers: dict | None = None):
        for attempt in range(4):
            r = self.s.post(url, json=body, timeout=timeout, headers=headers or {})
            if r.status_code == 429:
                _time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()


# =====================================================================================================
# source 1: insiders (SEC Form 4)
# =====================================================================================================
ATOM = "{http://www.w3.org/2005/Atom}"


def parse_atom(xml_text: str) -> list[dict]:
    """Entries of the EDGAR 'latest filings' Atom feed: form, accession, filing folder URL, title."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    out = []
    for e in root.iter(f"{ATOM}entry"):
        title = html.unescape((e.findtext(f"{ATOM}title") or "").strip())
        cat = e.find(f"{ATOM}category")
        form = (cat.get("term") if cat is not None else title.split(" - ")[0]).strip()
        link = e.find(f"{ATOM}link")
        href = link.get("href") if link is not None else ""
        m = re.search(r"accession-number=(\d{10}-\d{2}-\d{6})", e.findtext(f"{ATOM}id") or "") or \
            re.search(r"(\d{10}-\d{2}-\d{6})", href)
        if not m or not href:
            continue
        out.append({"form": form, "acc": m.group(1), "folder": href.rsplit("/", 1)[0] + "/", "title": title,
                    "updated": e.findtext(f"{ATOM}updated")})
    return out


def _txt(node, path: str) -> str | None:
    if node is None:
        return None
    el = node.find(path)
    if el is None:
        return None
    v = el.find("value")
    t = (v.text if v is not None else el.text) or ""
    return t.strip() or None


def _flag(node, path: str) -> bool:
    return (_txt(node, path) or "").lower() in ("1", "true")


def parse_form4(xml_text: str) -> dict | None:
    """Issuer, owners (with roles) and non-derivative transactions from a Form 4 ownershipDocument."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return None
    if root.tag != "ownershipDocument":
        return None
    iss = root.find("issuer")
    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        owners.append({"name": html.unescape(_txt(ro, "reportingOwnerId/rptOwnerName") or "?"),
                       "cik": _txt(ro, "reportingOwnerId/rptOwnerCik"),
                       "director": _flag(rel, "isDirector"), "officer": _flag(rel, "isOfficer"),
                       "ten_pct": _flag(rel, "isTenPercentOwner"),
                       "title": _txt(rel, "officerTitle") or ""})
    txns = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        try:
            shares = float(_txt(t, "transactionAmounts/transactionShares") or 0)
            price = float(_txt(t, "transactionAmounts/transactionPricePerShare") or 0)
        except ValueError:
            continue
        txns.append({"code": _txt(t, "transactionCoding/transactionCode"), "shares": shares, "price": price,
                     "date": _txt(t, "transactionDate"),
                     "ad": _txt(t, "transactionAmounts/transactionAcquiredDisposedCode")})
    return {"issuer": html.unescape(_txt(iss, "issuerName") or "?"),
            "ticker": (_txt(iss, "issuerTradingSymbol") or "").upper().strip() or None,
            "owners": owners, "txns": txns, "plan_10b5_1": _flag(root, "aff10b5One"),
            "amendment": root.findtext("documentType") == "4/A"}


def role_of(o: dict) -> str:
    bits = []
    if o["officer"]:
        bits.append(o["title"] or "officer")
    if o["director"]:
        bits.append("director")
    if o["ten_pct"]:
        bits.append("10% owner")
    return ", ".join(bits) or "insider"


def form4_events(doc: dict, acc: str, url: str, cfg: dict) -> list[dict]:
    """One event per filing and direction (open-market buys 'P' and sales 'S' only)."""
    if not doc or not doc["ticker"] or doc["amendment"]:
        return []
    out = []
    for code, action, min_usd in (("P", "buy", cfg["insider_min_buy_usd"]), ("S", "sell", cfg["insider_min_sell_usd"])):
        tx = [t for t in doc["txns"] if t["code"] == code]
        value = sum(t["shares"] * t["price"] for t in tx)
        if not tx or value < min_usd:
            continue
        shares = sum(t["shares"] for t in tx)
        o = doc["owners"][0] if doc["owners"] else {"name": "?", "officer": False, "director": False,
                                                     "ten_pct": False, "title": ""}
        who = o["name"] + (f" +{len(doc['owners']) - 1}" if len(doc["owners"]) > 1 else "")
        trade_date = max(t["date"] for t in tx if t["date"]) if any(t["date"] for t in tx) else None
        out.append({"source": "insiders", "who": who, "role": role_of(o), "symbol": doc["ticker"],
                    "action": action, "value_usd": round(value), "value_text": f"${value:,.0f}",
                    "trade_date": trade_date, "public_date": now_utc().date().isoformat(),
                    "detail": f"{doc['issuer']}: {shares:,.0f} sh @ ~${value / max(shares, 1):,.2f}"
                              + (" (pre-planned 10b5-1 sale)" if doc["plan_10b5_1"] and action == "sell" else ""),
                    "url": url, "key": acc, "is_exec": bool(o["officer"] or o["director"])})
    return out


def check_insiders(http: Http, store: Store, cfg: dict) -> list[dict]:
    first_run = store.get("insiders_started") is None
    entries, seen_any = [], False
    for page in range(4):                                   # up to 400 most recent filings
        feed = parse_atom(http.get(SEC_FEED.format(start=page * 100), sec=True).text)
        f4 = [e for e in feed if e["form"] in ("4", "4/A")]
        new = [e for e in f4 if not store.seen("form4", e["acc"])]
        have = {x["acc"] for x in entries}
        for e in new:
            if e["acc"] not in have:
                have.add(e["acc"])
                entries.append(e)
        if len(new) < len(f4) or not feed:
            seen_any = True
            break
    if first_run:                                           # don't alert on the backlog the first time
        for e in entries:
            store.mark("form4", e["acc"])
        store.put("insiders_started", now_utc().isoformat())
        log.info("insiders: first run, %d recent filings remembered (no alerts)", len(entries))
        return []
    events = []
    for e in entries[:250]:
        try:
            idx = http.get(e["folder"] + "index.json", sec=True).json()
            names = [i["name"] for i in idx.get("directory", {}).get("item", []) if i["name"].lower().endswith(".xml")]
            doc = None
            for n in names:
                doc = parse_form4(http.get(e["folder"] + n, sec=True).text)
                if doc:
                    break
            events += form4_events(doc, e["acc"], e["folder"], cfg)
        except Exception as ex:  # noqa: BLE001
            log.warning("insiders: %s failed: %s", e["acc"], ex)
            continue
        store.mark("form4", e["acc"])
    # clusters: 2+ different insiders buying the same stock within 10 days (including earlier events)
    since = (now_utc() - timedelta(days=10)).isoformat()
    for ev in events:
        if ev["action"] != "buy":
            continue
        prior = {r[0] for r in store.db.execute(
            "SELECT who FROM events WHERE source='insiders' AND action='buy' AND symbol=? AND ts>=?", (ev["symbol"], since))}
        others = {x["who"] for x in events if x["action"] == "buy" and x["symbol"] == ev["symbol"]} | prior
        ev["cluster"] = len(others | {ev["who"]}) >= 2
        if ev["cluster"]:
            ev["note"] = f"cluster: {len(others | {ev['who']})} insiders buying"
    log.info("insiders: %d new filings, %d events (caught up: %s)", len(entries), len(events), seen_any)
    return events


# =====================================================================================================
# source 2: Congress (House PTRs) - parser adapted from day-trader-skill (MIT)
# =====================================================================================================
def mdy(s: str | None) -> str | None:
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", s or "")
    if not m:
        return None
    y = int(m.group(3))
    y = y + 2000 if y < 100 else y
    try:
        return date(y, int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return None


def parse_house_index(data: bytes) -> list[dict]:
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            data = z.read(next(n for n in z.namelist() if n.lower().endswith(".xml")))
    root = ET.fromstring(data)
    out = []
    for m in root.iter("Member"):
        f = {c.tag: (c.text or "").strip() for c in m}
        if f.get("FilingType") != "P":
            continue
        out.append({"doc": f.get("DocID", ""), "year": int(f.get("Year") or 0), "last": f.get("Last", ""),
                    "first": f.get("First", ""), "district": f.get("StateDst", ""),
                    "name": " ".join(x for x in (f.get("Prefix"), f.get("First"), f.get("Last")) if x),
                    "filed": mdy(f.get("FilingDate"))})
    return out


PTR_ROW = re.compile(
    r"^\s*(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s+(?P<type>S \(partial\)|P|S|E)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})\s+(?P<amount>\S.*?)\s*$")
OWNER_NAMES = {"SP": "spouse", "JT": "joint", "DC": "child", "": "self"}


def parse_ptr_text(text: str) -> list[dict]:
    """Transaction rows from `pdftotext -layout` output of a House PTR."""
    rows, cur = [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = PTR_ROW.match(line)
        if m and mdy(m.group("date")):
            if cur:
                rows.append(cur)
            cur = {"owner": OWNER_NAMES.get(m.group("owner") or "", ""), "asset": m.group("asset"),
                   "type": m.group("type"), "date": mdy(m.group("date")), "amount": m.group("amount")}
            continue
        if cur is None:
            continue
        s = line.strip()
        if not s or re.match(r"^(F(ILING)?\s+S(TATUS)?|D(ESCRIPTION)?|S(UBHOLDING)?\s+O(F)?|L(OCATION)?)\s*:", s, re.I) \
                or re.match(r"^(ID\s+Owner|Type|Date|\* For the|I CERTIFY|Digitally Signed|Filing ID)", s, re.I):
            continue
        parts = re.split(r"\s{2,}", s)
        if cur["amount"].endswith("-") and re.fullmatch(r"\$[\d,]+", parts[-1]):
            cur["amount"] += " " + parts.pop()
        if parts and parts[0] and "[" not in cur["asset"]:
            cur["asset"] += " " + " ".join(parts)
    if cur:
        rows.append(cur)
    for r in rows:
        a = " ".join(r["asset"].split())
        tick = re.findall(r"\(([A-Z][A-Z0-9.\-]{0,6})\)", a)
        kind = re.findall(r"\[([A-Z]{2})\]", a)
        r["ticker"] = tick[-1].replace(".", "-") if tick else None
        r["asset_type"] = kind[-1] if kind else None
        r["asset"] = re.sub(r"\s*\[[A-Z]{2}\]|\s*\([A-Z][A-Z0-9.\-]{0,6}\)", "", a).strip(" -")
        nums = [int(x.replace(",", "")) for x in re.findall(r"\$([\d,]+)", r["amount"])]
        r["low"], r["high"] = (nums + [None, None])[:2]
        r["amount"] = " ".join(r["amount"].split())
    return rows


def pdf_text(pdf: bytes) -> str | None:
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf)
    try:
        out = subprocess.run([exe, "-layout", f.name, "-"], capture_output=True, timeout=60)
        return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None
    finally:
        os.unlink(f.name)


def member_watched(name_last: str, name_first: str, members: list[str]) -> bool:
    if not members:
        return True
    for m in members:
        parts = m.replace(",", " ").split()
        if parts and parts[-1].lower() == name_last.lower() or (parts and parts[0].lower() == name_last.lower()):
            return True
    return False


def check_congress(http: Http, store: Store, cfg: dict) -> list[dict]:
    today = now_utc().astimezone(ET_TZ).date()
    years = [today.year] + ([today.year - 1] if today.month == 1 else [])
    filings = []
    for y in years:
        filings += parse_house_index(http.get(HOUSE_INDEX.format(year=y)).content)
    first_run = store.get("congress_started") is None
    new = [f for f in filings if f["doc"] and not store.seen("ptr", f["doc"])]
    if first_run:
        cutoff = (today - timedelta(days=3)).isoformat()
        for f in new:
            if (f["filed"] or "") < cutoff:
                store.mark("ptr", f["doc"])
        new = [f for f in new if (f["filed"] or "") >= cutoff]
        store.put("congress_started", now_utc().isoformat())
    events = []
    for f in sorted(new, key=lambda f: f["filed"] or "")[:25]:
        url = HOUSE_PTR.format(year=f["year"], doc=f["doc"])
        try:
            text = pdf_text(http.get(url).content)
        except Exception as ex:  # noqa: BLE001
            log.warning("congress: %s failed: %s", f["doc"], ex)
            continue
        store.mark("ptr", f["doc"])
        if not text:
            events.append({"source": "congress", "who": f["name"], "role": f"House {f['district']}",
                           "action": "filed", "public_date": f["filed"], "url": url,
                           "detail": "new trade report (scanned PDF - open the link to read it)", "key": f["doc"],
                           "watched": member_watched(f["last"], f["first"], cfg["congress_members"])})
            continue
        for r in parse_ptr_text(text):
            if not r["ticker"] or r["type"] not in ("P", "S", "S (partial)") or (r["low"] or 0) < cfg["congress_min_usd"]:
                continue
            late = (date.fromisoformat(f["filed"]) - date.fromisoformat(r["date"])).days if f["filed"] and r["date"] else None
            events.append({"source": "congress", "who": f["name"], "role": f"House {f['district']}"
                           + (f", {r['owner']}" if r["owner"] and r["owner"] != "self" else ""),
                           "symbol": r["ticker"], "action": "buy" if r["type"] == "P" else "sell",
                           "value_usd": r["low"], "value_text": r["amount"], "trade_date": r["date"],
                           "public_date": f["filed"], "days_late": late, "detail": r["asset"], "url": url,
                           "key": f["doc"], "asset_type": r["asset_type"],
                           "watched": member_watched(f["last"], f["first"], cfg["congress_members"])})
    log.info("congress: %d new reports, %d trades", len(new), len(events))
    return events


# =====================================================================================================
# source 3: hedge funds (13F)
# =====================================================================================================
def parse_infotable(xml_text: str) -> dict:
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    out = {}
    for it in root.iter():
        if not it.tag.endswith("infoTable"):
            continue
        f = {c.tag.split("}")[-1]: c for c in it.iter()}
        if f.get("putCall") is not None and (f["putCall"].text or "").strip():
            continue
        cusip = (f["cusip"].text or "").strip()
        d = out.setdefault(cusip, {"name": (f["nameOfIssuer"].text or "").strip(), "value": 0.0, "shares": 0.0})
        d["value"] += float(f["value"].text or 0)
        d["shares"] += float(f["sshPrnamt"].text or 0) if "sshPrnamt" in f else 0
    return out


def fund_holdings(http: Http, cik: str, acc: str) -> dict:
    base = SEC_ARCHIVE.format(cik=int(cik), acc=acc.replace("-", ""))
    idx = http.get(base + "index.json", sec=True).json()
    for n in [i["name"] for i in idx["directory"]["item"] if i["name"].lower().endswith(".xml")]:
        if "primary_doc" in n.lower():
            continue
        try:
            h = parse_infotable(http.get(base + n, sec=True).text)
            if h:
                return h
        except ET.ParseError:
            continue
    return {}


def cusip_tickers(http: Http, store: Store, cusips: list[str]) -> dict:
    mp = store.get("cusip_map", {})
    todo = [c for c in cusips if c not in mp]
    key = env("OPENFIGI_API_KEY")
    hdr = {"Content-Type": "application/json", **({"X-OPENFIGI-APIKEY": key} if key else {})}
    for i in range(0, len(todo), 100 if key else 10):
        batch = todo[i:i + (100 if key else 10)]
        try:
            res = http.post_json(OPENFIGI, [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch],
                                 headers=hdr)
        except Exception as ex:  # noqa: BLE001
            log.warning("openfigi: %s", ex)
            break
        for c, r in zip(batch, res):
            data = r.get("data") or []
            mp[c] = data[0]["ticker"] if data else None
        if not key:
            _time.sleep(2.5)                                     # free tier: 25 requests/minute
    store.put("cusip_map", mp)
    return {c: mp.get(c) for c in cusips}


def check_funds(http: Http, store: Store, cfg: dict) -> list[dict]:
    events = []
    for fund, cik in cfg["funds"].items():
        try:
            sub = http.get(SEC_SUBMISSIONS.format(cik=str(cik).zfill(10)), sec=True).json()["filings"]["recent"]
        except Exception as ex:  # noqa: BLE001
            log.warning("funds: %s: %s", fund, ex)
            continue
        f13 = [(a, d) for a, f, d in zip(sub["accessionNumber"], sub["form"], sub["filingDate"]) if f == "13F-HR"]
        if len(f13) < 2 or store.seen("13f", f13[0][0]):
            continue
        (acc, filed), (prev_acc, _) = f13[0], f13[1]
        new, old = fund_holdings(http, cik, acc), fund_holdings(http, cik, prev_acc)
        store.mark("13f", acc)
        if not new:
            continue
        tick = cusip_tickers(http, store, sorted(set(new) | set(old)))
        url = SEC_ARCHIVE.format(cik=int(cik), acc=acc.replace("-", ""))
        changes = []
        for c, h in new.items():
            p = old.get(c)
            if p is None:
                changes.append(("new position", c, h["value"], None))
            elif p["shares"] and (h["shares"] - p["shares"]) / p["shares"] >= 0.25:
                changes.append(("added", c, h["value"], (h["shares"] / p["shares"] - 1) * 100))
        for c, p in old.items():
            if c not in new:
                changes.append(("sold out", c, p["value"], None))
        changes.sort(key=lambda x: -x[2])
        new_rank = 0
        for kind, c, val, pct in changes[:15]:
            t = tick.get(c)
            if not t:
                continue
            if kind == "new position":
                new_rank += 1
            events.append({"source": "funds", "who": fund, "role": "hedge fund 13F", "symbol": t.upper(),
                           "action": "sell" if kind == "sold out" else "buy", "value_usd": round(val),
                           "value_text": f"${val:,.0f}" + (f" (+{pct:.0f}% shares)" if pct else ""),
                           "public_date": filed, "detail": f"{kind}: {(new.get(c) or old.get(c))['name']}",
                           "url": url, "key": acc, "kind": kind, "new_rank": new_rank if kind == "new position" else None})
    log.info("funds: %d events", len(events))
    return events


# =====================================================================================================
# source 4: crypto whales (Hyperliquid)
# =====================================================================================================
def pick_whales(rows: list[dict], n: int) -> list[str]:
    """Consistent big winners: $1M+ account, profitable all-time, this month and this week, active, sane ROI."""
    scored = []
    for r in rows:
        try:
            av = float(r.get("accountValue") or 0)
            perf = {w: {k: float(v) for k, v in m.items()} for w, m in r.get("windowPerformances", [])}
        except (TypeError, ValueError):
            continue
        mo, wk, at = perf.get("month", {}), perf.get("week", {}), perf.get("allTime", {})
        if av < 1e6 or at.get("pnl", 0) <= 1e6 or mo.get("pnl", 0) <= 0 or wk.get("pnl", 0) <= 0:
            continue
        if not 0.02 <= mo.get("roi", 0) <= 3 or mo.get("vlm", 0) < 1e7:
            continue
        scored.append((mo["pnl"], r["ethAddress"].lower()))
    return [a for _, a in sorted(scored, reverse=True)[:n]]


def summarize_fills(fills: list[dict], min_notional: float) -> list[dict]:
    """Group a wallet's new fills by coin and direction; keep the big ones."""
    agg = defaultdict(lambda: {"sz": 0.0, "notional": 0.0, "time": 0})
    for f in fills:
        coin, d = f.get("coin", ""), f.get("dir", "")
        if coin.startswith("@") or not d:
            continue
        a = agg[(coin, d)]
        sz, px = float(f.get("sz", 0)), float(f.get("px", 0))
        a["sz"] += sz
        a["notional"] += sz * px
        a["time"] = max(a["time"], int(f.get("time", 0)))
    return [{"coin": c, "dir": d, **a, "px": a["notional"] / max(a["sz"], 1e-12)}
            for (c, d), a in agg.items() if a["notional"] >= min_notional]


def check_whales(http: Http, store: Store, cfg: dict) -> list[dict]:
    wl = store.get("whales", {})
    if not wl or wl.get("day") != now_utc().date().isoformat():
        try:
            rows = http.get(HL_LEADERBOARD, timeout=120).json().get("leaderboardRows", [])
            wl = {"day": now_utc().date().isoformat(), "auto": pick_whales(rows, cfg["whale_auto_top"])}
            store.put("whales", wl)
            log.info("whales: leaderboard refreshed, watching %d wallets", len(wl["auto"]))
        except Exception as ex:  # noqa: BLE001
            log.warning("whales: leaderboard failed (%s), keeping previous list", ex)
    wallets = list(dict.fromkeys([w.lower() for w in cfg["whale_wallets"]] + wl.get("auto", [])))
    last = store.get("whale_last", {})
    now_ms = int(now_utc().timestamp() * 1000)
    events = []
    for w in wallets:
        start = last.get(w, now_ms - 15 * 60 * 1000)          # first time: only the last 15 minutes
        try:
            fills = http.post_json(HL_INFO, {"type": "userFillsByTime", "user": w, "startTime": start + 1,
                                             "aggregateByTime": True})
        except Exception as ex:  # noqa: BLE001
            log.warning("whales: %s: %s", w[:10], ex)
            continue
        if fills:
            last[w] = max(int(f.get("time", 0)) for f in fills)
        else:
            last.setdefault(w, now_ms)
        for g in summarize_fills(fills or [], cfg["whale_min_notional_usd"]):
            d = g["dir"]
            action = ("buy" if d == "Open Long" else "sell" if d == "Close Long" else
                      "short" if d == "Open Short" else "cover" if d == "Close Short" else d.lower())
            events.append({"source": "whales", "who": w[:6] + "…" + w[-4:], "role": "Hyperliquid top wallet",
                           "symbol": g["coin"], "action": action, "value_usd": round(g["notional"]),
                           "value_text": f"${g['notional']:,.0f}", "trade_date": datetime.fromtimestamp(
                               g["time"] / 1000, timezone.utc).isoformat(timespec="minutes"),
                           "public_date": now_utc().isoformat(timespec="minutes"), "days_late": 0,
                           "detail": f"{d} {g['sz']:,.4g} {g['coin']} @ ~${g['px']:,.4g}",
                           "url": f"https://app.hyperliquid.xyz/explorer/address/{w}", "key": w, "wallet": w})
    store.put("whale_last", last)
    log.info("whales: %d wallets checked, %d events", len(wallets), len(events))
    return events


# =====================================================================================================
# alerts (ntfy.sh: free phone notifications, no account needed)
# =====================================================================================================
EMOJI = {"insiders": "briefcase", "congress": "classical_building", "funds": "bank", "whales": "whale"}


def alert_text(e: dict) -> tuple[str, str]:
    verb = {"buy": "BOUGHT", "sell": "SOLD", "short": "SHORTED", "cover": "COVERED", "filed": "FILED"}.get(
        e["action"], e["action"].upper())
    title = f"{e['who']} {verb} {e.get('symbol') or ''}".strip()
    lines = [f"{e.get('role') or ''}".strip(), e.get("detail") or ""]
    if e.get("value_text"):
        lines.append(f"Size: {e['value_text']}")
    if e.get("trade_date"):
        lag = f" ({e['days_late']} days before it was public)" if e.get("days_late") else ""
        lines.append(f"Traded: {e['trade_date']}{lag}")
    if e.get("note"):
        lines.append(e["note"])
    if e.get("copy_note"):
        lines.append(e["copy_note"])
    return title[:120], "\n".join(x for x in lines if x)


def push(title: str, body: str, tags: str = "", url: str | None = None, priority: str = "default") -> bool:
    topic = env("NTFY_TOPIC")
    if not topic:
        return False
    headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if url:
        headers["Click"] = url
    try:
        r = requests.post(NTFY.format(topic=topic), data=body.encode("utf-8"), headers=headers, timeout=20)
        return r.ok
    except Exception as ex:  # noqa: BLE001
        log.warning("push failed: %s", ex)
        return False


# =====================================================================================================
# paper copy-trader
# =====================================================================================================
class Copier:
    def __init__(self, cfg: dict, store: Store, trading=None, dry: bool = False):
        self.c, self.store, self.dry = cfg, store, dry
        self.t = trading
        self._assets = None

    def _client(self):
        if self.t is None:
            from alpaca.trading.client import TradingClient
            self.t = TradingClient(env("ALPACA_API_KEY"), env("ALPACA_SECRET_KEY"), paper=True)
        return self.t

    def crypto_symbols(self) -> set:
        if self._assets is None:
            from alpaca.trading.enums import AssetClass, AssetStatus
            from alpaca.trading.requests import GetAssetsRequest
            try:
                a = self._client().get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.CRYPTO))
                self._assets = {x.symbol for x in a if x.tradable and x.symbol.endswith("/USD")}
            except Exception as ex:  # noqa: BLE001
                log.warning("crypto asset list failed: %s", ex)
                self._assets = set()
        return self._assets

    def open_copies(self, asset_class: str | None = None) -> list[tuple]:
        q = "SELECT id, symbol, source, who, opened, asset_class, entry_px FROM copies WHERE status='open'"
        rows = self.store.db.execute(q).fetchall()
        return [r for r in rows if asset_class is None or r[5] == asset_class]

    def should_copy(self, e: dict) -> tuple[bool, str]:
        c = self.c
        if e["action"] != "buy" or not e.get("symbol"):
            return False, ""
        if e["source"] == "insiders":
            ok = e.get("is_exec") and (e["value_usd"] >= c["copy_insider_min_usd"] or e.get("cluster"))
            return bool(ok), "insider buy below copy size" if not ok else ""
        if e["source"] == "congress":
            if not e.get("watched"):
                return False, "member not on your copy list"
            if e.get("asset_type") not in (None, "ST"):
                return False, "not a plain stock"
            if e.get("days_late") is not None and e["days_late"] > c["copy_congress_max_age_days"]:
                return False, f"trade is {e['days_late']} days old"
            return True, ""
        if e["source"] == "funds":
            fresh = e.get("public_date") and (now_utc().date() - date.fromisoformat(e["public_date"])).days <= c["copy_fund_max_age_days"]
            ok = e.get("kind") == "new position" and (e.get("new_rank") or 99) <= c["copy_fund_top_new"] and fresh
            return bool(ok), "" if ok else "not a fresh top new position"
        if e["source"] == "whales":
            sym = f"{e['symbol']}/USD"
            return (sym in self.crypto_symbols()), "" if sym in self.crypto_symbols() else "coin not on Alpaca"
        return False, ""

    def open(self, e: dict, event_id: int) -> str:
        ok, why = self.should_copy(e)
        if not ok:
            return why
        crypto = e["source"] == "whales"
        sym = f"{e['symbol']}/USD" if crypto else e["symbol"]
        if any(r[1] == sym for r in self.open_copies()):
            return "already holding a copy"
        limit = self.c["copy_max_crypto_positions"] if crypto else self.c["copy_max_stock_positions"]
        if len(self.open_copies("crypto" if crypto else "stock")) >= limit:
            return "copy slots full"
        if self.dry:
            return f"would buy {self.c['copy_pct']:.0f}% of the account in {sym} (dry run)"
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        t = self._client()
        try:
            acct = t.get_account()
            equity, cash = float(acct.equity), float(acct.cash)
            if not crypto:
                a = t.get_asset(sym)
                if not (a.tradable and a.fractionable):
                    return "not tradable/fractionable at Alpaca"
            notional = round(min(equity * self.c["copy_pct"] / 100, cash * 0.95), 2)
            if notional < 1:
                return "not enough cash"
            o = t.submit_order(MarketOrderRequest(symbol=sym, notional=notional, side=OrderSide.BUY,
                                                  time_in_force=TimeInForce.GTC if crypto else TimeInForce.DAY))
        except Exception as ex:  # noqa: BLE001
            log.warning("copy %s failed: %s", sym, ex)
            return f"order failed: {str(ex)[:80]}"
        self.store.db.execute("INSERT INTO copies (opened, source, who, symbol, asset_class, notional, status, event_id,"
                              " order_id) VALUES (?,?,?,?,?,?,?,?,?)",
                              (now_utc().isoformat(timespec="seconds"), e["source"], e.get("wallet") or e["who"], sym,
                               "crypto" if crypto else "stock", notional, "open", event_id, str(o.id)))
        return f"COPIED: bought ${notional:.2f} of {sym} (paper)"

    def close(self, copy_id: int, sym: str, reason: str) -> str:
        if self.dry:
            return f"would sell {sym}: {reason} (dry run)"
        t = self._client()
        exit_px = pl = None
        try:
            pos = t.get_open_position(sym.replace("/", ""))
            exit_px, pl = float(pos.current_price), float(pos.unrealized_plpc) * 100
            t.close_position(sym.replace("/", ""))
        except Exception as ex:  # noqa: BLE001
            if "position does not exist" not in str(ex).lower() and "404" not in str(ex):
                log.warning("close %s failed: %s", sym, ex)
                return f"sell failed: {str(ex)[:80]}"
        self.store.db.execute("UPDATE copies SET status='closed', closed=?, exit_px=?, exit_reason=?, pl_pct=? WHERE id=?",
                              (now_utc().isoformat(timespec="seconds"), exit_px, reason, pl, copy_id))
        return f"SOLD copy of {sym} ({reason})" + (f", {pl:+.1f}%" if pl is not None else "")

    def follow_sells(self, e: dict) -> list[str]:
        """The investor sold: sell our copy of the same stock that came from them."""
        if e["action"] != "sell" or not e.get("symbol"):
            return []
        sym = f"{e['symbol']}/USD" if e["source"] == "whales" else e["symbol"]
        who = e.get("wallet") or e["who"]
        out = []
        for cid, s, src, cwho, *_ in self.open_copies():
            if s == sym and src == e["source"] and (cwho == who or e["source"] == "insiders"):
                out.append(self.close(cid, s, f"{e['who']} sold"))
        return out

    def maintain(self) -> list[str]:
        """Holding-period and stop-loss exits."""
        out = []
        if self.dry:
            return out
        t = self._client()
        try:
            held = {p.symbol: p for p in t.get_all_positions()}
        except Exception as ex:  # noqa: BLE001
            log.warning("positions failed: %s", ex)
            return out
        stock_open = False
        try:
            stock_open = bool(t.get_clock().is_open)
        except Exception:  # noqa: BLE001
            pass
        for cid, sym, src, who, opened, cls, _ in self.open_copies():
            p = held.get(sym.replace("/", ""))
            age = (now_utc() - datetime.fromisoformat(opened)).days
            crypto = cls == "crypto"
            if p is None:
                oid = self.store.db.execute("SELECT order_id FROM copies WHERE id=?", (cid,)).fetchone()[0]
                try:                                           # order still waiting for the market to open?
                    status = str(getattr(t.get_order_by_id(oid), "status", "")).lower() if oid else ""
                except Exception:  # noqa: BLE001
                    status = ""
                if any(s in status for s in ("new", "accepted", "pending", "partially", "held")):
                    continue
                if status or age >= 1:                         # never filled, or sold elsewhere
                    self.store.db.execute("UPDATE copies SET status='closed', closed=?, exit_reason=? WHERE id=?",
                                          (now_utc().isoformat(timespec="seconds"), "no position at Alpaca", cid))
                continue
            if not crypto and not stock_open:
                continue
            pl = float(p.unrealized_plpc) * 100
            hold, stop = ((self.c["crypto_hold_days"], self.c["crypto_stop_pct"]) if crypto
                          else (self.c["stock_hold_days"], self.c["stock_stop_pct"]))
            if pl <= -stop:
                out.append(self.close(cid, sym, f"stop-loss {pl:.1f}%"))
            elif age >= hold:
                out.append(self.close(cid, sym, f"held {age} days"))
        return out


# =====================================================================================================
# main loop
# =====================================================================================================
CHECKS = {"insiders": check_insiders, "congress": check_congress, "funds": check_funds, "whales": check_whales}


def due(store: Store, name: str, minutes: int) -> bool:
    last = store.get(f"last_{name}")
    return last is None or (now_utc() - datetime.fromisoformat(last)).total_seconds() >= minutes * 60 - 90


def run(dry: bool = False, only: list[str] | None = None, store: Store | None = None, http: Http | None = None,
        copier: Copier | None = None) -> list[dict]:
    cfg_all = load_config()
    cfg = {**DEFAULTS, **(cfg_all.get("tracker") or {})}
    path = cfg_all["journal_path"].replace("journal.sqlite", "tracker.sqlite")
    store = store or Store(":memory:" if dry else path)
    http = http or Http(env("SEC_USER_AGENT") or cfg_all.get("sec_user_agent", "tradebot research"))
    copier = copier or Copier(cfg, store, dry=dry or not cfg["copy_enabled"])

    events = []
    for name, fn in CHECKS.items():
        if only and name not in only:
            continue
        if not dry and not due(store, name, cfg["every_min"][name]):
            continue
        try:
            got = fn(http, store, cfg)
            events += got
            store.put(f"last_{name}", now_utc().isoformat())
        except Exception as ex:  # noqa: BLE001  one broken source never stops the others
            log.warning("%s check failed: %s", name, ex)
        store.commit()

    alerts = 0
    for e in events:
        e["copy_note"] = ""
        eid = store.add_event(e)
        notes = copier.follow_sells(e)
        msg = copier.open(e, eid) if e["action"] == "buy" else ""
        if msg.startswith(("COPIED", "would buy")):
            store.db.execute("UPDATE events SET copied=1 WHERE id=?", (eid,))
        e["copy_note"] = "; ".join([m for m in notes + [msg] if m.startswith(("COPIED", "SOLD", "would"))])
        store.db.execute("UPDATE events SET note=? WHERE id=?", ("; ".join(x for x in (e.get("note"), msg) if x), eid))
        if alerts < cfg["max_alerts_per_run"]:
            title, body = alert_text(e)
            sent = False if dry else push(title, body, EMOJI.get(e["source"], ""), e.get("url"),
                                          "high" if e["copy_note"] else "default")
            log.info("ALERT %s | %s", title, body.replace("\n", " | "))
            if sent:
                store.db.execute("UPDATE events SET alerted=1 WHERE id=?", (eid,))
            alerts += 1
    if len(events) > cfg["max_alerts_per_run"] and not dry:
        push(f"+{len(events) - cfg['max_alerts_per_run']} more trades", "See the Tracker tab on your dashboard.")
    for m in copier.maintain():
        log.info(m)
        if not dry:
            push("Copy closed", m, "moneybag")
    store.commit()
    log.info("done: %d events, %d alerts", len(events), alerts)
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--only", nargs="*", choices=list(CHECKS))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    if a.test_alert:
        ok = push("Tracker connected", "You'll get alerts here when watched investors trade.", "white_check_mark")
        log.info("test alert %s", "sent" if ok else "NOT sent (set the NTFY_TOPIC secret)")
        return
    run(dry=a.dry_run, only=a.only)


if __name__ == "__main__":
    main()
