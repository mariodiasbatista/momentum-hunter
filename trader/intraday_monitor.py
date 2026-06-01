"""
trader/intraday_monitor.py — intraday RSI monitor on 15-minute bars.

Runs every 30 minutes during market hours (10:15 AM – 3:00 PM ET).
Fetches live 15-min bars for all open positions, recomputes RSI(14).
Closes a position if intraday RSI > 70 (overbought on the 15-min chart).

Why 15-min bars:
  - Daily RSI is computed from yesterday's close — it's stale intraday.
  - 15-min RSI reflects the actual intraday momentum and catches reversals hours
    before the 3:30 PM end-of-day monitor would.
"""
import logging

import pandas_ta as ta

import config
from trader._utils import cancel_open_orders, log_api_error

log = logging.getLogger("trader.intraday")

RSI_PERIOD = 14
RSI_OVERBOUGHT = config.RSI_OVERBOUGHT  # 70


def _get_trading_client():
    from alpaca.trading.client import TradingClient
    paper = "paper-api" in config.ALPACA_BASE_URL
    return TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=paper)


def run_intraday_check() -> list[dict]:
    """
    1. Scan for Alpaca auto-closes (TP/SL fills) and notify Telegram.
    2. Fetch open positions, compute 15-min RSI, close overbought positions.
    Returns list of positions closed by this monitor (RSI-based, not TP/SL).
    """
    from data.alpaca_client import fetch_intraday_bars
    from trader.trade_recorder import scan_for_fills, send_fill_notifications

    # Check for any TP/SL fills Alpaca handled automatically
    fills = scan_for_fills()
    if fills:
        send_fill_notifications(fills)
        log.info("[intraday] %d auto-fill(s) detected and notified", len(fills))

    client = _get_trading_client()

    try:
        positions = client.get_all_positions()
    except Exception as exc:
        log_api_error(log, "[intraday] Failed to fetch open positions", exc)
        return []

    if not positions:
        log.debug("[intraday] No open positions to monitor")
        return []

    symbols = [p.symbol for p in positions]
    log.info("[intraday] Checking %d position(s): %s", len(symbols), ", ".join(symbols))

    try:
        bars_map = fetch_intraday_bars(symbols)
    except Exception as exc:
        log_api_error(log, "[intraday] Failed to fetch 15-min bars", exc)
        return []

    log.debug("[intraday] Got 15-min bars for %d/%d symbols", len(bars_map), len(symbols))

    closed = []
    for pos in positions:
        symbol = pos.symbol
        plpc = float(pos.unrealized_plpc or 0) * 100  # positive = gain, negative = loss

        # Max-loss exit — no RSI data needed
        if plpc < -config.MAX_LOSS_PCT:
            reason = f"loss {abs(plpc):.1f}% exceeds max {config.MAX_LOSS_PCT:.0f}%"
            log.info("[intraday] %s — %s, closing", symbol, reason)
            try:
                cancel_open_orders(client, symbol, log)
                client.close_position(symbol)
                pl = str(pos.unrealized_pl)
                log.info("[intraday] ✅ Closed %s | P&L=%s", symbol, pl)
                closed.append({"symbol": symbol, "intraday_rsi": None,
                                "unrealized_pl": pl, "reason": reason})
            except Exception as exc:
                log_api_error(log, f"[intraday] ❌ Failed to close {symbol}", exc)
            continue

        df = bars_map.get(symbol)

        if df is None:
            log.warning("[intraday] %s — no 15-min bar data, skipping", symbol)
            continue

        try:
            rsi_series = ta.rsi(df["close"], length=RSI_PERIOD)
            intraday_rsi = float(rsi_series.dropna().iloc[-1])
        except Exception as exc:
            log.warning("[intraday] %s — RSI compute failed: %s", symbol, exc)
            continue

        log.debug("[intraday] %s | 15-min RSI=%.1f | P&L=%.1f%% | bars=%d",
                  symbol, intraday_rsi, plpc, len(df))

        close_reason = None
        if intraday_rsi > RSI_OVERBOUGHT:
            close_reason = f"15-min RSI={intraday_rsi:.1f} > {RSI_OVERBOUGHT} (overbought)"
        elif plpc >= config.MIN_GAIN_TAKE_PCT and intraday_rsi < 50:
            close_reason = (f"gain {plpc:.1f}% with fading momentum "
                            f"(RSI={intraday_rsi:.1f} < 50)")

        if close_reason is None:
            log.debug("[intraday] %s — holding (RSI=%.1f, P&L=%.1f%%)",
                      symbol, intraday_rsi, plpc)
            continue

        log.info("[intraday] %s — %s, closing", symbol, close_reason)
        try:
            cancel_open_orders(client, symbol, log)
            client.close_position(symbol)
            pl = str(pos.unrealized_pl)
            log.info("[intraday] ✅ Closed %s | intraday RSI=%.1f | P&L=%s",
                     symbol, intraday_rsi, pl)
            closed.append({
                "symbol":       symbol,
                "intraday_rsi": intraday_rsi,
                "unrealized_pl": pl,
                "reason":       close_reason,
            })
        except Exception as exc:
            log_api_error(log, f"[intraday] ❌ Failed to close {symbol}", exc)

    log.info("[intraday] Done — %d/%d positions closed", len(closed), len(positions))
    return closed


def send_intraday_summary(closed: list[dict], total_checked: int) -> None:
    from notifier.telegram import _send
    if not closed:
        log.debug("[intraday] Nothing to report — no positions closed this cycle")
        return  # silent when nothing happens — avoid flooding Telegram every 30 min

    lines = [f"📈 *Intraday Exit — {len(closed)} position(s) closed*\n"]
    for c in closed:
        pl = c["unrealized_pl"]
        pl_sign = "+" if not pl.startswith("-") else ""
        lines.append(
            f"• *{c['symbol']}* | P&L: `{pl_sign}{pl}` | _{c['reason']}_"
        )
    _send("\n".join(lines))
