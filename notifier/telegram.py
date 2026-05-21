import requests

import config

_API_URL = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
_MAX_MSG_LEN = 4096


def _send(text: str) -> None:
    resp = requests.post(_API_URL, json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"})
    resp.raise_for_status()


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

    exit_label = "🟢 Trailing Stop" if exit_mode == "trailing_stop" else "🔴 Fixed Take-Profit"
    warn_text = f"\n⚠️ {', '.join(warnings)}" if warnings else ""

    return (
        f"*#{rank} {sym}* [{market}] — Score {score}/8\n"
        f"Price: `${close:.2f}` | RSI: `{rsi:.1f}` | ADX: `{adx:.1f}` | Vol: `{vol_ratio:.1f}x`\n"
        f"RS: `{rs_ret:+.1f}%` vs SPY `{spy_ret:+.1f}%`\n"
        f"{signal_line}\n"
        f"Exit: {exit_label} (ATR range: `{atr_min:.2f}–{atr_max:.2f}`){warn_text}"
    )


def send_results(candidates: list[dict], market_label: str) -> None:
    if not candidates:
        _send(f"🔍 *Momentum Hunter — {market_label}*\n\nNo candidates met the minimum score threshold.")
        return

    header = f"🚀 *Momentum Hunter — {market_label}*\n_{len(candidates)} candidate(s) found_\n\n"
    blocks = [_format_candidate(i + 1, c) for i, c in enumerate(candidates)]

    # Build messages that stay under Telegram's 4096-char limit
    current = header
    for block in blocks:
        chunk = block + "\n\n"
        if len(current) + len(chunk) > _MAX_MSG_LEN:
            _send(current.rstrip())
            current = chunk
        else:
            current += chunk

    if current.strip():
        _send(current.rstrip())
