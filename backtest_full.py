"""
backtest_full.py — definitive head-to-head: BASELINE vs days_in_scan≥2 filter.

Uses all 121 available trading days (2026-02-19 → 2026-06-19) vs the 37-day
window used in prior backtests.  Both variants apply current production controls
(dedup + max 15 concurrent positions, score≥6, no macd_shrink, trailing_stop only).

Output: summary stats, monthly P&L breakdown, max drawdown, exit reasons, trade list.
"""

import math
from collections import defaultdict
from pathlib import Path

import pandas as pd

CACHE_PATH = Path("/tmp/bt_signals.pkl")

ADX_THRESHOLD    = 30
MAX_HOLD_DAYS    = 10
AUTO_ORDER_TOP_N = 10
STOP_PCT         = 0.05
POS_THRESHOLD    = 50
MAX_CONCURRENT   = 15


def position_qty(price):
    return max(1, math.floor((750 if price < POS_THRESHOLD else 250) / price))


# ── 1. Load cache ─────────────────────────────────────────────────────────────
print("Loading cached signals…", flush=True)
sdf = pd.read_pickle(CACHE_PATH)
sdf["date"] = pd.to_datetime(sdf["date"])
all_dates = sorted(sdf["date"].unique())
print(f"  {len(sdf):,} rows | {sdf['symbol'].nunique():,} symbols")
print(f"  Full range: {str(all_dates[0])[:10]} → {str(all_dates[-1])[:10]} ({len(all_dates)} trading days)")


# ── 2. Compute in_prev_scan flag ──────────────────────────────────────────────
# True if the symbol had score ≥ 6 on the PREVIOUS scan date (consec. appearances)
print("Computing consecutive-scan eligibility…", flush=True)
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

total_eligible = (sdf["score"] >= 6).sum()
two_day = (sdf["in_prev_scan"] & (sdf["score"] >= 6)).sum()
print(f"  Score≥6 signal-days: {total_eligible:,} | pass days≥2 filter: {two_day:,} ({two_day/total_eligible*100:.1f}%)")


# ── 3. Price lookup ───────────────────────────────────────────────────────────
price_lkp = sdf.set_index(["symbol", "date"])[
    ["open", "high", "low", "close", "rsi", "atr"]
].to_dict("index")

print(f"\nRunning simulations over ALL {len(all_dates)} days…\n")


# ── 4. Simulation engine ──────────────────────────────────────────────────────
def simulate(require_prev_scan: bool, label: str) -> list[dict]:
    open_pos = {}
    trades   = []

    for i, signal_date in enumerate(all_dates):
        # ── Exit open positions ──────────────────────────────────────────────
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
            elif bar["rsi"] > 70:
                exit_price, exit_reason = bar["close"], "RSI > 70"
            elif (bar["close"] - entry) / entry >= 0.08 and bar["rsi"] < 50:
                exit_price, exit_reason = bar["close"], "gain≥8%+RSI<50"
            elif days >= MAX_HOLD_DAYS:
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

        if i + 1 >= len(all_dates):
            continue
        entry_date = all_dates[i + 1]

        if len(open_pos) >= MAX_CONCURRENT:
            continue

        # ── Select candidates ────────────────────────────────────────────────
        day  = sdf[sdf["date"] == signal_date]
        mask = (
            (day["score"] >= 6) &
            (day["adx"] > ADX_THRESHOLD) &
            (~day["vol_drying"]) &
            (~day["macd_shrink"]) &
            (day["exit_mode"] == "trailing_stop")
        )
        if require_prev_scan:
            mask &= day["in_prev_scan"]

        cands = day[mask].sort_values(
            ["rs_return", "adx", "vol_ratio"], ascending=False
        )

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

    # Force-close anything still open at the last bar
    last = all_dates[-1]
    for sym, pos in open_pos.items():
        key = (sym, last)
        if key in price_lkp:
            ep  = price_lkp[key]["close"]
            pnl = round((ep - pos["entry"]) * pos["qty"], 2)
            trades.append({
                "symbol":     sym,
                "entry_date": pos["entry_date"],
                "exit_date":  last,
                "entry":      pos["entry"],
                "exit":       ep,
                "qty":        pos["qty"],
                "pnl":        pnl,
                "pnl_pct":    round((ep - pos["entry"]) / pos["entry"] * 100, 2),
                "reason":     "open at end",
            })
    return trades


results = {
    "BASELINE":   simulate(False, "BASELINE"),
    "days_in_scan≥2": simulate(True,  "days_in_scan≥2"),
}


# ── 5. Stats helper ───────────────────────────────────────────────────────────
def stats(trades):
    if not trades:
        return {}
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    flat   = [t for t in trades if t["pnl"] == 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins) / len(trades) * 100
    aw     = sum(t["pnl_pct"] for t in wins)  / len(wins)  if wins   else 0
    al     = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    pf     = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses else 999
    stops  = sum(1 for t in trades if "stop" in t["reason"])
    rsi_ex = sum(1 for t in trades if "RSI"  in t["reason"])
    gain_e = sum(1 for t in trades if "gain" in t["reason"])
    hold_e = sum(1 for t in trades if "max"  in t["reason"])
    open_e = sum(1 for t in trades if "open at" in t["reason"])

    # Max drawdown on cumulative P&L
    pnl_by_date = defaultdict(float)
    for t in trades:
        pnl_by_date[str(t["exit_date"])[:10]] += t["pnl"]
    cum = 0.0; peak = 0.0; max_dd = 0.0
    for d in sorted(pnl_by_date):
        cum  += pnl_by_date[d]
        peak  = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return dict(
        n=len(trades), wins=len(wins), losses=len(losses), flat=len(flat),
        win_rate=wr, total_pnl=total, avg_win=aw, avg_loss=al,
        profit_factor=pf, max_drawdown=-max_dd,
        stops=stops, rsi_exits=rsi_ex, gain_exits=gain_e,
        hold_exits=hold_e, open_exits=open_e,
        best_trade=max((t["pnl_pct"] for t in trades), default=0),
        worst_trade=min((t["pnl_pct"] for t in trades), default=0),
        avg_hold=sum((t["exit_date"] - t["entry_date"]).days for t in trades) / len(trades),
    )


# ── 6. Summary table ──────────────────────────────────────────────────────────
s = {k: stats(v) for k, v in results.items()}

print("=" * 72)
print(f"  {'Metric':<30} {'BASELINE':>18} {'days_in_scan≥2':>18}")
print("=" * 72)

def row(label, key, fmt="{:.0f}", suffix=""):
    b = s["BASELINE"].get(key, 0)
    d = s["days_in_scan≥2"].get(key, 0)
    arrow = " ▲" if d > b else (" ▼" if d < b else "")
    print(f"  {label:<30} {fmt.format(b)+suffix:>18} {fmt.format(d)+suffix+arrow:>18}")

row("Total trades",         "n")
row("Winning trades",       "wins")
row("Losing trades",        "losses")
row("Win rate",             "win_rate",      "{:.1f}",  "%")
row("Total P&L",            "total_pnl",     "${:+.2f}")
row("Avg win",              "avg_win",       "{:+.1f}",  "%")
row("Avg loss",             "avg_loss",      "{:+.1f}",  "%")
row("Profit factor",        "profit_factor", "{:.2f}")
row("Max drawdown",         "max_drawdown",  "${:.2f}")
row("Best trade",           "best_trade",    "{:+.1f}",  "%")
row("Worst trade",          "worst_trade",   "{:+.1f}",  "%")
row("Avg hold (days)",      "avg_hold",      "{:.1f}")
print("-" * 72)
row("Stop-outs",            "stops")
row("RSI>70 exits",         "rsi_exits")
row("Gain+RSI exits",       "gain_exits")
row("Max-hold exits",       "hold_exits")
row("Open at period end",   "open_exits")
print("=" * 72)


# ── 7. Monthly P&L breakdown ──────────────────────────────────────────────────
print()
print("── Monthly P&L ──────────────────────────────────────────────────────")
print(f"  {'Month':<12} {'BASELINE':>14} {'days_in_scan≥2':>16} {'Δ':>12}")
print("  " + "-" * 58)

def monthly(trades):
    m = defaultdict(float)
    for t in trades:
        month = str(t["exit_date"])[:7]
        m[month] += t["pnl"]
    return m

months_b = monthly(results["BASELINE"])
months_d = monthly(results["days_in_scan≥2"])
all_months = sorted(set(months_b) | set(months_d))
for m in all_months:
    b = months_b.get(m, 0)
    d = months_d.get(m, 0)
    delta = d - b
    arrow = " ▲" if delta > 0 else " ▼"
    print(f"  {m:<12} {b:>+14.2f} {d:>+16.2f} {delta:>+11.2f}{arrow}")
print(f"  {'TOTAL':<12} {sum(months_b.values()):>+14.2f} {sum(months_d.values()):>+16.2f} "
      f"{sum(months_d.values())-sum(months_b.values()):>+11.2f}")


# ── 8. Cumulative P&L by week ─────────────────────────────────────────────────
print()
print("── Cumulative P&L (weekly snapshots) ───────────────────────────────")
print(f"  {'Date':<12} {'BASELINE':>14} {'days_in_scan≥2':>16} {'Δ':>12}")
print("  " + "-" * 58)

by_date_b = defaultdict(float)
by_date_d = defaultdict(float)
for t in results["BASELINE"]:
    by_date_b[str(t["exit_date"])[:10]] += t["pnl"]
for t in results["days_in_scan≥2"]:
    by_date_d[str(t["exit_date"])[:10]] += t["pnl"]

all_exit_dates = sorted(set(by_date_b) | set(by_date_d))
cum_b = cum_d = 0.0
prev_week = None
for d in all_exit_dates:
    cum_b += by_date_b.get(d, 0)
    cum_d += by_date_d.get(d, 0)
    week = d[:8] + str((int(d[8:10]) // 7) * 7 + 1).zfill(2)  # approximate week
    if week != prev_week:
        delta = cum_d - cum_b
        arrow = " ▲" if delta > 0 else " ▼"
        print(f"  {d:<12} {cum_b:>+14.2f} {cum_d:>+16.2f} {delta:>+11.2f}{arrow}")
        prev_week = week

# Always print final row
print(f"  {'FINAL':<12} {cum_b:>+14.2f} {cum_d:>+16.2f} {cum_d-cum_b:>+11.2f}")


# ── 9. Top winners and losers ─────────────────────────────────────────────────
for label, trades in results.items():
    print(f"\n── {label}: top 5 winners / worst 5 losers ──")
    by_pnl = sorted(trades, key=lambda t: t["pnl"], reverse=True)
    print(f"  {'Symbol':<8} {'Entry':>12} {'Exit':>12} {'P&L $':>9} {'P&L %':>8}  Reason")
    print("  " + "-" * 64)
    for t in by_pnl[:5]:
        print(f"  {t['symbol']:<8} {str(t['entry_date'])[:10]:>12} {str(t['exit_date'])[:10]:>12} "
              f"{t['pnl']:>+9.2f} {t['pnl_pct']:>+7.1f}%  {t['reason']}")
    print("  ···")
    for t in by_pnl[-5:]:
        print(f"  {t['symbol']:<8} {str(t['entry_date'])[:10]:>12} {str(t['exit_date'])[:10]:>12} "
              f"{t['pnl']:>+9.2f} {t['pnl_pct']:>+7.1f}%  {t['reason']}")
