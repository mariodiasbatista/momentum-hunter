"""
backtest_rsi_guard.py — backtest the RSI exit guard and threshold variants.

Problem observed (Aug–Sep 2026):
  6 of 15 RSI exits closed positions already in the red (e.g. BHC -3.8%,
  VSTM -1.8%). The RSI > 65 condition was firing on intraday bounces even
  when the position was underwater. Fix deployed: only RSI-exit if plpc >= 0.

This backtest compares the guard approach vs raising the RSI threshold.

Variants:
  A: RSI>65,  no guard           ← current production baseline
  B: RSI>65,  guard plpc>=0      ← deployed fix
  C: RSI>65,  guard plpc>=+1%
  D: RSI>70,  no guard
  E: RSI>70,  guard plpc>=0
  F: RSI>75,  no guard
"""

import math
from collections import defaultdict
from pathlib import Path

import pandas as pd

CACHE_PATH = Path("/tmp/bt_signals.pkl")

ADX_THRESHOLD    = 30
AUTO_ORDER_TOP_N = 10
MAX_CONCURRENT   = 15
MAX_HOLD         = 7
STOP_PCT         = 0.05
POS_THRESHOLD    = 50
POOL_N           = 100
GAIN_FADING_PCT  = 0.08   # gain >= 8% + RSI < 50 → exit (unchanged)


def position_qty(price):
    return max(1, math.floor((750 if price < POS_THRESHOLD else 250) / price))


# ── Load signals ──────────────────────────────────────────────────────────────
START_DATE = pd.Timestamp("2026-06-01")

print("Loading cached signals…", flush=True)
sdf = pd.read_pickle(CACHE_PATH)
sdf["date"] = pd.to_datetime(sdf["date"])
sdf = sdf[sdf["date"] >= START_DATE].copy()
all_dates = sorted(sdf["date"].unique())
print(f"  {len(sdf):,} rows | {sdf['symbol'].nunique():,} symbols")
print(f"  Range: {str(all_dates[0])[:10]} → {str(all_dates[-1])[:10]} ({len(all_dates)} trading days)")

# days_in_scan >= 2 filter
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
def simulate(rsi_exit: int, min_gain_for_rsi: float) -> tuple[list[dict], int]:
    """
    rsi_exit:           RSI threshold that triggers the overbought exit
    min_gain_for_rsi:   minimum position gain (fraction) required to RSI-exit
                        0.0  = must be at breakeven or better (deployed fix)
                        -inf = no guard (baseline)
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
            gain  = (bar["close"] - entry) / entry

            exit_price = exit_reason = None
            if bar["open"] <= stop:
                exit_price, exit_reason = bar["open"],  "gap-down stop"
            elif bar["low"] <= stop:
                exit_price, exit_reason = stop,         "stop hit"
            elif bar["rsi"] > rsi_exit and gain >= min_gain_for_rsi:
                exit_price, exit_reason = bar["close"], f"RSI>{rsi_exit}"
            elif gain >= GAIN_FADING_PCT and bar["rsi"] < 50:
                exit_price, exit_reason = bar["close"], "gain>=8%+RSI<50"
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
        all_cands = day[day["score"] >= 6].sort_values(
            ["score", "rs_return", "adx", "vol_ratio"], ascending=False
        )
        pool  = all_cands.head(POOL_N)
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

    # Close remaining open positions at last date
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
    rsi_exits = [t for t in trades if t["reason"].startswith("RSI")]
    rsi_losses = [t for t in rsi_exits if t["pnl"] < 0]
    return dict(
        n=len(trades), wins=len(wins), losses=len(losses),
        win_rate=len(wins) / len(trades) * 100,
        total_pnl=total,
        avg_win=sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0,
        avg_loss=sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0,
        profit_factor=pf,
        max_drawdown=max_dd,
        stops=sum(1 for t in trades if "stop" in t["reason"]),
        rsi_exits=len(rsi_exits),
        rsi_exits_at_loss=len(rsi_losses),
        rsi_exit_pnl=sum(t["pnl"] for t in rsi_exits),
    )


# ── Variants ──────────────────────────────────────────────────────────────────
NEG_INF = float("-inf")
variants = [
    ("A: RSI>65  no guard",      65, NEG_INF),   # baseline
    ("B: RSI>65  guard>=0%",     65, 0.0),        # deployed fix
    ("C: RSI>65  guard>=+1%",    65, 0.01),
    ("D: RSI>70  no guard",      70, NEG_INF),
    ("E: RSI>70  guard>=0%",     70, 0.0),
    ("F: RSI>75  no guard",      75, NEG_INF),
]

print("\nRunning simulations…", flush=True)
results = {}
order_counts = {}
for label, rsi_thr, gain_floor in variants:
    trades, n_placed = simulate(rsi_thr, gain_floor)
    results[label] = trades
    order_counts[label] = n_placed
    print(f"  {label:<28} → {len(trades):>3} trades  ({n_placed} entries)", flush=True)

S      = {k: stats(v) for k, v in results.items()}
labels = [v[0] for v in variants]
base   = S["A: RSI>65  no guard"]

# ── Results table ─────────────────────────────────────────────────────────────
W = 15
print()
sep = "=" * (28 + W * len(labels))
print(sep)
print(f"  {'Metric':<26}" + "".join(f"{l:>{W}}" for l in labels))
print(sep)

def row(title, key, fmt="{:.1f}", suffix="", higher_better=True):
    bv   = base.get(key, 0)
    line = f"  {title:<26}"
    for i, lbl in enumerate(labels):
        v = S[lbl].get(key, 0)
        s = fmt.format(v) + suffix
        if i > 0 and abs(v - bv) > 1e-6:
            better = (v > bv) if higher_better else (v < bv)
            s += " ▲" if better else " ▼"
        line += f"{s:>{W}}"
    print(line)

row("Trades",           "n",              "{:.0f}")
row("Win rate",         "win_rate",       "{:.1f}", "%")
row("Total P&L",        "total_pnl",      "${:+.2f}")
row("Avg win",          "avg_win",        "{:+.1f}", "%")
row("Avg loss",         "avg_loss",       "{:+.1f}", "%", False)
row("Profit factor",    "profit_factor",  "{:.2f}")
row("Max drawdown",     "max_drawdown",   "${:.2f}", "", False)
row("Stop-outs",        "stops",          "{:.0f}",  "", False)
row("RSI exits",        "rsi_exits",      "{:.0f}")
row("RSI exits@loss",   "rsi_exits_at_loss", "{:.0f}", "", False)
row("RSI exit P&L",     "rsi_exit_pnl",   "${:+.2f}")
print(sep)

entries_line = f"  {'Total entries':<26}"
for lbl in labels:
    entries_line += f"{order_counts[lbl]:>{W}}"
print(entries_line)
print(sep)

# ── Delta vs A ────────────────────────────────────────────────────────────────
print()
print(f"── Delta vs A (RSI>65, no guard) {'─' * max(0, W * len(labels) - 24)}")
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
print(f"  {'Rank':<5} {'Variant':<30} {'P&L':>10} {'PF':>7} {'WR':>8} {'MaxDD':>10}  {'RSI@loss':>9}  {'Verdict'}")
print("  " + "-" * 95)
for rank, (lbl, s) in enumerate(ranked, 1):
    if lbl == "A: RSI>65  no guard":
        verdict = "← baseline"
    elif s["total_pnl"] > base_pnl and s["profit_factor"] > base_pf:
        verdict = "BETTER ✓"
    elif s["total_pnl"] > base_pnl or s["profit_factor"] > base_pf:
        verdict = "MIXED"
    else:
        verdict = "WORSE ✗"
    print(f"  {rank:<5} {lbl:<30} {s['total_pnl']:>+10.2f} {s['profit_factor']:>7.2f} "
          f"{s['win_rate']:>7.1f}% {s['max_drawdown']:>10.2f}  {s['rsi_exits_at_loss']:>9}   {verdict}")
print(sep)
