"""
backtest_options.py — simulate 4 improvement options vs current production strategy.

All variants include current production controls (days_in_scan≥2, dedup, max 15 positions).
Full dataset: 121 trading days, 2026-02-19 → 2026-06-19.

  BASELINE    : current production (days≥2, score≥6, RSI>70 exit, max hold 10d)
  V1-regime   : exit all positions when SPY closes below its 9-day EMA
  V2a-rsi65   : exit on RSI > 65 instead of 70  (take profits earlier)
  V2b-rsi75   : exit on RSI > 75 instead of 70  (let winners run)
  V3-hold7    : reduce max hold from 10 → 7 days (free capital faster)
  V4-score7   : require score ≥ 7 instead of 6  (stricter entry quality)
"""

import math, sqlite3
from collections import defaultdict
from pathlib import Path

import pandas as pd

CACHE_PATH = Path("/tmp/bt_signals.pkl")
DB_PATH    = "data/momentum.db"

ADX_THRESHOLD    = 30
MAX_HOLD_DAYS    = 10
AUTO_ORDER_TOP_N = 10
STOP_PCT         = 0.05
POS_THRESHOLD    = 50
MAX_CONCURRENT   = 15


def position_qty(price):
    return max(1, math.floor((750 if price < POS_THRESHOLD else 250) / price))


# ── 1. Load signal cache ──────────────────────────────────────────────────────
print("Loading cached signals…", flush=True)
sdf = pd.read_pickle(CACHE_PATH)
sdf["date"] = pd.to_datetime(sdf["date"])
all_dates = sorted(sdf["date"].unique())
print(f"  {len(sdf):,} rows | {sdf['symbol'].nunique():,} symbols | {len(all_dates)} trading days")
print(f"  Range: {str(all_dates[0])[:10]} → {str(all_dates[-1])[:10]}")


# ── 2. days_in_scan≥2 flag ────────────────────────────────────────────────────
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


# ── 3. SPY 9-EMA regime signal ────────────────────────────────────────────────
print("Loading SPY bars for regime signal…", flush=True)
conn = sqlite3.connect(DB_PATH)
spy = pd.read_sql(
    "SELECT date, open, close FROM bars WHERE symbol='SPY' ORDER BY date",
    conn, parse_dates=["date"]
)
conn.close()
spy = spy.set_index("date").sort_index()
spy["ema9"] = spy["close"].ewm(span=9, adjust=False).mean()
spy["below_ema9"] = spy["close"] < spy["ema9"]
# Signal fires on next day: SPY closed below EMA9 yesterday → regime exit today
spy["regime_exit_today"] = spy["below_ema9"].shift(1).fillna(False)
spy_regime = spy["regime_exit_today"].to_dict()   # date → bool
spy_close  = spy["close"].to_dict()
print(f"  SPY regime-exit days in backtest window: "
      f"{sum(1 for d in all_dates if spy_regime.get(d, False))}")


# ── 4. Price lookup ───────────────────────────────────────────────────────────
price_lkp = sdf.set_index(["symbol", "date"])[
    ["open", "high", "low", "close", "rsi", "atr"]
].to_dict("index")


# ── 5. Simulation engine ──────────────────────────────────────────────────────
def simulate(cfg: dict) -> list[dict]:
    """
    cfg keys (all optional, default = baseline behaviour):
      regime_exit   bool  — exit all positions when SPY closes below 9-EMA
      rsi_exit      int   — RSI threshold for overbought exit  (default 70)
      max_hold      int   — max holding days                   (default 10)
      min_score     int   — minimum score for entry            (default 6)
    """
    rsi_exit  = cfg.get("rsi_exit",  70)
    max_hold  = cfg.get("max_hold",  MAX_HOLD_DAYS)
    min_score = cfg.get("min_score", 6)
    regime    = cfg.get("regime_exit", False)

    open_pos = {}
    trades   = []

    for i, signal_date in enumerate(all_dates):
        next_date = all_dates[i + 1] if i + 1 < len(all_dates) else None

        # ── Regime exit: SPY closed below 9-EMA yesterday → exit everything now
        if regime and spy_regime.get(signal_date, False):
            for sym in list(open_pos.keys()):
                key = (sym, signal_date)
                if key not in price_lkp:
                    continue
                bar   = price_lkp[key]
                entry = open_pos[sym]["entry"]
                qty   = open_pos[sym]["qty"]
                ep    = bar["open"]   # exit at today's open (gap protection)
                pnl   = round((ep - entry) * qty, 2)
                trades.append({
                    "symbol":     sym,
                    "entry_date": open_pos[sym]["entry_date"],
                    "exit_date":  signal_date,
                    "entry":      entry, "exit": ep, "qty": qty,
                    "pnl":        pnl,
                    "pnl_pct":    round((ep - entry) / entry * 100, 2),
                    "reason":     "regime exit",
                })
            open_pos.clear()
            # Also skip new entries on regime-exit days
            continue

        # ── Standard exit checks ─────────────────────────────────────────────
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
                    "entry":      entry, "exit": exit_price, "qty": qty,
                    "pnl":        pnl,
                    "pnl_pct":    round((exit_price - entry) / entry * 100, 2),
                    "reason":     exit_reason,
                })
                del open_pos[sym]

        if next_date is None or len(open_pos) >= MAX_CONCURRENT:
            continue

        # ── Select candidates for entry tomorrow ─────────────────────────────
        day  = sdf[sdf["date"] == signal_date]
        mask = (
            (day["score"]    >= min_score) &
            (day["adx"]       > ADX_THRESHOLD) &
            (~day["vol_drying"]) &
            (~day["macd_shrink"]) &
            (day["exit_mode"] == "trailing_stop") &
            (day["in_prev_scan"])                    # days_in_scan≥2 always on
        )
        cands = day[mask].sort_values(
            ["rs_return", "adx", "vol_ratio"], ascending=False
        )

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

    # Force-close anything still open at the final bar
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


# ── 6. Run all variants ───────────────────────────────────────────────────────
VARIANTS = {
    "BASELINE":     {},
    "V1-regime":    {"regime_exit": True},
    "V2a-rsi65":    {"rsi_exit": 65},
    "V2b-rsi75":    {"rsi_exit": 75},
    "V3-hold7":     {"max_hold": 7},
    "V4-score7":    {"min_score": 7},
}

print("\nRunning simulations…", flush=True)
results = {}
for label, cfg in VARIANTS.items():
    results[label] = simulate(cfg)
    print(f"  {label:<16} → {len(results[label]):>3} trades", flush=True)


# ── 7. Stats ──────────────────────────────────────────────────────────────────
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

    # Max drawdown (on daily cumulative P&L)
    by_day = defaultdict(float)
    for t in trades:
        by_day[str(t["exit_date"])[:10]] += t["pnl"]
    cum = peak = max_dd = 0.0
    for d in sorted(by_day):
        cum  += by_day[d]
        peak  = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    stops   = sum(1 for t in trades if "stop"    in t["reason"])
    rsi_ex  = sum(1 for t in trades if "RSI"     in t["reason"])
    regime  = sum(1 for t in trades if "regime"  in t["reason"])
    hold_ex = sum(1 for t in trades if "max"     in t["reason"])
    open_ex = sum(1 for t in trades if "open at" in t["reason"])

    return dict(
        n=len(trades), wins=len(wins), losses=len(losses),
        win_rate=wr, total_pnl=total, avg_win=aw, avg_loss=al,
        profit_factor=pf, max_drawdown=max_dd,
        stops=stops, rsi_exits=rsi_ex, regime_exits=regime,
        hold_exits=hold_ex, open_exits=open_ex,
    )

S = {k: stats(v) for k, v in results.items()}
base = S["BASELINE"]


# ── 8. Main results table ─────────────────────────────────────────────────────
labels = list(VARIANTS.keys())
W = 14   # column width

print()
print("=" * (22 + W * len(labels)))
print(f"  {'Metric':<20}" + "".join(f"{l:>{W}}" for l in labels))
print("=" * (22 + W * len(labels)))

def prow(metric, key, fmt, suffix="", higher_better=True):
    vals = [S[l].get(key, 0) for l in labels]
    best = max(vals) if higher_better else min(vals)
    cells = []
    for i, (l, v) in enumerate(zip(labels, vals)):
        txt = fmt.format(v) + suffix
        if i > 0:  # not baseline
            marker = " ◀" if v == best and v != vals[0] else ""
        else:
            marker = ""
        cells.append((txt + marker).rjust(W))
    print(f"  {metric:<20}" + "".join(cells))

prow("Trades",          "n",            "{:.0f}")
prow("Win rate",        "win_rate",     "{:.1f}", "%")
prow("Total P&L",       "total_pnl",    "${:+.0f}")
prow("Avg win",         "avg_win",      "{:+.1f}", "%")
prow("Avg loss",        "avg_loss",     "{:+.1f}", "%",  higher_better=False)
prow("Profit factor",   "profit_factor","{:.2f}")
prow("Max drawdown",    "max_drawdown", "${:.0f}", "",   higher_better=False)
print("  " + "-" * (2 + W * len(labels)))
prow("Stop-outs",       "stops",        "{:.0f}", "",    higher_better=False)
prow("RSI exits",       "rsi_exits",    "{:.0f}")
prow("Regime exits",    "regime_exits", "{:.0f}")
prow("Max-hold exits",  "hold_exits",   "{:.0f}")
print("=" * (22 + W * len(labels)))


# ── 9. Monthly P&L breakdown ──────────────────────────────────────────────────
def monthly_pnl(trades):
    m = defaultdict(float)
    for t in trades:
        m[str(t["exit_date"])[:7]] += t["pnl"]
    return m

monthly = {k: monthly_pnl(v) for k, v in results.items()}
all_months = sorted(set(m for mm in monthly.values() for m in mm))

print()
print("── Monthly P&L ──────────────────────────────────────────────────────────────────")
print(f"  {'Month':<10}" + "".join(f"{l:>{W}}" for l in labels))
print("  " + "-" * (10 + W * len(labels)))
for m in all_months:
    row_vals = [monthly[l].get(m, 0) for l in labels]
    best = max(row_vals[1:], default=0)  # best non-baseline
    cells = []
    for i, v in enumerate(row_vals):
        marker = " ◀" if i > 0 and v == best and best > row_vals[0] else ""
        cells.append((f"{v:>+.0f}" + marker).rjust(W))
    print(f"  {m:<10}" + "".join(cells))
print("  " + "-" * (10 + W * len(labels)))
totals = [sum(monthly[l].values()) for l in labels]
print(f"  {'TOTAL':<10}" + "".join(f"{v:>+{W}.0f}" for v in totals))


# ── 10. Cumulative P&L progression ───────────────────────────────────────────
by_date = {}
for l, trades in results.items():
    d = defaultdict(float)
    for t in trades:
        d[str(t["exit_date"])[:10]] += t["pnl"]
    by_date[l] = d

all_exit_dates = sorted(set(d for bd in by_date.values() for d in bd))
cums = {l: 0.0 for l in labels}

print()
print("── Cumulative P&L ────────────────────────────────────────────────────────────────")
print(f"  {'Date':<12}" + "".join(f"{l:>{W}}" for l in labels))
print("  " + "-" * (12 + W * len(labels)))

prev_month = None
for d in all_exit_dates:
    month = d[:7]
    for l in labels:
        cums[l] += by_date[l].get(d, 0)
    if month != prev_month:
        row = f"  {d:<12}" + "".join(f"{cums[l]:>+{W}.0f}" for l in labels)
        print(row)
        prev_month = month

# Always print the final row
print(f"  {'FINAL':<12}" + "".join(f"{cums[l]:>+{W}.0f}" for l in labels))


# ── 11. Verdict ───────────────────────────────────────────────────────────────
print()
print("── Verdict ──────────────────────────────────────────────────────────────────────")
base_pnl = base["total_pnl"]
for l in labels[1:]:
    s = S[l]
    delta = s["total_pnl"] - base_pnl
    dd_delta = base["max_drawdown"] - s["max_drawdown"]
    verdict = "BETTER" if s["total_pnl"] > base_pnl else "WORSE"
    print(f"  {l:<16}  P&L {s['total_pnl']:>+7.0f}  Δ{delta:>+7.0f}  "
          f"PF {s['profit_factor']:.2f}  DD ${s['max_drawdown']:.0f}  "
          f"WR {s['win_rate']:.1f}%  → {verdict}")
