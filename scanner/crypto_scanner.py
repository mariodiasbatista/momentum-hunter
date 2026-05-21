import pandas as pd

import config
from data.fetcher import get_crypto_data
from data.universe import get_crypto_universe
from signals.scorer import score_ticker


def run_crypto_scan(min_score: int = config.MIN_SCORE) -> list[dict]:
    pairs = get_crypto_universe()
    bars = get_crypto_data(pairs)

    # Use BTC/USD as the benchmark for relative strength in crypto
    btc_df = bars.get("BTC/USD")
    if btc_df is None:
        raise RuntimeError("Could not fetch BTC/USD data — needed as crypto benchmark.")

    candidates = []
    for pair in pairs:
        if pair == "BTC/USD":
            # Score BTC against itself (RS will be 0, so it won't earn that point)
            benchmark = btc_df
        else:
            benchmark = btc_df

        df = bars.get(pair)
        if df is None:
            continue
        result = score_ticker(df, benchmark)
        if result is None or result["score"] < min_score:
            continue
        candidates.append({"symbol": pair, "market": "crypto", **result})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates
