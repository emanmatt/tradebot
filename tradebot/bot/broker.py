"""Thin wrapper around Alpaca. Paper trading unless config.is_live() is true."""
from datetime import datetime, timedelta, timezone
import logging

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetOrdersRequest, MarketOrderRequest, StopLossRequest,
                                     TakeProfitRequest)

from .config import env, is_live

log = logging.getLogger("broker")


class Broker:
    def __init__(self, cfg: dict):
        key, secret = env("ALPACA_API_KEY"), env("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
        self.live = is_live(cfg)
        self.trading = TradingClient(key, secret, paper=not self.live)
        self.data = StockHistoricalDataClient(key, secret)
        self.cfg = cfg

    # ---- account ----
    def account(self) -> dict:
        a = self.trading.get_account()
        if a.trading_blocked or a.account_blocked:
            raise RuntimeError("Alpaca reports this account is blocked from trading")
        return {"equity": float(a.equity), "last_equity": float(a.last_equity),
                "cash": float(a.cash), "buying_power": float(a.buying_power)}

    def positions(self) -> dict:
        return {p.symbol: {"qty": float(p.qty), "value": float(p.market_value),
                           "entry": float(p.avg_entry_price), "pl_pct": float(p.unrealized_plpc)}
                for p in self.trading.get_all_positions()}

    def market_open(self) -> bool:
        return bool(self.trading.get_clock().is_open)

    def tradable(self, symbol: str) -> bool:
        try:
            a = self.trading.get_asset(symbol)
            return bool(a.tradable) and a.status == "active"
        except Exception:  # noqa: BLE001
            return False

    # ---- data ----
    def bars(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        if not symbols:
            return {}
        start = datetime.now(timezone.utc) - timedelta(days=int(self.cfg["trend"]["lookback_days"] * 1.5))
        out = {}
        for i in range(0, len(symbols), 100):
            req = StockBarsRequest(symbol_or_symbols=symbols[i:i + 100], timeframe=TimeFrame.Day,
                                   start=start, feed=DataFeed.IEX)
            df = self.data.get_stock_bars(req).df
            if df.empty:
                continue
            for sym, g in df.groupby(level=0):
                out[sym] = g.droplevel(0).sort_index()
        return out

    def last_price(self, symbol: str) -> float:
        t = self.data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))
        return float(t[symbol].price)

    # ---- orders ----
    def buy_bracket(self, symbol: str, qty: int, stop: float, target: float) -> str:
        o = self.trading.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=target),
            stop_loss=StopLossRequest(stop_price=stop)))
        return str(o.id)

    def close(self, symbol: str) -> str:
        # Cancel the bracket's stop/target legs first, or the shares stay "held".
        for o in self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])):
            self.trading.cancel_order_by_id(o.id)
        o = self.trading.close_position(symbol)
        return str(getattr(o, "id", ""))
