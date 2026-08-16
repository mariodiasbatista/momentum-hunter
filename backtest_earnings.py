"""
backtest_earnings.py — test earnings-proximity filter against current production strategy.

Skips entry if an earnings date falls within BUFFER trading days of entry.
Tests buffer windows: 3, 5, 7, 10 days.

Earnings dates fetched from yfinance and cached in /tmp/earnings_cache.pkl.
Baseline matches backtest_all_options.py CURRENT strategy.
"""

import math
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd
import yfinance as yf

CACHE_PATH    = Path("/tmp/bt_signals.pkl")
EARN_CACHE    = Path("/tmp/earnings_cache.pkl")
BUFFERS       = [3, 5, 7, 10]

ADX_THRESHOLD = 30
AUTO_ORDER_TOP_N = 10
POS_THRESHOLD = 50
MAX_CONCURRENT = 15
RSI_EXIT      = 65
MAX_HOLD      = 7
STOP_PCT      = 0.05


def position_qty(price):
    return max(1, math.floor((750 if price < POS_THRESHOLD else 250) / price))


# ── Load signals cache ────────────────────────────────────────────────────────
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


# ── Fetch earnings dates ──────────────────────────────────────────────────────
# Find candidate symbols (those that pass base filters at least once)
base_mask = (
    (sdf["score"] >= 6) &
    (sdf["adx"] > ADX_THRESHOLD) &
    (~sdf["vol_drying"]) &
    (~sdf["macd_shrink"]) &
    (sdf["exit_mode"] == "trailing_stop") &
    (sdf["in_prev_scan"])
)
candidate_syms = sdf[base_mask]["symbol"].unique().tolist()

if EARN_CACHE.exists():
    print(f"\nLoading earnings cache ({EARN_CACHE})…", flush=True)
    earnings_map = pd.read_pickle(EARN_CACHE)
    missing = [s for s in candidate_syms if s not in earnings_map]
    if missing:
        print(f"  {len(missing)} symbols missing from cache — fetching…", flush=True)
else:
    print(f"\nFetching earnings dates for {len(candidate_syms)} candidate symbols…", flush=True)
    earnings_map = {}
    missing = candidate_syms

if missing:
    period_start = all_dates[0] - pd.Timedelta(days=14)
    period_end   = all_dates[-1] + pd.Timedelta(days=14)
    failed = 0
    for i, sym in enumerate(missing):
        try:
            t  = yf.Ticker(sym)
            ed = t.earnings_dates
            if ed is not None and not ed.empty:
                dates = pd.DatetimeIndex(ed.index).normalize().tz_localize(None)
                dates = dates[(dates >= period_start) & (dates <= period_end)]
                earnings_map[sym] = sorted(dates.tolist())
            else:
                earnings_map[sym] = []
        except Exception:
            earnings_map[sym] = []
            failed += 1
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(missing)} fetched ({failed} failed so far)", flush=True)
            time.sleep(1)
    print(f"  Done — {len(missing)} fetched, {failed} failed")
    pd.to_pickle(earnings_map, EARN_CACHE)
    print(f"  Saved to {EARN_CACHE}")

# Build lookup: symbol → sorted list of earnings dates as pd.Timestamps
earn_dates = {sym: [pd.Timestamp(d) for d in dates]
              for sym, dates in earnings_map.items()}

# Precompute trading-day index for buffer calculation
td_index = {d: i for i, d in enumerate(all_dates)}


def near_earnings(symbol: str, entry_date: pd.Timestamp, buffer: int) -> bool:
    """Return True if any earnings date falls within `buffer` trading days of entry_date."""
    dates = earn_dates.get(symbol, [])
    if not dates:
        return False
    entry_idx = td_index.get(entry_date)
    if entry_idx is None:
        return False
    for ed in dates:
        ed_idx = td_index.get(ed)
        if ed_idx is not None and abs(ed_idx - entry_idx) <= buffer:
            return True
    return False


# ── Simulation engine ─────────────────────────────────────────────────────────
def simulate(earnings_buffer: int | None = None):
    open_pos = {}
    trades   = []

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
        cands = day[mask].sort_values(["rs_return", "adx", "vol_ratio"], ascending=False)

        placed = 0
        for _, c in cands.iterrows():
            if placed >= AUTO_ORDER_TOP_N or len(open_pos) >= MAX_CONCURRENT:
                break
            sym = c["symbol"]
            if sym in open_pos:
                continue
            if earnings_buffer is not None and near_earnings(sym, next_date, earnings_buffer):
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
    return trades


# ── Stats ─────────────────────────────────────────────────────────────────────
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
        skipped_for_earnings=0,  # filled below
    )


# ── Run variants ──────────────────────────────────────────────────────────────
print("\nRunning simulations…", flush=True)
variants = {"CURRENT (no filter)": None}
for b in BUFFERS:
    variants[f"+EARN-{b}d"] = b

results = {}
skipped = {}
for label, buf in variants.items():
    trades = simulate(earnings_buffer=buf)
    results[label] = trades
    # Count skipped entries
    if buf is not None:
        skip_count = 0
        for i, signal_date in enumerate(all_dates):
            next_date = all_dates[i + 1] if i + 1 < len(all_dates) else None
            if next_date is None:
                continue
            day  = sdf[sdf["date"] == signal_date]
            mask = (
                (day["score"] >= 6) & (day["adx"] > ADX_THRESHOLD) &
                (~day["vol_drying"]) & (~day["macd_shrink"]) &
                (day["exit_mode"] == "trailing_stop") & (day["in_prev_scan"])
            )
            for _, c in day[mask].iterrows():
                if near_earnings(c["symbol"], next_date, buf):
                    skip_count += 1
        skipped[label] = skip_count
    print(f"  {label:<24} → {len(trades):>3} trades", flush=True)

S = {k: stats(v) for k, v in results.items()}
base = S["CURRENT (no filter)"]

# ── Results table ─────────────────────────────────────────────────────────────
labels = list(variants.keys())
W = 14

print()
sep = "=" * (26 + W * len(labels))
print(sep)
print(f"  {'Metric':<24}" + "".join(f"{l:>{W}}" for l in labels))
print(sep)

def row(title, key, fmt="{:.1f}", suffix="", hb=True):
    bv = base.get(key, 0)
    line = f"  {title:<24}"
    for i, lbl in enumerate(labels):
        v = S[lbl].get(key, 0)
        s = fmt.format(v) + suffix
        if i > 0 and abs(v - bv) > 1e-6:
            better = (v > bv) if hb else (v < bv)
            s += " ▲" if better else " ▼"
        line += f"{s:>{W}}"
    print(line)

row("Trades",        "n",             "{:.0f}")
row("Win rate",      "win_rate",      "{:.1f}", "%",  True)
row("Total P&L",     "total_pnl",     "${:+.2f}", "", True)
row("Avg win",       "avg_win",       "{:+.1f}", "%", True)
row("Avg loss",      "avg_loss",      "{:+.1f}", "%", False)
row("Profit factor", "profit_factor", "{:.2f}",  "",  True)
row("Max drawdown",  "max_drawdown",  "${:.2f}", "",  False)
row("Stop-outs",     "stops",         "{:.0f}",  "",  False)
print(sep)

# Skipped row
skip_line = f"  {'Entries skipped':<24}" + f"{'—':>{W}}"
for lbl in labels[1:]:
    skip_line += f"{skipped.get(lbl, 0):>{W}}"
print(skip_line)
print(sep)

# Delta vs current
print()
print(f"── Delta vs CURRENT {'─' * max(0, W * len(labels) - 8)}")
for title, key, fmt, hb in [
    ("P&L Δ",   "total_pnl",     "${:+.2f}", True),
    ("WR Δ",    "win_rate",      "{:+.1f}%", True),
    ("PF Δ",    "profit_factor", "{:+.2f}",  True),
    ("MaxDD Δ", "max_drawdown",  "${:+.2f}", False),
    ("Stops Δ", "stops",         "{:+.0f}",  False),
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

# Conclusion
print()
print(sep)
print("  CONCLUSION — ranked by Total P&L")
print(sep)
ranked = sorted([(lbl, S[lbl]) for lbl in labels], key=lambda x: x[1]["total_pnl"], reverse=True)
base_pnl = base["total_pnl"]
base_pf  = base["profit_factor"]
print(f"  {'Rank':<5} {'Variant':<26} {'P&L':>10} {'PF':>7} {'WR':>8} {'MaxDD':>10}  {'Stops':>6}  {'Verdict'}")
print("  " + "-" * 80)
for rank, (lbl, s) in enumerate(ranked, 1):
    if lbl == "CURRENT (no filter)":
        verdict = "← current"
    elif s["total_pnl"] > base_pnl and s["profit_factor"] > base_pf:
        verdict = "BETTER ✓"
    elif s["total_pnl"] > base_pnl or s["profit_factor"] > base_pf:
        verdict = "MIXED"
    else:
        verdict = "WORSE ✗"
    print(f"  {rank:<5} {lbl:<26} {s['total_pnl']:>+10.2f} {s['profit_factor']:>7.2f} "
          f"{s['win_rate']:>7.1f}% {s['max_drawdown']:>10.2f}  {s['stops']:>6}   {verdict}")
print(sep)
