import pandas as pd

import config
from data.alpaca_client import fetch_stock_bars, fetch_crypto_bars


def get_stock_data(symbols: list[str]) -> dict[str, pd.DataFrame]:
    print(f"Fetching bars for {len(symbols)} stocks...")
    # Alpaca has a max symbols-per-request limit; batch in chunks of 100
    result: dict[str, pd.DataFrame] = {}
    chunk_size = 100
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i : i + chunk_size]
        result.update(fetch_stock_bars(chunk, config.BARS_LOOKBACK_DAYS))
        print(f"  fetched {min(i + chunk_size, len(symbols))}/{len(symbols)}")
    print(f"Got data for {len(result)} symbols.")
    return result


def get_crypto_data(pairs: list[str]) -> dict[str, pd.DataFrame]:
    print(f"Fetching bars for {len(pairs)} crypto pairs...")
    result = fetch_crypto_bars(pairs, config.BARS_LOOKBACK_DAYS)
    print(f"Got data for {len(result)} pairs.")
    return result
