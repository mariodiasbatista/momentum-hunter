"""
backtest_controls.py — simulate position correlation controls.

Variants (all use same scoring/exit logic as baseline):
  BASELINE : current system — no dedup, no position cap, no sector cap
  V1       : no duplicate (block symbol if already open)
  V2       : no duplicate + max 15 concurrent positions
  V3       : no duplicate + max 10 concurrent positions
  V4       : no duplicate + max 15 positions + max 3 per sector
  V5       : no duplicate + max 10 positions + max 3 per sector
"""

import math, time, json
from collections import defaultdict
from pathlib import Path

import pandas as pd

CACHE_PATH   = Path("/tmp/bt_signals.pkl")
SECTOR_CACHE = Path("/tmp/sector_cache.json")

BACKTEST_START   = "2026-05-12"
BACKTEST_END     = "2026-06-17"
ADX_THRESHOLD    = 30
MAX_HOLD_DAYS    = 10
AUTO_ORDER_TOP_N = 10
STOP_PCT         = 0.05
POS_THRESHOLD    = 50


def position_qty(price):
    return max(1, math.floor((750 if price < POS_THRESHOLD else 250) / price))


print("Loading cached signals…", flush=True)
sdf = pd.read_pickle(CACHE_PATH)
sdf["date"] = pd.to_datetime(sdf["date"])

sector_map = json.loads(SECTOR_CACHE.read_text()) if SECTOR_CACHE.exists() else {}
print(f"  {len(sdf):,} signal rows | {len(sector_map)} symbols with sector data")

bt_dates = sdf[
    (sdf["date"] >= BACKTEST_START) & (sdf["date"] <= BACKTEST_END)
]["date"].sort_values().unique()
print(f"  Backtest: {str(bt_dates[0])[:10]} → {str(bt_dates[-1])[:10]} ({len(bt_dates)} days)\n")

price_lkp = sdf.set_index(["symbol", "date"])[
    ["open", "high", "low", "close", "rsi", "atr"]].to_dict("index")

VARIANTS = {
    "BASELINE (current)":           {"dedup": False, "max_pos": None, "max_sector": None},
    "V1  no-duplicate":             {"dedup": True,  "max_pos": None, "max_sector": None},
    "V2  dedup + max15":            {"dedup": True,  "max_pos": 15,   "max_sector": None},
    "V3  dedup + max10":            {"dedup": True,  "max_pos": 10,   "max_sector": None},
    "V4  dedup + max15 + sector3":  {"dedup": True,  "max_pos": 15,   "max_sector": 3},
    "V5  dedup + max10 + sector3":  {"dedup": True,  "max_pos": 10,   "max_sector": 3},
}


def simulate(cfg: dict) -> list[dict]:
    open_pos = {}   # sym -> {entry, stop, qty, entry_date, sector}
    trades   = []

    for i, signal_date in enumerate(bt_dates):
        # ── Exit open positions ───────────────────────────────────────────────
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

        # ── Select candidates ─────────────────────────────────────────────────
        day = sdf[sdf["date"] == signal_date]
        cands = day[
            (day["score"] >= 6) &
            (day["adx"] > ADX_THRESHOLD) &
            (~day["vol_drying"]) &
            (~day["macd_shrink"]) &
            (day["exit_mode"] == "trailing_stop")
        ].sort_values(["rs_return", "adx", "vol_ratio"], ascending=False)

        # ── Current sector counts (for sector cap) ────────────────────────────
        sector_counts = defaultdict(int)
        for pos in open_pos.values():
            sector_counts[pos.get("sector", "Unknown")] += 1

        placed = 0
        for _, c in cands.iterrows():
            if placed >= AUTO_ORDER_TOP_N:
                break

            sym = c["symbol"]

            # Dedup: skip if already open
            if cfg["dedup"] and sym in open_pos:
                continue

            # Baseline: skip if open (original same-day dedup only)
            if not cfg["dedup"] and sym in open_pos:
                continue

            # Max concurrent positions cap
            if cfg["max_pos"] is not None and len(open_pos) >= cfg["max_pos"]:
                break

            key = (sym, entry_date)
            if key not in price_lkp:
                continue
            ep = price_lkp[key]["open"]
            if ep <= 0:
                continue

            sec = sector_map.get(sym, "Unknown")

            # Per-sector cap
            if cfg["max_sector"] is not None and sector_counts[sec] >= cfg["max_sector"]:
                continue

            atr_min = c["atr"] * 1.5
            stop    = min(ep - atr_min, ep * (1 - STOP_PCT))
            stop    = min(stop, ep - 0.01)

            open_pos[sym] = {
                "entry": ep, "stop": stop,
                "qty": position_qty(ep),
                "entry_date": entry_date,
                "sector": sec,
            }
            sector_counts[sec] += 1
            placed += 1

    # Force-close remaining open positions at last bar
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


# ── Run all variants ──────────────────────────────────────────────────────────
results = {}
for label, cfg in VARIANTS.items():
    print(f"Running {label}…", flush=True)
    results[label] = simulate(cfg)


# ── Summary table ─────────────────────────────────────────────────────────────
def stats(trades):
    if not trades: return {}
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins) / len(trades) * 100
    aw     = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    al     = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    pf     = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses else 999
    stops  = sum(1 for t in trades if "stop" in t["reason"])
    max_open = 0  # can't track this here easily
    return dict(n=len(trades), wins=len(wins), losses=len(losses),
                win_rate=wr, total_pnl=total, avg_win=aw,
                avg_loss=al, profit_factor=pf, stops=stops)

print()
print("=" * 100)
print(f"  {'Variant':<32} {'Trades':>7} {'WinRate':>8} {'TotalP&L':>11} {'AvgWin%':>9} {'AvgLoss%':>9} {'PF':>6} {'Stops%':>8}")
print("=" * 100)
for label, trades in results.items():
    s = stats(trades)
    stop_pct = s["stops"] / s["n"] * 100 if s["n"] else 0
    print(f"  {label:<32} {s['n']:>7} {s['win_rate']:>7.1f}% "
          f"{s['total_pnl']:>+11.2f} {s['avg_win']:>+9.1f}% "
          f"{s['avg_loss']:>+9.1f}% {s['profit_factor']:>6.2f} "
          f"{stop_pct:>7.1f}%")

# ── Cumulative P&L by exit date ───────────────────────────────────────────────
print()
labels_short = ["BASELINE", "V1-dedup", "V2-max15", "V3-max10", "V4-s3+15", "V5-s3+10"]
col_w = 12
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

# ── Max concurrent open positions per variant (re-simulate just to track) ─────
print("\n── Peak concurrent open positions per variant ──")
for label, cfg in VARIANTS.items():
    open_pos = {}
    peak = 0
    for i, signal_date in enumerate(bt_dates):
        for sym in list(open_pos.keys()):
            key = (sym, signal_date)
            if key not in price_lkp:
                continue
            bar = price_lkp[key]
            entry = open_pos[sym]["entry"]
            stop  = open_pos[sym]["stop"]
            days  = (signal_date - open_pos[sym]["entry_date"]).days
            done  = (bar["open"] <= stop or bar["low"] <= stop or
                     bar["rsi"] > 70 or
                     ((bar["close"] - entry) / entry >= 0.08 and bar["rsi"] < 50) or
                     days >= MAX_HOLD_DAYS)
            if done:
                del open_pos[sym]
        if i + 1 >= len(bt_dates):
            continue
        entry_date = bt_dates[i + 1]
        day = sdf[sdf["date"] == signal_date]
        cands = day[
            (day["score"] >= 6) & (day["adx"] > ADX_THRESHOLD) &
            (~day["vol_drying"]) & (~day["macd_shrink"]) &
            (day["exit_mode"] == "trailing_stop")
        ].sort_values(["rs_return", "adx", "vol_ratio"], ascending=False)
        sector_counts = defaultdict(int)
        for pos in open_pos.values():
            sector_counts[pos.get("sector", "Unknown")] += 1
        placed = 0
        for _, c in cands.iterrows():
            if placed >= AUTO_ORDER_TOP_N: break
            sym = c["symbol"]
            if cfg["dedup"] and sym in open_pos: continue
            if not cfg["dedup"] and sym in open_pos: continue
            if cfg["max_pos"] is not None and len(open_pos) >= cfg["max_pos"]: break
            key = (sym, entry_date)
            if key not in price_lkp: continue
            ep = price_lkp[key]["open"]
            if ep <= 0: continue
            sec = sector_map.get(sym, "Unknown")
            if cfg["max_sector"] is not None and sector_counts[sec] >= cfg["max_sector"]: continue
            atr_min = c["atr"] * 1.5
            stop = min(ep - atr_min, ep * (1 - STOP_PCT))
            stop = min(stop, ep - 0.01)
            open_pos[sym] = {"entry": ep, "stop": stop,
                             "qty": position_qty(ep), "entry_date": entry_date, "sector": sec}
            sector_counts[sec] += 1
            placed += 1
        peak = max(peak, len(open_pos))
    print(f"  {label:<35} peak={peak}")
