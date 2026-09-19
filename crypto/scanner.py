"""Crypto candidate scan — same momentum scoring as equities, own universe filter.

This deliberately does not reuse `scanner/crypto_scanner.py`. That path reads the
`signals` table, which is populated by an ingest filtered on MIN_AVG_VOLUME — a
raw *unit* volume threshold. For shares that is a liquidity filter; for coins it
means BTC/USD (~3 BTC/day) is rejected while PEPE/USD (~7e8 tokens/day) passes,
so the only crypto that ever reaches that scanner is meme coins and stablecoins.
Here the universe is filtered on dollar volume instead, and scored from raw bars.

Relative strength is measured against SPY forward-filled onto crypto's 7-day
calendar, matching the backtest that produced the exit rules.
"""
import logging

import pandas as pd

import config
from data.db import load_all_bars, load_bars
from signals.scorer import score_ticker

log = logging.getLogger("crypto.scanner")


def eligible_pairs(bars: dict) -> list[str]:
    """Pairs quoted in USD, not stablecoins, above the dollar-volume floor."""
    out = []
    for symbol, df in bars.items():
        if not symbol.endswith(config.CRYPTO_QUOTE):
            continue
        if symbol in config.CRYPTO_EXCLUDE_SYMBOLS:
            continue
        if df is None or df.empty:
            continue
        if float((df["close"] * df["volume"]).mean()) < config.CRYPTO_MIN_DOLLAR_VOLUME:
            continue
        out.append(symbol)
    return sorted(out)


def _spy_aligned(spy: pd.DataFrame, index: pd.Index) -> pd.DataFrame:
    """SPY closes forward-filled onto crypto's 7-day calendar.

    Without this the relative-strength comparison drops every Saturday and
    Sunday, which is most of what crypto does.
    """
    closes = spy["close"].reindex(index.union(spy.index)).ffill().reindex(index)
    return pd.DataFrame({"close": closes})


def scan(min_score: int = config.MIN_SCORE) -> list[dict]:
    """Return scored crypto candidates, ranked, each with an `in_prev_scan` flag."""
    bars = load_all_bars("crypto")
    pairs = eligible_pairs(bars)
    log.info("[crypto] Universe: %d pair(s) above $%s/day dollar volume",
             len(pairs), f"{config.CRYPTO_MIN_DOLLAR_VOLUME:,}")

    spy_raw = load_bars("SPY")
    if spy_raw is None or spy_raw.empty:
        log.warning("[crypto] No SPY bars — cannot compute relative strength")
        return []

    candidates = []
    for symbol in pairs:
        df = bars[symbol]
        spy = _spy_aligned(spy_raw, df.index)

        result = score_ticker(df, spy)
        if result is None or result["score"] < min_score:
            continue

        # Momentum must have been present yesterday too. Re-scoring on the
        # truncated frame is the live equivalent of the backtest's in_prev_scan
        # filter, and avoids trusting the `signals` table's crypto rows.
        prev = score_ticker(df.iloc[:-1], spy.iloc[:-1])
        in_prev_scan = bool(prev and prev["score"] >= min_score)

        candidates.append({
            "symbol": symbol,
            "market": "crypto",
            "in_prev_scan": in_prev_scan,
            **result,
        })

    candidates.sort(key=lambda c: (
        -c["score"],
        -c["relative_strength"]["rs_return"],
        -c["momentum"]["adx"],
        -c["volume"]["volume_ratio"],
    ))
    log.info("[crypto] Scored: %d candidate(s) at score >= %d", len(candidates), min_score)
    return candidates


def entry_filtered(candidates: list[dict]) -> list[dict]:
    """Apply the entry filters validated in the crypto backtest."""
    out = []
    for c in candidates:
        symbol = c["symbol"]
        if not c["in_prev_scan"]:
            log.info("[crypto] Skip %s — not in yesterday's scan", symbol)
            continue
        if c["exit"]["exit_mode"] != "trailing_stop":
            log.info("[crypto] Skip %s — fixed_take_profit mode (momentum fading)", symbol)
            continue
        if c["momentum"]["adx"] <= config.ADX_THRESHOLD:
            log.info("[crypto] Skip %s — ADX %.1f below %.0f",
                     symbol, c["momentum"]["adx"], config.ADX_THRESHOLD)
            continue
        if c["volume"]["volume_drying_up"]:
            log.info("[crypto] Skip %s — volume drying up", symbol)
            continue
        if c["momentum"]["macd_histogram_shrinking"]:
            log.info("[crypto] Skip %s — MACD histogram shrinking", symbol)
            continue
        out.append(c)
    return out
