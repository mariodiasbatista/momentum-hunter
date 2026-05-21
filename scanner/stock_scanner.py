import config
from data.db import load_signals, signals_computed_today, signal_persistence


def run_stock_scan(min_score: int = config.MIN_SCORE) -> list[dict]:
    if signals_computed_today():
        print("Loading pre-computed stock signals from DB...")
        candidates = load_signals("us_equity", min_score=min_score)
        persistence = signal_persistence("us_equity")
        for c in candidates:
            c["days_in_scan"] = persistence.get(c["symbol"], 1)
        print(f"Loaded {len(candidates)} candidates from DB.")
        return candidates

    # Fallback: compute on the fly if ingestion hasn't run yet today
    print("Signals not yet computed today — running live scan...")
    from data.db import load_symbols
    from data.fetcher import get_stock_data
    from signals.scorer import score_ticker

    universe = load_symbols("us_equity") or []
    if not universe:
        from data.universe import get_stock_universe
        universe = get_stock_universe()

    symbols_to_fetch = sorted(set(universe + ["SPY"]))
    bars = get_stock_data(symbols_to_fetch)
    spy_df = bars.get("SPY")
    if spy_df is None:
        raise RuntimeError("Could not fetch SPY data.")

    candidates = []
    for symbol in universe:
        df = bars.get(symbol)
        if df is None:
            continue
        result = score_ticker(df, spy_df)
        if result is None or result["score"] < min_score:
            continue
        candidates.append({"symbol": symbol, "market": "stocks", **result})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates
