"""
backtest_variants.py — simulate multiple entry-filter configurations side by side.

Variants tested (all use old 5% stop / same exit logic as baseline):
  BASELINE : score ≥ 6  (current system)
  V1       : score ≥ 7
  V2       : score ≥ 7  + ROC20 ≥ 5%  (price up ≥5% over last 20 bars)
  V3       : score ≥ 7  + ROC20 ≥ 5%  + dual RS  (outperform SPY on both 63d AND 21d)

Precomputed signals are cached to /tmp/bt_signals.parquet to avoid re-running
the slow indicator step on every invocation.
"""

import sys, os, math, time, json
from collections import defaultdict
from pathlib import Path

import sqlite3
import pandas as pd
import pandas_ta as ta

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH          = "data/momentum.db"
CACHE_PATH       = Path("/tmp/bt_signals.pkl")
BACKTEST_START   = "2026-05-12"
BACKTEST_END     = "2026-06-17"
MIN_AVG_VOLUME   = 500_000
MIN_BARS_HISTORY = 210
ADX_THRESHOLD    = 30
VOLUME_MULT      = 1.2
RS_LOOKBACK      = 63
RS_SHORT         = 21
ROC_PERIOD       = 20
ROC_MIN_PCT      = 5.0
MAX_HOLD_DAYS    = 10
AUTO_ORDER_TOP_N = 10
STOP_PCT         = 0.05   # old 5% intraday monitor (unchanged)
POS_THRESHOLD    = 50


def position_qty(price):
    dollars = 750 if price < POS_THRESHOLD else 250
    return max(1, math.floor(dollars / price))


# ── 1. Load / compute signals ─────────────────────────────────────────────────
if CACHE_PATH.exists():
    print(f"Loading cached signals from {CACHE_PATH}…", flush=True)
    sdf = pd.read_pickle(CACHE_PATH)
    print(f"  {len(sdf):,} rows loaded", flush=True)
else:
    print("Loading bars from DB…", flush=True)
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    raw = pd.read_sql("SELECT symbol, date, open, high, low, close, volume FROM bars",
                      conn, parse_dates=["date"])
    conn.close()
    print(f"  {len(raw):,} rows | {raw['symbol'].nunique():,} symbols | {time.time()-t0:.1f}s")

    spy_all = raw[raw["symbol"] == "SPY"].set_index("date").sort_index()

    vol_means = raw.groupby("symbol")["volume"].mean()
    passing = set(vol_means[vol_means >= MIN_AVG_VOLUME].index)
    print(f"  {len(passing):,} symbols pass volume filter", flush=True)

    print("Precomputing indicators…", flush=True)
    t0 = time.time()
    records = []
    groups = raw[raw["symbol"].isin(passing)].groupby("symbol")
    total = len(passing)
    done = 0

    for symbol, df in groups:
        df = df.set_index("date").sort_index()
        if len(df) < MIN_BARS_HISTORY:
            done += 1
            continue

        close  = df["close"]
        high   = df["high"]
        low_s  = df["low"]
        volume = df["volume"]

        sma50  = ta.sma(close, 50)
        sma200 = ta.sma(close, 200)
        ema9   = ta.ema(close, 9)
        ema21  = ta.ema(close, 21)
        rsi    = ta.rsi(close, 14)
        macd_r = ta.macd(close, 12, 26, 9)
        adx_r  = ta.adx(high, low_s, close, 14)
        atr_r  = ta.atr(high, low_s, close, 14)
        vol_ma = volume.rolling(20).mean()
        vol_3d = volume.rolling(3).mean()
        roc20  = (close / close.shift(ROC_PERIOD) - 1) * 100

        hist       = macd_r["MACDh_12_26_9"]
        adx_series = adx_r["ADX_14"]
        prev_hist  = hist.shift(1)
        prev_adx   = adx_series.shift(1)

        combined  = pd.DataFrame({"tk": close, "spy": spy_all["close"]}).dropna()
        rs_long   = (combined["tk"] / combined["tk"].shift(RS_LOOKBACK) - 1) * 100
        spy_long  = (combined["spy"] / combined["spy"].shift(RS_LOOKBACK) - 1) * 100
        rs_short  = (combined["tk"] / combined["tk"].shift(RS_SHORT) - 1) * 100
        spy_short = (combined["spy"] / combined["spy"].shift(RS_SHORT) - 1) * 100

        for dt in df.index:
            def g(s): return s.get(dt)

            s50 = g(sma50); s200 = g(sma200); e9 = g(ema9); e21 = g(ema21)
            r   = g(rsi);   mc  = g(macd_r["MACD_12_26_9"])
            ms  = g(macd_r["MACDs_12_26_9"]); mh = g(hist); mhp = g(prev_hist)
            adx = g(adx_series); padx = g(prev_adx)
            atr = g(atr_r); vm = g(vol_ma); v3 = g(vol_3d); v = g(volume)
            cl  = g(close); op = g(df["open"]); lo = g(df["low"]); hi = g(df["high"])
            roc = g(roc20)
            rs_l = g(rs_long); spy_l = g(spy_long)
            rs_s = g(rs_short); spy_s = g(spy_short)

            if any(x is None or (isinstance(x, float) and math.isnan(x))
                   for x in [s50, s200, e9, e21, r, mc, ms, mh, mhp,
                              adx, padx, atr, vm, v, cl]):
                continue

            vol_ratio   = v / vm if vm > 0 else 0.0
            vol_3d_rat  = v3 / vm if (v3 and vm and vm > 0) else 0.0
            adx_falling = adx < padx and adx < ADX_THRESHOLD
            vol_drying  = vol_ratio < 0.8
            macd_shrink = mh < mhp and mh > 0
            rsi_ob      = r > 70

            above_sma50  = cl > s50
            above_sma200 = cl > s200
            e9_gt_e21    = e9 > e21
            rsi_range    = 50 <= r <= 70
            macd_bull    = mc > ms and mh > 0
            adx_strong   = adx > ADX_THRESHOLD
            vol_above    = vol_ratio >= VOLUME_MULT
            out_spy_long = (rs_l > spy_l) if rs_l is not None and spy_l is not None else False

            score = sum([above_sma50, above_sma200, e9_gt_e21, rsi_range,
                         macd_bull, adx_strong, vol_above, out_spy_long])

            warnings    = sum([rsi_ob, adx_falling, vol_drying, macd_shrink])
            exit_mode   = "trailing_stop" if (warnings < 2 and not rsi_ob) else "fixed_take_profit"

            out_spy_short = (rs_s > spy_s) if rs_s is not None and spy_s is not None else False
            dual_rs       = out_spy_long and out_spy_short
            roc_pass      = (roc >= ROC_MIN_PCT) if roc is not None and not math.isnan(roc) else False
            vol_3d_pass   = vol_3d_rat >= VOLUME_MULT

            records.append({
                "symbol": symbol, "date": dt, "score": score, "exit_mode": exit_mode,
                "adx": adx, "rsi": r, "atr": atr,
                "vol_ratio": vol_ratio, "vol_3d_ratio": vol_3d_rat,
                "rs_return": rs_l if rs_l is not None else 0.0,
                "adx_strong": adx_strong, "vol_drying": vol_drying,
                "macd_shrink": macd_shrink, "roc_pass": roc_pass,
                "dual_rs": dual_rs, "vol_3d_pass": vol_3d_pass,
                "close": cl, "open": op, "low": lo, "high": hi,
            })

        done += 1
        if done % 500 == 0:
            el = time.time() - t0
            eta = el / done * (total - done)
            print(f"  {done}/{total} | {el:.0f}s elapsed | ~{eta:.0f}s left", flush=True)

    print(f"  Done in {time.time()-t0:.1f}s — {len(records):,} rows", flush=True)
    sdf = pd.DataFrame(records)
    sdf["date"] = pd.to_datetime(sdf["date"])
    sdf.to_pickle(CACHE_PATH)
    print(f"  Cached to {CACHE_PATH}", flush=True)


# ── 2. Backtest engine ────────────────────────────────────────────────────────
bt_dates = sdf[(sdf["date"] >= BACKTEST_START) &
               (sdf["date"] <= BACKTEST_END)]["date"].sort_values().unique()
print(f"\nBacktest: {str(bt_dates[0])[:10]} → {str(bt_dates[-1])[:10]} ({len(bt_dates)} days)\n")

price_lkp = sdf.set_index(["symbol", "date"])[
    ["open", "high", "low", "close", "rsi", "atr"]].to_dict("index")


VARIANTS = {
    "BASELINE (score≥6)":          {"min_score": 6, "roc": False, "dual_rs": False},
    "V1  score≥7":                 {"min_score": 7, "roc": False, "dual_rs": False},
    "V2  score≥7 + ROC20≥5%":     {"min_score": 7, "roc": True,  "dual_rs": False},
    "V3  score≥7 + ROC + dual-RS": {"min_score": 7, "roc": True,  "dual_rs": True},
}


def simulate(cfg: dict) -> list[dict]:
    open_pos = {}
    trades   = []

    for i, signal_date in enumerate(bt_dates):
        # Exit check for open positions
        for sym in list(open_pos.keys()):
            pos = open_pos[sym]
            key = (sym, signal_date)
            if key not in price_lkp:
                continue
            bar   = price_lkp[key]
            entry = pos["entry"]
            stop  = pos["stop"]
            qty   = pos["qty"]
            days  = (signal_date - pos["entry_date"]).days

            exit_price = exit_reason = None
            if bar["open"] <= stop:
                exit_price, exit_reason = bar["open"],  "gap-down stop"
            elif bar["low"] <= stop:
                exit_price, exit_reason = stop,         "stop hit"
            elif bar["rsi"] > 70:
                exit_price, exit_reason = bar["close"], "RSI > 70"
            elif (bar["close"] - entry) / entry >= 0.08 and bar["rsi"] < 50:
                exit_price, exit_reason = bar["close"], "gain≥8%+RSI<50"
            elif days >= MAX_HOLD_DAYS:
                exit_price, exit_reason = bar["close"], "max hold"

            if exit_price is not None:
                pnl = round((exit_price - entry) * qty, 2)
                trades.append({"symbol": sym, "entry_date": pos["entry_date"],
                                "exit_date": signal_date, "entry": entry,
                                "exit": exit_price, "qty": qty, "pnl": pnl,
                                "pnl_pct": round((exit_price-entry)/entry*100, 2),
                                "reason": exit_reason})
                del open_pos[sym]

        if i + 1 >= len(bt_dates):
            continue
        entry_date = bt_dates[i + 1]

        day = sdf[sdf["date"] == signal_date].copy()
        mask = (
            (day["score"] >= cfg["min_score"]) &
            (day["adx"] > ADX_THRESHOLD) &
            (~day["vol_drying"]) &
            (~day["macd_shrink"]) &
            (day["exit_mode"] == "trailing_stop")
        )
        if cfg["roc"]:
            mask &= day["roc_pass"]
        if cfg["dual_rs"]:
            mask &= day["dual_rs"]

        cands = day[mask].sort_values(
            ["rs_return", "adx", "vol_ratio"], ascending=False)

        placed = 0
        for _, c in cands.iterrows():
            if placed >= AUTO_ORDER_TOP_N:
                break
            sym = c["symbol"]
            if sym in open_pos:
                continue
            key = (sym, entry_date)
            if key not in price_lkp:
                continue
            ep  = price_lkp[key]["open"]
            if ep <= 0:
                continue
            atr_min = c["atr"] * 1.5
            stop    = min(ep - atr_min, ep * (1 - STOP_PCT))
            stop    = min(stop, ep - 0.01)
            open_pos[sym] = {"entry": ep, "stop": stop,
                             "qty": position_qty(ep), "entry_date": entry_date}
            placed += 1

    last = bt_dates[-1]
    for sym, pos in open_pos.items():
        key = (sym, last)
        if key in price_lkp:
            ep  = price_lkp[key]["close"]
            pnl = round((ep - pos["entry"]) * pos["qty"], 2)
            trades.append({"symbol": sym, "entry_date": pos["entry_date"],
                           "exit_date": last, "entry": pos["entry"],
                           "exit": ep, "qty": pos["qty"], "pnl": pnl,
                           "pnl_pct": round((ep-pos["entry"])/pos["entry"]*100, 2),
                           "reason": "open at end"})
    return trades


# ── 3. Run all variants ───────────────────────────────────────────────────────
results = {}
for label, cfg in VARIANTS.items():
    print(f"Running {label}…", flush=True)
    results[label] = simulate(cfg)


# ── 4. Summary table ──────────────────────────────────────────────────────────
def stats(trades):
    if not trades: return {}
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins)/len(trades)*100
    aw     = sum(t["pnl_pct"] for t in wins)/len(wins) if wins else 0
    al     = sum(t["pnl_pct"] for t in losses)/len(losses) if losses else 0
    pf     = abs(sum(t["pnl"] for t in wins)/sum(t["pnl"] for t in losses)) if losses else 999
    stops  = sum(1 for t in trades if "stop" in t["reason"])
    return dict(n=len(trades), wins=len(wins), losses=len(losses),
                win_rate=wr, total_pnl=total, avg_win=aw,
                avg_loss=al, profit_factor=pf, stops=stops)

print()
print("=" * 90)
hdr = f"{'Variant':<32} {'Trades':>7} {'WinRate':>8} {'TotalP&L':>10} {'AvgWin%':>9} {'AvgLoss%':>9} {'PF':>6} {'Stops%':>8}"
print(hdr)
print("=" * 90)
for label, trades in results.items():
    s = stats(trades)
    stop_pct = s["stops"] / s["n"] * 100 if s["n"] else 0
    print(f"  {label:<30} {s['n']:>7} {s['win_rate']:>7.1f}% "
          f"{s['total_pnl']:>+10.2f} {s['avg_win']:>+9.1f}% "
          f"{s['avg_loss']:>+9.1f}% {s['profit_factor']:>6.2f} "
          f"{stop_pct:>7.1f}%")

# ── 5. Cumulative P&L by date ─────────────────────────────────────────────────
print()
print("=" * 90)
col_w = 14
header = f"{'Date':<12}" + "".join(f"{'cum':>{col_w}}" for _ in VARIANTS)
labels_short = ["BASELINE", "V1-score7", "V2-+ROC", "V3-+dualRS"]
print(f"{'Date':<12}" + "".join(f"{l:>{col_w}}" for l in labels_short))
print("=" * 90)

by_date = {}
for label, trades in results.items():
    d = defaultdict(float)
    for t in trades:
        d[str(t["exit_date"])[:10]] += t["pnl"]
    by_date[label] = d

all_dates = sorted(set(d for bd in by_date.values() for d in bd))
cums = {label: 0.0 for label in results}
for d in all_dates:
    for label in results:
        cums[label] += by_date[label].get(d, 0)
    row = f"  {d:<10}" + "".join(f"{cums[label]:>+{col_w}.2f}" for label in results)
    print(row)

print("=" * 90)
print(f"  {'FINAL':<10}" + "".join(
    f"{sum(t['pnl'] for t in trades):>+{col_w}.2f}"
    for trades in results.values()))
