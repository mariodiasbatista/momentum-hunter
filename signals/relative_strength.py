import pandas as pd

import config


def compute_relative_strength(df: pd.DataFrame, spy_df: pd.DataFrame) -> dict:
    """Compare ticker's return over RS_LOOKBACK_DAYS to SPY's return."""
    lookback = config.RS_LOOKBACK_DAYS

    ticker_close = df["close"]
    spy_close = spy_df["close"]

    # Align on common dates
    combined = pd.DataFrame({"ticker": ticker_close, "spy": spy_close}).dropna()

    if len(combined) < lookback + 1:
        return {"rs_return": 0.0, "spy_return": 0.0, "outperforming_spy": False}

    ticker_ret = (combined["ticker"].iloc[-1] / combined["ticker"].iloc[-lookback] - 1) * 100
    spy_ret = (combined["spy"].iloc[-1] / combined["spy"].iloc[-lookback] - 1) * 100

    return {
        "rs_return": round(float(ticker_ret), 2),
        "spy_return": round(float(spy_ret), 2),
        "outperforming_spy": bool(ticker_ret > spy_ret),
    }
