import config
from data.db import load_signals, signals_computed_today


def run_crypto_scan(min_score: int = config.MIN_SCORE) -> list[dict]:
    if signals_computed_today():
        print("Loading pre-computed crypto signals from DB...")
        candidates = load_signals("crypto", min_score=min_score)
        print(f"Loaded {len(candidates)} candidates from DB.")
        return candidates

    # Fallback: compute on the fly if ingestion hasn't run yet today
    print("Signals not yet computed today — running live scan...")
    from data.db import load_symbols
    from data.fetcher import get_crypto_data
    from signals.scorer import score_ticker

    pairs = load_symbols("crypto") or []
    if not pairs:
        from data.universe import get_crypto_universe
        pairs = get_crypto_universe()

    bars = get_crypto_data(pairs)
    btc_df = bars.get("BTC/USD")
    if btc_df is None:
        raise RuntimeError("Could not fetch BTC/USD data.")

    candidates = []
    for pair in pairs:
        df = bars.get(pair)
        if df is None:
            continue
        result = score_ticker(df, btc_df)
        if result is None or result["score"] < min_score:
            continue
        candidates.append({"symbol": pair, "market": "crypto", **result})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates
