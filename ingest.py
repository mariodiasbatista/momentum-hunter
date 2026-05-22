"""
Daily data ingestion — runs at 06:00 ET (10:00 UTC) Monday–Friday.

Steps per market:
  1. Fetch asset universe from Alpaca → save to assets table (full replace)
  2. Fetch OHLCV bars from Alpaca    → save to bars table (full replace)
  3. Compute all signals             → save to signals table (full replace)

After this runs, main.py reads pre-computed signals from DB — near instant.
"""
import logging
import time
from datetime import datetime, timezone

import config  # noqa: F401 — loads .env early
from data.db import (
    init_db, save_assets, save_bars, save_signals,
    load_all_bars, log_ingestion,
)
from data.alpaca_client import fetch_stock_bars, fetch_crypto_bars
from signals.scorer import score_ticker

log = logging.getLogger("ingest")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s" if m else f"{s}s"


def _compute_and_save_signals(all_bars: dict, benchmark_symbol: str, asset_class: str) -> int:
    benchmark_df = all_bars.get(benchmark_symbol)
    if benchmark_df is None:
        print(f"  Warning: benchmark {benchmark_symbol} not found, skipping signal computation.")
        return 0

    computed_at = datetime.now(timezone.utc).date().isoformat()
    records = []
    for symbol, df in all_bars.items():
        result = score_ticker(df, benchmark_df)
        if result is None:
            continue
        result["symbol"] = symbol
        result["asset_class"] = asset_class
        result["computed_at"] = computed_at
        records.append(result)

    save_signals(records)
    return len(records)


def _ingest_stocks() -> None:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetAssetsRequest
    from alpaca.trading.enums import AssetClass, AssetStatus

    print("\n[stocks] ── Step 1: Fetching asset universe...")
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
    log.info("[stocks] Universe: %d symbols fetched", len(symbols))
    print(f"[stocks] Saved {len(symbols)} tradable symbols.")

    print("[stocks] ── Step 2: Fetching OHLCV bars...")
    bar_count = 0
    symbols_with_data = []
    chunk_size = 100
    # Always include SPY for relative strength benchmark
    all_symbols = sorted(set(symbols + ["SPY"]))
    for i in range(0, len(all_symbols), chunk_size):
        chunk = all_symbols[i : i + chunk_size]
        bars = fetch_stock_bars(chunk, config.BARS_LOOKBACK_DAYS)
        for symbol, df in bars.items():
            save_bars(df, symbol)
            bar_count += len(df)
            symbols_with_data.append(symbol)
        print(f"  {min(i + chunk_size, len(all_symbols))}/{len(all_symbols)} symbols")
    log.info("[stocks] Bars: %s rows for %d symbols", f"{bar_count:,}", len(symbols_with_data))
    print(f"[stocks] Saved {bar_count:,} bars for {len(symbols_with_data)} symbols.")

    print("[stocks] ── Step 3: Computing signals...")
    t_signals = time.time()
    all_bars = load_all_bars("us_equity")

    # Filter out illiquid symbols before signal computation
    before = len(all_bars)
    all_bars = {
        sym: df for sym, df in all_bars.items()
        if df["volume"].mean() >= config.MIN_AVG_VOLUME
    }
    log.info("[stocks] Volume filter: %d / %d passed (≥%s avg vol)", len(all_bars), before, f"{config.MIN_AVG_VOLUME:,}")
    print(f"[stocks] Volume filter: {len(all_bars)}/{before} symbols passed (min avg {config.MIN_AVG_VOLUME:,}/day).")

    # Load SPY separately (not in us_equity assets, but saved to bars)
    from data.db import load_bars
    spy_df = load_bars("SPY")
    if spy_df is not None:
        all_bars["SPY"] = spy_df
    computed = _compute_and_save_signals(all_bars, "SPY", "us_equity")
    log.info("[stocks] Signals: %d scored in %s", computed, _fmt(time.time() - t_signals))
    print(f"[stocks] Signals computed for {computed} symbols in {_fmt(time.time() - t_signals)}.")

    duration = time.time() - t0
    log_ingestion(run_date, "us_equity", len(symbols), bar_count, duration,
                  "success", started_at, _now())
    log.info("[stocks] Complete in %s", _fmt(duration))
    print(f"[stocks] ✓ Complete in {_fmt(duration)}.")


def _ingest_crypto() -> None:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetAssetsRequest
    from alpaca.trading.enums import AssetClass, AssetStatus

    print("\n[crypto] ── Step 1: Fetching asset universe...")
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
    log.info("[crypto] Universe: %d pairs fetched", len(symbols))
    print(f"[crypto] Saved {len(symbols)} pairs.")

    print("[crypto] ── Step 2: Fetching OHLCV bars...")
    bars = fetch_crypto_bars(symbols, config.BARS_LOOKBACK_DAYS)
    bar_count = 0
    for symbol, df in bars.items():
        save_bars(df, symbol)
        bar_count += len(df)
    log.info("[crypto] Bars: %s rows for %d pairs", f"{bar_count:,}", len(bars))
    print(f"[crypto] Saved {bar_count:,} bars for {len(bars)} pairs.")

    print("[crypto] ── Step 3: Computing signals...")
    t_signals = time.time()
    all_bars = load_all_bars("crypto")
    computed = _compute_and_save_signals(all_bars, "BTC/USD", "crypto")
    log.info("[crypto] Signals: %d scored in %s", computed, _fmt(time.time() - t_signals))
    print(f"[crypto] Signals computed for {computed} pairs in {_fmt(time.time() - t_signals)}.")

    duration = time.time() - t0
    log_ingestion(run_date, "crypto", len(symbols), bar_count, duration,
                  "success", started_at, _now())
    log.info("[crypto] Complete in %s", _fmt(duration))
    print(f"[crypto] ✓ Complete in {_fmt(duration)}.")


def main() -> None:
    t_total = time.time()
    log.info("Alpaca Pull started")
    print(f"=== Momentum Hunter — Daily Ingestion [{_now()}] ===")
    init_db()
    try:
        _ingest_stocks()
    except Exception as exc:
        log.error("Stocks ingestion failed: %s", exc)
        raise
    try:
        _ingest_crypto()
    except Exception as exc:
        log.error("Crypto ingestion failed: %s", exc)
        raise
    log.info("Alpaca Pull complete — total %s", _fmt(time.time() - t_total))
    print("\n=== Ingestion complete ===")


if __name__ == "__main__":
    main()
