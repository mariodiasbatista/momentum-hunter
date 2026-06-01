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


def _recorded_today() -> set:
    today = date.today().isoformat()
    return {t["symbol"] for t in _load_trades().get(today, [])}


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

def record_manual_close(pos, reason: str) -> dict:
    """Record a close executed directly by a monitor (not via broker TP/SL).

    Uses the position object's current data as the exit snapshot. Skips if
    this symbol was already recorded today (avoids double-counting).
    Returns the trade dict, or {} if skipped.
    """
    symbol = pos.symbol
    if symbol in _recorded_today():
        log.debug("[recorder] %s already recorded today, skipping", symbol)
        return {}

    qty         = int(float(pos.qty or 1))
    entry_price = float(pos.avg_entry_price or 0)
    exit_price  = float(pos.current_price or 0)
    pnl         = float(pos.unrealized_pl or 0)
    pnl_pct     = float(pos.unrealized_plpc or 0) * 100

    trade = {
        "symbol":      symbol,
        "entry_price": entry_price,
        "exit_price":  exit_price,
        "qty":         qty,
        "reason":      reason,
        "pnl":         round(pnl, 2),
        "pnl_pct":     round(pnl_pct, 2),
        "exited_at":   datetime.now(timezone.utc).isoformat(),
        "notified":    True,
    }
    _save_trade(trade)
    log.info("[recorder] Recorded close: %s | %s | P&L $%.2f (%.1f%%)",
             symbol, reason, pnl, pnl_pct)
    return trade


def scan_for_fills() -> list[dict]:
    """
    Fetch today's closed SELL orders from Alpaca (broker-initiated TP/SL fills).
    Looks up entry price across all order dates, not just today's buys.
    Deduplicates — symbols already recorded today are skipped.
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus, OrderSide
    from trader.order_placer import load_entry_for_symbol

    already_recorded = _recorded_today()

    client = _get_client()
    try:
        today_start = datetime.combine(date.today(), time.min, tzinfo=timezone.utc)
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            side=OrderSide.SELL,
            after=today_start,
            limit=100,
            nested=False,  # flatten bracket child orders so stop/TP legs are visible
        )
        closed_sells = client.get_orders(filter=req)
    except Exception as exc:
        log_api_error(log, "[recorder] Failed to fetch closed orders", exc)
        return []

    log.debug("[recorder] Alpaca returned %d closed sell order(s) today", len(closed_sells))

    new_fills = []
    for order in closed_sells:
        symbol = order.symbol

        if symbol in already_recorded:
            log.debug("[recorder] %s — already recorded today, skipping", symbol)
            continue

        if str(order.status) != "filled" or not order.filled_avg_price:
            log.debug("[recorder] %s — status=%s, not filled, skipping", symbol, order.status)
            continue

        order_details = load_entry_for_symbol(symbol) or {}
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
