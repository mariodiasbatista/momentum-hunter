"""
backtest.py — compare OLD stop (5% intraday-monitor) vs NEW stop (3% hard cap)
over a multi-week window using the daily OHLCV bars already in the DB.

Methodology
-----------
  Entry  : signal day D → enter at D+1 OPEN
  Old stop: exit when daily LOW < entry × 0.95  (mirrors intraday 5% monitor)
  New stop: exit when daily LOW < entry × 0.97  (mirrors new 3% bracket cap)
  RSI exit: exit at day's CLOSE when RSI(14) > 70  (proxy for 15-min RSI >70)
  Gain exit: exit at CLOSE when gain ≥ 8% AND RSI < 50
  EOD cap : exit at CLOSE after MAX_HOLD_DAYS if none of the above triggered
  Position sizing matches live system: price <$50 → $750, ≥$50 → $250
"""

import sys
import sqlite3
import math
import time
from collections import defaultdict

import pandas as pd
import pandas_ta as ta

# ── Config (mirrors config.py) ────────────────────────────────────────────────
DB_PATH          = "data/momentum.db"
BACKTEST_START   = "2026-05-12"
BACKTEST_END     = "2026-06-17"
MIN_AVG_VOLUME   = 500_000
MIN_BARS_HISTORY = 210
MIN_SCORE        = 6
ADX_THRESHOLD    = 30
VOLUME_MULT      = 1.2
RSI_MIN, RSI_MAX = 50, 70
RS_LOOKBACK      = 63
MAX_HOLD_DAYS    = 10
AUTO_ORDER_TOP_N = 10

OLD_STOP_PCT = 0.05   # 5%  — old: intraday monitor threshold
NEW_STOP_PCT = 0.03   # 3%  — new: hard cap

POS_SIZE_HIGH = 250   # price ≥ $50
POS_SIZE_LOW  = 750   # price <  $50
POS_THRESHOLD = 50


def position_qty(price: float) -> int:
    dollars = POS_SIZE_LOW if price < POS_THRESHOLD else POS_SIZE_HIGH
    return max(1, math.floor(dollars / price))


# ── 1. Load bars ──────────────────────────────────────────────────────────────
print("Loading bars from DB…", flush=True)
t0 = time.time()
conn = sqlite3.connect(DB_PATH)
raw = pd.read_sql("SELECT symbol, date, open, high, low, close, volume FROM bars", conn,
                  parse_dates=["date"])
conn.close()
print(f"  {len(raw):,} rows for {raw['symbol'].nunique():,} symbols in {time.time()-t0:.1f}s")

spy_all = raw[raw["symbol"] == "SPY"].set_index("date").sort_index()
all_dates = sorted(raw["date"].unique())

# ── 2. Volume filter ──────────────────────────────────────────────────────────
print("Applying volume filter…", flush=True)
vol_means = raw.groupby("symbol")["volume"].mean()
passing_vol = set(vol_means[vol_means >= MIN_AVG_VOLUME].index)
print(f"  {len(passing_vol):,} symbols pass ≥{MIN_AVG_VOLUME:,} avg volume")

# ── 3. Precompute indicators for every qualifying symbol ──────────────────────
print("Precomputing indicators (this takes a few minutes)…", flush=True)
t0 = time.time()

records = []
universe = raw[raw["symbol"].isin(passing_vol)]
groups = universe.groupby("symbol")
total = len(passing_vol)
done = 0

for symbol, df in groups:
    df = df.set_index("date").sort_index()
    if len(df) < MIN_BARS_HISTORY:
        done += 1
        continue

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    # Indicators (full history, no lookahead — pandas_ta is causal)
    sma50  = ta.sma(close, 50)
    sma200 = ta.sma(close, 200)
    ema9   = ta.ema(close, 9)
    ema21  = ta.ema(close, 21)
    rsi    = ta.rsi(close, 14)
    macd_r = ta.macd(close, 12, 26, 9)
    adx_r  = ta.adx(high, low, close, 14)
    atr_r  = ta.atr(high, low, close, 14)
    vol_ma = volume.rolling(20).mean()

    macd_col = "MACD_12_26_9"
    sig_col  = "MACDs_12_26_9"
    hist_col = "MACDh_12_26_9"
    adx_col  = "ADX_14"

    hist         = macd_r[hist_col]
    prev_hist    = hist.shift(1)
    adx_series   = adx_r[adx_col]
    prev_adx     = adx_series.shift(1)

    # RS vs SPY (63-day return)
    combined = pd.DataFrame({"ticker": close, "spy": spy_all["close"]}).dropna()
    rs_ret = (combined["ticker"] / combined["ticker"].shift(RS_LOOKBACK) - 1) * 100
    spy_ret_s = (combined["spy"] / combined["spy"].shift(RS_LOOKBACK) - 1) * 100

    # Build per-date signal rows
    for dt in df.index:
        s50   = sma50.get(dt)
        s200  = sma200.get(dt)
        e9    = ema9.get(dt)
        e21   = ema21.get(dt)
        r     = rsi.get(dt)
        mc    = macd_r[macd_col].get(dt) if dt in macd_r.index else None
        ms    = macd_r[sig_col].get(dt)  if dt in macd_r.index else None
        mh    = hist.get(dt)
        mhp   = prev_hist.get(dt)
        adx   = adx_series.get(dt)
        padx  = prev_adx.get(dt)
        atr   = atr_r.get(dt)
        vm    = vol_ma.get(dt)
        v     = volume.get(dt)
        cl    = close.get(dt)
        op    = df["open"].get(dt)
        lo    = df["low"].get(dt)
        hi    = df["high"].get(dt)
        rs    = rs_ret.get(dt)
        sp    = spy_ret_s.get(dt)

        if any(x is None or (isinstance(x, float) and math.isnan(x))
               for x in [s50, s200, e9, e21, r, mc, ms, mh, mhp, adx, padx, atr, vm, v, cl]):
            continue

        vol_ratio      = v / vm if vm > 0 else 0.0
        above_sma50    = cl > s50
        above_sma200   = cl > s200
        ema9_gt_ema21  = e9 > e21
        rsi_in_range   = RSI_MIN <= r <= RSI_MAX
        macd_bullish   = mc > ms and mh > 0
        adx_strong     = adx > ADX_THRESHOLD
        vol_above      = vol_ratio >= VOLUME_MULT
        out_spy        = (rs > sp) if rs is not None and sp is not None else False
        adx_falling    = adx < padx and adx < ADX_THRESHOLD
        vol_drying     = vol_ratio < 0.8
        macd_shrink    = mh < mhp and mh > 0
        rsi_overbought = r > 70

        score = sum([above_sma50, above_sma200, ema9_gt_ema21, rsi_in_range,
                     macd_bullish, adx_strong, vol_above, out_spy])

        # exit_mode: same logic as exit_mode.py
        warnings = sum([rsi_overbought, adx_falling, vol_drying, macd_shrink])
        use_trailing = warnings < 2 and not rsi_overbought
        exit_mode = "trailing_stop" if use_trailing else "fixed_take_profit"

        records.append({
            "symbol": symbol, "date": dt,
            "score": score, "exit_mode": exit_mode,
            "adx": adx, "rsi": r, "atr": atr,
            "vol_ratio": vol_ratio, "rs_return": rs if rs is not None else 0.0,
            "adx_strong": adx_strong, "vol_drying": vol_drying,
            "macd_shrink": macd_shrink,
            "close": cl, "open": op, "low": lo, "high": hi,
        })

    done += 1
    if done % 500 == 0:
        elapsed = time.time() - t0
        eta = elapsed / done * (total - done)
        print(f"  {done}/{total} symbols | {elapsed:.0f}s elapsed | ~{eta:.0f}s remaining", flush=True)

print(f"  Done in {time.time()-t0:.1f}s — {len(records):,} signal rows", flush=True)

# ── 4. Build signals DataFrame ────────────────────────────────────────────────
sdf = pd.DataFrame(records)
sdf["date"] = pd.to_datetime(sdf["date"])

# ── 5. Identify backtest trading days ─────────────────────────────────────────
bt_dates = sdf[(sdf["date"] >= BACKTEST_START) & (sdf["date"] <= BACKTEST_END)]["date"].sort_values().unique()
print(f"\nBacktest window: {str(bt_dates[0])[:10]} → {str(bt_dates[-1])[:10]} ({len(bt_dates)} days)")

# Price lookup: (symbol, date) → OHLC for next-day simulation
price_lkp = sdf.set_index(["symbol", "date"])[["open", "high", "low", "close", "rsi"]].to_dict("index")

# ── 6. Simulate ───────────────────────────────────────────────────────────────
def simulate(stop_pct: float, label: str):
    open_positions = {}   # symbol → {entry, stop, qty, entry_date}
    trades = []

    for i, signal_date in enumerate(bt_dates):
        # Check exit conditions for all open positions using TODAY's bars
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            key = (sym, signal_date)
            if key not in price_lkp:
                continue
            bar = price_lkp[key]
            entry = pos["entry"]
            stop  = pos["stop"]
            qty   = pos["qty"]
            days_held = (signal_date - pos["entry_date"]).days

            exit_price = None
            exit_reason = None

            # Gap-down through stop
            if bar["open"] <= stop:
                exit_price  = bar["open"]
                exit_reason = f"gap-down stop ({stop_pct*100:.0f}%)"

            # Intraday low hits stop
            elif bar["low"] <= stop:
                exit_price  = stop
                exit_reason = f"stop hit ({stop_pct*100:.0f}%)"

            # RSI overbought exit
            elif bar["rsi"] > 70:
                exit_price  = bar["close"]
                exit_reason = "RSI > 70"

            # Gain + fading momentum exit
            elif (bar["close"] - entry) / entry >= 0.08 and bar["rsi"] < 50:
                exit_price  = bar["close"]
                exit_reason = "gain ≥8% + RSI<50"

            # EOD forced exit after max hold
            elif days_held >= MAX_HOLD_DAYS:
                exit_price  = bar["close"]
                exit_reason = "max hold"

            if exit_price is not None:
                pnl     = round((exit_price - entry) * qty, 2)
                pnl_pct = round((exit_price - entry) / entry * 100, 2)
                trades.append({
                    "symbol": sym, "entry_date": pos["entry_date"],
                    "exit_date": signal_date, "entry": entry,
                    "exit": exit_price, "qty": qty, "pnl": pnl,
                    "pnl_pct": pnl_pct, "reason": exit_reason,
                })
                del open_positions[sym]

        # Select new candidates for signal_date → enter tomorrow (next bar)
        if i + 1 >= len(bt_dates):
            continue
        entry_date = bt_dates[i + 1]

        day_signals = sdf[sdf["date"] == signal_date].copy()
        candidates = day_signals[
            (day_signals["score"] >= MIN_SCORE) &
            (day_signals["adx"] > ADX_THRESHOLD) &
            (~day_signals["vol_drying"]) &
            (~day_signals["macd_shrink"]) &
            (day_signals["exit_mode"] == "trailing_stop")
        ].sort_values(["rs_return", "adx", "vol_ratio"], ascending=False)

        placed = 0
        for _, cand in candidates.iterrows():
            if placed >= AUTO_ORDER_TOP_N:
                break
            sym = cand["symbol"]
            if sym in open_positions:
                continue
            key = (sym, entry_date)
            if key not in price_lkp:
                continue
            entry_bar = price_lkp[key]
            entry_price = entry_bar["open"]
            if entry_price <= 0:
                continue

            atr_min = cand["atr"] * 1.5
            if stop_pct == OLD_STOP_PCT:
                stop = entry_price * (1 - OLD_STOP_PCT)
            else:
                # new: tighter of ATR×1.5 or 3% cap
                stop = max(entry_price - atr_min, entry_price * (1 - NEW_STOP_PCT))
                stop = min(stop, entry_price - 0.01)

            qty = position_qty(entry_price)
            open_positions[sym] = {
                "entry": entry_price, "stop": stop,
                "qty": qty, "entry_date": entry_date,
            }
            placed += 1

    # Force-close any remaining open positions at last available price
    last_date = bt_dates[-1]
    for sym, pos in open_positions.items():
        key = (sym, last_date)
        if key in price_lkp:
            ep = price_lkp[key]["close"]
            pnl = round((ep - pos["entry"]) * pos["qty"], 2)
            trades.append({
                "symbol": sym, "entry_date": pos["entry_date"],
                "exit_date": last_date, "entry": pos["entry"],
                "exit": ep, "qty": pos["qty"], "pnl": pnl,
                "pnl_pct": round((ep - pos["entry"]) / pos["entry"] * 100, 2),
                "reason": "open at end",
            })

    return trades


print("\nRunning OLD stop (5%) simulation…", flush=True)
old_trades = simulate(OLD_STOP_PCT, "old")

print("Running NEW stop (3%) simulation…", flush=True)
new_trades = simulate(NEW_STOP_PCT, "new")


# ── 7. Report ─────────────────────────────────────────────────────────────────
def report(trades, label):
    if not trades:
        print(f"\n{label}: no trades")
        return
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins) / len(trades) * 100
    aw_pct = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    al_pct = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    pf     = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses else float("inf")
    max_dd = min(t["pnl"] for t in trades)

    from collections import Counter
    reasons = Counter(t["reason"] for t in trades)

    print(f"\n{'='*55}")
    print(f"  {label}")
    print(f"{'='*55}")
    print(f"  Trades        : {len(trades)} ({len(wins)} W / {len(losses)} L)")
    print(f"  Win rate      : {wr:.1f}%")
    print(f"  Total P&L     : ${total:+.2f}")
    print(f"  Avg win       : +{aw_pct:.1f}%")
    print(f"  Avg loss      : {al_pct:.1f}%")
    print(f"  Profit factor : {pf:.2f}")
    print(f"  Worst trade   : ${max_dd:.2f}")
    print(f"  Exit breakdown:")
    for r, c in reasons.most_common():
        print(f"    {r:<30} {c}")


report(old_trades, "OLD — 5% intraday stop (baseline)")
report(new_trades, "NEW — 3% hard cap stop")


# ── 8. Side-by-side cumulative P&L ───────────────────────────────────────────
print(f"\n{'='*65}")
print(f"{'Date':<14} {'OLD cum':>12} {'NEW cum':>12} {'Delta':>10}")
print(f"{'='*65}")

by_date_old = defaultdict(float)
by_date_new = defaultdict(float)
for t in old_trades:
    by_date_old[str(t["exit_date"])[:10]] += t["pnl"]
for t in new_trades:
    by_date_new[str(t["exit_date"])[:10]] += t["pnl"]

all_exit_dates = sorted(set(by_date_old) | set(by_date_new))
cum_old = cum_new = 0.0
for d in all_exit_dates:
    cum_old += by_date_old.get(d, 0)
    cum_new += by_date_new.get(d, 0)
    print(f"  {d:<12} {cum_old:>+12.2f} {cum_new:>+12.2f} {cum_new-cum_old:>+10.2f}")

print(f"\nFinal: OLD=${sum(t['pnl'] for t in old_trades):+.2f}  "
      f"NEW=${sum(t['pnl'] for t in new_trades):+.2f}  "
      f"Delta=${sum(t['pnl'] for t in new_trades)-sum(t['pnl'] for t in old_trades):+.2f}")
