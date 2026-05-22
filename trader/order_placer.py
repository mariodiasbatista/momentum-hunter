"""
trader/order_placer.py — position sizing and Alpaca bracket order execution.

Buy rules (Execution v1 — 2026-05-22):
  - price > $50  → 1 position ($250)
  - price < $50  → 3 positions ($750)
  - Entry: market order at 9:45 AM ET (after first 15-min candle confirms direction)
  - Stop loss: price − ATR×1.5 (tight end), capped at 1% below entry
  - Take profit:
      trailing_stop mode  → price + ATR×6  (wide safety net — let the winner run)
      fixed_take_profit   → price + ATR×3  (defined target — take profit and exit)
  - Priority: RS% > ADX > Volume ratio
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import config

log = logging.getLogger("trader.orders")

_ORDERS_FILE = Path(__file__).parent.parent / "data" / "orders_placed.json"


# ── Position sizing ──────────────────────────────────────────────────────────

def position_qty(price: float) -> int:
    multiplier = config.POSITION_MULTIPLIER if price < config.POSITION_PRICE_THRESHOLD else 1
    dollars = multiplier * config.POSITION_SIZE_DOLLARS
    return max(1, int(dollars / price))


def position_label(price: float) -> str:
    if price < config.POSITION_PRICE_THRESHOLD:
        return f"3 pos · ${config.POSITION_SIZE_DOLLARS * config.POSITION_MULTIPLIER}"
    return f"1 pos · ${config.POSITION_SIZE_DOLLARS}"


# ── Alpaca client ────────────────────────────────────────────────────────────

def _get_client():
    from alpaca.trading.client import TradingClient
    paper = "paper-api" in config.ALPACA_BASE_URL
    return TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=paper)


# ── Order dedup + entry detail tracking ─────────────────────────────────────
# orders_placed.json format:
# { "2026-05-22": { "AAPL": {qty, entry_price, stop_price, take_price, exit_mode} } }

def _orders_today() -> set:
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        data = json.loads(_ORDERS_FILE.read_text()) if _ORDERS_FILE.exists() else {}
        day = data.get(today, {})
        return set(day.keys()) if isinstance(day, dict) else set(day)
    except Exception:
        return set()


def load_orders_today() -> dict:
    """Return {symbol: {qty, entry_price, stop_price, take_price, exit_mode}} for today."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        data = json.loads(_ORDERS_FILE.read_text()) if _ORDERS_FILE.exists() else {}
        day = data.get(today, {})
        if isinstance(day, list):
            return {s: {} for s in day}   # migrate old format
        return day
    except Exception:
        return {}


def _record_order(symbol: str, qty: int, entry_price: float,
                  stop_price: float, take_price: float, exit_mode: str) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        data = json.loads(_ORDERS_FILE.read_text()) if _ORDERS_FILE.exists() else {}
        if isinstance(data.get(today), list):
            data[today] = {s: {} for s in data[today]}   # migrate old format
        data.setdefault(today, {})
        data[today][symbol] = {
            "qty": qty,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "take_price": take_price,
            "exit_mode": exit_mode,
        }
        _ORDERS_FILE.write_text(json.dumps(data))
    except Exception:
        pass


# ── Order placement ──────────────────────────────────────────────────────────

def place_orders(candidates: list[dict]) -> list[dict]:
    """
    Place bracket market orders for top candidates.
    Each order has:
      - market buy entry
      - stop loss  : price − ATR×1.5
      - take profit: price + ATR×6 (trailing_stop mode) or price + ATR×3 (fixed mode)
    Returns list of placed order summaries.
    """
    from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

    client = _get_client()
    already_ordered = _orders_today()
    placed = []

    # Respect pre-market filter if validator ran this morning
    from trader.premarket_validator import load_approved_today
    approved = load_approved_today()
    if approved is not None:
        log.info("[orders] Pre-market filter active: %d approved symbol(s)", len(approved))
    else:
        log.info("[orders] No pre-market filter — using full candidate list")

    log.info("[orders] Starting — %d candidates, %d already ordered today",
             len(candidates[:config.AUTO_ORDER_TOP_N]), len(already_ordered))

    for c in candidates[:config.AUTO_ORDER_TOP_N]:
        symbol = c["symbol"]

        if symbol in already_ordered:
            log.debug("[orders] Skip %s — already ordered today", symbol)
            continue

        if approved is not None and symbol not in approved:
            log.info("[orders] Skip %s — failed pre-market validation", symbol)
            continue

        price      = c["trend"]["last_close"]
        exit_mode  = c["exit"]["exit_mode"]
        atr_min    = c["exit"]["trailing_stop_atr_range"][0]
        atr_max    = c["exit"]["trailing_stop_atr_range"][1]
        qty        = position_qty(price)

        # Stop loss: ATR×1.5 below entry, never more than 1% below
        stop_price = round(min(price - atr_min, price * 0.99), 2)
        stop_price = max(stop_price, 0.01)

        # Take profit: wide net for trailing_stop mode, defined target for fixed mode
        if exit_mode == "trailing_stop":
            take_price = round(price + atr_max * 2, 2)   # ATR×6 — let the winner run
        else:
            take_price = round(price + atr_max, 2)        # ATR×3 — take profit and exit

        log.debug("[orders] %s | price $%.2f | qty %d | stop $%.2f | tp $%.2f | mode=%s",
                  symbol, price, qty, stop_price, take_price, exit_mode)

        try:
            order = client.submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=stop_price),
                take_profit=TakeProfitRequest(limit_price=take_price),
            ))
            _record_order(symbol, qty, price, stop_price, take_price, exit_mode)
            log.info("[orders] ✅ %s x%d | entry mkt | stop $%.2f | tp $%.2f | %s | mode=%s",
                     symbol, qty, stop_price, take_price, position_label(price), exit_mode)
            placed.append({
                "symbol":     symbol,
                "qty":        qty,
                "price":      price,
                "stop_price": stop_price,
                "take_price": take_price,
                "exit_mode":  exit_mode,
                "pos_label":  position_label(price),
                "order_id":   str(order.id),
            })
        except Exception as exc:
            log.error("[orders] ❌ Failed to place order for %s: %s", symbol, exc)

    log.info("[orders] Done — %d placed, %d skipped", len(placed),
             len(candidates[:config.AUTO_ORDER_TOP_N]) - len(placed))
    return placed


def send_order_summary(placed: list[dict]) -> None:
    from notifier.telegram import _send
    if not placed:
        _send("📤 *Auto-Order* — No new orders placed (already ordered or no candidates).")
        return

    lines = [f"📤 *Auto-Order — {len(placed)} order(s) placed*\n"]
    for o in placed:
        mode_icon = "🟢" if o["exit_mode"] == "trailing_stop" else "🔴"
        lines.append(
            f"• *{o['symbol']}* x{o['qty']} @ mkt\n"
            f"  stop `${o['stop_price']:.2f}` | tp `${o['take_price']:.2f}` | "
            f"{mode_icon} {o['exit_mode'].replace('_', ' ')} | {o['pos_label']}"
        )
    _send("\n".join(lines))
