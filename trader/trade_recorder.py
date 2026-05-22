"""
trader/trade_recorder.py — detect and record Alpaca auto-closes (TP and SL fills).

Called at each intraday monitor cycle and at EOD monitor.
Scans today's closed SELL orders from Alpaca, matches against our placed orders,
computes P&L, saves to trades.json, and sends Telegram notification per fill.

trades.json format:
{
  "2026-05-22": [
    {
      "symbol": "AAPL",
      "entry_price": 150.0,
      "exit_price": 153.0,
      "qty": 1,
      "reason": "take_profit",      # "take_profit" | "stop_loss"
      "pnl": 3.0,
      "pnl_pct": 2.0,
      "exited_at": "2026-05-22T11:30:00+00:00",
      "notified": true
    }
  ]
}
"""
import json
import logging
from datetime import date, datetime, time, timezone
from pathlib import Path

import config
from trader._utils import log_api_error

log = logging.getLogger("trader.recorder")

_TRADES_FILE = Path(__file__).parent.parent / "data" / "trades.json"


# ── Internal helpers ─────────────────────────────────────────────────────────

def _get_client():
    from alpaca.trading.client import TradingClient
    paper = "paper-api" in config.ALPACA_BASE_URL
    return TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=paper)


def _load_trades() -> dict:
    try:
        return json.loads(_TRADES_FILE.read_text()) if _TRADES_FILE.exists() else {}
    except Exception:
        return {}


def _notified_today() -> set:
    today = date.today().isoformat()
    return {t["symbol"] for t in _load_trades().get(today, []) if t.get("notified")}


def _save_trade(trade: dict) -> None:
    today = date.today().isoformat()
    try:
        trades = _load_trades()
        trades.setdefault(today, [])
        trades[today].append(trade)
        _TRADES_FILE.write_text(json.dumps(trades, indent=2))
    except Exception as exc:
        log.error("[recorder] Failed to save trade record: %s", exc)


# ── Main scan ────────────────────────────────────────────────────────────────

def scan_for_fills() -> list[dict]:
    """
    Fetch today's closed SELL orders from Alpaca.
    Match against our placed orders, compute P&L, save and return new fills.
    Deduplicates — symbols already notified today are skipped.
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus, OrderSide
    from trader.order_placer import load_orders_today

    our_orders = load_orders_today()
    if not our_orders:
        log.debug("[recorder] No orders placed today — skipping fill scan")
        return []

    already_notified = _notified_today()
    pending = set(our_orders.keys()) - already_notified
    if not pending:
        log.debug("[recorder] All orders already notified — skipping fill scan")
        return []

    log.debug("[recorder] Scanning fills for: %s", ", ".join(sorted(pending)))

    client = _get_client()
    try:
        today_start = datetime.combine(date.today(), time.min, tzinfo=timezone.utc)
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            side=OrderSide.SELL,
            after=today_start,
            limit=100,
        )
        closed_sells = client.get_orders(filter=req)
    except Exception as exc:
        log_api_error(log, "[recorder] Failed to fetch closed orders", exc)
        return []

    log.debug("[recorder] Alpaca returned %d closed sell order(s) today", len(closed_sells))

    new_fills = []
    for order in closed_sells:
        symbol = order.symbol

        if symbol not in pending:
            log.debug("[recorder] %s — not in our pending orders, skipping", symbol)
            continue

        if str(order.status) != "filled" or not order.filled_avg_price:
            log.debug("[recorder] %s — status=%s, not filled, skipping", symbol, order.status)
            continue

        order_details = our_orders.get(symbol, {})
        entry_price  = float(order_details.get("entry_price", 0.0))
        qty          = int(float(order.qty or order_details.get("qty", 1)))
        exit_price   = float(order.filled_avg_price)
        order_type   = str(order.order_type).lower()

        # Distinguish TP (limit sell) from SL (stop sell)
        if "limit" in order_type:
            reason = "take_profit"
        elif "stop" in order_type:
            reason = "stop_loss"
        else:
            reason = "unknown"

        pnl     = round((exit_price - entry_price) * qty, 2) if entry_price else None
        pnl_pct = round((exit_price - entry_price) / entry_price * 100, 2) if entry_price else None

        log.info("[recorder] %s FILL: %s | entry $%.2f → exit $%.2f | qty %d | P&L %s$%.2f (%.1f%%)",
                 symbol, reason.upper(), entry_price, exit_price, qty,
                 "+" if (pnl or 0) >= 0 else "",
                 abs(pnl or 0), abs(pnl_pct or 0))

        trade = {
            "symbol":      symbol,
            "entry_price": entry_price,
            "exit_price":  exit_price,
            "qty":         qty,
            "reason":      reason,
            "pnl":         pnl,
            "pnl_pct":     pnl_pct,
            "exited_at":   str(order.filled_at),
            "notified":    True,
        }
        _save_trade(trade)
        new_fills.append(trade)

    if not new_fills:
        log.debug("[recorder] No new fills detected this cycle")

    return new_fills


# ── Telegram notifications ───────────────────────────────────────────────────

def send_fill_notifications(fills: list[dict]) -> None:
    from notifier.telegram import _send
    for fill in fills:
        reason    = fill["reason"]
        symbol    = fill["symbol"]
        entry     = fill["entry_price"]
        exit_p    = fill["exit_price"]
        pnl       = fill["pnl"]
        pnl_pct   = fill["pnl_pct"]

        if reason == "take_profit":
            icon, label = "🎯", "Take Profit Hit"
        elif reason == "stop_loss":
            icon, label = "🛡️", "Stop Loss Hit"
        else:
            icon, label = "📋", "Position Closed"

        profit = (pnl or 0) >= 0
        result_icon = "✅" if profit else "🔴"

        if pnl is not None and pnl_pct is not None:
            sign   = "+" if profit else "-"
            pnl_str = f"{sign}${abs(pnl):.2f} ({sign}{abs(pnl_pct):.1f}%)"
        else:
            pnl_str = "unknown (no entry price recorded)"

        _send(
            f"{icon} *{label} — {symbol}*\n"
            f"Entry: `${entry:.2f}` → Exit: `${exit_p:.2f}` | qty `{fill['qty']}`\n"
            f"{result_icon} P&L: `{pnl_str}`"
        )
        log.info("[recorder] Telegram notified: %s %s P&L=%s", symbol, reason, pnl_str)
