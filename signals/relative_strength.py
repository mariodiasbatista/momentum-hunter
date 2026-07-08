import pandas as pd

import config


def compute_relative_strength(df: pd.DataFrame, spy_df: pd.DataFrame) -> dict:
    """Compare ticker's return to SPY over 63-day (long) and 21-day (short) windows.

    dual_rs is True when the ticker outperforms SPY on BOTH timeframes — a filter
    that was backtested as part of +ALL-ENTRY and improved win rate to 61.4%.
    """
    lookback = config.RS_LOOKBACK_DAYS
    short_lb = config.RS_SHORT_DAYS

    ticker_close = df["close"]
    spy_close = spy_df["close"]

    combined = pd.DataFrame({"ticker": ticker_close, "spy": spy_close}).dropna()

    if len(combined) < lookback + 1:
        return {"rs_return": 0.0, "spy_return": 0.0, "outperforming_spy": False, "dual_rs": False}

    ticker_ret = (combined["ticker"].iloc[-1] / combined["ticker"].iloc[-lookback] - 1) * 100
    spy_ret    = (combined["spy"].iloc[-1]    / combined["spy"].iloc[-lookback]    - 1) * 100
    outperforming = bool(ticker_ret > spy_ret)

    dual_rs = False
    if outperforming and len(combined) >= short_lb + 1:
        ticker_short = (combined["ticker"].iloc[-1] / combined["ticker"].iloc[-short_lb] - 1) * 100
        spy_short    = (combined["spy"].iloc[-1]    / combined["spy"].iloc[-short_lb]    - 1) * 100
        dual_rs = bool(ticker_short > spy_short)

    return {
        "rs_return":       round(float(ticker_ret), 2),
        "spy_return":      round(float(spy_ret), 2),
        "outperforming_spy": outperforming,
        "dual_rs":         dual_rs,
    }
