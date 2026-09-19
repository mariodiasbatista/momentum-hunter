"""TP-new-ST — adaptive take-profit derived from a pair's own price history.

A flat 8% target is the same number for a stablecoin-adjacent major and a coin
that routinely runs 30% in a week, so it is simultaneously unreachable for one
and leaves money on the table for the other. TP-new-ST derives the target per
pair from its Max Favorable Excursion distribution:

  1. For every rolling window in the pair's pre-entry history, measure how far
     price ran up within HORIZON days: max(high[i+1:i+1+H]) / close[i] - 1.
  2. Sort those runs — that is the MFE distribution.
  3. If the flat target is reached in >= keep_flat_reach_pct of windows it is
     already achievable, so keep it.
  4. Otherwise take the level the pair reaches in target_reach_probability of
     windows, clamped to [tp_min, tp_max].

Backtested 2026-09-17 (`backtest_tp_new_st.py`). On equities a hard take-profit
truncates the right tail and loses money; on crypto, where positions resolve in
1.5–2.6 days, it wins. That asymmetry is why this lives in crypto/ only.
"""
import logging
from bisect import bisect_left

import config

log = logging.getLogger("crypto.exits")


def mfe_distribution(closes: list, highs: list, horizon_days: int) -> list:
    out = []
    for i in range(len(closes) - horizon_days):
        base = closes[i]
        if base <= 0:
            continue
        out.append((max(highs[i + 1:i + 1 + horizon_days]) / base - 1) * 100)
    return sorted(out)


def reach_rate(dist: list, level_pct: float) -> float | None:
    if not dist:
        return None
    return sum(1 for x in dist if x >= level_pct) / len(dist) * 100


def level_at_reach_probability(dist: list, probability: float) -> float | None:
    if not dist:
        return None
    idx = int(round((1.0 - probability) * (len(dist) - 1)))
    return dist[min(len(dist) - 1, max(0, idx))]


def resolve_take_profit(dist: list, flat_tp: float, cfg: dict) -> float | None:
    if not dist or len(dist) < cfg["min_windows"]:
        return None
    rr = reach_rate(dist, flat_tp * 100)
    if rr is not None and rr >= cfg["keep_flat_reach_pct"]:
        return flat_tp
    level = level_at_reach_probability(dist, cfg["target_reach_probability"])
    if level is None:
        return None
    return max(cfg["tp_min"], min(cfg["tp_max"], level / 100.0))


def adaptive_tp(symbol: str, bars, entry_date=None) -> tuple[float, str]:
    """Take-profit fraction for `symbol`, using only bars strictly before entry.

    `bars` is a DataFrame indexed by date with high/close columns. Returns
    (fraction, kind) where kind is one of no-history / thin-history / kept-flat /
    adaptive — the kind is logged so a surprising target can be traced back.
    """
    flat = config.CRYPTO_TP_FLAT
    cfg = config.CRYPTO_TP_CONFIG

    if bars is None or bars.empty:
        return flat, "no-history"

    dates = list(bars.index)
    closes = bars["close"].tolist()
    highs = bars["high"].tolist()

    end = bisect_left(dates, entry_date) if entry_date is not None else len(dates)
    start = max(0, end - cfg["lookback_days"])
    dist = mfe_distribution(closes[start:end], highs[start:end], config.CRYPTO_TP_HORIZON)

    tp = resolve_take_profit(dist, flat, cfg)
    if tp is None:
        return flat, "thin-history"
    if abs(tp - flat) < 1e-9:
        return tp, "kept-flat"
    return tp, "adaptive"
