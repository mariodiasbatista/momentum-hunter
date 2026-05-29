"""Shared utilities for trader modules."""
import logging

_log = logging.getLogger("trader.utils")

_TRANSIENT_KEYWORDS = (
    "connection refused", "connection reset", "connection error",
    "timeout", "timed out", "network", "temporary",
    "service unavailable", "502", "503", "429",
)


def is_transient(exc: Exception) -> bool:
    """True for network/connection errors that may resolve on the next cycle."""
    msg = str(exc).lower()
    return any(k in msg for k in _TRANSIENT_KEYWORDS)


def log_api_error(log, context: str, exc: Exception) -> None:
    """Log transient errors as WARNING, real failures as ERROR."""
    if is_transient(exc):
        log.warning("%s: %s — transient, will retry next cycle", context, exc)
    else:
        log.error("%s: %s", context, exc)


def cancel_open_orders(client, symbol: str, log=None) -> int:
    """Cancel all open orders for symbol so shares are free to close.

    Bracket orders lock all shares in TP/SL legs — close_position() will fail
    with 'insufficient qty' unless those legs are cancelled first.
    Returns the number of orders cancelled.
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    _l = log or _log
    try:
        orders = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[symbol],
        ))
        for o in orders:
            try:
                client.cancel_order_by_id(str(o.id))
            except Exception as exc:
                _l.warning("[utils] %s — could not cancel order %s: %s", symbol, o.id, exc)
        if orders:
            _l.info("[utils] %s — cancelled %d open order(s) before close", symbol, len(orders))
        return len(orders)
    except Exception as exc:
        _l.warning("[utils] %s — failed to fetch orders before close: %s", symbol, exc)
        return 0
