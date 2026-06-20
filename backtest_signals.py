"""
backtest_signals.py — test two signal criterion improvements side by side.

All variants apply current production controls (dedup + max 15 concurrent positions).

  BASELINE   : current scoring (macd_bull = hist>0, ema9_above_ema21)
  MACD-fix   : macd_bull = hist>0 AND NOT histogram_shrinking  (removes fading MACD signals)
  EMA-cross  : replace ema9_above_ema21 with fresh cross within 3 bars
  BOTH       : MACD-fix + EMA-cross combined

Score adjustment logic (derived from existing cache — no full recompute needed):
  MACD fix  : macd_shrink=True stocks currently get 1pt for macd_bull (histogram>0 & shrinking).
              With fix, that point is removed → adjusted_score = score - macd_shrink
  EMA cross : ema9_above_ema21=True contributes 1pt. Replace with ema_cross flag.
              adjusted_score = score - ema9_above_ema21 + ema_cross
              (ema_cross=True implies ema9_above_ema21=True, so this can only reduce scores)
"""

import math, sqlite3, time
from collections import defaultdict
from pathlib import Path

import pandas as pd

CACHE_PATH     = Path("/tmp/bt_signals.pkl")
EMA_CACHE_PATH = Path("/tmp/bt_ema_cross.pkl")
DB_PATH        = "data/momentum.db"

BACKTEST_START   = "2026-05-12"
BACKTEST_END     = "2026-06-17"
ADX_THRESHOLD    = 30
MAX_HOLD_DAYS    = 10
AUTO_ORDER_TOP_N = 10
STOP_PCT         = 0.05
POS_THRESHOLD    = 50
MAX_CONCURRENT   = 15  # current production cap


def position_qty(price):
    return max(1, math.floor((750 if price < POS_THRESHOLD else 250) / price))


# ── 1. Load cached signals ────────────────────────────────────────────────────
print("Loading cached signals…", flush=True)
sdf = pd.read_pickle(CACHE_PATH)
sdf["date"] = pd.to_datetime(sdf["date"])
symbols_in_cache = set(sdf["symbol"].unique())
print(f"  {len(sdf):,} rows | {len(symbols_in_cache):,} symbols", flush=True)


# ── 2. Compute / load EMA crossover ──────────────────────────────────────────
if EMA_CACHE_PATH.exists():
    print("Loading EMA crossover cache…", flush=True)
    ema_df = pd.read_pickle(EMA_CACHE_PATH)
    print(f"  {len(ema_df):,} rows loaded", flush=True)
else:
    print("Computing EMA crossover from bars (close prices only)…", flush=True)
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    raw = pd.read_sql(
        "SELECT symbol, date, close FROM bars ORDER BY symbol, date",
        conn, parse_dates=["date"]
    )
    conn.close()
    raw = raw[raw["symbol"].isin(symbols_in_cache)]
    print(f"  {len(raw):,} rows | {raw['symbol'].nunique():,} symbols | {time.time()-t0:.1f}s")

    parts = []
    total = raw["symbol"].nunique()
    done  = 0
    for symbol, grp in raw.groupby("symbol"):
        close = grp.set_index("date")["close"].sort_index()
        ema9  = close.ewm(span=9,  adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        above = ema9 > ema21
        # Cross detected if EMA9 transitions from ≤EMA21 to >EMA21 within the last 3 bars
        # Matches trend.py logic: checks yesterday's cross + 2 bars back + 3 bars back
        cross = (
            (~above.shift(1).fillna(True)  & above) |                           # crossed today
            (~above.shift(2).fillna(True)  & above.shift(1).fillna(False)) |   # crossed 1 bar ago
            (~above.shift(3).fillna(True)  & above.shift(2).fillna(False))     # crossed 2 bars ago
        )
        parts.append(pd.DataFrame({
            "symbol":           symbol,
            "date":             close.index,
            "ema9_above_ema21": above.values,
            "ema_cross":        cross.values,
        }))
        done += 1
        if done % 500 == 0:
            print(f"  {done}/{total} symbols | {time.time()-t0:.0f}s", flush=True)

    ema_df = pd.concat(parts, ignore_index=True)
    ema_df["date"] = pd.to_datetime(ema_df["date"])
    ema_df.to_pickle(EMA_CACHE_PATH)
    print(f"  Done in {time.time()-t0:.1f}s | {len(ema_df):,} rows cached", flush=True)


# ── 3. Merge EMA features and compute adjusted scores ────────────────────────
print("Merging EMA features…", flush=True)
sdf = sdf.merge(
    ema_df[["symbol", "date", "ema9_above_ema21", "ema_cross"]],
    on=["symbol", "date"], how="left"
)
sdf["ema9_above_ema21"] = sdf["ema9_above_ema21"].fillna(False)
sdf["ema_cross"]        = sdf["ema_cross"].fillna(False)

# MACD fix: stocks with macd_shrink=True lose their macd_bull point
sdf["score_macd"] = sdf["score"] - sdf["macd_shrink"].astype(int)

# EMA crossover: swap ema9_above_ema21 for ema_cross (can only reduce scores)
sdf["score_ema"]  = (sdf["score"]
                     - sdf["ema9_above_ema21"].astype(int)
                     + sdf["ema_cross"].astype(int))

# Both fixes combined
sdf["score_both"] = (sdf["score"]
                     - sdf["macd_shrink"].astype(int)
                     - sdf["ema9_above_ema21"].astype(int)
                     + sdf["ema_cross"].astype(int))

# Quick diagnostic: how many signals change score?
bt = sdf[(sdf["date"] >= BACKTEST_START) & (sdf["date"] <= BACKTEST_END)]
macd_affected = (bt["macd_shrink"] & (bt["score"] >= 6)).sum()
ema_affected  = (bt["ema9_above_ema21"] & ~bt["ema_cross"] & (bt["score"] >= 6)).sum()
print(f"  Signals affected by MACD fix (score drops 1pt): {macd_affected:,}")
print(f"  Signals affected by EMA cross fix (score drops 1pt): {ema_affected:,}")


# ── 4. Backtest engine ────────────────────────────────────────────────────────
bt_dates = (
    sdf[(sdf["date"] >= BACKTEST_START) & (sdf["date"] <= BACKTEST_END)]
    ["date"].sort_values().unique()
)
print(f"\nBacktest: {str(bt_dates[0])[:10]} → {str(bt_dates[-1])[:10]} ({len(bt_dates)} days)\n")

price_lkp = sdf.set_index(["symbol", "date"])[
    ["open", "high", "low", "close", "rsi", "atr"]].to_dict("index")


def simulate(score_col: str) -> list[dict]:
    open_pos = {}
    trades   = []

    for i, signal_date in enumerate(bt_dates):
        # ── Exit open positions ──────────────────────────────────────────────
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
                trades.append({
                    "symbol": sym, "entry_date": pos["entry_date"],
                    "exit_date": signal_date, "entry": entry,
                    "exit": exit_price, "qty": qty, "pnl": pnl,
                    "pnl_pct": round((exit_price - entry) / entry * 100, 2),
                    "reason": exit_reason,
                })
                del open_pos[sym]

        if i + 1 >= len(bt_dates):
            continue
        entry_date = bt_dates[i + 1]

        # ── Select candidates ────────────────────────────────────────────────
        day = sdf[sdf["date"] == signal_date]
        cands = day[
            (day[score_col] >= 6) &
            (day["adx"] > ADX_THRESHOLD) &
            (~day["vol_drying"]) &
            (~day["macd_shrink"]) &
            (day["exit_mode"] == "trailing_stop")
        ].sort_values(["rs_return", "adx", "vol_ratio"], ascending=False)

        placed = 0
        for _, c in cands.iterrows():
            if placed >= AUTO_ORDER_TOP_N:
                break
            if len(open_pos) >= MAX_CONCURRENT:
                break
            sym = c["symbol"]
            if sym in open_pos:
                continue
            key = (sym, entry_date)
            if key not in price_lkp:
                continue
            ep = price_lkp[key]["open"]
            if ep <= 0:
                continue
            atr_min = c["atr"] * 1.5
            stop    = min(ep - atr_min, ep * (1 - STOP_PCT))
            stop    = min(stop, ep - 0.01)
            open_pos[sym] = {
                "entry": ep, "stop": stop,
                "qty": position_qty(ep), "entry_date": entry_date,
            }
            placed += 1

    last = bt_dates[-1]
    for sym, pos in open_pos.items():
        key = (sym, last)
        if key in price_lkp:
            ep  = price_lkp[key]["close"]
            pnl = round((ep - pos["entry"]) * pos["qty"], 2)
            trades.append({
                "symbol": sym, "entry_date": pos["entry_date"],
                "exit_date": last, "entry": pos["entry"],
                "exit": ep, "qty": pos["qty"], "pnl": pnl,
                "pnl_pct": round((ep - pos["entry"]) / pos["entry"] * 100, 2),
                "reason": "open at end",
            })
    return trades


# ── 5. Run all variants ───────────────────────────────────────────────────────
VARIANTS = {
    "BASELINE (current)": "score",
    "MACD-fix":           "score_macd",
    "EMA-crossover":      "score_ema",
    "MACD-fix + EMA-X":   "score_both",
}

results = {}
for label, score_col in VARIANTS.items():
    print(f"Running {label}…", flush=True)
    results[label] = simulate(score_col)


# ── 6. Summary table ──────────────────────────────────────────────────────────
def stats(trades):
    if not trades:
        return {}
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins) / len(trades) * 100
    aw     = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    al     = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    pf     = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses else 999
    stops  = sum(1 for t in trades if "stop" in t["reason"])
    return dict(n=len(trades), wins=len(wins), losses=len(losses),
                win_rate=wr, total_pnl=total, avg_win=aw,
                avg_loss=al, profit_factor=pf, stops=stops)

print()
print("=" * 100)
print(f"  {'Variant':<24} {'Trades':>7} {'WinRate':>8} {'TotalP&L':>11} {'AvgWin%':>9} {'AvgLoss%':>9} {'PF':>6} {'Stops%':>8}")
print("=" * 100)
for label, trades in results.items():
    s = stats(trades)
    if not s:
        print(f"  {label:<24}  (no trades)")
        continue
    stop_pct = s["stops"] / s["n"] * 100 if s["n"] else 0
    print(f"  {label:<24} {s['n']:>7} {s['win_rate']:>7.1f}% "
          f"{s['total_pnl']:>+11.2f} {s['avg_win']:>+9.1f}% "
          f"{s['avg_loss']:>+9.1f}% {s['profit_factor']:>6.2f} "
          f"{stop_pct:>7.1f}%")


# ── 7. Cumulative P&L by exit date ────────────────────────────────────────────
col_w = 14
labels_short = ["BASELINE", "MACD-fix", "EMA-cross", "BOTH"]
print()
print("=" * (14 + col_w * len(VARIANTS)))
print(f"  {'Date':<10}" + "".join(f"{l:>{col_w}}" for l in labels_short))
print("=" * (14 + col_w * len(VARIANTS)))

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

print("=" * (14 + col_w * len(VARIANTS)))
print(f"  {'FINAL':<10}" + "".join(
    f"{sum(t['pnl'] for t in trades):>+{col_w}.2f}"
    for trades in results.values()))
