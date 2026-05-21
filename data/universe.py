import pandas as pd

def get_sp500_tickers() -> list[str]:
    table = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", header=0)[0]
    return sorted(table["Symbol"].str.replace(".", "-", regex=False).tolist())


def get_nasdaq100_tickers() -> list[str]:
    table = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100", header=0)[4]
    return sorted(table["Ticker"].tolist())


def get_stock_universe() -> list[str]:
    sp500 = set(get_sp500_tickers())
    ndx = set(get_nasdaq100_tickers())
    return sorted(sp500 | ndx)
