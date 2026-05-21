import pandas as pd

import config
from data.universe import get_stock_universe
from data.fetcher import get_stock_data
from signals.scorer import score_ticker


def run_stock_scan(min_score: int = config.MIN_SCORE) -> list[dict]:
    universe = get_stock_universe()
    # Always include SPY so we can compute relative strength
    symbols_to_fetch = sorted(set(universe + ["SPY"]))

    bars = get_stock_data(symbols_to_fetch)

    spy_df = bars.get("SPY")
    if spy_df is None:
        raise RuntimeError("Could not fetch SPY data — needed for relative strength calculation.")

    candidates = []
    for symbol in universe:
        df = bars.get(symbol)
        if df is None:
            continue
        result = score_ticker(df, spy_df)
        if result is None or result["score"] < min_score:
            continue
        candidates.append({"symbol": symbol, "market": "stocks", **result})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates
