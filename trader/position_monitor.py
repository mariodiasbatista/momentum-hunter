"""
trader/position_monitor.py — 3:30 PM ET exit monitor.

Checks every open Alpaca position against today's signals.
Closes a position (market sell) when ANY of:
  - exit_mode == "fixed_take_profit"  → momentum weakening, RSI approaching 70
  - warning_count >= 2                → multiple bearish signals detected
  - RSI > 70                          → overbought, risk of reversal

Positions not found in today's signals are left untouched (logged as warning).
"""
import logging

import config
from trader._utils import close_position_with_retry, log_api_error

log = logging.getLogger("trader.monitor")


def _get_client():
    from alpaca.trading.client import TradingClient
    paper = "paper-api" in config.ALPACA_BASE_URL
    return TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=paper)


def check_and_exit(signals: dict) -> list[dict]:
    # Final sweep for any TP/SL fills not yet recorded
    from trader.trade_recorder import scan_for_fills, send_fill_notifications
    fills = scan_for_fills()
    if fills:
        send_fill_notifications(fills)
        log.info("[monitor] %d auto-fill(s) swept and notified at EOD", len(fills))


    """
    signals: {symbol: signal_dict} — today's pre-computed signals keyed by symbol.
    Returns list of closed position summaries.
    """
    client = _get_client()

    try:
        positions = client.get_all_positions()
    except Exception as exc:
        log_api_error(log, "[monitor] Failed to fetch open positions", exc)
        return []

    log.info("[monitor] Checking %d open position(s) against today's signals", len(positions))
    closed = []

    for pos in positions:
        symbol = pos.symbol
        signal = signals.get(symbol)

        if signal is None:
            log.warning("[monitor] %s — no signal data today, skipping", symbol)
            continue

        exit_mode     = signal["exit"]["exit_mode"]
        warning_count = signal["exit"]["warning_count"]
        warnings      = signal["exit"]["warnings"]
        rsi           = signal["momentum"]["rsi"]
        plpc          = float(pos.unrealized_plpc or 0) * 100

        log.debug("[monitor] %s | exit_mode=%s | warnings=%d | RSI=%.1f | P&L=%.1f%%",
                  symbol, exit_mode, warning_count, rsi, plpc)

        reasons = []
        if plpc < -config.MAX_LOSS_PCT:
            reasons.append(f"loss {abs(plpc):.1f}% exceeds max {config.MAX_LOSS_PCT:.0f}%")
        if plpc >= config.MIN_GAIN_TAKE_PCT:
            reasons.append(f"gain {plpc:.1f}% at EOD — locking in profit")
        if exit_mode == "fixed_take_profit":
            reasons.append(f"exit_mode=fixed_take_profit")
        if warning_count >= 2:
            reasons.append(f"{warning_count} warnings: {', '.join(warnings)}")
        if rsi > config.RSI_OVERBOUGHT:
            reasons.append(f"RSI={rsi:.1f} (overbought)")

        if not reasons:
            log.debug("[monitor] %s — holding, no exit trigger", symbol)
            continue

        reason_str = " | ".join(reasons)
        log.info("[monitor] Closing %s — %s", symbol, reason_str)

        try:
            close_position_with_retry(client, symbol, log)
            log.info("[monitor] ✅ Closed %s | P&L: %s", symbol, pos.unrealized_pl)
            from trader.trade_recorder import record_manual_close
            record_manual_close(pos, " | ".join(reasons))
            closed.append({
                "symbol":     symbol,
                "reasons":    reasons,
                "rsi":        rsi,
                "exit_mode":  exit_mode,
                "unrealized_pl": str(pos.unrealized_pl),
            })
        except Exception as exc:
            log_api_error(log, f"[monitor] ❌ Failed to close {symbol}", exc)

    log.info("[monitor] Done — %d/%d positions closed", len(closed), len(positions))
    return closed


def send_monitor_summary(closed: list[dict], total_positions: int) -> None:
    from notifier.telegram import _send
    if not closed:
        _send(f"🔍 *Exit Monitor* — {total_positions} position(s) checked, none closed.")
        return

    lines = [f"🚪 *Exit Monitor — {len(closed)} position(s) closed*\n"]
    for c in closed:
        pl = c["unrealized_pl"]
        pl_sign = "+" if not pl.startswith("-") else ""
        lines.append(
            f"• *{c['symbol']}* closed | P&L: `{pl_sign}{pl}`\n"
            f"  _{' | '.join(c['reasons'])}_"
        )
    _send("\n".join(lines))
