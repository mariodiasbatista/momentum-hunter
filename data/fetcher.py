from datetime import date, timedelta

import pandas as pd
import pandas_market_calendars as mcal

import config
from data.alpaca_client import fetch_stock_bars, fetch_crypto_bars
from data.db import load_all_bars, load_symbols, bars_last_date

_nyse = mcal.get_calendar("NYSE")


def _last_trading_date() -> str:
    """Return the most recent NYSE trading day as an ISO date string."""
    today = date.today()
    schedule = _nyse.schedule(
        start_date=(today - timedelta(days=10)).isoformat(),
        end_date=today.isoformat(),
    )
    if schedule.empty:
        return today.isoformat()
    return schedule.index[-1].date().isoformat()


def _cache_is_fresh() -> bool:
    last = bars_last_date()
    if not last:
        return False
    return last >= _last_trading_date()


def get_stock_data(symbols: list[str] | None = None) -> dict[str, pd.DataFrame]:
    if _cache_is_fresh():
        print("Loading stock bars from cache...")
        result = load_all_bars("us_equity")
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
        result = load_all_bars("crypto")
        print(f"Loaded {len(result)} pairs from cache.")
        return result

    print("Cache stale or missing — fetching crypto bars from Alpaca...")
    if pairs is None:
        from data.universe import get_crypto_universe
        pairs = get_crypto_universe()

    result = fetch_crypto_bars(pairs, config.BARS_LOOKBACK_DAYS)
    print(f"Got data for {len(result)} pairs.")
    return result
