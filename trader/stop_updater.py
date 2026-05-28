"""
trader/stop_updater.py — morning trailing stop management.

Runs at 9:40 AM ET (before new orders at 9:45 AM).

For each open position that has gained >= STOP_TRAIL_MIN_GAIN_PCT above entry:
  - Raises the stop-loss order to lock in STOP_TRAIL_LOCK_RATIO of the gain
  - If no active stop order exists (e.g. bracket expired), places a new GTC stop
Never lowers a stop.
"""
import logging

import config
from trader._utils import log_api_error

log = logging.getLogger("trader.stops")


def _get_client():
    from alpaca.trading.client import TradingClient
    paper = "paper-api" in config.ALPACA_BASE_URL
    return TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=paper)


def _find_stop_order(client, symbol: str):
    """Return the active GTC stop-sell order for symbol, or None."""
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus, OrderType, OrderSide
    try:
        orders = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[symbol],
        ))
        for o in orders:
            if getattr(o, "type", None) == OrderType.STOP and getattr(o, "side", None) == OrderSide.SELL:
                return o
    except Exception as exc:
        log.warning("[stops] Could not fetch orders for %s: %s", symbol, exc)
    return None


def update_trailing_stops() -> list[dict]:
    """
    Raise stop-loss orders for profitable positions. Returns list of update summaries.
    """
    from alpaca.trading.requests import ReplaceOrderRequest, StopOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    from data.alpaca_client import fetch_latest_asks
    from trader.order_placer import load_entry_for_symbol, update_stop_in_record

    client = _get_client()

    try:
        positions = client.get_all_positions()
    except Exception as exc:
        log_api_error(log, "[stops] Failed to fetch open positions", exc)
        return []

    if not positions:
        log.info("[stops] No open positions")
        return []

    symbols = [p.symbol for p in positions]
    asks = fetch_latest_asks(symbols)
    updated = []

    for pos in positions:
        symbol = pos.symbol
        entry = load_entry_for_symbol(symbol)
        if not entry:
            log.debug("[stops] %s — no entry record, skipping", symbol)
            continue

        entry_price  = float(entry["entry_price"])
        current_stop = float(entry.get("stop_price", 0))
        current_price = asks.get(symbol) or float(getattr(pos, "current_price", 0) or 0)

        if current_price <= 0 or entry_price <= 0:
            continue

        gain_pct = (current_price - entry_price) / entry_price

        # Calculate the stop that locks in LOCK_RATIO of the gain above entry
        candidate_stop = round(
            entry_price + (current_price - entry_price) * config.STOP_TRAIL_LOCK_RATIO, 2
        )
        # Broker requires stop <= current_price - 0.01
        candidate_stop = min(candidate_stop, round(current_price - 0.01, 2))
        candidate_stop = max(candidate_stop, 0.01)

        stop_order = _find_stop_order(client, symbol)
        broker_stop = float(getattr(stop_order, "stop_price", None) or 0)

        # Use the higher of recorded and broker stop as the current floor
        effective_current_stop = max(current_stop, broker_stop)

        if gain_pct < config.STOP_TRAIL_MIN_GAIN_PCT:
            log.debug("[stops] %s — gain %.1f%% below threshold %.1f%%, no update",
                      symbol, gain_pct * 100, config.STOP_TRAIL_MIN_GAIN_PCT * 100)
            # Still ensure a stop exists if none is active
            if not stop_order:
                _place_new_stop(client, symbol, pos, current_stop or candidate_stop, current_price)
            continue

        if candidate_stop <= effective_current_stop:
            log.debug("[stops] %s — candidate stop $%.2f not above current $%.2f, no update",
                      symbol, candidate_stop, effective_current_stop)
            if not stop_order:
                _place_new_stop(client, symbol, pos, effective_current_stop, current_price)
            continue

        # Update or place the stop
        try:
            if stop_order:
                client.replace_order(
                    order_id=stop_order.id,
                    order_data=ReplaceOrderRequest(stop_price=candidate_stop),
                )
                action = "raised"
            else:
                _place_new_stop(client, symbol, pos, candidate_stop, current_price)
                action = "placed"

            update_stop_in_record(symbol, candidate_stop)
            log.info("[stops] ✅ %s stop %s $%.2f → $%.2f (gain +%.1f%%)",
                     symbol, action, effective_current_stop, candidate_stop, gain_pct * 100)
            updated.append({
                "symbol":    symbol,
                "action":    action,
                "old_stop":  effective_current_stop,
                "new_stop":  candidate_stop,
                "gain_pct":  round(gain_pct * 100, 1),
            })
        except Exception as exc:
            log_api_error(log, f"[stops] ❌ Failed to update stop for {symbol}", exc)

    log.info("[stops] Done — %d/%d stop(s) updated", len(updated), len(positions))
    return updated


def _place_new_stop(client, symbol: str, pos, stop_price: float, current_price: float) -> None:
    """Place a standalone GTC stop-sell order for a position with no active stop."""
    from alpaca.trading.requests import StopOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    if stop_price >= current_price:
        log.warning(
            "[stops] %s — recorded stop $%.2f is >= current price $%.2f "
            "(position already below intended stop, no stop placed)",
            symbol, stop_price, current_price,
        )
        return

    qty = int(float(getattr(pos, "qty", 1)))
    client.submit_order(StopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        stop_price=stop_price,
    ))
    log.info("[stops] ✅ %s — new GTC stop placed at $%.2f (no prior stop found)", symbol, stop_price)


def send_stop_summary(updated: list[dict]) -> None:
    from notifier.telegram import _send
    if not updated:
        _send("🛡 *Stop Update* — All stops current, no changes needed.")
        return
    lines = [f"🛡 *Stop Update — {len(updated)} stop(s) raised*\n"]
    for u in updated:
        lines.append(
            f"• *{u['symbol']}* stop `${u['old_stop']:.2f}` → `${u['new_stop']:.2f}` "
            f"(+{u['gain_pct']}% gain)"
        )
    _send("\n".join(lines))
