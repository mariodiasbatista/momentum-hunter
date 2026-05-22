"""
trader/premarket_validator.py — 9:15 AM ET pre-market sanity check.

Fetches the latest live price for each watchlist candidate and validates it
against yesterday's signal data already in the DB. Lightweight — only touches
the top N symbols, no bar computation, no rescoring.

Validation rules per symbol:
  DROP  — price below SMA50 or SMA200  (overnight gap-down broke the trend)
  DROP  — price dropped > ATR×2 since yesterday's close  (excessive gap-down)
  WARN  — price gapped up > 5% since yesterday's close   (chasing risk, still tradeable)
  KEEP  — passes all checks

Result saved to data/premarket_filter.json so order_placer reads it at 9:45 AM.

premarket_filter.json format:
{
  "2026-05-22": {
    "approved": ["AAPL", "MSFT"],
    "warned":   {"ALLR":  {"reason": "gap up +7.2%", "current": 1.78, "prev_close": 1.66}},
    "dropped":  {"MDAI":  {"reason": "below SMA50",  "current": 2.10, "prev_close": 2.64}}
  }
}
"""
import json
import logging
from datetime import date
from pathlib import Path

import config

log = logging.getLogger("trader.premarket")

_FILTER_FILE = Path(__file__).parent.parent / "data" / "premarket_filter.json"

GAP_DOWN_ATR_MULT = 2.0   # drop > ATR×2 → drop
GAP_UP_PCT        = 0.05  # rise > 5% → warn


def validate(candidates: list[dict]) -> dict:
    """
    Validate candidates against live pre-market prices.
    Returns {"approved": [...], "warned": {...}, "dropped": {...}}
    """
    from data.alpaca_client import fetch_latest_prices

    symbols = [c["symbol"] for c in candidates]
    log.info("[premarket] Fetching live prices for %d symbols: %s",
             len(symbols), ", ".join(symbols))

    prices = fetch_latest_prices(symbols)
    log.info("[premarket] Got prices for %d/%d symbols", len(prices), len(symbols))

    approved, warned, dropped = [], {}, {}

    for c in candidates:
        symbol     = c["symbol"]
        sma50      = c["trend"]["sma50"]
        sma200     = c["trend"]["sma200"]
        prev_close = c["trend"]["last_close"]
        atr        = c["momentum"]["atr"]

        current = prices.get(symbol)
        if current is None:
            log.warning("[premarket] %s — no live price available, keeping (benefit of doubt)", symbol)
            approved.append(symbol)
            continue

        gap_pct = (current - prev_close) / prev_close
        gap_atr = (prev_close - current) / atr if atr else 0

        log.debug("[premarket] %s | prev $%.2f → now $%.2f | gap %.1f%% | SMA50 $%.2f | SMA200 $%.2f",
                  symbol, prev_close, current, gap_pct * 100, sma50, sma200)

        # ── Drop conditions ──────────────────────────────────────────────
        if current < sma50:
            reason = f"below SMA50 (${current:.2f} < ${sma50:.2f})"
            log.info("[premarket] ❌ DROP %s — %s", symbol, reason)
            dropped[symbol] = {"reason": reason, "current": current, "prev_close": prev_close}
            continue

        if current < sma200:
            reason = f"below SMA200 (${current:.2f} < ${sma200:.2f})"
            log.info("[premarket] ❌ DROP %s — %s", symbol, reason)
            dropped[symbol] = {"reason": reason, "current": current, "prev_close": prev_close}
            continue

        if gap_atr >= GAP_DOWN_ATR_MULT:
            reason = f"gap down {gap_pct*100:.1f}% > {GAP_DOWN_ATR_MULT}×ATR"
            log.info("[premarket] ❌ DROP %s — %s", symbol, reason)
            dropped[symbol] = {"reason": reason, "current": current, "prev_close": prev_close}
            continue

        # ── Warn condition ───────────────────────────────────────────────
        if gap_pct >= GAP_UP_PCT:
            reason = f"gap up +{gap_pct*100:.1f}% (chasing risk)"
            log.info("[premarket] ⚠️  WARN %s — %s", symbol, reason)
            warned[symbol] = {"reason": reason, "current": current, "prev_close": prev_close}
            approved.append(symbol)   # still tradeable, but flagged
            continue

        log.info("[premarket] ✅ KEEP %s — $%.2f (gap %.1f%%)", symbol, current, gap_pct * 100)
        approved.append(symbol)

    result = {"approved": approved, "warned": warned, "dropped": dropped}
    _save_filter(result)
    return result


def _save_filter(result: dict) -> None:
    today = date.today().isoformat()
    try:
        data = json.loads(_FILTER_FILE.read_text()) if _FILTER_FILE.exists() else {}
        data[today] = result
        _FILTER_FILE.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        log.error("[premarket] Failed to save filter: %s", exc)


def load_approved_today() -> set[str] | None:
    """
    Returns the approved symbol set for today, or None if validator hasn't run yet.
    order_placer uses this — if None, no filtering is applied.
    """
    today = date.today().isoformat()
    try:
        data = json.loads(_FILTER_FILE.read_text()) if _FILTER_FILE.exists() else {}
        day = data.get(today)
        return set(day["approved"] + list(day.get("warned", {}).keys())) if day else None
    except Exception:
        return None


def send_premarket_summary(result: dict) -> None:
    from notifier.telegram import _send

    approved = result["approved"]
    warned   = result.get("warned", {})
    dropped  = result.get("dropped", {})

    lines = [f"🌅 *Pre-Market Check — {date.today().strftime('%A %Y-%m-%d')}*\n"]

    if approved:
        keep_syms = [f"`{s}`{'⚠️' if s in warned else ''}" for s in approved]
        lines.append(f"✅ *Trading today ({len(approved)}):* {' '.join(keep_syms)}")

    if warned:
        lines.append("\n⚠️ *Gap-up warnings (still trading):*")
        for sym, info in warned.items():
            lines.append(f"  • *{sym}* — {info['reason']}")

    if dropped:
        lines.append("\n❌ *Dropped from queue:*")
        for sym, info in dropped.items():
            lines.append(f"  • *{sym}* — {info['reason']}")

    if not approved and not dropped:
        lines.append("No candidates to validate today.")

    _send("\n".join(lines))
