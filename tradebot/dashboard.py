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

tab_pos, tab_bot, tab_ctl, tab_hist = st.tabs(["Positions", "Bot picks", "Controls", "History"])

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
