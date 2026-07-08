import math

import pandas as pd
import pandas_ta as ta

import config


def compute_momentum(df: pd.DataFrame) -> dict:
    close = df["close"]
    high = df["high"]
    low = df["low"]

    rsi = ta.rsi(close, length=config.RSI_PERIOD)
    macd_result = ta.macd(close, fast=config.MACD_FAST, slow=config.MACD_SLOW, signal=config.MACD_SIGNAL)
    adx_result = ta.adx(high, low, close, length=config.ADX_PERIOD)
    atr_result = ta.atr(high, low, close, length=14)

    roc20 = close / close.shift(config.ROC_PERIOD) - 1

    last_rsi = float(rsi.iloc[-1])

    macd_col = f"MACD_{config.MACD_FAST}_{config.MACD_SLOW}_{config.MACD_SIGNAL}"
    signal_col = f"MACDs_{config.MACD_FAST}_{config.MACD_SLOW}_{config.MACD_SIGNAL}"
    hist_col = f"MACDh_{config.MACD_FAST}_{config.MACD_SLOW}_{config.MACD_SIGNAL}"
    last_macd = float(macd_result[macd_col].iloc[-1])
    last_signal = float(macd_result[signal_col].iloc[-1])
    last_hist = float(macd_result[hist_col].iloc[-1])
    prev_hist = float(macd_result[hist_col].iloc[-2]) if len(macd_result) >= 2 else last_hist

    adx_col = f"ADX_{config.ADX_PERIOD}"
    last_adx = float(adx_result[adx_col].iloc[-1])
    prev_adx = float(adx_result[adx_col].iloc[-2]) if len(adx_result) >= 2 else last_adx

    last_atr = float(atr_result.iloc[-1])

    roc_val = roc20.iloc[-1] if len(roc20) >= config.ROC_PERIOD + 1 else float("nan")
    roc_pass = bool(not math.isnan(roc_val) and roc_val * 100 >= config.ROC_MIN_PCT)

    return {
        "rsi": last_rsi,
        "rsi_in_range": bool(config.RSI_MIN <= last_rsi <= config.RSI_MAX),
        "rsi_overbought": bool(last_rsi > config.RSI_OVERBOUGHT),
        "macd_above_signal": bool(last_macd > last_signal),
        "macd_histogram_positive": bool(last_hist > 0),
        "macd_histogram_shrinking": bool(last_hist < prev_hist and last_hist > 0),
        "adx": last_adx,
        "adx_strong": bool(last_adx > config.ADX_THRESHOLD),
        "adx_falling": bool(last_adx < prev_adx and last_adx < config.ADX_THRESHOLD),
        "atr": last_atr,
        "roc_pass": roc_pass,
    }
