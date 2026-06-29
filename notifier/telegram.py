import time

import requests

import config

_API_URL = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
_MAX_MSG_LEN = 4096


def _send(text: str) -> None:
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    resp = requests.post(_API_URL, json=payload)
    if resp.status_code == 429:
        wait = resp.json().get("parameters", {}).get("retry_after", 5)
        time.sleep(wait)
        resp = requests.post(_API_URL, json=payload)
    resp.raise_for_status()


def send_alert(message: str) -> None:
    try:
        _send(f"⚠️ *Momentum Hunter Alert*\n\n{message}")
    except Exception:
        pass  # Don't raise — alerts must never crash the caller


def _format_candidate(rank: int, c: dict) -> str:
    sym = c["symbol"]
    score = c["score"]
    market = c["market"].upper()
    close = c["trend"]["last_close"]
    rsi = c["momentum"]["rsi"]
    adx = c["momentum"]["adx"]
    vol_ratio = c["volume"]["volume_ratio"]
    rs_ret = c["relative_strength"]["rs_return"]
    spy_ret = c["relative_strength"]["spy_return"]
    exit_mode = c["exit"]["exit_mode"]
    atr_min, atr_max = c["exit"]["trailing_stop_atr_range"]
    warnings = c["exit"]["warnings"]

    criteria = c["criteria"]
    checks = {
        "above_sma50": "SMA50",
        "above_sma200": "SMA200",
        "ema9_above_ema21": "EMA9>21",
        "rsi_in_range": "RSI",
        "macd_bullish": "MACD",
        "adx_strong": "ADX",
        "volume_above_avg": "Volume",
        "outperforming_spy": "RS>SPY",
    }
    signal_line = " ".join(f"{'✅' if criteria[k] else '❌'}{v}" for k, v in checks.items())

    is_watch_only = exit_mode == "fixed_take_profit"
    exit_label = "🟢 Trailing Stop" if not is_watch_only else "👁 Watch Only — Fixed TP"
    warn_text = f"\n⚠️ {', '.join(warnings)}" if warnings else ""

    days = c.get("days_in_scan", 1)
    streak = f" 📅 {days}d streak" if days > 1 else ""

    from trader.order_placer import position_qty, position_label
    qty = position_qty(close)
    pos = position_label(close)

    # Mirror the bracket order price levels (trailing_stop only — watch-only skips order)
    stop_price = round(min(close - atr_min, close * 0.99), 2)
    take_price = round(close + atr_max * 2, 2)

    order_line = (f"📦 `x{qty}` shares — {pos} | stop `${stop_price:.2f}` | tp `${take_price:.2f}`"
                  if not is_watch_only else "👁 No order — momentum fading, watch only")

    return (
        f"*#{rank} {sym}* [{market}] — Score {score}/8{streak}\n"
        f"Price: `${close:.2f}` | RSI: `{rsi:.1f}` | ADX: `{adx:.1f}` | Vol: `{vol_ratio:.1f}x`\n"
        f"RS: `{rs_ret:+.1f}%` vs SPY `{spy_ret:+.1f}%`\n"
        f"{signal_line}\n"
        f"{order_line}\n"
        f"Exit: {exit_label} (ATR `{atr_min:.2f}–{atr_max:.2f}`){warn_text}"
    )


def send_results(candidates: list[dict], market_label: str, stale_warning: str | None = None) -> None:
    if not candidates:
        _send(f"🔍 *Momentum Hunter — {market_label}*\n\nNo candidates met the minimum score threshold.")
        return

    buys = sum(1 for c in candidates if c["exit"]["exit_mode"] == "trailing_stop")
    watches = len(candidates) - buys
    summary = f"{buys} buy" + (f", {watches} watch only" if watches else "")
    stale_line = f"\n⚠️ _{stale_warning}_\n" if stale_warning else ""
    header = f"🚀 *Momentum Hunter — {market_label}*\n_{len(candidates)} candidate(s) — {summary}_{stale_line}\n\n"
    blocks = [_format_candidate(i + 1, c) for i, c in enumerate(candidates)]

    # Build messages that stay under Telegram's 4096-char limit
    current = header
    for block in blocks:
        chunk = block + "\n\n"
        if len(current) + len(chunk) > _MAX_MSG_LEN:
            _send(current.rstrip())
            time.sleep(1)
            current = chunk
        else:
            current += chunk

    if current.strip():
        _send(current.rstrip())
