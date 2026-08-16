"""
backtest_pool_sort.py — compare pre-market pool size and sort order.

Tests whether prioritising trailing_stop candidates first in the pre-market
pool (before fixed_take_profit) improves P&L vs. the current score+RS sort.

Variants:
  A: top 30, current sort (score DESC, rs_return DESC)
  B: top 30, trailing_stop first
  C: top 100, current sort  ← current production
  D: top 100, trailing_stop first  ← proposed change
  E: no pool restriction  ← theoretical maximum

Baseline matches current production strategy filters.
"""

import math
from collections import defaultdict
from pathlib import Path

import pandas as pd

CACHE_PATH = Path("/tmp/bt_signals.pkl")

ADX_THRESHOLD    = 30
AUTO_ORDER_TOP_N = 10
MAX_CONCURRENT   = 15
RSI_EXIT         = 65
MAX_HOLD         = 7
STOP_PCT         = 0.05
POS_THRESHOLD    = 50


def position_qty(price):
    return max(1, math.floor((750 if price < POS_THRESHOLD else 250) / price))


# ── Load signals ──────────────────────────────────────────────────────────────
print("Loading cached signals…", flush=True)
sdf = pd.read_pickle(CACHE_PATH)
sdf["date"] = pd.to_datetime(sdf["date"])
all_dates = sorted(sdf["date"].unique())
print(f"  {len(sdf):,} rows | {sdf['symbol'].nunique():,} symbols")
print(f"  Range: {str(all_dates[0])[:10]} → {str(all_dates[-1])[:10]} ({len(all_dates)} trading days)")

# days_in_scan≥2
date_to_idx = {d: i for i, d in enumerate(all_dates)}
elig = sdf[sdf["score"] >= 6][["symbol", "date"]].copy()
elig["prev_date"] = elig["date"].map(
    lambda d: all_dates[date_to_idx[d] - 1] if date_to_idx[d] > 0 else None)
elig = elig.dropna(subset=["prev_date"])
prev_elig = sdf[sdf["score"] >= 6][["symbol", "date"]].rename(columns={"date": "prev_date"})
prev_elig["in_prev_scan"] = True
elig = elig.merge(prev_elig, on=["symbol", "prev_date"], how="left")
elig["in_prev_scan"] = elig["in_prev_scan"].fillna(False)
sdf = sdf.merge(elig[["symbol", "date", "in_prev_scan"]], on=["symbol", "date"], how="left")
sdf["in_prev_scan"] = sdf["in_prev_scan"].fillna(False)

price_lkp = sdf.set_index(["symbol", "date"])[
    ["open", "high", "low", "close", "rsi", "atr"]
].to_dict("index")


# ── Simulation engine ─────────────────────────────────────────────────────────
def simulate(pool_n: int | None, trailing_first: bool) -> tuple[list[dict], int]:
    """
    pool_n: max candidates per day passed to order filter (None = unlimited)
    trailing_first: sort trailing_stop candidates before fixed_take_profit
    Returns (trades, total_orders_placed)
    """
    open_pos = {}
    trades   = []
    total_placed = 0

    for i, signal_date in enumerate(all_dates):
        next_date = all_dates[i + 1] if i + 1 < len(all_dates) else None

        # Exit logic
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
            elif bar["rsi"] > RSI_EXIT:
                exit_price, exit_reason = bar["close"], f"RSI > {RSI_EXIT}"
            elif (bar["close"] - entry) / entry >= 0.08 and bar["rsi"] < 50:
                exit_price, exit_reason = bar["close"], "gain≥8%+RSI<50"
            elif days >= MAX_HOLD:
                exit_price, exit_reason = bar["close"], "max hold"

            if exit_price is not None:
                pnl = round((exit_price - entry) * qty, 2)
                trades.append({
                    "symbol": sym, "entry_date": pos["entry_date"],
                    "exit_date": signal_date, "entry": entry, "exit": exit_price,
                    "qty": qty, "pnl": pnl,
                    "pnl_pct": round((exit_price - entry) / entry * 100, 2),
                    "reason": exit_reason,
                })
                del open_pos[sym]

        if next_date is None or len(open_pos) >= MAX_CONCURRENT:
            continue

        day  = sdf[sdf["date"] == signal_date]
        mask = (
            (day["score"]     >= 6) &
            (day["adx"]        > ADX_THRESHOLD) &
            (~day["vol_drying"]) &
            (~day["macd_shrink"]) &
            (day["exit_mode"] == "trailing_stop") &
            (day["in_prev_scan"])
        )
        # Full candidate list sorted by score DESC, rs_return DESC
        all_cands = day[day["score"] >= 6].sort_values(
            ["score", "rs_return", "adx", "vol_ratio"], ascending=False
        )

        # Apply pool restriction (simulate pre-market top-N)
        if pool_n is not None:
            if trailing_first:
                ts   = all_cands[all_cands["exit_mode"] == "trailing_stop"]
                fp   = all_cands[all_cands["exit_mode"] != "trailing_stop"]
                pool = pd.concat([ts, fp]).head(pool_n)
            else:
                pool = all_cands.head(pool_n)
        else:
            pool = all_cands

        # Apply order filters to pool
        cands = pool[mask.reindex(pool.index, fill_value=False)]

        placed = 0
        for _, c in cands.iterrows():
            if placed >= AUTO_ORDER_TOP_N or len(open_pos) >= MAX_CONCURRENT:
                break
            sym = c["symbol"]
            if sym in open_pos:
                continue
            key = (sym, next_date)
            if key not in price_lkp:
                continue
            ep = price_lkp[key]["open"]
            if ep <= 0:
                continue

            stop = min(ep - c["atr"] * 1.5, ep * (1 - STOP_PCT), ep - 0.01)
            open_pos[sym] = {"entry": ep, "stop": stop,
                             "qty": position_qty(ep), "entry_date": next_date}
            placed += 1
            total_placed += 1

    # Close open positions at end
    last = all_dates[-1]
    for sym, pos in open_pos.items():
        key = (sym, last)
        if key in price_lkp:
            ep  = price_lkp[key]["close"]
            pnl = round((ep - pos["entry"]) * pos["qty"], 2)
            trades.append({
                "symbol": sym, "entry_date": pos["entry_date"], "exit_date": last,
                "entry": pos["entry"], "exit": ep, "qty": pos["qty"], "pnl": pnl,
                "pnl_pct": round((ep - pos["entry"]) / pos["entry"] * 100, 2),
                "reason": "open at end",
            })
    return trades, total_placed


def stats(trades):
    if not trades:
        return {}
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    total  = sum(t["pnl"] for t in trades)
    pf     = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses else 999
    by_day = defaultdict(float)
    for t in trades:
        by_day[str(t["exit_date"])[:10]] += t["pnl"]
    cum = peak = max_dd = 0.0
    for d in sorted(by_day):
        cum  += by_day[d]
        peak  = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return dict(
        n=len(trades), wins=len(wins), losses=len(losses),
        win_rate=len(wins) / len(trades) * 100,
        total_pnl=total,
        avg_win=sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0,
        avg_loss=sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0,
        profit_factor=pf,
        max_drawdown=max_dd,
        stops=sum(1 for t in trades if "stop" in t["reason"]),
    )


# ── Run variants ──────────────────────────────────────────────────────────────
variants = [
    ("A: top-30  current sort",  30,   False),
    ("B: top-30  TS-first",      30,   True),
    ("C: top-100 current sort",  100,  False),
    ("D: top-100 TS-first",      100,  True),
    ("E: no pool limit",         None, False),
]

print("\nRunning simulations…", flush=True)
results = {}
order_counts = {}
for label, pool_n, ts_first in variants:
    trades, n_placed = simulate(pool_n, ts_first)
    results[label] = trades
    order_counts[label] = n_placed
    print(f"  {label:<28} → {len(trades):>3} trades  ({n_placed} entries)", flush=True)

S = {k: stats(v) for k, v in results.items()}
labels = [v[0] for v in variants]
base = S["A: top-30  current sort"]

# ── Results table ─────────────────────────────────────────────────────────────
W = 16
print()
sep = "=" * (28 + W * len(labels))
print(sep)
print(f"  {'Metric':<26}" + "".join(f"{l:>{W}}" for l in labels))
print(sep)

def row(title, key, fmt="{:.1f}", suffix="", hb=True):
    bv = base.get(key, 0)
    line = f"  {title:<26}"
    for i, lbl in enumerate(labels):
        v = S[lbl].get(key, 0)
        s = fmt.format(v) + suffix
        if i > 0 and abs(v - bv) > 1e-6:
            better = (v > bv) if hb else (v < bv)
            s += " ▲" if better else " ▼"
        line += f"{s:>{W}}"
    print(line)

row("Trades",        "n",             "{:.0f}")
row("Entries placed","n",             "{:.0f}")
row("Win rate",      "win_rate",      "{:.1f}", "%")
row("Total P&L",     "total_pnl",     "${:+.2f}", "")
row("Avg win",       "avg_win",       "{:+.1f}", "%")
row("Avg loss",      "avg_loss",      "{:+.1f}", "%", False)
row("Profit factor", "profit_factor", "{:.2f}")
row("Max drawdown",  "max_drawdown",  "${:.2f}", "", False)
row("Stop-outs",     "stops",         "{:.0f}",  "", False)
print(sep)

entries_line = f"  {'Total entries':<26}"
for lbl in labels:
    entries_line += f"{order_counts[lbl]:>{W}}"
print(entries_line)
print(sep)

# ── Delta vs A ────────────────────────────────────────────────────────────────
print()
print(f"── Delta vs A (top-30 current sort) {'─' * max(0, W * len(labels) - 20)}")
for title, key, fmt, hb in [
    ("P&L Δ",   "total_pnl",     "${:+.2f}", True),
    ("WR Δ",    "win_rate",      "{:+.1f}%", True),
    ("PF Δ",    "profit_factor", "{:+.2f}",  True),
    ("MaxDD Δ", "max_drawdown",  "${:+.2f}", False),
]:
    bv  = base.get(key, 0)
    line = f"  {title:<10}"
    for i, lbl in enumerate(labels):
        v   = S[lbl].get(key, 0)
        raw = v - bv
        if key == "max_drawdown":
            raw = bv - v
        s = "—" if i == 0 else fmt.format(raw) + (" ▲" if raw > 0 else " ▼" if raw < 0 else "")
        line += f"{s:>{W}}"
    print(line)

# ── Conclusion ────────────────────────────────────────────────────────────────
print()
print(sep)
print("  CONCLUSION — ranked by Total P&L")
print(sep)
ranked = sorted([(lbl, S[lbl]) for lbl in labels], key=lambda x: x[1]["total_pnl"], reverse=True)
base_pnl = base["total_pnl"]
base_pf  = base["profit_factor"]
print(f"  {'Rank':<5} {'Variant':<30} {'P&L':>10} {'PF':>7} {'WR':>8} {'MaxDD':>10}  {'Entries':>8}  {'Verdict'}")
print("  " + "-" * 88)
for rank, (lbl, s) in enumerate(ranked, 1):
    if lbl == "A: top-30  current sort":
        verdict = "← baseline"
    elif s["total_pnl"] > base_pnl and s["profit_factor"] > base_pf:
        verdict = "BETTER ✓"
    elif s["total_pnl"] > base_pnl or s["profit_factor"] > base_pf:
        verdict = "MIXED"
    else:
        verdict = "WORSE ✗"
    print(f"  {rank:<5} {lbl:<30} {s['total_pnl']:>+10.2f} {s['profit_factor']:>7.2f} "
          f"{s['win_rate']:>7.1f}% {s['max_drawdown']:>10.2f}  {order_counts[lbl]:>8}   {verdict}")
print(sep)
