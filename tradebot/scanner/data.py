"""Market data for the scanner, from Alpaca's free feed (IEX).

Kept in one small class so tests can swap in fake data and so a paid feed (SIP) is a one-line change.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from zoneinfo import ZoneInfo

from bot.config import env

log = logging.getLogger("scanner.data")
ET = ZoneInfo("America/New_York")


class AlpacaData:
    def __init__(self, feed: str = "iex"):
        from alpaca.data.enums import DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.historical.screener import ScreenerClient
        from alpaca.trading.client import TradingClient
        key, secret = env("ALPACA_API_KEY"), env("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY")
        self.feed = DataFeed.SIP if feed.lower() == "sip" else DataFeed.IEX
        self.stocks = StockHistoricalDataClient(key, secret)
        self.screener = ScreenerClient(key, secret)
        self.newsc = NewsClient(key, secret)
        self.trading = TradingClient(key, secret, paper=True)   # read-only use: asset list

    # ---- candidate lists ----
    def movers(self, top: int) -> dict[str, str]:
        """{symbol: why it was picked up} from Alpaca's most-active and top-gainer lists."""
        from alpaca.data.requests import MarketMoversRequest, MostActivesRequest
        out = {}
        try:
            for a in self.screener.get_most_actives(MostActivesRequest(top=min(top, 100), by="volume")).most_actives:
                out[a.symbol] = "most active"
        except Exception as e:  # noqa: BLE001
            log.warning("most-actives list failed: %s", e)
        try:
            for m in self.screener.get_market_movers(MarketMoversRequest(top=min(top, 50))).gainers:
                out.setdefault(m.symbol, "top gainer")
        except Exception as e:  # noqa: BLE001
            log.warning("top-gainers list failed: %s", e)
        return out

    def assets(self, exchanges: list[str]) -> dict[str, dict]:
        """Every tradable US stock: {symbol: {name, exchange, fractionable}}."""
        if getattr(self, "_assets", None) is not None:
            return self._assets
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest
        out = self._assets = {}
        for a in self.trading.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)):
            ex = str(getattr(a.exchange, "value", a.exchange)).upper()
            if a.tradable and ex in exchanges:
                out[a.symbol] = {"name": a.name, "exchange": ex, "fractionable": bool(a.fractionable)}
        return out

    # ---- prices and volume ----
    def snapshots(self, syms: list[str]) -> dict[str, dict]:
        """{symbol: {price, prev_close, today_volume, ts}} with yesterday's close worked out correctly
        before the open (when Alpaca's 'daily bar' is still yesterday's)."""
        from alpaca.data.requests import StockSnapshotRequest
        out, today = {}, datetime.now(ET).date()
        for i in range(0, len(syms), 200):
            try:
                snap = self.stocks.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=syms[i:i + 200], feed=self.feed))
            except Exception as e:  # noqa: BLE001
                log.warning("snapshots failed for a batch: %s", e)
                continue
            for s, x in snap.items():
                try:
                    price = float(x.latest_trade.price)
                    d, p = x.daily_bar, x.previous_daily_bar
                    if d is not None and d.timestamp.astimezone(ET).date() == today:
                        prev = float(p.close) if p is not None else None
                    else:
                        prev = float(d.close) if d is not None else None
                    out[s] = {"price": price, "prev_close": prev, "ts": x.latest_trade.timestamp}
                except Exception:  # noqa: BLE001
                    continue
        return out

    def avg_daily_volume(self, syms: list[str], days: int) -> dict[str, float]:
        """Average shares/day over the last `days` completed sessions (same feed as today's volume)."""
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        out = {}
        today = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        for i in range(0, len(syms), 100):
            try:
                df = self.stocks.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=syms[i:i + 100], timeframe=TimeFrame.Day, feed=self.feed,
                    start=today - timedelta(days=int(days * 1.6) + 5), end=today)).df
            except Exception as e:  # noqa: BLE001
                log.warning("daily bars failed for a batch: %s", e)
                continue
            if df.empty:
                continue
            for s, g in df.groupby(level=0):
                v = g["volume"].tail(days)
                if len(v) >= max(5, days // 2):
                    out[s] = float(v.mean())
        return out

    def volume_today(self, syms: list[str]) -> dict[str, float]:
        """Shares traded today so far, pre-market included (from 4:00 AM ET)."""
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        start = datetime.now(ET).replace(hour=4, minute=0, second=0, microsecond=0)
        out = {}
        for i in range(0, len(syms), 100):
            try:
                df = self.stocks.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=syms[i:i + 100], timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                    feed=self.feed, start=start)).df
            except Exception as e:  # noqa: BLE001
                log.warning("intraday bars failed for a batch: %s", e)
                continue
            if not df.empty:
                out.update({s: float(g["volume"].sum()) for s, g in df.groupby(level=0)})
        return out

    def news(self, syms: list[str], hours: float) -> dict[str, list[dict]]:
        """{symbol: [{headline, source, ts}]} newest first."""
        from alpaca.data.requests import NewsRequest
        out: dict[str, list[dict]] = {}
        start = datetime.now(timezone.utc) - timedelta(hours=hours)
        for i in range(0, len(syms), 50):
            try:
                res = self.newsc.get_news(NewsRequest(symbols=",".join(syms[i:i + 50]), start=start, limit=50,
                                                      exclude_contentless=False))
                items = res.data.get("news", []) if isinstance(res.data, dict) else []
            except Exception as e:  # noqa: BLE001
                log.warning("news failed for a batch: %s", e)
                continue
            for n in items:
                for s in n.symbols or []:
                    if s in syms:
                        out.setdefault(s, []).append({"headline": n.headline, "source": n.source,
                                                      "ts": n.created_at})
        for v in out.values():
            v.sort(key=lambda n: n["ts"], reverse=True)
        return out
