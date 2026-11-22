"""Trade bot command center (phone-friendly).

Shows live Alpaca account, positions and P/L, the bot's picks and reasoning, the
latest run log, and buttons to run / dry-run / pause the bot on GitHub.

Streamlit Cloud -> app Settings -> Secrets:
    ALPACA_API_KEY = "..."
    ALPACA_SECRET_KEY = "..."
    GH_TOKEN = "github_pat_..."      # fine-grained token: tradebot repo, Actions read/write, Contents read
    GH_REPO = "emanmatt/tradebot"
"""
import json
import os
import sqlite3
import tempfile
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

# Streamlit secrets -> environment, before the bot code reads keys
try:
    for k, v in st.secrets.items():
        if isinstance(v, str):
            os.environ.setdefault(k, v)
except Exception:  # noqa: BLE001  (no secrets file locally)
    pass

from bot.config import load_config  # noqa: E402

cfg = load_config()
GH_REPO = os.environ.get("GH_REPO", "emanmatt/tradebot")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
WORKFLOW = "trade.yml"
JOURNAL_IN_REPO = "tradebot/data/journal.sqlite"
TRACKER_IN_REPO = "tradebot/data/tracker.sqlite"
WATCHLIST_IN_REPO = "tradebot/data/watchlist.sqlite"

st.set_page_config(page_title="Trade Bot", page_icon="📈", layout="centered")


# ---------------- GitHub ----------------
def gh(method, path, **kw):
    h = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28", **kw.pop("headers", {})}
    r = requests.request(method, f"https://api.github.com/repos/{GH_REPO}{path}", headers=h, timeout=30, **kw)
    r.raise_for_status()
    return r


@st.cache_data(ttl=60)
def journal_bytes() -> bytes | None:
    if GH_TOKEN:
        try:
            return gh("GET", f"/contents/{JOURNAL_IN_REPO}",
                      headers={"Accept": "application/vnd.github.raw"}).content
        except Exception:  # noqa: BLE001
            pass
    try:
        return open(cfg["journal_path"], "rb").read()
    except OSError:
        return None


@st.cache_data(ttl=60)
def tracker_db():
    raw = None
    if GH_TOKEN:
        try:
            raw = gh("GET", f"/contents/{TRACKER_IN_REPO}", headers={"Accept": "application/vnd.github.raw"}).content
        except Exception:  # noqa: BLE001
            pass
    if raw is None:
        try:
            raw = open(cfg["journal_path"].replace("journal.sqlite", "tracker.sqlite"), "rb").read()
        except OSError:
            return None
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        f.write(raw)
    db = sqlite3.connect(f.name)
    ev = pd.read_sql_query("SELECT ts, source, who, role, symbol, action, value_text, trade_date, days_late, "
                           "detail, url, copied, note FROM events ORDER BY id DESC LIMIT 300", db)
    cp = pd.read_sql_query("SELECT opened, source, who, symbol, notional, status, closed, exit_reason, pl_pct "
                           "FROM copies ORDER BY id DESC", db)
    return ev, cp


@st.cache_data(ttl=60)
def watchlist_data():
    raw = None
    if GH_TOKEN:
        try:
            raw = gh("GET", f"/contents/{WATCHLIST_IN_REPO}", headers={"Accept": "application/vnd.github.raw"}).content
        except Exception:  # noqa: BLE001
            pass
    if raw is None:
        try:
            raw = open(os.path.join(os.path.dirname(cfg["journal_path"]), "watchlist.sqlite"), "rb").read()
        except OSError:
            return None
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        f.write(raw)
    from scanner.watchlist import latest
    db = sqlite3.connect(f.name)
    scan = db.execute("SELECT ts, n_picks, n_rejected, rejected FROM scans ORDER BY ts DESC LIMIT 1").fetchone()
    return latest(f.name), scan


@st.cache_data(ttl=30)
def runs():
    return gh("GET", f"/actions/workflows/{WORKFLOW}/runs", params={"per_page": 8}).json()["workflow_runs"]


@st.cache_data(ttl=30)
def workflow_state() -> str:
    return gh("GET", f"/actions/workflows/{WORKFLOW}").json()["state"]


@st.cache_data(ttl=60)
def run_log(run_id: int) -> str:
    job = gh("GET", f"/actions/runs/{run_id}/jobs").json()["jobs"][0]
    text = gh("GET", f"/actions/jobs/{job['id']}/logs").text
    lines = [ln.split(" ", 1)[1] if ln[:4].isdigit() and " " in ln else ln for ln in text.splitlines()]
    keep, on = [], False
    for ln in lines:
        if "=== run start" in ln or "Market closed" in ln:
            on = True
        if on and ("INFO" in ln or "WARNING" in ln or "ERROR" in ln or ln.startswith("Traceback")):
            keep.append(ln.split(" ", 1)[1] if ln[:4].isdigit() else ln)
        if "=== run done" in ln:
            break
    return "\n".join(keep) or "\n".join(lines[-40:])


# ---------------- Alpaca ----------------
@st.cache_resource
def alpaca():
    from alpaca.trading.client import TradingClient
    return TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)


@st.cache_data(ttl=30)
def live():
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest
    c = alpaca()
    a = c.get_account()
    pos = [{"Ticker": p.symbol, "Shares": float(p.qty), "Entry": float(p.avg_entry_price),
            "Now": float(p.current_price), "Value": float(p.market_value),
            "P/L $": float(p.unrealized_pl), "P/L %": float(p.unrealized_plpc) * 100,
            "Today %": float(p.change_today) * 100} for p in c.get_all_positions()]
    orders = [{"Time": o.submitted_at, "Ticker": o.symbol, "Side": o.side.value, "Type": o.type.value,
               "Qty": o.qty, "Filled @": o.filled_avg_price, "Status": o.status.value}
              for o in c.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=40, nested=False))]
    hist = c.get_portfolio_history(GetPortfolioHistoryRequest(period="3M", timeframe="1D"))
    eq = pd.Series(hist.equity, index=pd.to_datetime(hist.timestamp, unit="s"), name="Equity").dropna()
    eq = eq[eq > 0]
    return {"equity": float(a.equity), "last": float(a.last_equity), "cash": float(a.cash),
            "open": c.get_clock().is_open, "pos": pos, "orders": orders, "hist": eq}


def money(x):
    return f"${x:,.2f}"


# ================= UI =================
st.title("📈 Trade Bot")

if not os.environ.get("ALPACA_API_KEY"):
    st.error("Add ALPACA_API_KEY and ALPACA_SECRET_KEY in the app's Secrets to see live data.")
    st.stop()

try:
    L = live()
except Exception as e:  # noqa: BLE001
    st.error(f"Couldn't reach Alpaca: {e}")
    st.stop()

day = L["equity"] - L["last"]
c1, c2 = st.columns(2)
c1.metric("Account value", money(L["equity"]), f"{money(day)} today")
c2.metric("Open P/L", money(sum(p["P/L $"] for p in L["pos"])), f"{len(L['pos'])} positions")
c3, c4 = st.columns(2)
c3.metric("Cash", money(L["cash"]))
c4.metric("Market", "🟢 Open" if L["open"] else "🔴 Closed")
if st.button("🔄 Refresh", width="stretch"):
    st.cache_data.clear()
    st.rerun()

tab_wl, tab_trk, tab_pos, tab_bot, tab_ctl, tab_hist = st.tabs(
    ["Watchlist", "Tracker", "Positions", "Bot picks", "Controls", "History"])

# ---------- step 1: watchlist ----------
with tab_wl:
    W = watchlist_data()
    if not W or not W[0]:
        st.info("No watchlist yet. The Scanner runs every 30 minutes on weekdays from 4 AM ET.")
    else:
        picks, scan = W
        st.caption(f"Scanned {scan[0][:16].replace('T', ' ')} UTC · {scan[1]} picks · {scan[2]} rejected")
        KIND = {"momentum": "🚀", "smart money": "🧠", "both": "🚀🧠"}
        for p in picks:
            rv = f" · vol {p['rvol']:.1f}x" if p["rvol"] else ""
            with st.expander(f"{p['rank']}. {KIND.get(p['kind'], '')} **{p['symbol']}** ${p['price']:.2f} · "
                             f"{p['gap_pct']:+.1f}%{rv} · score {p['score']:.2f}"):
                for r in p["reasons"]:
                    st.write("• " + r)
        with st.expander("Why other stocks were rejected"):
            st.dataframe(pd.DataFrame(json.loads(scan[3] or "[]")), hide_index=True, width="stretch")

# ---------- investor tracker ----------
ICON = {"insiders": "💼", "congress": "🏛️", "funds": "🏦", "whales": "🐋"}
with tab_trk:
    T = tracker_db()
    if T is None:
        st.info("The tracker hasn't saved anything yet. It runs every 15 minutes once tracker.yml is uploaded.")
    else:
        ev, cp = T
        opn = cp[cp["status"] == "open"]
        done = cp[cp["status"] == "closed"]
        a, b = st.columns(2)
        a.metric("Open copies", len(opn), f"${opn['notional'].sum():,.2f} invested")
        b.metric("Closed copies", len(done),
                 f"{done['pl_pct'].mean():+.1f}% avg" if done["pl_pct"].notna().any() else None)
        pick = st.multiselect("Show", list(ICON), default=list(ICON),
                              format_func=lambda k: f"{ICON[k]} {k}")
        only_copied = st.toggle("Only trades I copied")
        view = ev[ev["source"].isin(pick)]
        if only_copied:
            view = view[view["copied"] == 1]
        if view.empty:
            st.info("No alerts yet. Insider buys show up within a day or two; Congress and funds are slower.")
        for _, r in view.head(60).iterrows():
            verb = {"buy": "🟢 bought", "sell": "🔴 sold", "short": "🔻 shorted", "cover": "↩️ covered"}.get(
                r["action"], r["action"])
            head = f"{ICON.get(r['source'], '')} **{r['symbol'] or ''}** · {r['who']} {verb}"
            with st.expander(head + (" · ✅ copied" if r["copied"] else "")):
                st.write(f"{r['role'] or ''} · {r['detail'] or ''}")
                if r["value_text"]:
                    st.write(f"Size: {r['value_text']}")
                if r["trade_date"]:
                    lag = f" · public {int(r['days_late'])} days later" if pd.notna(r["days_late"]) and r["days_late"] else ""
                    st.write(f"Traded {r['trade_date']}{lag}")
                st.caption(f"Seen {r['ts'][:16].replace('T', ' ')} UTC" + (f" · {r['note']}" if r["note"] else ""))
                if r["url"]:
                    st.link_button("Open filing", r["url"])
        if not cp.empty:
            st.subheader("Copies")
            st.dataframe(cp.rename(columns={"notional": "$", "pl_pct": "P/L %"}), hide_index=True, width="stretch")

# ---------- positions ----------
with tab_pos:
    if not L["pos"]:
        st.info("No open positions.")
    for p in sorted(L["pos"], key=lambda p: -p["Value"]):
        color = "🟢" if p["P/L $"] >= 0 else "🔴"
        with st.expander(f"{color} **{p['Ticker']}**  {money(p['Value'])}  ·  {p['P/L %']:+.1f}%"):
            st.write(f"{p['Shares']:.0f} shares · bought {money(p['Entry'])} · now {money(p['Now'])}")
            st.write(f"P/L {money(p['P/L $'])} · today {p['Today %']:+.1f}%")
            if st.checkbox(f"Confirm sell all {p['Ticker']}", key="c" + p["Ticker"]):
                if st.button(f"Sell {p['Ticker']} now", key="s" + p["Ticker"], type="primary"):
                    from bot.broker import Broker
                    Broker(cfg).close(p["Ticker"])
                    st.success(f"Sell order sent for {p['Ticker']}.")
                    st.cache_data.clear()
    st.subheader("Recent orders")
    st.dataframe(pd.DataFrame(L["orders"]), hide_index=True, width="stretch")

# ---------- bot reasoning ----------
with tab_bot:
    raw = journal_bytes()
    if not raw:
        st.info("No bot runs saved yet.")
    else:
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
            f.write(raw)
        db = sqlite3.connect(f.name)
        sig = pd.read_sql_query("SELECT ts, ticker, score, acted, reasons FROM signals ORDER BY ts DESC LIMIT 40", db)
        if sig.empty:
            st.info("No picks yet.")
        last_day = None
        for _, r in sig.iterrows():
            d = r["ts"][:10]
            if d != last_day:
                st.markdown(f"#### {d}")
                last_day = d
            tag = "✅ bought" if r["acted"] else "⏸ not bought"
            with st.expander(f"**{r['ticker']}** · score {r['score']:.2f} · {tag}"):
                for reason in json.loads(r["reasons"]):
                    st.write("• " + reason)
        ev = pd.read_sql_query("SELECT ts, msg FROM events ORDER BY ts DESC LIMIT 10", db)
        if not ev.empty:
            st.subheader("⚠️ Safety warnings")
            st.dataframe(ev, hide_index=True, width="stretch")

# ---------- controls ----------
with tab_ctl:
    if not GH_TOKEN:
        st.warning("Add GH_TOKEN in the app's Secrets to control the bot from here.")
    else:
        try:
            state = workflow_state()
        except Exception as e:  # noqa: BLE001
            st.error(f"GitHub error: {e}")
            state = "unknown"
        active = state == "active"
        st.write(f"Schedule: **{'▶️ ON: trading all day, 9:45 AM–3:50 PM ET' if active else '⏸ PAUSED'}**")

        b1, b2 = st.columns(2)
        if b1.button("▶️ Run now", width="stretch", type="primary", disabled=not active):
            gh("POST", f"/actions/workflows/{WORKFLOW}/dispatches", json={"ref": "main", "inputs": {"dry_run": "false"}})
            st.success("Started. Results in ~2 min, then tap Refresh.")
            st.cache_data.clear()
        if b2.button("🧪 Dry run", width="stretch"):
            gh("POST", f"/actions/workflows/{WORKFLOW}/dispatches", json={"ref": "main", "inputs": {"dry_run": "true"}})
            st.success("Dry run started. Check the log below in ~2 min.")
            st.cache_data.clear()
        if active:
            if st.button("⏸ Pause bot (stop daily runs)", width="stretch"):
                gh("PUT", f"/actions/workflows/{WORKFLOW}/disable")
                st.cache_data.clear()
                st.rerun()
        elif st.button("▶️ Resume bot", width="stretch"):
            gh("PUT", f"/actions/workflows/{WORKFLOW}/enable")
            st.cache_data.clear()
            st.rerun()
        st.caption("Pausing stops new runs. Existing positions keep their stop-loss and take-profit at Alpaca.")

        st.subheader("Latest runs")
        try:
            rs = runs()
            icons = {"success": "✅", "failure": "❌", "cancelled": "⛔"}
            for r in rs[:5]:
                when = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone()
                ic = icons.get(r["conclusion"], "⏳")
                kind = "scheduled" if r["event"] == "schedule" else "manual"
                st.write(f"{ic} {when:%a %b %d, %I:%M %p} · {kind}")
            if rs:
                done = next((r for r in rs if r["status"] == "completed"), None)
                if done:
                    st.subheader("Latest log")
                    st.code(run_log(done["id"]), language=None)
        except Exception as e:  # noqa: BLE001
            st.error(f"Couldn't load runs: {e}")

# ---------- history ----------
with tab_hist:
    if len(L["hist"]) > 1:
        st.line_chart(L["hist"])
        start = L["hist"].iloc[0]
        st.write(f"Since {L['hist'].index[0]:%b %d}: {money(L['equity'] - start)} "
                 f"({(L['equity'] / start - 1) * 100:+.2f}%)")
    else:
        st.info("Chart fills in after a few trading days.")
