"""
Daily data ingestion — fetches asset universe + OHLCV bars from Alpaca
and stores them in the local SQLite database.

Scheduled to run at 06:00 ET (10:00 UTC) Monday–Friday.
"""
import time
from datetime import datetime, timezone

import config  # noqa: F401 — loads .env early
from data.db import init_db, save_assets, save_bars, log_ingestion
from data.alpaca_client import fetch_stock_bars, fetch_crypto_bars


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ingest_stocks() -> None:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetAssetsRequest
    from alpaca.trading.enums import AssetClass, AssetStatus

    print("[stocks] Fetching asset universe from Alpaca...")
    started_at = _now()
    t0 = time.time()
    run_date = datetime.now(timezone.utc).date().isoformat()

    paper = "paper-api" in config.ALPACA_BASE_URL
    client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=paper)
    assets = client.get_all_assets(GetAssetsRequest(
        asset_class=AssetClass.US_EQUITY,
        status=AssetStatus.ACTIVE,
    ))

    records = [
        {
            "symbol": a.symbol,
            "name": a.name,
            "exchange": str(a.exchange.value) if a.exchange else None,
            "asset_class": "us_equity",
            "tradable": int(a.tradable),
            "fractionable": int(a.fractionable),
            "shortable": int(a.shortable),
            "updated_at": run_date,
        }
        for a in assets
        if a.symbol.isalpha()
    ]
    save_assets(records)
    symbols = [r["symbol"] for r in records if r["tradable"]]
    print(f"[stocks] Saved {len(records)} assets ({len(symbols)} tradable). Fetching bars...")

    bar_count = 0
    chunk_size = 100
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i : i + chunk_size]
        bars = fetch_stock_bars(chunk, config.BARS_LOOKBACK_DAYS)
        for symbol, df in bars.items():
            save_bars(df, symbol)
            bar_count += len(df)
        print(f"  [stocks] {min(i + chunk_size, len(symbols))}/{len(symbols)} symbols processed")

    duration = time.time() - t0
    completed_at = _now()
    log_ingestion(run_date, "us_equity", len(symbols), bar_count, duration, "success", started_at, completed_at)
    m, s = divmod(int(duration), 60)
    print(f"[stocks] Done — {len(symbols)} symbols, {bar_count:,} bars saved in {m}m {s}s.")


def _ingest_crypto() -> None:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetAssetsRequest
    from alpaca.trading.enums import AssetClass, AssetStatus

    print("[crypto] Fetching asset universe from Alpaca...")
    started_at = _now()
    t0 = time.time()
    run_date = datetime.now(timezone.utc).date().isoformat()

    paper = "paper-api" in config.ALPACA_BASE_URL
    client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=paper)
    assets = client.get_all_assets(GetAssetsRequest(
        asset_class=AssetClass.CRYPTO,
        status=AssetStatus.ACTIVE,
    ))

    records = [
        {
            "symbol": a.symbol,
            "name": a.name,
            "exchange": str(a.exchange.value) if a.exchange else None,
            "asset_class": "crypto",
            "tradable": int(a.tradable),
            "fractionable": int(a.fractionable),
            "shortable": int(a.shortable),
            "updated_at": run_date,
        }
        for a in assets
        if a.tradable
    ]
    save_assets(records)
    symbols = [r["symbol"] for r in records]
    print(f"[crypto] Saved {len(symbols)} pairs. Fetching bars...")

    bars = fetch_crypto_bars(symbols, config.BARS_LOOKBACK_DAYS)
    bar_count = 0
    for symbol, df in bars.items():
        save_bars(df, symbol)
        bar_count += len(df)

    duration = time.time() - t0
    completed_at = _now()
    log_ingestion(run_date, "crypto", len(symbols), bar_count, duration, "success", started_at, completed_at)
    m, s = divmod(int(duration), 60)
    print(f"[crypto] Done — {len(symbols)} pairs, {bar_count:,} bars saved in {m}m {s}s.")


def main() -> None:
    print(f"=== Momentum Hunter — Daily Ingestion [{_now()}] ===")
    init_db()
    _ingest_stocks()
    _ingest_crypto()
    print("=== Ingestion complete ===")


if __name__ == "__main__":
    main()
