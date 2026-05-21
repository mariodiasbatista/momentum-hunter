import pandas as pd
import pandas_ta as ta

import config


def compute_trend(df: pd.DataFrame) -> dict:
    close = df["close"]

    sma50 = ta.sma(close, length=config.SMA_SHORT)
    sma200 = ta.sma(close, length=config.SMA_LONG)
    ema9 = ta.ema(close, length=config.EMA_FAST)
    ema21 = ta.ema(close, length=config.EMA_SLOW)

    last_close = close.iloc[-1]
    last_sma50 = sma50.iloc[-1]
    last_sma200 = sma200.iloc[-1]
    last_ema9 = ema9.iloc[-1]
    last_ema21 = ema21.iloc[-1]

    # EMA crossover: ema9 crossed above ema21 recently (within last 3 bars)
    ema_cross = False
    if len(ema9) >= 4 and len(ema21) >= 4:
        prev_ema9 = ema9.iloc[-2]
        prev_ema21 = ema21.iloc[-2]
        ema_cross = (prev_ema9 <= prev_ema21) and (last_ema9 > last_ema21)
        # Also accept if cross happened within last 3 bars
        if not ema_cross and len(ema9) >= 5:
            for i in range(-4, -1):
                if ema9.iloc[i - 1] <= ema21.iloc[i - 1] and ema9.iloc[i] > ema21.iloc[i]:
                    ema_cross = True
                    break

    return {
        "above_sma50": bool(last_close > last_sma50),
        "above_sma200": bool(last_close > last_sma200),
        "ema9_above_ema21": bool(last_ema9 > last_ema21),
        "ema_crossover": bool(ema_cross),
        "last_close": float(last_close),
        "sma50": float(last_sma50),
        "sma200": float(last_sma200),
        "ema9": float(last_ema9),
        "ema21": float(last_ema21),
    }
