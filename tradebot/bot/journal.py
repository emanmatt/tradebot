"""SQLite trade journal: every run, signal, order and equity snapshot."""
from datetime import datetime, timezone
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS equity (ts TEXT, equity REAL, cash REAL, exposure REAL, mode TEXT);
CREATE TABLE IF NOT EXISTS signals (ts TEXT, ticker TEXT, score REAL, follow REAL, trend REAL,
    acted INTEGER, reasons TEXT);
CREATE TABLE IF NOT EXISTS orders (ts TEXT, ticker TEXT, side TEXT, qty REAL, price REAL,
    stop REAL, target REAL, reason TEXT, order_id TEXT, dry_run INTEGER);
CREATE TABLE IF NOT EXISTS entries (ticker TEXT PRIMARY KEY, opened TEXT);
CREATE TABLE IF NOT EXISTS events (ts TEXT, level TEXT, msg TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def equity(self, equity, cash, exposure, mode):
        self.db.execute("INSERT INTO equity VALUES (?,?,?,?,?)", (now(), equity, cash, exposure, mode))
        self.db.commit()

    def peak_equity(self) -> float:
        row = self.db.execute("SELECT MAX(equity) FROM equity").fetchone()
        return row[0] or 0.0

    def signal(self, p: dict, acted: bool):
        self.db.execute("INSERT INTO signals VALUES (?,?,?,?,?,?,?)",
                        (now(), p["ticker"], p["score"], p["follow"], p["trend"], int(acted),
                         json.dumps(p["reasons"])))
        self.db.commit()

    def order(self, ticker, side, qty, price, stop, target, reason, order_id, dry):
        self.db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (now(), ticker, side, qty, price, stop, target, reason, order_id, int(dry)))
        if side == "buy" and not dry:
            self.db.execute("INSERT OR REPLACE INTO entries VALUES (?,?)", (ticker, now()))
        if side == "sell" and not dry:
            self.db.execute("DELETE FROM entries WHERE ticker=?", (ticker,))
        self.db.commit()

    def opened(self, ticker) -> str | None:
        row = self.db.execute("SELECT opened FROM entries WHERE ticker=?", (ticker,)).fetchone()
        return row[0] if row else None

    def event(self, level, msg):
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (now(), level, msg))
        self.db.commit()
