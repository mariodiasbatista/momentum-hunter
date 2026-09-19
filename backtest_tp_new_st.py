"""
backtest_tp_new_st.py — TP-new-ST: adaptive per-stock take-profit.

Current strategy exits on a FLAT 8% gain (plus RSI/stop/max-hold). That target is
the same for a sleepy utility and a biotech that routinely moves 30% in a week —
so it is simultaneously unreachable for one and leaves money on the table for the
other.

TP-new-ST derives the take-profit per stock from that stock's own Max Favorable
Excursion (MFE) distribution, ported from the llm-trader project:

  1. For every rolling window in the stock's pre-entry history, measure how far
     price ran up within HORIZON days: (max(high[i+1:i+1+H]) / close[i] - 1).
  2. Sort those runs. This is the MFE distribution.
  3. If the flat 8% target is reached in >= keep_flat_reach_pct of windows, the
     flat target is already achievable — keep it.
  4. Otherwise take the level the stock reaches in target_reach_probability of
     windows, clamped to [tp_min, tp_max].

The lookback is strictly PRE-ENTRY (bisect on the entry date) — no lookahead.

Variants:
  A: CURRENT             production rules as deployed today
  B: TP-new-ST H=7       adaptive TP, horizon matched to MAX_HOLD_DAYS=7
  C: TP-new-ST H=20      adaptive TP, llm-trader's native 20-day horizon
  D: TP-new-ST H=7 free  adaptive TP with max-hold lifted (target gets time to work)
  E: flat 8% hard TP     control — isolates "intrabar hard TP" from "adaptive"
"""

import math
import os
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

import pandas as pd

DB_PATH    = "data/momentum.db"
CACHE_PATH = Path(os.getenv("BT_CACHE", "/tmp/bt_signals.pkl"))

ADX_THRESHOLD    = 30
AUTO_ORDER_TOP_N = 10
MAX_CONCURRENT   = 15
MAX_HOLD         = 7
STOP_PCT         = 0.05
POS_THRESHOLD    = 50
POOL_N           = 100
RSI_EXIT         = 65
RSI_GAIN_GUARD   = 0.0     # deployed fix: only RSI-exit at breakeven or better
GAIN_FADING_PCT  = 0.08    # gain >= 8% + RSI < 50 → exit

# llm-trader adaptive_take_profit config
ADAPTIVE_CFG = {
    "keep_flat_reach_pct":       50,
    "target_reach_probability":  0.7,
    "lookback_days":             400,
    "min_windows":               40,
    "tp_min":                    0.03,
    "tp_max":                    0.60,
}
FLAT_TP = 0.08


def position_qty(price, symbol=""):
    dollars = 750 if price < POS_THRESHOLD else 250
    # Crypto is fractionally tradeable. Whole-unit rounding with a max(1,…) floor
    # would buy 1 whole BTC (~$76k) for a $250 slot — a 300x oversized position.
    if "/" in symbol:
        return dollars / price
    return max(1, math.floor(dollars / price))


# ── llm-trader exit_levels.py (verbatim port) ────────────────────────────────
def mfe_distribution(closes, highs, horizon_days):
    out = []
    for i in range(len(closes) - horizon_days):
        base = closes[i]
        if base <= 0:
            continue
        out.append((max(highs[i + 1:i + 1 + horizon_days]) / base - 1) * 100)
    return sorted(out)


def reach_rate(dist, level_pct):
    if not dist:
        return None
    return sum(1 for x in dist if x >= level_pct) / len(dist) * 100


def level_at_reach_probability(dist, probability):
    if not dist:
        return None
    idx = int(round((1.0 - probability) * (len(dist) - 1)))
    return dist[min(len(dist) - 1, max(0, idx))]


def resolve_take_profit(dist, flat_tp, cfg):
    if not dist or len(dist) < cfg["min_windows"]:
        return None
    rr = reach_rate(dist, flat_tp * 100)
    if rr is not None and rr >= cfg["keep_flat_reach_pct"]:
        return flat_tp
    level = level_at_reach_probability(dist, cfg["target_reach_probability"])
    if level is None:
        return None
    return max(cfg["tp_min"], min(cfg["tp_max"], level / 100.0))


# ── Load signals ─────────────────────────────────────────────────────────────
START_DATE = pd.Timestamp("2026-06-01")

print("Loading cached signals…", flush=True)
sdf = pd.read_pickle(CACHE_PATH)
sdf["date"] = pd.to_datetime(sdf["date"])
sdf = sdf[sdf["date"] >= START_DATE].copy()
all_dates = sorted(sdf["date"].unique())
print(f"  {len(sdf):,} rows | {sdf['symbol'].nunique():,} symbols")
print(f"  Range: {str(all_dates[0])[:10]} → {str(all_dates[-1])[:10]} "
      f"({len(all_dates)} trading days)")

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

# ── Load raw bar history for the candidate universe only ─────────────────────
# MFE needs pre-entry history that predates the signal cache window, so it comes
# from the DB. Restricted to symbols that can actually be bought.
cand_mask = (
    (sdf["score"]     >= 6) &
    (sdf["adx"]        > ADX_THRESHOLD) &
    (~sdf["vol_drying"]) &
    (~sdf["macd_shrink"]) &
    (sdf["exit_mode"] == "trailing_stop") &
    (sdf["in_prev_scan"])
)
universe = sorted(sdf[cand_mask]["symbol"].unique())
print(f"\nLoading bar history for {len(universe):,} candidate symbols…", flush=True)

conn = sqlite3.connect(DB_PATH)
placeholders = ",".join("?" * len(universe))
hist = pd.read_sql(
    f"SELECT symbol, date, high, close FROM bars WHERE symbol IN ({placeholders})",
    conn, params=universe)
conn.close()
hist["date"] = pd.to_datetime(hist["date"])
hist = hist.sort_values(["symbol", "date"])
print(f"  {len(hist):,} bars | {hist['symbol'].nunique():,} symbols "
      f"| {str(hist['date'].min())[:10]} → {str(hist['date'].max())[:10]}")

HIST = {}
for sym, g in hist.groupby("symbol", sort=False):
    HIST[sym] = (g["date"].tolist(), g["close"].tolist(), g["high"].tolist())

_tp_cache = {}
TP_KIND = defaultdict(int)


def adaptive_tp(symbol, entry_date, horizon):
    """Take-profit fraction for `symbol` using only bars strictly before entry."""
    key = (symbol, entry_date, horizon)
    if key in _tp_cache:
        return _tp_cache[key]

    rec = HIST.get(symbol)
    if rec is None:
        _tp_cache[key] = (FLAT_TP, "no-history")
        return _tp_cache[key]

    dates, closes, highs = rec
    end = bisect_left(dates, entry_date)                 # strictly pre-entry
    start = max(0, end - ADAPTIVE_CFG["lookback_days"])
    dist = mfe_distribution(closes[start:end], highs[start:end], horizon)

    tp = resolve_take_profit(dist, FLAT_TP, ADAPTIVE_CFG)
    if tp is None:
        out = (FLAT_TP, "thin-history")
    elif abs(tp - FLAT_TP) < 1e-9:
        out = (tp, "kept-flat")
    else:
        out = (tp, "adaptive")
    _tp_cache[key] = out
    return out


# ── Simulation engine ────────────────────────────────────────────────────────
def simulate(use_tp, horizon, use_max_hold=True, flat_only=False):
    """
    use_tp        : apply a hard intrabar take-profit
    horizon       : MFE horizon in days (ignored when flat_only)
    use_max_hold  : force-close at MAX_HOLD days
    flat_only     : use the flat 8% target instead of the adaptive one
    """
    open_pos, trades = {}, []
    total_placed = 0
    tp_kinds = defaultdict(int)
    tp_values = []

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
            # Stop is checked first — a gap below the stop resolves before any upside.
            if bar["open"] <= stop:
                exit_price, exit_reason = bar["open"], "gap-down stop"
            elif bar["low"] <= stop:
                exit_price, exit_reason = stop, "stop hit"
            elif use_tp and bar["high"] >= entry * (1 + pos["tp"]):
                exit_price, exit_reason = entry * (1 + pos["tp"]), "take profit"
            elif bar["rsi"] > RSI_EXIT and gain >= RSI_GAIN_GUARD:
                exit_price, exit_reason = bar["close"], f"RSI>{RSI_EXIT}"
            elif not use_tp and gain >= GAIN_FADING_PCT and bar["rsi"] < 50:
                exit_price, exit_reason = bar["close"], "gain>=8%+RSI<50"
            elif use_max_hold and days >= MAX_HOLD:
                exit_price, exit_reason = bar["close"], "max hold"

            if exit_price is not None:
                pnl = round((exit_price - entry) * qty, 2)
                trades.append({
                    "symbol": sym, "entry_date": pos["entry_date"],
                    "exit_date": signal_date, "entry": entry, "exit": exit_price,
                    "qty": qty, "pnl": pnl,
                    "pnl_pct": round((exit_price - entry) / entry * 100, 2),
                    "reason": exit_reason, "tp": pos["tp"],
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
            if flat_only or not use_tp:
                tp, kind = FLAT_TP, "flat"
            else:
                tp, kind = adaptive_tp(sym, next_date, horizon)
            tp_kinds[kind] += 1
            tp_values.append(tp)
            stop = min(ep - c["atr"] * 1.5, ep * (1 - STOP_PCT), ep - 0.01)
            open_pos[sym] = {"entry": ep, "stop": stop, "tp": tp,
                             "qty": position_qty(ep, sym), "entry_date": next_date}
            placed += 1
            total_placed += 1

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
                "reason": "open at end", "tp": pos["tp"],
            })
    return trades, total_placed, tp_kinds, tp_values


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
    return dict(
        n=len(trades), wins=len(wins), losses=len(losses),
        win_rate=len(wins) / len(trades) * 100,
        total_pnl=total,
        avg_win=sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0,
        avg_loss=sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0,
        profit_factor=pf,
        max_drawdown=max_dd,
        gross_profit=sum(t["pnl"] for t in wins),
        gross_loss=sum(t["pnl"] for t in losses),
        stops=sum(1 for t in trades if "stop" in t["reason"]),
        tp_hits=sum(1 for t in trades if t["reason"] == "take profit"),
        rsi_exits=sum(1 for t in trades if t["reason"].startswith("RSI")),
        maxhold_exits=sum(1 for t in trades if t["reason"] == "max hold"),
        avg_hold=sum(hold) / len(hold),
    )


# ── Variants ─────────────────────────────────────────────────────────────────
variants = [
    ("A: CURRENT",            dict(use_tp=False, horizon=7)),
    ("B: TP-new-ST H=7",      dict(use_tp=True,  horizon=7)),
    ("C: TP-new-ST H=20",     dict(use_tp=True,  horizon=20)),
    ("D: TP-new-ST H=7 free", dict(use_tp=True,  horizon=7, use_max_hold=False)),
    ("E: flat 8% hard TP",    dict(use_tp=True,  horizon=7, flat_only=True)),
]

print("\nRunning simulations…", flush=True)
results, order_counts, kinds_map, vals_map = {}, {}, {}, {}
for label, kw in variants:
    trades, n_placed, kinds, vals = simulate(**kw)
    results[label] = trades
    order_counts[label] = n_placed
    kinds_map[label] = kinds
    vals_map[label] = vals
    print(f"  {label:<24} → {len(trades):>3} trades  ({n_placed} entries)", flush=True)

S      = {k: stats(v) for k, v in results.items()}
labels = [v[0] for v in variants]
base   = S["A: CURRENT"]

# ── Results table ────────────────────────────────────────────────────────────
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


row("Trades",         "n",             "{:.0f}")
row("Win rate",       "win_rate",      "{:.1f}", "%")
row("Total P&L",      "total_pnl",     "${:+.2f}")
row("Gross profit",   "gross_profit",  "${:+.2f}")
row("Gross loss",     "gross_loss",    "${:+.2f}")
row("Avg win",        "avg_win",       "{:+.1f}", "%")
row("Avg loss",       "avg_loss",      "{:+.1f}", "%", False)
row("Profit factor",  "profit_factor", "{:.2f}")
row("Max drawdown",   "max_drawdown",  "${:.2f}", "", False)
row("Avg hold (days)", "avg_hold",     "{:.1f}")
row("Stop-outs",      "stops",         "{:.0f}", "", False)
row("TP hits",        "tp_hits",       "{:.0f}")
row("RSI exits",      "rsi_exits",     "{:.0f}")
row("Max-hold exits", "maxhold_exits", "{:.0f}", "", False)
print(sep)

# ── Target derivation diagnostics ────────────────────────────────────────────
print()
print("── How TP-new-ST set its targets " + "─" * 40)
for lbl in labels:
    vals = [v for v in vals_map[lbl]]
    if not vals:
        continue
    k = kinds_map[lbl]
    med = sorted(vals)[len(vals) // 2]
    print(f"  {lbl:<24} median TP={med * 100:>5.1f}%  "
          f"min={min(vals) * 100:.1f}%  max={max(vals) * 100:.1f}%  "
          f"| " + "  ".join(f"{kk}={vv}" for kk, vv in sorted(k.items())))

# ── Delta vs A ───────────────────────────────────────────────────────────────
print()
print(f"── Delta vs A (CURRENT) {'─' * max(0, W * len(labels) - 16)}")
for title, key, fmt, hb in [
    ("P&L Δ",   "total_pnl",     "${:+.2f}", True),
    ("WR Δ",    "win_rate",      "{:+.1f}%", True),
    ("PF Δ",    "profit_factor", "{:+.2f}",  True),
    ("MaxDD Δ", "max_drawdown",  "${:+.2f}", False),
]:
    bv = base.get(key, 0)
    line = f"  {title:<10}"
    for i, lbl in enumerate(labels):
        v = S[lbl].get(key, 0)
        raw = (bv - v) if key == "max_drawdown" else (v - bv)
        s = "—" if i == 0 else fmt.format(raw) + (" ▲" if raw > 0 else " ▼" if raw < 0 else "")
        line += f"{s:>{W}}"
    print(line)

# ── Conclusion ───────────────────────────────────────────────────────────────
print()
print(sep)
print("  CONCLUSION — is TP-new-ST more profitable than CURRENT?")
print(sep)
ranked = sorted([(l, S[l]) for l in labels], key=lambda x: x[1]["total_pnl"], reverse=True)
base_pnl, base_pf = base["total_pnl"], base["profit_factor"]
print(f"  {'Rank':<5} {'Variant':<26} {'P&L':>10} {'PF':>7} {'WR':>8} "
      f"{'MaxDD':>10} {'TPhits':>7}  Verdict")
print("  " + "-" * 95)
for rank, (lbl, s) in enumerate(ranked, 1):
    if lbl == "A: CURRENT":
        verdict = "← baseline"
    elif s["total_pnl"] > base_pnl and s["profit_factor"] > base_pf:
        verdict = "BETTER ✓"
    elif s["total_pnl"] > base_pnl or s["profit_factor"] > base_pf:
        verdict = "MIXED"
    else:
        verdict = "WORSE ✗"
    print(f"  {rank:<5} {lbl:<26} {s['total_pnl']:>+10.2f} {s['profit_factor']:>7.2f} "
          f"{s['win_rate']:>7.1f}% {s['max_drawdown']:>10.2f} {s['tp_hits']:>7}  {verdict}")
print(sep)
