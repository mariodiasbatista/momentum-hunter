import io
import requests
import pandas as pd

import config

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; momentum-hunter/1.0)"}


# --- Alpaca (primary) ---

def _alpaca_stock_universe() -> list[str]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetAssetsRequest
    from alpaca.trading.enums import AssetClass, AssetStatus

    paper = "paper-api" in config.ALPACA_BASE_URL
    client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=paper)
    assets = client.get_all_assets(GetAssetsRequest(
        asset_class=AssetClass.US_EQUITY,
        status=AssetStatus.ACTIVE,
    ))
    return sorted(
        a.symbol for a in assets
        if a.tradable and a.symbol.isalpha()
    )


def _alpaca_crypto_universe() -> list[str]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetAssetsRequest
    from alpaca.trading.enums import AssetClass, AssetStatus

    paper = "paper-api" in config.ALPACA_BASE_URL
    client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=paper)
    assets = client.get_all_assets(GetAssetsRequest(
        asset_class=AssetClass.CRYPTO,
        status=AssetStatus.ACTIVE,
    ))
    return sorted(a.symbol for a in assets if a.tradable)


# --- Wikipedia (fallback) ---

def _wikipedia_sp500() -> list[str]:
    html = requests.get(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers=_HEADERS, timeout=15,
    ).text
    table = pd.read_html(io.StringIO(html), header=0)[0]
    return sorted(t for t in table["Symbol"].tolist() if t.isalpha())


def _wikipedia_nasdaq100() -> list[str]:
    html = requests.get(
        "https://en.wikipedia.org/wiki/Nasdaq-100",
        headers=_HEADERS, timeout=15,
    ).text
    tables = pd.read_html(io.StringIO(html), header=0)
    table = next(t for t in tables if "Ticker" in t.columns)
    return sorted(table["Ticker"].tolist())


def _wikipedia_stock_universe() -> list[str]:
    sp500 = set(_wikipedia_sp500())
    ndx = set(_wikipedia_nasdaq100())
    return sorted(sp500 | ndx)


# --- Public API ---

def get_stock_universe() -> list[str]:
    try:
        print("Fetching stock universe from Alpaca...")
        universe = _alpaca_stock_universe()
        print(f"Alpaca returned {len(universe)} active tradable equities.")
        return universe
    except Exception as e:
        print(f"Alpaca asset list failed ({e}), falling back to Wikipedia (S&P 500 + NASDAQ 100).")
        return _wikipedia_stock_universe()


def get_crypto_universe() -> list[str]:
    try:
        print("Fetching crypto universe from Alpaca...")
        universe = _alpaca_crypto_universe()
        print(f"Alpaca returned {len(universe)} active crypto pairs.")
        return universe
    except Exception as e:
        print(f"Alpaca crypto list failed ({e}), falling back to config defaults.")
        return config.CRYPTO_PAIRS
