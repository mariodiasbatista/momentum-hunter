"""Shared utilities for trader modules."""
import logging
import time

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


def equity_positions(client) -> list:
    """Open positions excluding crypto.

    Crypto is traded by `crypto/` under its own entry, exit and sizing rules, and
    holds its own capital allocation. Every equity job must therefore ignore it —
    otherwise the equity exit monitors would close crypto positions on equity
    rules, and crypto holdings would consume equity position slots.
    """
    from alpaca.trading.enums import AssetClass
    return [p for p in client.get_all_positions() if p.asset_class != AssetClass.CRYPTO]


def cancel_open_orders(client, symbol: str, log=None) -> int:
    """Cancel all open orders for symbol so shares are free to close.

    Bracket orders lock all shares in TP/SL legs — close_position() will fail
    with 'insufficient qty' unless those legs are cancelled first.
    Returns the number of orders cancelled.

    Only the *active* leg of a bracket is returned here: the OCO sibling sits in
    'held' status, which QueryOrderStatus.OPEN does not include. Cancelling the
    visible leg makes Alpaca cancel the sibling too, but asynchronously — so the
    count returned is not the number of orders that will actually release, and
    callers must wait on qty_available rather than on this returning.
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


_RELEASE_TIMEOUT = 10.0
_RELEASE_POLL = 0.5


def _held_qty(client, symbol: str):
    """Shares the broker still reserves for open orders, or None if unreadable."""
    try:
        return float(client.get_open_position(symbol).qty_available or 0)
    except Exception:
        return None


def _await_qty_release(client, symbol: str, log=None, timeout: float = _RELEASE_TIMEOUT) -> bool:
    """Block while the broker still reserves the position's shares.

    Cancelling one bracket leg triggers an asynchronous cancel of its OCO sibling,
    and the sibling keeps the shares in held_for_orders until it lands — measured
    at ~2s on the paper account. Only a reading of exactly zero available is worth
    waiting on; if the position cannot be read at all there is nothing to wait for,
    so let close_position report the authoritative error instead of stalling here.
    """
    _l = log or _log
    deadline = time.monotonic() + timeout
    while True:
        available = _held_qty(client, symbol)
        if available is None or available > 0:
            return True
        if time.monotonic() >= deadline:
            _l.warning("[utils] %s — shares still held %.0fs after cancel", symbol, timeout)
            return False
        time.sleep(_RELEASE_POLL)


def restore_stop(client, symbol: str, log=None) -> bool:
    """Re-place a standalone GTC stop after a close attempt cancelled the brackets.

    A failed close is not a no-op: cancel_open_orders has already torn down both
    protective legs, so returning without this leaves the position naked. Uses the
    stop recorded at entry (kept current by the trailing-stop job).
    """
    from alpaca.trading.requests import StopOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    from trader.order_placer import load_entry_for_symbol

    _l = log or _log
    stop_price = float((load_entry_for_symbol(symbol) or {}).get("stop_price") or 0)
    if not stop_price:
        _l.error("[utils] %s — close failed and no recorded stop to restore, position is naked", symbol)
        return False
    try:
        pos = client.get_open_position(symbol)
        if stop_price >= float(pos.current_price or 0):
            _l.error("[utils] %s — close failed and recorded stop $%.2f is at or above "
                     "market, position is naked", symbol, stop_price)
            return False
        client.submit_order(StopOrderRequest(
            symbol=symbol,
            qty=int(float(pos.qty_available or pos.qty)),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=stop_price,
        ))
    except Exception as exc:
        _l.error("[utils] %s — could not restore stop after failed close: %s", symbol, exc)
        return False
    _l.warning("[utils] %s — close failed, restored GTC stop at $%.2f", symbol, stop_price)
    return True


def close_position_with_retry(client, symbol: str, log=None) -> None:
    """Cancel the protective orders, wait for the shares to release, then close.

    Raises if the position could not be closed. A protective stop is restored
    first, so the caller never turns a protected position into a naked one.
    """
    _l = log or _log
    cancel_open_orders(client, symbol, _l)
    _await_qty_release(client, symbol, _l)
    try:
        client.close_position(symbol)
    except Exception:
        restore_stop(client, symbol, _l)
        raise
