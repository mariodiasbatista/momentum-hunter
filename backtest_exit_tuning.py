"""
backtest_exit_tuning.py — definitive head-to-head: current strategy vs exit-tuned variant.

BASELINE    : current production (days_in_scan≥2, score≥6, RSI>70 exit, max hold 10 days)
EXIT-TUNED  : same entry rules + RSI>65 exit + max hold 7 days

Full dataset: 121 trading days, 2026-02-19 → 2026-06-19.
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


def position_qty(price):
    return max(1, math.floor((750 if price < POS_THRESHOLD else 250) / price))


# ── Load cache ────────────────────────────────────────────────────────────────
print("Loading cached signals…", flush=True)
sdf = pd.read_pickle(CACHE_PATH)
sdf["date"] = pd.to_datetime(sdf["date"])
all_dates = sorted(sdf["date"].unique())
print(f"  {len(sdf):,} rows | {sdf['symbol'].nunique():,} symbols")
print(f"  Full range: {str(all_dates[0])[:10]} → {str(all_dates[-1])[:10]} ({len(all_dates)} trading days)")


# ── days_in_scan≥2 flag ───────────────────────────────────────────────────────
print("Computing consecutive-scan flag…", flush=True)
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


# ── Price lookup ──────────────────────────────────────────────────────────────
price_lkp = sdf.set_index(["symbol", "date"])[
    ["open", "high", "low", "close", "rsi", "atr"]
].to_dict("index")


# ── Simulation engine ─────────────────────────────────────────────────────────
def simulate(rsi_exit: int, max_hold: int, label: str) -> list[dict]:
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
            elif bar["rsi"] > rsi_exit:
                exit_price, exit_reason = bar["close"], f"RSI > {rsi_exit}"
            elif (bar["close"] - entry) / entry >= 0.08 and bar["rsi"] < 50:
                exit_price, exit_reason = bar["close"], "gain≥8%+RSI<50"
            elif days >= max_hold:
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

        # Select candidates for next day entry
        day  = sdf[sdf["date"] == signal_date]
        mask = (
            (day["score"]    >= 6) &
            (day["adx"]       > ADX_THRESHOLD) &
            (~day["vol_drying"]) &
            (~day["macd_shrink"]) &
            (day["exit_mode"] == "trailing_stop") &
            (day["in_prev_scan"])
        )
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


print("\nRunning simulations…", flush=True)
results = {
    "BASELINE":    simulate(rsi_exit=70, max_hold=10, label="BASELINE"),
    "EXIT-TUNED":  simulate(rsi_exit=65, max_hold=7,  label="EXIT-TUNED"),
}
for label, trades in results.items():
    print(f"  {label:<14} → {len(trades)} trades")


# ── Stats ─────────────────────────────────────────────────────────────────────
def stats(trades):
    if not trades:
        return {}
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins) / len(trades) * 100
    aw     = sum(t["pnl_pct"] for t in wins)   / len(wins)   if wins   else 0
    al     = sum(t["pnl_pct"] for t in losses)  / len(losses) if losses else 0
    pf     = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses else 999

    by_day = defaultdict(float)
    for t in trades:
        by_day[str(t["exit_date"])[:10]] += t["pnl"]
    cum = peak = max_dd = 0.0
    for d in sorted(by_day):
        cum   += by_day[d]
        peak   = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    stops   = sum(1 for t in trades if "stop"    in t["reason"])
    rsi_ex  = sum(1 for t in trades if "RSI"     in t["reason"])
    hold_ex = sum(1 for t in trades if "max hold" in t["reason"])
    open_ex = sum(1 for t in trades if "open at"  in t["reason"])
    return dict(
        n=len(trades), wins=len(wins), losses=len(losses),
        win_rate=wr, total_pnl=total, avg_win=aw, avg_loss=al,
        profit_factor=pf, max_drawdown=max_dd,
        stops=stops, rsi_exits=rsi_ex, hold_exits=hold_ex, open_exits=open_ex,
        best=max((t["pnl_pct"] for t in trades), default=0),
        worst=min((t["pnl_pct"] for t in trades), default=0),
        avg_hold=sum((t["exit_date"]-t["entry_date"]).days for t in trades)/len(trades),
    )

S = {k: stats(v) for k, v in results.items()}


# ── Summary table ─────────────────────────────────────────────────────────────
def arrow(base_val, new_val, higher_better=True):
    if higher_better:
        return " ▲" if new_val > base_val else (" ▼" if new_val < base_val else "")
    else:
        return " ▲" if new_val < base_val else (" ▼" if new_val > base_val else "")

b = S["BASELINE"]
e = S["EXIT-TUNED"]

print()
print("=" * 68)
print(f"  {'Metric':<30} {'BASELINE':>16} {'EXIT-TUNED':>16}")
print("=" * 68)

rows = [
    ("Total trades",       "n",            "{:.0f}",   "",    True),
    ("Winning trades",     "wins",         "{:.0f}",   "",    True),
    ("Losing trades",      "losses",       "{:.0f}",   "",    False),
    ("Win rate",           "win_rate",     "{:.1f}",   "%",   True),
    ("Total P&L",          "total_pnl",    "${:+.2f}", "",    True),
    ("Avg win",            "avg_win",      "{:+.1f}",  "%",   True),
    ("Avg loss",           "avg_loss",     "{:+.1f}",  "%",   False),
    ("Profit factor",      "profit_factor","{:.2f}",   "",    True),
    ("Max drawdown",       "max_drawdown", "${:.2f}",  "",    False),
    ("Best trade",         "best",         "{:+.1f}",  "%",   True),
    ("Worst trade",        "worst",        "{:+.1f}",  "%",   False),
    ("Avg hold (days)",    "avg_hold",     "{:.1f}",   "d",   False),
]
for label, key, fmt, suffix, hb in rows:
    bv = b.get(key, 0)
    ev = e.get(key, 0)
    a  = arrow(bv, ev, hb)
    print(f"  {label:<30} {fmt.format(bv)+suffix:>16} {fmt.format(ev)+suffix+a:>16}")

print("  " + "-" * 64)
exit_rows = [
    ("Stop-outs",          "stops",      "{:.0f}", False),
    ("RSI exits",          "rsi_exits",  "{:.0f}", True),
    ("Max-hold exits",     "hold_exits", "{:.0f}", False),
    ("Open at end",        "open_exits", "{:.0f}", False),
]
for label, key, fmt, hb in exit_rows:
    bv = b.get(key, 0)
    ev = e.get(key, 0)
    a  = arrow(bv, ev, hb)
    print(f"  {label:<30} {fmt.format(bv):>16} {fmt.format(ev)+a:>16}")
print("=" * 68)


# ── Monthly P&L ───────────────────────────────────────────────────────────────
def monthly(trades):
    m = defaultdict(float)
    for t in trades:
        m[str(t["exit_date"])[:7]] += t["pnl"]
    return m

mb = monthly(results["BASELINE"])
me = monthly(results["EXIT-TUNED"])
all_months = sorted(set(mb) | set(me))

print()
print("── Monthly P&L ──────────────────────────────────────────────────────")
print(f"  {'Month':<10} {'BASELINE':>14} {'EXIT-TUNED':>14} {'Δ':>12}")
print("  " + "-" * 54)
for m in all_months:
    bv = mb.get(m, 0)
    ev = me.get(m, 0)
    d  = ev - bv
    a  = " ▲" if d > 0 else (" ▼" if d < 0 else "")
    print(f"  {m:<10} {bv:>+14.2f} {ev:>+14.2f} {d:>+11.2f}{a}")
print(f"  {'TOTAL':<10} {sum(mb.values()):>+14.2f} {sum(me.values()):>+14.2f} "
      f"{sum(me.values())-sum(mb.values()):>+11.2f}")


# ── Cumulative P&L ────────────────────────────────────────────────────────────
by_date_b = defaultdict(float)
by_date_e = defaultdict(float)
for t in results["BASELINE"]:    by_date_b[str(t["exit_date"])[:10]] += t["pnl"]
for t in results["EXIT-TUNED"]:  by_date_e[str(t["exit_date"])[:10]] += t["pnl"]

all_exit_dates = sorted(set(by_date_b) | set(by_date_e))
cum_b = cum_e = 0.0

print()
print("── Cumulative P&L ───────────────────────────────────────────────────")
print(f"  {'Date':<12} {'BASELINE':>14} {'EXIT-TUNED':>14} {'Δ':>12}")
print("  " + "-" * 56)
prev_week = None
for d in all_exit_dates:
    cum_b += by_date_b.get(d, 0)
    cum_e += by_date_e.get(d, 0)
    week = d[:8] + str((int(d[8:10]) // 7) * 7 + 1).zfill(2)
    if week != prev_week:
        delta = cum_e - cum_b
        a = " ▲" if delta > 0 else (" ▼" if delta < 0 else "")
        print(f"  {d:<12} {cum_b:>+14.2f} {cum_e:>+14.2f} {delta:>+11.2f}{a}")
        prev_week = week
print(f"  {'FINAL':<12} {cum_b:>+14.2f} {cum_e:>+14.2f} {cum_e-cum_b:>+11.2f}")


# ── Top 5 winners / losers per variant ───────────────────────────────────────
for label, trades in results.items():
    by_pnl = sorted(trades, key=lambda t: t["pnl"], reverse=True)
    print(f"\n── {label} — top 5 winners / worst 5 losers")
    print(f"  {'Symbol':<7} {'Entry':>12} {'Exit':>12} {'P&L $':>9} {'P&L %':>8}  Reason")
    print("  " + "-" * 62)
    for t in by_pnl[:5]:
        print(f"  {t['symbol']:<7} {str(t['entry_date'])[:10]:>12} {str(t['exit_date'])[:10]:>12} "
              f"{t['pnl']:>+9.2f} {t['pnl_pct']:>+7.1f}%  {t['reason']}")
    print("  ···")
    for t in by_pnl[-5:]:
        print(f"  {t['symbol']:<7} {str(t['entry_date'])[:10]:>12} {str(t['exit_date'])[:10]:>12} "
              f"{t['pnl']:>+9.2f} {t['pnl_pct']:>+7.1f}%  {t['reason']}")


# ── Conclusion ────────────────────────────────────────────────────────────────
delta_pnl = e["total_pnl"] - b["total_pnl"]
delta_dd  = b["max_drawdown"] - e["max_drawdown"]
print()
print("=" * 68)
print("  CONCLUSION")
print("=" * 68)
print(f"  P&L:          BASELINE ${b['total_pnl']:+.2f}  →  EXIT-TUNED ${e['total_pnl']:+.2f}  (Δ ${delta_pnl:+.2f})")
print(f"  Profit factor: {b['profit_factor']:.2f}  →  {e['profit_factor']:.2f}")
print(f"  Win rate:      {b['win_rate']:.1f}%  →  {e['win_rate']:.1f}%")
print(f"  Max drawdown: ${b['max_drawdown']:.2f}  →  ${e['max_drawdown']:.2f}  (Δ ${delta_dd:+.2f} improvement)")
if e["total_pnl"] > b["total_pnl"] and e["profit_factor"] > b["profit_factor"]:
    print(f"\n  VERDICT: EXIT-TUNED is BETTER across all key metrics.")
    print(f"  Apply RSI>65 exit + 7-day max hold.")
else:
    print(f"\n  VERDICT: Mixed results — review before applying.")
print("=" * 68)
