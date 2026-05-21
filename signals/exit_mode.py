import config


def compute_exit_mode(momentum: dict, volume: dict) -> dict:
    """
    Determine whether to use a trailing stop or fixed take-profit.
    Returns the recommendation and the count of warning signs.
    """
    warnings = []

    if momentum["rsi_overbought"]:
        warnings.append("RSI overbought (>70)")
    if momentum["adx_falling"]:
        warnings.append("ADX falling below 25")
    if volume["volume_drying_up"]:
        warnings.append("Volume drying up")
    if momentum["macd_histogram_shrinking"]:
        warnings.append("MACD histogram shrinking")

    warning_count = len(warnings)
    use_trailing = warning_count < 2 and not momentum["rsi_overbought"]

    atr = momentum["atr"]
    trailing_stop_atr_min = round(atr * config.ATR_TRAILING_MIN, 2)
    trailing_stop_atr_max = round(atr * config.ATR_TRAILING_MAX, 2)

    return {
        "exit_mode": "trailing_stop" if use_trailing else "fixed_take_profit",
        "warning_count": warning_count,
        "warnings": warnings,
        "trailing_stop_atr_range": (trailing_stop_atr_min, trailing_stop_atr_max),
    }
