"""
backtest_all_options.py — all improvement options vs current production strategy.

CURRENT     : production (days_in_scan≥2, RSI>65, hold≤7d, ADX>30, MACD/vol guards)
+RS-DUAL    : also require 21-day AND 63-day RS both outperform SPY
+ROC        : also require close up ≥5% over 20 days (price acceleration)
+GAP-FILTER : skip entry if stock gaps up >4% from prior close at open
+VOL-3D     : require 3-day rolling volume avg above average (sustained demand)
+SCORE7     : raise entry bar to score ≥7/8 (higher conviction only)
+ADX-STOP   : tighten stop to ATR×1.0 when ADX > 40 (strong trend → tighter leash)
+ALL-ENTRY  : RS-DUAL + ROC + GAP-FILTER (entry quality gates combined)
+ALL        : all six filters combined

121 trading days: 2026-02-19 → 2026-06-19
"""

import math
from collections import defaultdict
from pathlib import Path

import pandas as pd

CACHE_PATH = Path("/tmp/bt_signals.pkl")

ADX_THRESHOLD    = 30
AUTO_ORDER_TOP_N = 10
POS_THRESHOLD    = 50
MAX_CONCURRENT   = 15
RSI_EXIT         = 65
MAX_HOLD         = 7
STOP_PCT         = 0.05
GAP_THRESHOLD    = 0.04
ADX_TIGHT_CUTOFF = 40   # ADX above this → use tighter ATR multiplier


def position_qty(price):
    return max(1, math.floor((750 if price < POS_THRESHOLD else 250) / price))


# ── Load & enrich cache ───────────────────────────────────────────────────────
print("Loading cached signals…", flush=True)
sdf = pd.read_pickle(CACHE_PATH)
sdf["date"] = pd.to_datetime(sdf["date"])
all_dates = sorted(sdf["date"].unique())
print(f"  {len(sdf):,} rows | {sdf['symbol'].nunique():,} symbols")
print(f"  Range: {str(all_dates[0])[:10]} → {str(all_dates[-1])[:10]} ({len(all_dates)} trading days)")

# days_in_scan≥2
print("Computing auxiliary flags…", flush=True)
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

# gap-up: next-day open > today's close × (1 + GAP_THRESHOLD)
sdf = sdf.sort_values(["symbol", "date"])
sdf["next_open"] = sdf.groupby("symbol")["open"].shift(-1)
sdf["gap_up"]    = sdf["next_open"] > sdf["close"] * (1 + GAP_THRESHOLD)
sdf["gap_up"]    = sdf["gap_up"].fillna(False)

# score≥7 flag
sdf["score7"] = sdf["score"] >= 7

# price lookup (used at exit time)
price_lkp = sdf.set_index(["symbol", "date"])[
    ["open", "high", "low", "close", "rsi", "atr"]
].to_dict("index")


# ── Simulation engine ─────────────────────────────────────────────────────────
def simulate(
    use_dual_rs=False,
    use_roc=False,
    use_gap_filter=False,
    use_vol3d=False,
    use_score7=False,
    use_adx_stop=False,
):
    open_pos = {}
    trades   = []

    for i, signal_date in enumerate(all_dates):
        next_date = all_dates[i + 1] if i + 1 < len(all_dates) else None

        # Exit open positions
        for sym in list(open_pos.keys()):
            pos   = open_pos[sym]
            key   = (sym, signal_date)
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

        # Build candidate mask
        day  = sdf[sdf["date"] == signal_date]
        mask = (
            (day["score"]     >= (7 if use_score7 else 6)) &
            (day["adx"]        > ADX_THRESHOLD) &
            (~day["vol_drying"]) &
            (~day["macd_shrink"]) &
            (day["exit_mode"] == "trailing_stop") &
            (day["in_prev_scan"])
        )
        if use_dual_rs:
            mask = mask & day["dual_rs"]
        if use_roc:
            mask = mask & day["roc_pass"]
        if use_gap_filter:
            mask = mask & (~day["gap_up"])
        if use_vol3d:
            mask = mask & day["vol_3d_pass"]

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

            # ADX-adaptive stop: tighter multiplier for strong trends
            atr_mult = 1.0 if (use_adx_stop and c["adx"] > ADX_TIGHT_CUTOFF) else 1.5
            stop = min(ep - c["atr"] * atr_mult, ep * (1 - STOP_PCT), ep - 0.01)

            open_pos[sym] = {
                "entry": ep, "stop": stop,
                "qty": position_qty(ep), "entry_date": next_date,
            }
            placed += 1

    # Force-close remaining at final bar
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
    "CURRENT":     dict(),
    "+RS-DUAL":    dict(use_dual_rs=True),
    "+ROC":        dict(use_roc=True),
    "+GAP-FILTER": dict(use_gap_filter=True),
    "+VOL-3D":     dict(use_vol3d=True),
    "+SCORE7":     dict(use_score7=True),
    "+ADX-STOP":   dict(use_adx_stop=True),
    "+ALL-ENTRY":  dict(use_dual_rs=True, use_roc=True, use_gap_filter=True),
    "+ADX-STOP":   dict(use_adx_stop=True),
    "+COMBINED":   dict(use_dual_rs=True, use_roc=True, use_gap_filter=True, use_adx_stop=True),
    "+ALL":        dict(use_dual_rs=True, use_roc=True, use_gap_filter=True,
                        use_vol3d=True, use_score7=True, use_adx_stop=True),
}
results = {}
for label, kwargs in variants.items():
    trades = simulate(**kwargs)
    results[label] = trades
    print(f"  {label:<14} → {len(trades):>3} trades")


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
        cum  += by_day[d]
        peak  = max(peak, cum)
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
base = S["CURRENT"]
labels = list(variants.keys())


# ── Comparison table ──────────────────────────────────────────────────────────
W = 13

def cell(val, base_val, fmt, suffix, higher_better):
    s = fmt.format(val) + suffix
    if abs(val - base_val) < 1e-6:
        return s
    better = (val > base_val) if higher_better else (val < base_val)
    return s + (" ▲" if better else " ▼")

def print_row(title, key, fmt="{:.1f}", suffix="", hb=True):
    row = f"  {title:<22}"
    bv  = base.get(key, 0)
    for i, lbl in enumerate(labels):
        v = S[lbl].get(key, 0)
        c = fmt.format(v) + suffix if i == 0 else cell(v, bv, fmt, suffix, hb)
        row += f"{c:>{W}}"
    print(row)

sep = "=" * (24 + W * len(labels))
print()
print(sep)
print(f"  {'Metric':<22}" + "".join(f"{l:>{W}}" for l in labels))
print(sep)
print_row("Trades",         "n",             "{:.0f}", "",    False)
print_row("Win rate",       "win_rate",      "{:.1f}", "%",   True)
print_row("Total P&L",      "total_pnl",     "${:+.2f}", "",  True)
print_row("Avg win",        "avg_win",       "{:+.1f}", "%",  True)
print_row("Avg loss",       "avg_loss",      "{:+.1f}", "%",  False)
print_row("Profit factor",  "profit_factor", "{:.2f}",  "",   True)
print_row("Max drawdown",   "max_drawdown",  "${:.2f}", "",   False)
print_row("Best trade",     "best",          "{:+.1f}", "%",  True)
print_row("Worst trade",    "worst",         "{:+.1f}", "%",  False)
print_row("Avg hold",       "avg_hold",      "{:.1f}",  "d",  False)
print("  " + "-" * (22 + W * len(labels)))
print_row("Stop-outs",      "stops",  "{:.0f}", "", False)
print_row("RSI exits",      "rsi_ex", "{:.0f}", "", True)
print_row("Max-hold exits", "hold_ex","{:.0f}", "", False)
print(sep)


# ── Monthly P&L ───────────────────────────────────────────────────────────────
def monthly(trades):
    m = defaultdict(float)
    for t in trades:
        m[str(t["exit_date"])[:7]] += t["pnl"]
    return m

mdata     = {k: monthly(v) for k, v in results.items()}
all_months = sorted(set().union(*[set(m.keys()) for m in mdata.values()]))

print()
print(f"── Monthly P&L {'─' * max(0, W * len(labels) - 2)}")
print(f"  {'Month':<10}" + "".join(f"{l:>{W}}" for l in labels))
print("  " + "-" * (10 + W * len(labels)))
for m in all_months:
    row = f"  {m:<10}"
    for lbl in labels:
        row += f"{mdata[lbl].get(m, 0):>+{W}.2f}"
    print(row)
tot_row = f"  {'TOTAL':<10}"
for lbl in labels:
    tot_row += f"{sum(mdata[lbl].values()):>+{W}.2f}"
print(tot_row)


# ── Delta vs current ─────────────────────────────────────────────────────────
print()
print(f"── Delta vs CURRENT {'─' * max(0, W * len(labels) - 8)}")
metrics = [
    ("P&L Δ",    "total_pnl",     "${:+.2f}", True),
    ("WR Δ",     "win_rate",      "{:+.1f}%", True),
    ("PF Δ",     "profit_factor", "{:+.2f}",  True),
    ("MaxDD Δ",  "max_drawdown",  "${:+.2f}", False),
]
for title, key, fmt, hb in metrics:
    row = f"  {title:<10}"
    bv  = base.get(key, 0)
    for i, lbl in enumerate(labels):
        v   = S[lbl].get(key, 0)
        raw = v - bv
        if key == "max_drawdown":
            raw = bv - v  # positive = improvement
        s   = fmt.format(raw)
        if i == 0:
            s = "—"
        elif raw > 0:
            s += " ▲"
        elif raw < 0:
            s += " ▼"
        row += f"{s:>{W}}"
    print(row)


# ── Ranked conclusion ─────────────────────────────────────────────────────────
print()
print(sep)
print("  CONCLUSION — ranked by Total P&L")
print(sep)
ranked = sorted(
    [(lbl, S[lbl]) for lbl in labels],
    key=lambda x: x[1]["total_pnl"], reverse=True
)
print(f"  {'Rank':<5} {'Variant':<16} {'P&L':>10} {'PF':>7} {'WR':>8} {'MaxDD':>10} {'Verdict'}")
print("  " + "-" * 66)
base_pnl = base["total_pnl"]
base_pf  = base["profit_factor"]
for rank, (lbl, s) in enumerate(ranked, 1):
    if lbl == "CURRENT":
        verdict = "← current"
    elif s["total_pnl"] > base_pnl and s["profit_factor"] > base_pf:
        verdict = "BETTER ✓"
    elif s["total_pnl"] > base_pnl or s["profit_factor"] > base_pf:
        verdict = "MIXED"
    else:
        verdict = "WORSE ✗"
    print(f"  {rank:<5} {lbl:<16} {s['total_pnl']:>+10.2f} {s['profit_factor']:>7.2f} "
          f"{s['win_rate']:>7.1f}% {s['max_drawdown']:>10.2f}   {verdict}")
print(sep)
