import pandas as pd

from signals.trend import compute_trend
from signals.momentum import compute_momentum
from signals.volume import compute_volume
from signals.relative_strength import compute_relative_strength
from signals.exit_mode import compute_exit_mode

# adx_strong and outperforming_spy worth 2 points each — max score is 10
_WEIGHTS = {
    "above_sma50": 1,
    "above_sma200": 1,
    "ema9_above_ema21": 1,
    "rsi_in_range": 1,
    "macd_bullish": 1,
    "adx_strong": 2,
    "volume_above_avg": 1,
    "outperforming_spy": 2,
}
MAX_SCORE = sum(_WEIGHTS.values())  # 10


def score_ticker(df: pd.DataFrame, spy_df: pd.DataFrame) -> dict | None:
    """
    Run all signal modules and return a composite score dict, or None if data is insufficient.
    Score is out of 10; adx_strong and outperforming_spy each count 2 points.
    """
    if len(df) < 210:
        return None

    try:
        trend = compute_trend(df)
        momentum = compute_momentum(df)
        volume = compute_volume(df)
        rs = compute_relative_strength(df, spy_df)
        exit_info = compute_exit_mode(momentum, volume)
    except Exception:
        return None

    criteria = {
        "above_sma50": trend["above_sma50"],
        "above_sma200": trend["above_sma200"],
        "ema9_above_ema21": trend["ema9_above_ema21"],
        "rsi_in_range": momentum["rsi_in_range"],
        "macd_bullish": momentum["macd_above_signal"] and momentum["macd_histogram_positive"],
        "adx_strong": momentum["adx_strong"],
        "volume_above_avg": volume["volume_above_avg"],
        "outperforming_spy": rs["outperforming_spy"],
    }

    score = sum(_WEIGHTS[k] for k, v in criteria.items() if v)

    return {
        "score": score,
        "criteria": criteria,
        "trend": trend,
        "momentum": momentum,
        "volume": volume,
        "relative_strength": rs,
        "exit": exit_info,
    }
