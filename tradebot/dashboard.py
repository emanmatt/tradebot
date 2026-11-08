"""Local dashboard:  streamlit run dashboard.py"""
import json
import sqlite3

import pandas as pd
import streamlit as st

from bot.config import load_config

cfg = load_config()
db = sqlite3.connect(cfg["journal_path"])
q = lambda sql: pd.read_sql_query(sql, db)  # noqa: E731

st.set_page_config(page_title="Trade Bot", layout="wide")
st.title("Swing Trade Bot")

eq = q("SELECT ts, equity, exposure, mode FROM equity ORDER BY ts")
if eq.empty:
    st.info("No runs yet. Run `python -m bot.main --dry-run` first.")
    st.stop()

eq["ts"] = pd.to_datetime(eq["ts"])
cur, start = eq["equity"].iloc[-1], eq["equity"].iloc[0]
peak = eq["equity"].max()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Equity", f"${cur:,.0f}", f"{cur / start - 1:+.2%} since start")
c2.metric("Invested", f"${eq['exposure'].iloc[-1]:,.0f}")
c3.metric("From peak", f"{cur / peak - 1:+.2%}")
c4.metric("Mode", eq["mode"].iloc[-1])
st.line_chart(eq.set_index("ts")["equity"])

st.subheader("Orders")
st.dataframe(q("SELECT ts, ticker, side, qty, price, stop, target, reason, dry_run FROM orders ORDER BY ts DESC"),
             use_container_width=True)

st.subheader("Latest signals")
sig = q("SELECT ts, ticker, score, follow, trend, acted, reasons FROM signals ORDER BY ts DESC LIMIT 50")
sig["reasons"] = sig["reasons"].map(lambda r: " | ".join(json.loads(r)))
st.dataframe(sig, use_container_width=True)

st.subheader("Warnings")
st.dataframe(q("SELECT * FROM events ORDER BY ts DESC LIMIT 50"), use_container_width=True)
