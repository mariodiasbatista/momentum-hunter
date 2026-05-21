from datetime import date

import pandas as pd

import config
from data.alpaca_client import fetch_stock_bars, fetch_crypto_bars
from data.db import load_bars, load_symbols, bars_last_date


def _cache_is_fresh() -> bool:
    last = bars_last_date()
    if not last:
        return False
    today = date.today().isoformat()
    # Accept yesterday's data on weekends or before market close
    return last >= today or (today > last and _is_recent_trading_day(last))


def _is_recent_trading_day(date_str: str) -> bool:
    from datetime import datetime, timedelta
    d = datetime.fromisoformat(date_str).date()
    today = date.today()
    # Accept if within last 3 calendar days (covers weekends + holidays)
    return (today - d).days <= 3


def get_stock_data(symbols: list[str] | None = None) -> dict[str, pd.DataFrame]:
    if _cache_is_fresh():
        print("Loading stock bars from cache...")
        syms = symbols or load_symbols("us_equity")
        result = {}
        for sym in syms:
            df = load_bars(sym)
            if df is not None:
                result[sym] = df
        print(f"Loaded {len(result)} symbols from cache.")
        return result

    print("Cache stale or missing — fetching stock bars from Alpaca...")
    if symbols is None:
        from data.universe import get_stock_universe
        symbols = get_stock_universe()

    result: dict[str, pd.DataFrame] = {}
    chunk_size = 100
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i : i + chunk_size]
        result.update(fetch_stock_bars(chunk, config.BARS_LOOKBACK_DAYS))
        print(f"  fetched {min(i + chunk_size, len(symbols))}/{len(symbols)}")
    print(f"Got data for {len(result)} symbols.")
    return result


def get_crypto_data(pairs: list[str] | None = None) -> dict[str, pd.DataFrame]:
    if _cache_is_fresh():
        print("Loading crypto bars from cache...")
        syms = pairs or load_symbols("crypto")
        result = {}
        for sym in syms:
            df = load_bars(sym)
            if df is not None:
                result[sym] = df
        print(f"Loaded {len(result)} pairs from cache.")
        return result

    print("Cache stale or missing — fetching crypto bars from Alpaca...")
    if pairs is None:
        from data.universe import get_crypto_universe
        pairs = get_crypto_universe()

    result = fetch_crypto_bars(pairs, config.BARS_LOOKBACK_DAYS)
    print(f"Got data for {len(result)} pairs.")
    return result
