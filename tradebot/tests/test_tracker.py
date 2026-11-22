"""Offline tests for the investor tracker (no network, no orders)."""
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot import tracker as T

CFG = dict(T.DEFAULTS)

ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>4 - Smith John (0001234567) (Reporting)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1234567/000123456726000001/0001234567-26-000001-index.htm"/>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
<id>urn:tag:sec.gov,2008:accession-number=0001234567-26-000001</id><updated>2026-11-20T16:01:02-05:00</updated></entry>
<entry><title>4 - ACME CORP (0000999999) (Issuer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/999999/000123456726000001/0001234567-26-000001-index.htm"/>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
<id>urn:tag:sec.gov,2008:accession-number=0001234567-26-000001</id></entry>
<entry><title>424B2 - Something</title>
<link href="https://www.sec.gov/Archives/edgar/data/1/000000000126000009/x-index.htm"/>
<category term="424B2"/><id>urn:tag:sec.gov,2008:accession-number=0000000001-26-000009</id></entry>
</feed>"""

FORM4 = """<?xml version="1.0"?>
<ownershipDocument><documentType>4</documentType>
<issuer><issuerCik>0000999999</issuerCik><issuerName>ACME CORP</issuerName><issuerTradingSymbol>acme</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerCik>0001234567</rptOwnerCik><rptOwnerName>Smith John</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><officerTitle>CEO</officerTitle></reportingOwnerRelationship></reportingOwner>
<aff10b5One>0</aff10b5One>
<nonDerivativeTable>
<nonDerivativeTransaction><transactionDate><value>2026-11-18</value></transactionDate>
<transactionCoding><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>10000</value></transactionShares><transactionPricePerShare><value>25.50</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
<nonDerivativeTransaction><transactionDate><value>2026-11-19</value></transactionDate>
<transactionCoding><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>2000</value></transactionShares><transactionPricePerShare><value>26</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
<nonDerivativeTransaction><transactionDate><value>2026-11-19</value></transactionDate>
<transactionCoding><transactionCode>M</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>99999</value></transactionShares><transactionPricePerShare><value>1</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
</nonDerivativeTable></ownershipDocument>"""

HOUSE_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<FinancialDisclosure>
<Member><Prefix>Hon.</Prefix><Last>Pelosi</Last><First>Nancy</First><Suffix /><FilingType>P</FilingType>
<StateDst>CA11</StateDst><Year>2026</Year><FilingDate>11/19/2026</FilingDate><DocID>20033333</DocID></Member>
<Member><Prefix>Hon.</Prefix><Last>Doe</Last><First>Jane</First><Suffix /><FilingType>O</FilingType>
<StateDst>TX01</StateDst><Year>2026</Year><FilingDate>11/19/2026</FilingDate><DocID>10011111</DocID></Member>
</FinancialDisclosure>"""

PTR_TEXT = """P       T      R
Clerk of the House of Representatives

Name:         Hon. Nancy Pelosi
Status:       Member
State/District: CA11

ID    Owner Asset                                 Transaction Date            Notification Amount
                                                  Type                        Date

      SP    NVIDIA Corporation - Common Stock (NVDA) P         11/01/2026 11/01/2026 $1,000,001 -
            [ST]                                                                     $5,000,000
            F     S     : New
      SP    Apple Inc. - Common Stock (AAPL) [ST] S (partial) 10/02/2026 10/02/2026 $250,001 - $500,000
      JT    Tiny Holdings (TINY) [ST]             P         11/03/2026 11/03/2026 $1,001 - $15,000
            Some Bond Fund [GS]                   P         11/04/2026 11/04/2026 $15,001 - $50,000
* For the complete list of asset type abbreviations
"""

INFOTABLE_NEW = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
<infoTable><nameOfIssuer>ALPHA INC</nameOfIssuer><cusip>111111111</cusip><value>5000000</value>
<shrsOrPrnAmt><sshPrnamt>1000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>BETA INC</nameOfIssuer><cusip>222222222</cusip><value>9000000</value>
<shrsOrPrnAmt><sshPrnamt>3000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>BETA INC</nameOfIssuer><cusip>222222222</cusip><value>100</value>
<shrsOrPrnAmt><sshPrnamt>1</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt><putCall>Put</putCall></infoTable>
</informationTable>"""

INFOTABLE_OLD = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
<infoTable><nameOfIssuer>BETA INC</nameOfIssuer><cusip>222222222</cusip><value>4000000</value>
<shrsOrPrnAmt><sshPrnamt>1000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>GAMMA INC</nameOfIssuer><cusip>333333333</cusip><value>7000000</value>
<shrsOrPrnAmt><sshPrnamt>500</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
</informationTable>"""


def lb_row(addr, av, month_pnl, week_pnl=1e5, at_pnl=5e6, roi=0.2, vlm=5e7):
    perf = lambda pnl, r=roi: {"pnl": str(pnl), "roi": str(r), "vlm": str(vlm)}  # noqa: E731
    return {"ethAddress": addr, "accountValue": str(av), "displayName": None,
            "windowPerformances": [["day", perf(1)], ["week", perf(week_pnl)], ["month", perf(month_pnl)],
                                   ["allTime", perf(at_pnl)]]}


# ---------------------------------------------------------------------------------------- fakes
class Resp:
    def __init__(self, text="", content=None, js=None):
        self.text, self.content, self._js = text, content if content is not None else text.encode(), js

    def json(self):
        return self._js if self._js is not None else json.loads(self.text)


class FakeHttp:
    def __init__(self, routes, posts=None):
        self.routes, self.posts, self.calls = routes, posts or (lambda url, body: []), []

    def get(self, url, sec=False, timeout=60, **kw):
        self.calls.append(url)
        for k, v in self.routes.items():
            if k in url:
                return v(url) if callable(v) else v
        raise AssertionError(f"unexpected GET {url}")

    def post_json(self, url, body, timeout=30, headers=None):
        self.calls.append(url)
        return self.posts(url, body)


class FakeTrading:
    def __init__(self, equity=100.0, positions=None, is_open=True):
        self.orders, self.closed = [], []
        self.equity, self.positions, self.is_open = equity, positions or {}, is_open
        self.order_status = {}

    def get_account(self):
        return SimpleNamespace(equity=str(self.equity), cash=str(self.equity))

    def get_asset(self, sym):
        return SimpleNamespace(tradable=True, fractionable=True, symbol=sym)

    def get_all_assets(self, req):
        return [SimpleNamespace(symbol="BTC/USD", tradable=True), SimpleNamespace(symbol="ETH/USD", tradable=True)]

    def submit_order(self, req):
        self.orders.append(req)
        return SimpleNamespace(id=f"o{len(self.orders)}")

    def get_open_position(self, sym):
        if sym not in self.positions:
            raise Exception("position does not exist")
        return self.positions[sym]

    def close_position(self, sym):
        self.closed.append(sym)

    def get_all_positions(self):
        return [SimpleNamespace(symbol=s, **vars(p)) for s, p in self.positions.items()]

    def get_order_by_id(self, oid):
        return SimpleNamespace(status=self.order_status.get(oid, "filled"))

    def get_clock(self):
        return SimpleNamespace(is_open=self.is_open)


def pos(price=10.0, plpc=0.0):
    return SimpleNamespace(current_price=str(price), unrealized_plpc=str(plpc))


@pytest.fixture
def store():
    return T.Store(":memory:")


# ---------------------------------------------------------------------------------------- insiders
def test_parse_atom_dedupes_and_keeps_form():
    e = T.parse_atom(ATOM)
    assert [x["form"] for x in e] == ["4", "4", "424B2"]
    assert e[0]["acc"] == "0001234567-26-000001"
    assert e[0]["folder"].endswith("/000123456726000001/")


def test_form4_buy_event():
    doc = T.parse_form4(FORM4)
    assert doc["ticker"] == "ACME" and doc["owners"][0]["title"] == "CEO"
    ev = T.form4_events(doc, "acc1", "u", CFG)
    assert len(ev) == 1 and ev[0]["action"] == "buy"
    assert ev[0]["value_usd"] == 10000 * 25.5 + 2000 * 26          # the 'M' option exercise is ignored
    assert ev[0]["trade_date"] == "2026-11-19" and ev[0]["is_exec"]


def test_form4_small_or_nonform_ignored():
    assert T.parse_form4("<notOwnership/>") is None
    assert T.parse_form4("garbage<") is None
    small = FORM4.replace("<value>10000</value>", "<value>10</value>").replace("<value>2000</value>", "<value>1</value>")
    assert T.form4_events(T.parse_form4(small), "a", "u", CFG) == []


def insider_http():
    return FakeHttp({"getcurrent": Resp(ATOM), "index.json": Resp(js={"directory": {"item": [
        {"name": "0001234567-26-000001-index.htm"}, {"name": "form4.xml"}]}}), "form4.xml": Resp(FORM4)})


def test_check_insiders_first_run_silent_then_alerts(store):
    http = insider_http()
    assert T.check_insiders(http, store, CFG) == []                # first run: remember backlog only
    assert store.seen("form4", "0001234567-26-000001")
    store.db.execute("DELETE FROM seen")
    ev = T.check_insiders(http, store, CFG)
    assert len(ev) == 1 and ev[0]["symbol"] == "ACME"              # same accession listed twice -> one event
    assert T.check_insiders(http, store, CFG) == []                # already seen


# ---------------------------------------------------------------------------------------- congress
def test_house_index_zip_and_filter():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("2026FD.xml", HOUSE_XML)
        z.writestr("2026FD.txt", "ignored")
    rows = T.parse_house_index(buf.getvalue())
    assert len(rows) == 1 and rows[0]["doc"] == "20033333" and rows[0]["filed"] == "2026-11-19"
    assert rows[0]["name"] == "Hon. Nancy Pelosi"


def test_parse_ptr_rows():
    rows = T.parse_ptr_text(PTR_TEXT)
    nv = rows[0]
    assert nv["ticker"] == "NVDA" and nv["type"] == "P" and nv["owner"] == "spouse"
    assert nv["low"] == 1_000_001 and nv["high"] == 5_000_000 and nv["asset_type"] == "ST"
    assert nv["date"] == "2026-11-01" and "NVIDIA" in nv["asset"]
    assert rows[1]["ticker"] == "AAPL" and rows[1]["type"] == "S (partial)"
    assert rows[3]["ticker"] is None and rows[3]["asset_type"] == "GS"


def test_check_congress(store, monkeypatch):
    monkeypatch.setattr(T, "pdf_text", lambda b: PTR_TEXT)
    monkeypatch.setattr(T, "now_utc", lambda: datetime(2026, 11, 20, 18, tzinfo=timezone.utc))
    http = FakeHttp({"FD.zip": Resp(content=HOUSE_XML), "ptr-pdfs": Resp(content=b"%PDF")})
    ev = T.check_congress(http, store, CFG)
    assert [(e["symbol"], e["action"]) for e in ev] == [("NVDA", "buy"), ("AAPL", "sell")]
    assert ev[0]["days_late"] == 18 and ev[0]["watched"]
    assert T.check_congress(http, store, CFG) == []


def test_member_watched():
    assert T.member_watched("Pelosi", "Nancy", [])
    assert T.member_watched("Pelosi", "Nancy", ["Nancy Pelosi"])
    assert not T.member_watched("Doe", "Jane", ["Pelosi"])


# ---------------------------------------------------------------------------------------- funds
def test_infotable_and_check_funds(store, monkeypatch):
    h = T.parse_infotable(INFOTABLE_NEW)
    assert h["222222222"]["shares"] == 3000                        # put option excluded
    monkeypatch.setattr(T, "cusip_tickers", lambda http, st, cs: {"111111111": "ALPH", "222222222": "BETA",
                                                                  "333333333": "GAMA"})
    sub = {"filings": {"recent": {"accessionNumber": ["0000000001-26-000002", "x", "0000000001-26-000001"],
                                  "form": ["13F-HR", "SC 13G", "13F-HR"],
                                  "filingDate": [datetime.now(timezone.utc).date().isoformat(), "", "2026-08-14"]}}}

    def route(url):
        if "000000000126000002/index.json" in url or "000000000126000001/index.json" in url:
            return Resp(js={"directory": {"item": [{"name": "primary_doc.xml"}, {"name": "info.xml"}]}})
        if "000000000126000002/info.xml" in url:
            return Resp(INFOTABLE_NEW)
        if "000000000126000001/info.xml" in url:
            return Resp(INFOTABLE_OLD)
        raise AssertionError(url)

    http = FakeHttp({"submissions": Resp(js=sub), "Archives": route})
    cfg = {**CFG, "funds": {"Test Fund": "1"}}
    ev = T.check_funds(http, store, cfg)
    kinds = {(e["symbol"], e["kind"]) for e in ev}
    assert kinds == {("BETA", "added"), ("ALPH", "new position"), ("GAMA", "sold out")}
    assert next(e for e in ev if e["symbol"] == "ALPH")["new_rank"] == 1
    assert T.check_funds(http, store, cfg) == []                   # same filing not re-alerted


# ---------------------------------------------------------------------------------------- whales
def test_pick_whales_filters():
    rows = [lb_row("0xA", 5e6, 9e5), lb_row("0xB", 5e6, 2e6), lb_row("0xC", 5e5, 9e6),      # C too small
            lb_row("0xD", 5e6, 3e6, week_pnl=-1), lb_row("0xE", 5e6, 3e6, roi=50),         # D losing week, E crazy ROI
            lb_row("0xF", 5e6, 3e6, vlm=10)]                                                # F inactive
    assert T.pick_whales(rows, 10) == ["0xb", "0xa"]


def test_summarize_fills():
    fills = [{"coin": "BTC", "dir": "Open Long", "sz": "3", "px": "100000", "time": 5},
             {"coin": "BTC", "dir": "Open Long", "sz": "1", "px": "100000", "time": 9},
             {"coin": "ETH", "dir": "Close Short", "sz": "1", "px": "3000", "time": 7},
             {"coin": "@107", "dir": "Buy", "sz": "1e9", "px": "1", "time": 7}]
    g = T.summarize_fills(fills, 250_000)
    assert len(g) == 1 and g[0]["notional"] == 400_000 and g[0]["time"] == 9


def test_check_whales(store):
    now_ms = int(T.now_utc().timestamp() * 1000)

    def posts(url, body):
        if body["user"] == "0xb":
            return [{"coin": "BTC", "dir": "Open Long", "sz": "5", "px": "100000", "time": now_ms}]
        return []

    http = FakeHttp({"leaderboard": Resp(js={"leaderboardRows": [lb_row("0xB", 5e6, 2e6)]})}, posts)
    ev = T.check_whales(http, store, CFG)
    assert len(ev) == 1 and ev[0]["action"] == "buy" and ev[0]["symbol"] == "BTC" and ev[0]["wallet"] == "0xb"
    assert store.get("whale_last")["0xb"] == now_ms
    assert T.check_whales(http, store, CFG)[0]["symbol"] == "BTC"   # fake returns same fill; startTime moved on
    assert sum("leaderboard" in c for c in http.calls) == 1        # leaderboard cached for the day


# ---------------------------------------------------------------------------------------- copier
def ev(**kw):
    base = {"source": "insiders", "who": "Smith John", "symbol": "ACME", "action": "buy", "value_usd": 300_000,
            "is_exec": True}
    return {**base, **kw}


def test_copy_rules(store):
    c = T.Copier(CFG, store, trading=FakeTrading())
    assert c.should_copy(ev())[0]
    assert not c.should_copy(ev(value_usd=120_000))[0]
    assert c.should_copy(ev(value_usd=120_000, cluster=True))[0]
    assert not c.should_copy(ev(source="congress", watched=True, days_late=40))[0]
    assert c.should_copy(ev(source="congress", watched=True, days_late=10, asset_type="ST"))[0]
    assert not c.should_copy(ev(source="congress", watched=False, days_late=1))[0]
    today = T.now_utc().date().isoformat()
    assert c.should_copy(ev(source="funds", kind="new position", new_rank=2, public_date=today))[0]
    assert not c.should_copy(ev(source="funds", kind="added", new_rank=None, public_date=today))[0]
    assert c.should_copy(ev(source="whales", symbol="BTC"))[0]
    assert not c.should_copy(ev(source="whales", symbol="HYPE"))[0]
    assert not c.should_copy(ev(action="sell"))[0]


def test_copy_open_limits_and_follow_sell(store):
    tr = FakeTrading(equity=100)
    c = T.Copier({**CFG, "copy_max_stock_positions": 1}, store, trading=tr)
    assert c.open(ev(), 1).startswith("COPIED: bought $5.00 of ACME")
    assert c.open(ev(), 2) == "already holding a copy"
    assert c.open(ev(symbol="ZZZ"), 3) == "copy slots full"
    tr.positions["ACME"] = pos(12, 0.2)
    msg = c.follow_sells(ev(action="sell", who="Other Insider"))
    assert msg and "SOLD copy of ACME" in msg[0] and tr.closed == ["ACME"]
    assert c.open_copies() == []


def test_crypto_copy_and_whale_close(store):
    tr = FakeTrading(equity=200)
    c = T.Copier(CFG, store, trading=tr)
    assert "BTC/USD" in c.open(ev(source="whales", symbol="BTC", wallet="0xb", who="0xb…"), 1)
    assert tr.orders[0].symbol == "BTC/USD" and tr.orders[0].notional == 10.0
    tr.positions["BTCUSD"] = pos(1, 0.01)
    assert c.follow_sells(ev(source="whales", symbol="BTC", action="sell", wallet="0xOTHER")) == []
    assert c.follow_sells(ev(source="whales", symbol="BTC", action="sell", wallet="0xb"))
    assert tr.closed == ["BTCUSD"]


def test_maintain_stop_and_age(store):
    tr = FakeTrading()
    c = T.Copier(CFG, store, trading=tr)
    old = (T.now_utc() - timedelta(days=30)).isoformat(timespec="seconds")
    store.db.executemany("INSERT INTO copies (opened, source, who, symbol, asset_class, status) VALUES (?,?,?,?,?,?)",
                         [(T.now_utc().isoformat(), "insiders", "a", "LOSE", "stock", "open"),
                          (old, "congress", "b", "OLD", "stock", "open"),
                          (T.now_utc().isoformat(), "insiders", "c", "OK", "stock", "open"),
                          (old, "insiders", "d", "GONE", "stock", "open")])
    tr.positions = {"LOSE": pos(8, -0.2), "OLD": pos(11, 0.1), "OK": pos(10, 0.02)}
    out = c.maintain()
    assert any("stop-loss" in m for m in out) and any("held 30 days" in m for m in out)
    assert sorted(tr.closed) == ["LOSE", "OLD"]
    assert [r[1] for r in c.open_copies()] == ["OK"]                 # GONE cleaned up, OK kept
    tr2 = FakeTrading(is_open=False, positions={"OK": pos(1, -0.5)})
    assert T.Copier(CFG, store, trading=tr2).maintain() == []        # stocks wait for market hours


def test_queued_weekend_order_kept(store):
    tr = FakeTrading()
    c = T.Copier(CFG, store, trading=tr)
    old = (T.now_utc() - timedelta(days=2)).isoformat(timespec="seconds")
    store.db.execute("INSERT INTO copies (opened, source, who, symbol, asset_class, status, order_id) "
                     "VALUES (?,?,?,?,?,?,?)", (old, "insiders", "a", "WAIT", "stock", "open", "o9"))
    tr.order_status["o9"] = "OrderStatus.ACCEPTED"
    c.maintain()
    assert [r[1] for r in c.open_copies()] == ["WAIT"]
    tr.order_status["o9"] = "OrderStatus.CANCELED"
    c.maintain()
    assert c.open_copies() == []


def test_dry_run_never_orders(store):
    tr = FakeTrading()
    c = T.Copier(CFG, store, trading=tr, dry=True)
    assert "dry run" in c.open(ev(), 1) and tr.orders == []


# ---------------------------------------------------------------------------------------- run loop
def test_run_end_to_end(store, monkeypatch):
    pushed = []
    monkeypatch.setattr(T, "push", lambda *a, **k: pushed.append(a) or True)
    fake = {"insiders": lambda h, s, c: [ev()], "congress": lambda h, s, c: [],
            "funds": lambda h, s, c: 1 / 0, "whales": lambda h, s, c: []}      # a broken source is skipped
    monkeypatch.setattr(T, "CHECKS", fake)
    tr = FakeTrading()
    out = T.run(store=store, http=object(), copier=T.Copier(CFG, store, trading=tr))
    assert len(out) == 1 and len(tr.orders) == 1
    row = store.db.execute("SELECT symbol, copied, alerted, note FROM events").fetchone()
    assert row[:3] == ("ACME", 1, 1) and "COPIED" in row[3]
    assert "COPIED" in pushed[0][1]
    assert T.run(store=store, http=object(), copier=T.Copier(CFG, store, trading=tr)) == []   # not due yet


def test_alert_text():
    title, body = T.alert_text({**ev(), "role": "CEO", "detail": "ACME: 100 sh", "value_text": "$300,000",
                                "trade_date": "2026-11-01", "days_late": 18, "copy_note": "COPIED: x"})
    assert title == "Smith John BOUGHT ACME"
    assert "18 days before it was public" in body and "COPIED" in body
