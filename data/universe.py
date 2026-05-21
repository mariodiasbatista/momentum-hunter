import io
import requests
import pandas as pd

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; momentum-hunter/1.0)"}


def get_sp500_tickers() -> list[str]:
    html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=_HEADERS, timeout=15).text
    table = pd.read_html(io.StringIO(html), header=0)[0]
    tickers = table["Symbol"].tolist()
    # Drop non-standard symbols (e.g. BRK.B, BF.B) that Alpaca doesn't support
    return sorted(t for t in tickers if t.isalpha())


def get_nasdaq100_tickers() -> list[str]:
    html = requests.get("https://en.wikipedia.org/wiki/Nasdaq-100", headers=_HEADERS, timeout=15).text
    tables = pd.read_html(io.StringIO(html), header=0)
    table = next(t for t in tables if "Ticker" in t.columns)
    return sorted(table["Ticker"].tolist())


def get_stock_universe() -> list[str]:
    sp500 = set(get_sp500_tickers())
    ndx = set(get_nasdaq100_tickers())
    return sorted(sp500 | ndx)
