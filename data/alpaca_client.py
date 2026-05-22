from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import config


_stock_client: StockHistoricalDataClient | None = None
_crypto_client: CryptoHistoricalDataClient | None = None


def _stock() -> StockHistoricalDataClient:
    global _stock_client
    if _stock_client is None:
        _stock_client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    return _stock_client


def _crypto() -> CryptoHistoricalDataClient:
    global _crypto_client
    if _crypto_client is None:
        _crypto_client = CryptoHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    return _crypto_client


def fetch_stock_bars(symbols: list[str], lookback_days: int) -> dict[str, pd.DataFrame]:
    start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start, adjustment="all")
    bars = _stock().get_stock_bars(req).df
    return _split_by_symbol(bars, symbols)


def fetch_latest_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch the most recent trade price for each symbol (pre-market aware)."""
    from alpaca.data.requests import StockLatestBarRequest
    try:
        bars = _stock().get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=symbols))
        return {sym: float(bar.close) for sym, bar in bars.items()}
    except Exception:
        return {}


def fetch_intraday_bars(symbols: list[str], lookback_hours: int = 48) -> dict[str, pd.DataFrame]:
    """Fetch 15-minute bars for intraday RSI monitoring. lookback_hours covers enough bars for RSI(14)."""
    start = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    tf = TimeFrame(15, TimeFrameUnit.Minute)
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=tf, start=start, adjustment="all")
    bars = _stock().get_stock_bars(req).df
    result = _split_by_symbol(bars, symbols)
    # Intraday needs at least 20 bars (5 hours of 15-min data) for a meaningful RSI
    return {sym: df for sym, df in result.items() if len(df) >= 20}


def fetch_crypto_bars(pairs: list[str], lookback_days: int) -> dict[str, pd.DataFrame]:
    start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    req = CryptoBarsRequest(symbol_or_symbols=pairs, timeframe=TimeFrame.Day, start=start)
    bars = _crypto().get_crypto_bars(req).df
    return _split_by_symbol(bars, pairs)


def _split_by_symbol(df: pd.DataFrame, symbols: list[str]) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    if df.empty:
        return result
    if isinstance(df.index, pd.MultiIndex):
        for sym in symbols:
            try:
                sym_df = df.xs(sym, level="symbol").copy()
                sym_df.index = pd.to_datetime(sym_df.index)
                sym_df.sort_index(inplace=True)
                if len(sym_df) >= 50:
                    result[sym] = sym_df
            except KeyError:
                pass
    else:
        df.index = pd.to_datetime(df.index)
        df.sort_index(inplace=True)
        for sym in symbols:
            result[sym] = df.copy()
    return result
