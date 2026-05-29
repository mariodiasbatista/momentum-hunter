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
import math
from datetime import date, datetime, timezone
from pathlib import Path

import config
from trader._utils import log_api_error

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


def _open_positions_in_cooldown(open_symbols: set) -> set:
    """
    Return symbols that are:
      - currently held as an open position, AND
      - were bought within ORDER_COOLDOWN_DAYS calendar days (including today)

    Closed positions are never blocked — exited = free to re-enter.
    Positions older than ORDER_COOLDOWN_DAYS are not blocked either.
    Set ORDER_COOLDOWN_DAYS = 0 to disable.
    """
    if config.ORDER_COOLDOWN_DAYS <= 0 or not open_symbols:
        return set()
    today = date.today()
    blocked = set()
    try:
        data = json.loads(_ORDERS_FILE.read_text()) if _ORDERS_FILE.exists() else {}
        for date_str, day_data in data.items():
            days_ago = (today - date.fromisoformat(date_str)).days
            if 0 <= days_ago <= config.ORDER_COOLDOWN_DAYS:
                symbols = day_data.keys() if isinstance(day_data, dict) else day_data
                blocked.update(sym for sym in symbols if sym in open_symbols)
    except Exception:
        pass
    return blocked


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


def load_entry_for_symbol(symbol: str) -> dict | None:
    """Return the most recent order record for symbol across all dates, or None."""
    try:
        data = json.loads(_ORDERS_FILE.read_text()) if _ORDERS_FILE.exists() else {}
        for date_str in sorted(data.keys(), reverse=True):
            day = data[date_str]
            if isinstance(day, dict) and symbol in day:
                return day[symbol]
    except Exception:
        pass
    return None


def update_stop_in_record(symbol: str, new_stop: float) -> None:
    """Update the recorded stop_price for symbol in the most recent order entry."""
    try:
        data = json.loads(_ORDERS_FILE.read_text()) if _ORDERS_FILE.exists() else {}
        for date_str in sorted(data.keys(), reverse=True):
            day = data[date_str]
            if isinstance(day, dict) and symbol in day:
                day[symbol]["stop_price"] = new_stop
                _ORDERS_FILE.write_text(json.dumps(data))
                return
    except Exception:
        pass


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

    # Fetch open positions, then apply cooldown only to those within the window
    try:
        open_symbols = {p.symbol for p in client.get_all_positions()}
    except Exception as exc:
        log.warning("[orders] Could not fetch open positions: %s", exc)
        open_symbols = set()

    in_cooldown = _open_positions_in_cooldown(open_symbols)
    placed = []

    # Respect pre-market filter if validator ran this morning
    from trader.premarket_validator import load_approved_today
    approved = load_approved_today()
    if approved is not None:
        log.info("[orders] Pre-market filter active: %d approved symbol(s)", len(approved))
    else:
        log.info("[orders] No pre-market filter — using full candidate list")

    if in_cooldown:
        symbols_str = ", ".join(sorted(in_cooldown))
        log.info(
            "⏳ Cooldown (%dd) — skipping open position(s): %s",
            config.ORDER_COOLDOWN_DAYS, symbols_str
        )

    log.info("[orders] Starting — %d candidates, %d already ordered today",
             len(candidates[:config.AUTO_ORDER_TOP_N]), len(already_ordered))

    # Fetch current ask prices in one batch to anchor stop calculations to actual market price.
    # Falls back to last_close per symbol if the quote fetch fails.
    top_symbols = [c["symbol"] for c in candidates[:config.AUTO_ORDER_TOP_N]]
    from data.alpaca_client import fetch_latest_asks
    current_asks = fetch_latest_asks(top_symbols)
    if current_asks:
        log.debug("[orders] Fetched current asks for %d symbol(s)", len(current_asks))
    else:
        log.warning("[orders] Could not fetch current asks — using last_close for stop calculation")

    for c in candidates[:config.AUTO_ORDER_TOP_N]:
        symbol = c["symbol"]

        if symbol in already_ordered:
            log.debug("[orders] Skip %s — already ordered today", symbol)
            continue

        if approved is not None and symbol not in approved:
            log.info("[orders] Skip %s — failed pre-market validation", symbol)
            continue

        if symbol in in_cooldown:
            log.info("[orders] Skip %s — open position within %dd cooldown", symbol, config.ORDER_COOLDOWN_DAYS)
            continue

        last_close = c["trend"]["last_close"]
        market_price = current_asks.get(symbol, last_close)
        exit_mode  = c["exit"]["exit_mode"]
        atr_min    = c["exit"]["trailing_stop_atr_range"][0]
        atr_max    = c["exit"]["trailing_stop_atr_range"][1]
        qty        = position_qty(market_price)

        # Stop loss: ATR×1.5 below entry, never more than 1% below.
        # Floor (not round) the cap — round() can produce a value above market_price - 0.01
        # when the fractional cent rounds up (e.g. 0.4263 - 0.01 = 0.4163 → round gives 0.42).
        stop_cap = math.floor((market_price - 0.01) * 100) / 100
        stop_price = round(min(market_price - atr_min, market_price * 0.99), 2)
        stop_price = min(stop_price, stop_cap)
        stop_price = max(stop_price, 0.01)

        # Take profit: wide net for trailing_stop mode, defined target for fixed mode
        if exit_mode == "trailing_stop":
            take_price = round(market_price + atr_max * 2, 2)   # ATR×6 — let the winner run
        else:
            take_price = round(market_price + atr_max, 2)        # ATR×3 — take profit and exit

        log.debug("[orders] %s | ask $%.2f | last_close $%.2f | qty %d | stop $%.2f | tp $%.2f | mode=%s",
                  symbol, market_price, last_close, qty, stop_price, take_price, exit_mode)

        try:
            order = client.submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=stop_price),
                take_profit=TakeProfitRequest(limit_price=take_price),
            ))
            _record_order(symbol, qty, market_price, stop_price, take_price, exit_mode)
            log.info("[orders] ✅ %s x%d | entry mkt | stop $%.2f | tp $%.2f | %s | mode=%s",
                     symbol, qty, stop_price, take_price, position_label(market_price), exit_mode)
            placed.append({
                "symbol":     symbol,
                "qty":        qty,
                "price":      market_price,
                "stop_price": stop_price,
                "take_price": take_price,
                "exit_mode":  exit_mode,
                "pos_label":  position_label(market_price),
                "order_id":   str(order.id),
            })
        except Exception as exc:
            if "not found" in str(exc).lower():
                log.warning("[orders] %s — asset not available on Alpaca, skipping: %s", symbol, exc)
            else:
                log_api_error(log, f"[orders] ❌ Failed to place order for {symbol}", exc)

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
