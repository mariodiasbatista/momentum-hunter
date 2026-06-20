"""
backtest_improvements.py — simulate 4 strategy improvements vs current production baseline.

All variants apply existing production controls (dedup + max 15 concurrent positions).
Backtest period: 2026-05-12 → 2026-06-17 (37 trading days)

  BASELINE   : current production
  V1-days2   : require stock in scan ≥2 consecutive days before entry
  V2-trail   : trailing stop — ratchets up once position gains ≥3%, locks 50% of gain
  V3-spy03   : SPY regime guard tightened to -0.3% open vs prior close (vs -0.5%)
  V4-sector3 : max 3 concurrent positions per sector
  COMBINED   : all 4 improvements together
"""

import json, math, sqlite3, time
from collections import defaultdict
from pathlib import Path

import pandas as pd

CACHE_PATH   = Path("/tmp/bt_signals.pkl")
SECTOR_CACHE = Path("/tmp/sector_cache.json")
DB_PATH      = "data/momentum.db"

BACKTEST_START = "2026-05-12"
BACKTEST_END   = "2026-06-17"

ADX_THRESHOLD    = 30
MAX_HOLD_DAYS    = 10
AUTO_ORDER_TOP_N = 10
STOP_PCT         = 0.05
POS_THRESHOLD    = 50
MAX_CONCURRENT   = 15

STOP_TRAIL_MIN_GAIN = 0.03   # 3% gain before trailing
STOP_TRAIL_LOCK     = 0.50   # lock in 50% of gain


def position_qty(price):
    return max(1, math.floor((750 if price < POS_THRESHOLD else 250) / price))


# ── 1. Load cached signals ────────────────────────────────────────────────────
print("Loading cached signals…", flush=True)
sdf = pd.read_pickle(CACHE_PATH)
sdf["date"] = pd.to_datetime(sdf["date"])
print(f"  {len(sdf):,} rows | {sdf['symbol'].nunique():,} symbols")


# ── 2. Compute days_in_scan flag ──────────────────────────────────────────────
# For each (symbol, date), was this symbol eligible (score≥6) on the PREVIOUS scan date?
print("Computing days_in_scan eligibility…", flush=True)
all_dates  = sorted(sdf["date"].unique())
date_to_idx = {d: i for i, d in enumerate(all_dates)}

eligible_syms = (
    sdf[sdf["score"] >= 6][["symbol", "date"]]
    .assign(in_scan=True)
    .set_index(["symbol", "date"])["in_scan"]
    .to_dict()
)

def prev_scan_date(d):
    idx = date_to_idx.get(d, 0)
    return all_dates[idx - 1] if idx > 0 else None

# Build lookup: (symbol, date) -> was in scan on previous date
def in_prev_scan(symbol, date):
    prev = prev_scan_date(date)
    return prev is not None and eligible_syms.get((symbol, prev), False)

# Vectorized: create shifted-date column and join
elig_df = sdf[sdf["score"] >= 6][["symbol", "date"]].copy()
elig_df["prev_date"] = elig_df["date"].map(lambda d: prev_scan_date(d))
elig_df = elig_df.dropna(subset=["prev_date"])

prev_elig = sdf[sdf["score"] >= 6][["symbol", "date"]].copy()
prev_elig = prev_elig.rename(columns={"date": "prev_date"})
prev_elig["in_prev_scan"] = True

elig_df = elig_df.merge(prev_elig, on=["symbol", "prev_date"], how="left")
elig_df["in_prev_scan"] = elig_df["in_prev_scan"].fillna(False)

sdf = sdf.merge(
    elig_df[["symbol", "date", "in_prev_scan"]],
    on=["symbol", "date"], how="left"
)
sdf["in_prev_scan"] = sdf["in_prev_scan"].fillna(False)
print(f"  Symbols with ≥2 consecutive days in scan (in backtest window): "
      f"{sdf[(sdf['date'] >= BACKTEST_START) & sdf['in_prev_scan'] & (sdf['score'] >= 6)]['symbol'].nunique()}")


# ── 3. Build SPY regime lookup ────────────────────────────────────────────────
print("Building SPY regime lookup…", flush=True)
spy_bars = sdf[sdf["symbol"] == "SPY"][["date", "open", "close"]].sort_values("date").copy()

if spy_bars.empty:
    print("  SPY not in cache — loading from DB…")
    conn = sqlite3.connect(DB_PATH)
    spy_bars = pd.read_sql(
        "SELECT date, open, close FROM bars WHERE symbol='SPY' ORDER BY date",
        conn, parse_dates=["date"]
    )
    conn.close()

spy_bars = spy_bars.set_index("date").sort_index()
spy_bars["prev_close"] = spy_bars["close"].shift(1)
spy_bars["open_ret_pct"] = (spy_bars["open"] - spy_bars["prev_close"]) / spy_bars["prev_close"] * 100
spy_regime = spy_bars["open_ret_pct"].to_dict()
print(f"  {len(spy_regime)} SPY dates available")


# ── 4. Load sector map ────────────────────────────────────────────────────────
sector_map = json.loads(SECTOR_CACHE.read_text()) if SECTOR_CACHE.exists() else {}
print(f"  Sector map: {len(sector_map)} symbols")


# ── 5. Build price lookup ─────────────────────────────────────────────────────
bt_dates = (
    sdf[(sdf["date"] >= BACKTEST_START) & (sdf["date"] <= BACKTEST_END)]
    ["date"].sort_values().unique()
)
print(f"\nBacktest: {str(bt_dates[0])[:10]} → {str(bt_dates[-1])[:10]} ({len(bt_dates)} days)\n")

price_lkp = sdf.set_index(["symbol", "date"])[
    ["open", "high", "low", "close", "rsi", "atr"]
].to_dict("index")


# ── 6. Simulation engine ──────────────────────────────────────────────────────
def simulate(cfg: dict) -> list[dict]:
    """
    cfg keys:
      days2        bool  — require in_prev_scan before entry
      trail_stop   bool  — trail stop up as position gains
      spy_thresh   float — skip entries if SPY open_ret <= -spy_thresh (None = disabled)
      sector_cap   int   — max positions per sector (None = no cap)
    """
    open_pos = {}
    trades   = []

    for i, signal_date in enumerate(bt_dates):
        # ── Update trailing stops + exit checks ──────────────────────────────
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

            # Trail stop up using previous day's close
            if cfg.get("trail_stop"):
                prev_close = pos.get("prev_close", entry)
                gain = (prev_close - entry) / entry
                if gain >= STOP_TRAIL_MIN_GAIN:
                    trail_stop = entry + (prev_close - entry) * STOP_TRAIL_LOCK
                    if trail_stop > stop:
                        stop = trail_stop
                        open_pos[sym]["stop"] = stop

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
                    "symbol": sym, "entry_date": pos["entry_date"],
                    "exit_date": signal_date, "entry": entry,
                    "exit": exit_price, "qty": qty, "pnl": pnl,
                    "pnl_pct": round((exit_price - entry) / entry * 100, 2),
                    "reason": exit_reason,
                })
                del open_pos[sym]
            else:
                open_pos[sym]["prev_close"] = bar["close"]

        if i + 1 >= len(bt_dates):
            continue
        entry_date = bt_dates[i + 1]

        # ── SPY regime guard ─────────────────────────────────────────────────
        spy_thresh = cfg.get("spy_thresh")
        if spy_thresh is not None:
            spy_ret = spy_regime.get(entry_date)
            if spy_ret is not None and spy_ret <= -spy_thresh:
                continue  # skip all entries on this day

        # ── Position cap check ────────────────────────────────────────────────
        if len(open_pos) >= MAX_CONCURRENT:
            continue

        # ── Select candidates ─────────────────────────────────────────────────
        day = sdf[sdf["date"] == signal_date]
        mask = (
            (day["score"] >= 6) &
            (day["adx"] > ADX_THRESHOLD) &
            (~day["vol_drying"]) &
            (~day["macd_shrink"]) &
            (day["exit_mode"] == "trailing_stop")
        )
        if cfg.get("days2"):
            mask &= day["in_prev_scan"]

        cands = day[mask].sort_values(["rs_return", "adx", "vol_ratio"], ascending=False)

        # Sector counts among open positions
        sector_counts = defaultdict(int)
        if cfg.get("sector_cap"):
            for pos in open_pos.values():
                sector_counts[pos.get("sector", "Unknown")] += 1

        placed = 0
        for _, c in cands.iterrows():
            if placed >= AUTO_ORDER_TOP_N:
                break
            if len(open_pos) >= MAX_CONCURRENT:
                break

            sym = c["symbol"]
            if sym in open_pos:
                continue

            sector = sector_map.get(sym, "Unknown")
            if cfg.get("sector_cap") and sector_counts[sector] >= cfg["sector_cap"]:
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
                "prev_close": ep,
                "sector": sector,
            }
            sector_counts[sector] += 1
            placed += 1

    # Force-close remaining positions at last bar
    last = bt_dates[-1]
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


# ── 7. Run all variants ───────────────────────────────────────────────────────
VARIANTS = {
    "BASELINE":         {"days2": False, "trail_stop": False, "spy_thresh": None, "sector_cap": None},
    "V1-days2":         {"days2": True,  "trail_stop": False, "spy_thresh": None, "sector_cap": None},
    "V2-trail-stop":    {"days2": False, "trail_stop": True,  "spy_thresh": None, "sector_cap": None},
    "V3-spy-0.3%":     {"days2": False, "trail_stop": False, "spy_thresh": 0.3,  "sector_cap": None},
    "V4-sector-cap3":  {"days2": False, "trail_stop": False, "spy_thresh": None, "sector_cap": 3},
    "COMBINED":         {"days2": True,  "trail_stop": True,  "spy_thresh": 0.3,  "sector_cap": 3},
}

results = {}
for label, cfg in VARIANTS.items():
    t0 = time.time()
    results[label] = simulate(cfg)
    print(f"  {label:<18} → {len(results[label])} trades  ({time.time()-t0:.1f}s)", flush=True)


# ── 8. Stats helper ───────────────────────────────────────────────────────────
def stats(trades):
    if not trades:
        return {}
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins) / len(trades) * 100
    aw     = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    al     = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    pf     = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses else 999
    stops  = sum(1 for t in trades if "stop" in t["reason"])
    max_pnl_pct = max((t["pnl_pct"] for t in wins), default=0)
    return dict(n=len(trades), wins=len(wins), losses=len(losses),
                win_rate=wr, total_pnl=total, avg_win=aw, avg_loss=al,
                profit_factor=pf, stops=stops, max_win_pct=max_pnl_pct)


# ── 9. Summary table ──────────────────────────────────────────────────────────
print()
W = 18
print("=" * 105)
print(f"  {'Variant':<{W}} {'Trades':>7} {'WinRate':>8} {'TotalP&L':>11} {'AvgWin%':>9} {'AvgLoss%':>9} {'PF':>6} {'Stops%':>8}")
print("=" * 105)
for label, trades in results.items():
    s = stats(trades)
    if not s:
        print(f"  {label:<{W}}  (no trades)")
        continue
    stop_pct = s["stops"] / s["n"] * 100 if s["n"] else 0
    marker = " ◀" if label != "BASELINE" and s["total_pnl"] > stats(results["BASELINE"])["total_pnl"] else ""
    print(f"  {label:<{W}} {s['n']:>7} {s['win_rate']:>7.1f}% "
          f"{s['total_pnl']:>+11.2f} {s['avg_win']:>+9.1f}% "
          f"{s['avg_loss']:>+9.1f}% {s['profit_factor']:>6.2f} "
          f"{stop_pct:>7.1f}%{marker}")
print("=" * 105)


# ── 10. Cumulative P&L table ──────────────────────────────────────────────────
col_w   = 13
labels  = list(VARIANTS.keys())
shorts  = ["BASE", "V1-days2", "V2-trail", "V3-spy.3", "V4-sec3", "COMBINED"]

print()
print("=" * (14 + col_w * len(VARIANTS)))
print(f"  {'Date':<10}" + "".join(f"{l:>{col_w}}" for l in shorts))
print("=" * (14 + col_w * len(VARIANTS)))

by_date = {}
for label, trades in results.items():
    d = defaultdict(float)
    for t in trades:
        d[str(t["exit_date"])[:10]] += t["pnl"]
    by_date[label] = d

all_exit_dates = sorted(set(d for bd in by_date.values() for d in bd))
cums = {label: 0.0 for label in labels}
for d in all_exit_dates:
    for label in labels:
        cums[label] += by_date[label].get(d, 0)
    print(f"  {d:<10}" + "".join(f"{cums[label]:>+{col_w}.2f}" for label in labels))

print("=" * (14 + col_w * len(VARIANTS)))
print(f"  {'FINAL':<10}" + "".join(
    f"{sum(t['pnl'] for t in results[l]):>+{col_w}.2f}"
    for l in labels))
print()


# ── 11. Exit reason breakdown ─────────────────────────────────────────────────
print("── Exit breakdown (% of trades) ──")
print(f"  {'Variant':<{W}} {'Stop':>8} {'RSI>70':>8} {'Gain+RSI':>10} {'MaxHold':>9} {'OpenEnd':>9}")
print("  " + "-" * 52)
for label, trades in results.items():
    n = len(trades)
    if n == 0:
        continue
    def pct(reason_substr):
        return sum(1 for t in trades if reason_substr in t["reason"]) / n * 100
    print(f"  {label:<{W}} {pct('stop'):>7.1f}% {pct('RSI'):>7.1f}% "
          f"{pct('gain'):>9.1f}% {pct('max hold'):>8.1f}% {pct('open at'):>8.1f}%")
