"""
backtest_max_hold.py — sweep MAX_HOLD_DAYS with no take-profit change.

Fell out of the TP-new-ST backtest: the one variant that improved win rate
(66.2% vs 60.5%) and drawdown ($453 vs $503) was the one with max-hold lifted —
not the one with the adaptive take-profit. 20 of 81 baseline trades exit on the
7-day timer, so the timer is doing a lot of work and was never tuned on its own.

Two scenarios, both with the current exit set unchanged (stop, RSI>65+guard,
gain>=8%+RSI<50):
  RELAX  — push the timer out: 10, 14, 21 days
  REMOVE — no timer at all; positions exit only on stop/RSI/fade

The cost of holding longer is slot contention: MAX_CONCURRENT_POSITIONS=15 is a
hard ceiling, so a position that lingers blocks a new entry. Entries and slot
utilisation are reported alongside P&L so that tradeoff is visible.
"""

import math
import os
from collections import defaultdict
from pathlib import Path

import pandas as pd

CACHE_PATH = Path(os.getenv("BT_CACHE", "/tmp/bt_signals.pkl"))

ADX_THRESHOLD    = 30
AUTO_ORDER_TOP_N = 10
MAX_CONCURRENT   = 15
STOP_PCT         = 0.05
POS_THRESHOLD    = 50
POOL_N           = 100
RSI_EXIT         = 65
RSI_GAIN_GUARD   = 0.0
GAIN_FADING_PCT  = 0.08

NO_LIMIT = 10**9


def position_qty(price, symbol=""):
    dollars = 750 if price < POS_THRESHOLD else 250
    # Crypto is fractionally tradeable. Whole-unit rounding with a max(1,…) floor
    # would buy 1 whole BTC (~$76k) for a $250 slot — a 300x oversized position.
    if "/" in symbol:
        return dollars / price
    return max(1, math.floor(dollars / price))


# ── Load signals ─────────────────────────────────────────────────────────────
START_DATE      = pd.Timestamp(os.getenv("BT_START", "2026-06-01"))
EXCLUDE_CRYPTO  = os.getenv("BT_NO_CRYPTO", "0") == "1"

print("Loading cached signals…", flush=True)
sdf = pd.read_pickle(CACHE_PATH)
sdf["date"] = pd.to_datetime(sdf["date"])
sdf = sdf[sdf["date"] >= START_DATE].copy()
if os.getenv("BT_END"):
    sdf = sdf[sdf["date"] < pd.Timestamp(os.environ["BT_END"])].copy()
if EXCLUDE_CRYPTO:
    n0 = len(sdf)
    sdf = sdf[~sdf["symbol"].str.contains("/", na=False)].copy()
    print(f"  excluded crypto pairs: {n0 - len(sdf):,} rows dropped")
all_dates = sorted(sdf["date"].unique())
print(f"  {len(sdf):,} rows | {sdf['symbol'].nunique():,} symbols")
print(f"  Range: {str(all_dates[0])[:10]} → {str(all_dates[-1])[:10]} "
      f"({len(all_dates)} trading days)")

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


# ── Simulation engine ────────────────────────────────────────────────────────
def simulate(max_hold: int):
    open_pos, trades = {}, []
    total_placed = 0
    slots_used = []       # open positions at each day's close
    blocked_days = 0      # days where the concurrency ceiling blocked entries

    for i, signal_date in enumerate(all_dates):
        next_date = all_dates[i + 1] if i + 1 < len(all_dates) else None

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
                exit_price, exit_reason = bar["open"], "gap-down stop"
            elif bar["low"] <= stop:
                exit_price, exit_reason = stop, "stop hit"
            elif bar["rsi"] > RSI_EXIT and gain >= RSI_GAIN_GUARD:
                exit_price, exit_reason = bar["close"], f"RSI>{RSI_EXIT}"
            elif gain >= GAIN_FADING_PCT and bar["rsi"] < 50:
                exit_price, exit_reason = bar["close"], "gain>=8%+RSI<50"
            elif days >= max_hold:
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

        if next_date is None:
            slots_used.append(len(open_pos))
            continue
        if len(open_pos) >= MAX_CONCURRENT:
            blocked_days += 1
            slots_used.append(len(open_pos))
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
            ["score", "rs_return", "adx", "vol_ratio"], ascending=False)
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
                             "qty": position_qty(ep, sym), "entry_date": next_date}
            placed += 1
            total_placed += 1
        slots_used.append(len(open_pos))

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
    avg_slots = sum(slots_used) / len(slots_used) if slots_used else 0
    return trades, total_placed, avg_slots, blocked_days


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
        cum += by_day[d]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    hold = [(t["exit_date"] - t["entry_date"]).days for t in trades]
    mh = [t for t in trades if t["reason"] == "max hold"]
    return dict(
        n=len(trades), win_rate=len(wins) / len(trades) * 100,
        total_pnl=total,
        gross_profit=sum(t["pnl"] for t in wins),
        gross_loss=sum(t["pnl"] for t in losses),
        avg_win=sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0,
        avg_loss=sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0,
        profit_factor=pf, max_drawdown=max_dd,
        avg_hold=sum(hold) / len(hold), max_hold_seen=max(hold),
        stops=sum(1 for t in trades if "stop" in t["reason"]),
        rsi_exits=sum(1 for t in trades if t["reason"].startswith("RSI")),
        fade_exits=sum(1 for t in trades if t["reason"].startswith("gain")),
        maxhold_exits=len(mh),
        maxhold_pnl=sum(t["pnl"] for t in mh),
        open_at_end=sum(1 for t in trades if t["reason"] == "open at end"),
    )


# ── Variants ─────────────────────────────────────────────────────────────────
variants = [
    ("A: 7d (CURRENT)", 7),
    ("B: RELAX 10d",   10),
    ("C: RELAX 14d",   14),
    ("D: RELAX 21d",   21),
    ("E: REMOVE",      NO_LIMIT),
]

print("\nRunning simulations…", flush=True)
results, meta = {}, {}
for label, mh in variants:
    trades, n_placed, avg_slots, blocked = simulate(mh)
    results[label] = trades
    meta[label] = (n_placed, avg_slots, blocked)
    print(f"  {label:<18} → {len(trades):>3} trades  ({n_placed} entries, "
          f"avg {avg_slots:.1f}/15 slots, {blocked} blocked days)", flush=True)

S      = {k: stats(v) for k, v in results.items()}
labels = [v[0] for v in variants]
base   = S["A: 7d (CURRENT)"]

W = 16
sep = "=" * (28 + W * len(labels))
print()
print(sep)
print(f"  {'Metric':<26}" + "".join(f"{l:>{W}}" for l in labels))
print(sep)


def row(title, key, fmt="{:.1f}", suffix="", higher_better=True):
    bv = base.get(key, 0)
    line = f"  {title:<26}"
    for i, lbl in enumerate(labels):
        v = S[lbl].get(key, 0)
        s = fmt.format(v) + suffix
        if i > 0 and abs(v - bv) > 1e-6:
            better = (v > bv) if higher_better else (v < bv)
            s += " ▲" if better else " ▼"
        line += f"{s:>{W}}"
    print(line)


row("Trades",          "n",             "{:.0f}")
row("Win rate",        "win_rate",      "{:.1f}", "%")
row("Total P&L",       "total_pnl",     "${:+.2f}")
row("Gross profit",    "gross_profit",  "${:+.2f}")
row("Gross loss",      "gross_loss",    "${:+.2f}")
row("Avg win",         "avg_win",       "{:+.1f}", "%")
row("Avg loss",        "avg_loss",      "{:+.1f}", "%", False)
row("Profit factor",   "profit_factor", "{:.2f}")
row("Max drawdown",    "max_drawdown",  "${:.2f}", "", False)
row("Avg hold (days)", "avg_hold",      "{:.1f}")
row("Longest hold",    "max_hold_seen", "{:.0f}")
row("Stop-outs",       "stops",         "{:.0f}", "", False)
row("RSI exits",       "rsi_exits",     "{:.0f}")
row("Fade exits",      "fade_exits",    "{:.0f}")
row("Max-hold exits",  "maxhold_exits", "{:.0f}", "", False)
row("Max-hold P&L",    "maxhold_pnl",   "${:+.2f}")
row("Open at end",     "open_at_end",   "{:.0f}", "", False)
print(sep)

line = f"  {'Entries placed':<26}"
for lbl in labels:
    line += f"{meta[lbl][0]:>{W}}"
print(line)
line = f"  {'Avg slots used (of 15)':<26}"
for lbl in labels:
    line += f"{meta[lbl][1]:>{W}.1f}"
print(line)
line = f"  {'Days blocked by ceiling':<26}"
for lbl in labels:
    line += f"{meta[lbl][2]:>{W}}"
print(line)
print(sep)

print()
print(f"── Delta vs A (7d current) {'─' * max(0, W * len(labels) - 18)}")
for title, key, fmt in [
    ("P&L Δ",   "total_pnl",     "${:+.2f}"),
    ("WR Δ",    "win_rate",      "{:+.1f}%"),
    ("PF Δ",    "profit_factor", "{:+.2f}"),
    ("MaxDD Δ", "max_drawdown",  "${:+.2f}"),
]:
    bv = base.get(key, 0)
    line = f"  {title:<10}"
    for i, lbl in enumerate(labels):
        v = S[lbl].get(key, 0)
        raw = (bv - v) if key == "max_drawdown" else (v - bv)
        s = "—" if i == 0 else fmt.format(raw) + (" ▲" if raw > 0 else " ▼" if raw < 0 else "")
        line += f"{s:>{W}}"
    print(line)

print()
print(sep)
print("  CONCLUSION — ranked by Total P&L")
print(sep)
ranked = sorted([(l, S[l]) for l in labels], key=lambda x: x[1]["total_pnl"], reverse=True)
base_pnl, base_pf = base["total_pnl"], base["profit_factor"]
print(f"  {'Rank':<5} {'Variant':<20} {'P&L':>10} {'PF':>7} {'WR':>8} "
      f"{'MaxDD':>10} {'AvgHold':>9}  Verdict")
print("  " + "-" * 92)
for rank, (lbl, s) in enumerate(ranked, 1):
    if lbl == "A: 7d (CURRENT)":
        verdict = "← baseline"
    elif s["total_pnl"] > base_pnl and s["profit_factor"] > base_pf:
        verdict = "BETTER ✓"
    elif s["total_pnl"] > base_pnl or s["profit_factor"] > base_pf:
        verdict = "MIXED"
    else:
        verdict = "WORSE ✗"
    print(f"  {rank:<5} {lbl:<20} {s['total_pnl']:>+10.2f} {s['profit_factor']:>7.2f} "
          f"{s['win_rate']:>7.1f}% {s['max_drawdown']:>10.2f} {s['avg_hold']:>8.1f}d  {verdict}")
print(sep)
