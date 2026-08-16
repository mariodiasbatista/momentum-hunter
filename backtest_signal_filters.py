"""
backtest_signal_filters.py — test 3 entry signal improvements vs current strategy.

BASELINE    : current production (days_in_scan≥2, RSI>65 exit, max hold 7 days)
+RS-DUAL    : also require dual_rs (21-day AND 63-day RS both positive vs SPY)
+ROC        : also require roc_pass (close up ≥5% over 20 days)
+GAP-FILTER : skip entry if stock gaps up >4% from prior close at open
+ALL-3      : all three filters combined

Full dataset: same 121-day window as prior backtests.
"""

import math
from collections import defaultdict
from pathlib import Path

import pandas as pd

CACHE_PATH = Path("/tmp/bt_signals.pkl")

ADX_THRESHOLD    = 30
AUTO_ORDER_TOP_N = 10
STOP_PCT         = 0.05
POS_THRESHOLD    = 50
MAX_CONCURRENT   = 15
RSI_EXIT         = 65
MAX_HOLD         = 7
GAP_THRESHOLD    = 0.04   # skip if open > prior_close * (1 + this)


def position_qty(price):
    return max(1, math.floor((750 if price < POS_THRESHOLD else 250) / price))


# ── Load cache ────────────────────────────────────────────────────────────────
print("Loading cached signals…", flush=True)
sdf = pd.read_pickle(CACHE_PATH)
sdf["date"] = pd.to_datetime(sdf["date"])
all_dates = sorted(sdf["date"].unique())
print(f"  {len(sdf):,} rows | {sdf['symbol'].nunique():,} symbols")
print(f"  Range: {str(all_dates[0])[:10]} → {str(all_dates[-1])[:10]} ({len(all_dates)} trading days)")


# ── days_in_scan≥2 flag ───────────────────────────────────────────────────────
print("Computing days_in_scan≥2 flag…", flush=True)
date_to_idx = {d: i for i, d in enumerate(all_dates)}
elig = sdf[sdf["score"] >= 6][["symbol", "date"]].copy()
elig["prev_date"] = elig["date"].map(
    lambda d: all_dates[date_to_idx[d] - 1] if date_to_idx[d] > 0 else None
)
elig = elig.dropna(subset=["prev_date"])
prev_elig = sdf[sdf["score"] >= 6][["symbol", "date"]].rename(columns={"date": "prev_date"})
prev_elig["in_prev_scan"] = True
elig = elig.merge(prev_elig, on=["symbol", "prev_date"], how="left")
elig["in_prev_scan"] = elig["in_prev_scan"].fillna(False)
sdf = sdf.merge(elig[["symbol", "date", "in_prev_scan"]], on=["symbol", "date"], how="left")
sdf["in_prev_scan"] = sdf["in_prev_scan"].fillna(False)


# ── Prior-close gap computation ───────────────────────────────────────────────
# gap_up = True if next-day open > today's close * (1 + GAP_THRESHOLD)
# We store it on the signal date so the simulation can apply it at entry.
print("Computing gap-up flag…", flush=True)
close_lkp = sdf.set_index(["symbol", "date"])["close"].to_dict()

def get_gap_up(row):
    prev_idx = date_to_idx.get(row["date"])
    if prev_idx is None or prev_idx + 1 >= len(all_dates):
        return False
    next_date = all_dates[prev_idx + 1]
    next_open  = sdf.loc[(sdf["symbol"] == row["symbol"]) & (sdf["date"] == next_date), "open"]
    if next_open.empty:
        return False
    return float(next_open.iloc[0]) > row["close"] * (1 + GAP_THRESHOLD)

# Vectorised approach: shift close by 1 day per symbol, compare to next open
sdf = sdf.sort_values(["symbol", "date"])
sdf["prior_close"] = sdf.groupby("symbol")["close"].shift(1)
sdf["next_open"]   = sdf.groupby("symbol")["open"].shift(-1)
sdf["gap_up"]      = sdf["next_open"] > sdf["prior_close"].shift(-1) * (1 + GAP_THRESHOLD)
sdf["gap_up"]      = sdf["gap_up"].fillna(False)


# ── Price lookup ──────────────────────────────────────────────────────────────
price_lkp = sdf.set_index(["symbol", "date"])[
    ["open", "high", "low", "close", "rsi", "atr"]
].to_dict("index")


# ── Base candidate mask builder ───────────────────────────────────────────────
def base_mask(day):
    return (
        (day["score"]     >= 6) &
        (day["adx"]        > ADX_THRESHOLD) &
        (~day["vol_drying"]) &
        (~day["macd_shrink"]) &
        (day["exit_mode"] == "trailing_stop") &
        (day["in_prev_scan"])
    )


# ── Simulation engine ─────────────────────────────────────────────────────────
def simulate(label: str, use_dual_rs=False, use_roc=False, use_gap_filter=False):
    open_pos = {}
    trades   = []

    for i, signal_date in enumerate(all_dates):
        next_date = all_dates[i + 1] if i + 1 < len(all_dates) else None

        # Exit open positions
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
                    "symbol":     sym,
                    "entry_date": pos["entry_date"],
                    "exit_date":  signal_date,
                    "entry":      entry,
                    "exit":       exit_price,
                    "qty":        qty,
                    "pnl":        pnl,
                    "pnl_pct":    round((exit_price - entry) / entry * 100, 2),
                    "reason":     exit_reason,
                })
                del open_pos[sym]

        if next_date is None or len(open_pos) >= MAX_CONCURRENT:
            continue

        day  = sdf[sdf["date"] == signal_date]
        mask = base_mask(day)
        if use_dual_rs:
            mask = mask & day["dual_rs"]
        if use_roc:
            mask = mask & day["roc_pass"]
        if use_gap_filter:
            mask = mask & (~day["gap_up"])

        cands = day[mask].sort_values(["rs_return", "adx", "vol_ratio"], ascending=False)

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
            atr_min = c["atr"] * 1.5
            stop    = min(ep - atr_min, ep * (1 - STOP_PCT))
            stop    = min(stop, ep - 0.01)
            open_pos[sym] = {
                "entry": ep, "stop": stop,
                "qty": position_qty(ep), "entry_date": next_date,
            }
            placed += 1

    # Force-close remaining
    last = all_dates[-1]
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
print("\nRunning simulations…", flush=True)
variants = {
    "BASELINE":    dict(use_dual_rs=False, use_roc=False, use_gap_filter=False),
    "+RS-DUAL":    dict(use_dual_rs=True,  use_roc=False, use_gap_filter=False),
    "+ROC":        dict(use_dual_rs=False, use_roc=True,  use_gap_filter=False),
    "+GAP-FILTER": dict(use_dual_rs=False, use_roc=False, use_gap_filter=True),
    "+ALL-3":      dict(use_dual_rs=True,  use_roc=True,  use_gap_filter=True),
}
results = {}
for label, kwargs in variants.items():
    trades = simulate(label, **kwargs)
    results[label] = trades
    print(f"  {label:<14} → {len(trades)} trades")


# ── Stats ─────────────────────────────────────────────────────────────────────
def stats(trades):
    if not trades:
        return {}
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins) / len(trades) * 100
    aw     = sum(t["pnl_pct"] for t in wins)  / len(wins)   if wins   else 0
    al     = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    pf     = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses else 999

    by_day = defaultdict(float)
    for t in trades:
        by_day[str(t["exit_date"])[:10]] += t["pnl"]
    cum = peak = max_dd = 0.0
    for d in sorted(by_day):
        cum   += by_day[d]
        peak   = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return dict(
        n=len(trades), wins=len(wins), losses=len(losses),
        win_rate=wr, total_pnl=total, avg_win=aw, avg_loss=al,
        profit_factor=pf, max_drawdown=max_dd,
        stops=sum(1 for t in trades if "stop"     in t["reason"]),
        rsi_ex=sum(1 for t in trades if "RSI"      in t["reason"]),
        hold_ex=sum(1 for t in trades if "max hold" in t["reason"]),
        best=max((t["pnl_pct"] for t in trades), default=0),
        worst=min((t["pnl_pct"] for t in trades), default=0),
        avg_hold=sum((t["exit_date"] - t["entry_date"]).days for t in trades) / len(trades),
    )

S = {k: stats(v) for k, v in results.items()}


# ── Summary table ─────────────────────────────────────────────────────────────
COL = 14
labels = list(variants.keys())
b = S["BASELINE"]

def fmt_arrow(base_val, new_val, higher_better=True, fmt="{:.1f}", suffix=""):
    cell = fmt.format(new_val) + suffix
    if new_val > base_val:
        cell += " ▲" if higher_better else " ▼"
    elif new_val < base_val:
        cell += " ▼" if higher_better else " ▲"
    return cell

W = 14
header = f"  {'Metric':<26}" + "".join(f"{l:>{W}}" for l in labels)
print()
print("=" * (26 + W * len(labels) + 4))
print(header)
print("=" * (26 + W * len(labels) + 4))

def row(title, key, fmt="{:.1f}", suffix="", higher_better=True):
    base_val = b.get(key, 0)
    cells = f"  {title:<26}"
    for i, lbl in enumerate(labels):
        v = S[lbl].get(key, 0)
        if i == 0:
            cells += f"{fmt.format(v)+suffix:>{W}}"
        else:
            cells += f"{fmt_arrow(base_val, v, higher_better, fmt, suffix):>{W}}"
    print(cells)

row("Total trades",     "n",             "{:.0f}", "",   False)
row("Win rate",         "win_rate",      "{:.1f}", "%",  True)
row("Total P&L",        "total_pnl",     "${:+.2f}", "", True)
row("Avg win",          "avg_win",       "{:+.1f}", "%", True)
row("Avg loss",         "avg_loss",      "{:+.1f}", "%", False)
row("Profit factor",    "profit_factor", "{:.2f}",  "",  True)
row("Max drawdown",     "max_drawdown",  "${:.2f}", "",  False)
row("Best trade",       "best",          "{:+.1f}", "%", True)
row("Worst trade",      "worst",         "{:+.1f}", "%", False)
row("Avg hold (days)",  "avg_hold",      "{:.1f}",  "d", False)
print("  " + "-" * (22 + W * len(labels) + 4))
row("Stop-outs",        "stops",  "{:.0f}", "", False)
row("RSI exits",        "rsi_ex", "{:.0f}", "", True)
row("Max-hold exits",   "hold_ex","{:.0f}", "", False)
print("=" * (26 + W * len(labels) + 4))


# ── Monthly P&L table ─────────────────────────────────────────────────────────
def monthly(trades):
    m = defaultdict(float)
    for t in trades:
        m[str(t["exit_date"])[:7]] += t["pnl"]
    return m

monthly_data = {k: monthly(v) for k, v in results.items()}
all_months = sorted(set().union(*[set(m.keys()) for m in monthly_data.values()]))

print()
print(f"── Monthly P&L {'─' * (W * len(labels) - 8)}")
print(f"  {'Month':<10}" + "".join(f"{l:>{W}}" for l in labels))
print("  " + "-" * (10 + W * len(labels)))
for m in all_months:
    row_str = f"  {m:<10}"
    for lbl in labels:
        v = monthly_data[lbl].get(m, 0)
        row_str += f"{v:>+{W}.2f}"
    print(row_str)
totals_row = f"  {'TOTAL':<10}"
for lbl in labels:
    totals_row += f"{sum(monthly_data[lbl].values()):>+{W}.2f}"
print(totals_row)


# ── Conclusion ────────────────────────────────────────────────────────────────
print()
print("=" * (26 + W * len(labels) + 4))
print("  CONCLUSION")
print("=" * (26 + W * len(labels) + 4))

base_pnl = b["total_pnl"]
base_pf  = b["profit_factor"]
base_wr  = b["win_rate"]

best_label = max(
    [l for l in labels if l != "BASELINE"],
    key=lambda l: S[l]["total_pnl"]
)
best = S[best_label]

for lbl in labels[1:]:
    s = S[lbl]
    delta = s["total_pnl"] - base_pnl
    sign  = "+" if delta >= 0 else ""
    verdict = "BETTER" if s["total_pnl"] > base_pnl and s["profit_factor"] > base_pf else (
              "MIXED"  if s["total_pnl"] > base_pnl or  s["profit_factor"] > base_pf else "WORSE")
    print(f"  {lbl:<14} P&L {s['total_pnl']:>+8.2f}  PF {s['profit_factor']:.2f}"
          f"  WR {s['win_rate']:.1f}%  Δ P&L {sign}{delta:.2f}  → {verdict}")

print()
print(f"  Best single filter: {best_label}")
print(f"    P&L ${best['total_pnl']:+.2f} | PF {best['profit_factor']:.2f} | WR {best['win_rate']:.1f}% | MaxDD ${best['max_drawdown']:.2f}")
print("=" * (26 + W * len(labels) + 4))
