"""Each follow-signal returns {ticker: {"score": 0..1, "reason": str}}.

A source that fails (network down, missing key) returns {} and logs a warning,
so one broken feed never stops the bot or causes a bad trade.
"""
import logging

log = logging.getLogger("signals")


def safe(name, fn, *args, **kwargs) -> dict:
    try:
        out = fn(*args, **kwargs) or {}
        log.info("%s: %d tickers flagged", name, len(out))
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("%s source failed, skipping: %s", name, e)
        return {}
