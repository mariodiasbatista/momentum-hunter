"""
build_crypto_cache.py — build a signal cache for crypto pairs.

Crypto is absent from the equity signal cache for a reason that is a bug, not a
choice: MIN_AVG_VOLUME=500_000 filters on raw unit volume. For equities that is
shares; for crypto it is coins. BTC/USD trades ~3 BTC/day and fails the filter,
while PEPE/USD passes with 7e8 tokens. The result is that the only crypto that
ever reaches the scanner is meme coins and stablecoins.

This builder applies the same indicators and scoring as the equity cache, but:
  - filters on DOLLAR volume (close * volume), not unit volume
  - keeps only /USD pairs — /USDC, /USDT and /BTC are the same asset quoted
    differently and would triple-count exposure
  - drops stablecoins, which score highly on nothing and trade flat by design
  - forward-fills SPY onto weekend dates, since crypto trades 7 days and the
    relative-strength comparison would otherwise drop every Saturday/Sunday

Output: /tmp/bt_signals_crypto.pkl, same columns as the equity cache.
"""

import math
import sqlite3
import time
from pathlib import Path

import pandas as pd
import pandas_ta as ta

DB_PATH    = "data/momentum.db"
CACHE_PATH = Path("/tmp/bt_signals_crypto.pkl")

# Alpaca reports only its own venue's crypto volume, not market-wide: BTC/USD
# averages ~$240k/day here against billions globally. So this threshold is a
# relative liquidity rank within Alpaca, not a real liquidity floor. $5k/day
# keeps the majors and drops the micro-caps.
MIN_DOLLAR_VOLUME = 5_000
MIN_BARS_HISTORY  = 200
ADX_THRESHOLD     = 30
VOLUME_MULT       = 1.2
RS_LOOKBACK       = 63
RS_SHORT          = 21
ROC_PERIOD        = 20
ROC_MIN_PCT       = 5.0

STABLES = {"USDC/USD", "USDT/USD", "USDG/USD", "USDT/USDC", "DAI/USD"}

print("Loading bars from DB…", flush=True)
t0 = time.time()
conn = sqlite3.connect(DB_PATH)
raw = pd.read_sql(
    "SELECT symbol, date, open, high, low, close, volume FROM bars "
    "WHERE symbol LIKE '%/%' OR symbol = 'SPY'", conn, parse_dates=["date"])
conn.close()
print(f"  {len(raw):,} rows | {raw['symbol'].nunique():,} symbols | {time.time()-t0:.1f}s")

spy_all = raw[raw["symbol"] == "SPY"].set_index("date").sort_index()
cry = raw[raw["symbol"].str.contains("/", na=False)].copy()

# /USD quote only, no stablecoins
cry = cry[cry["symbol"].str.endswith("/USD") & ~cry["symbol"].isin(STABLES)]
print(f"  {cry['symbol'].nunique()} /USD non-stable pairs")

cry["dollar_vol"] = cry["close"] * cry["volume"]
dv = cry.groupby("symbol")["dollar_vol"].mean().sort_values(ascending=False)
passing = set(dv[dv >= MIN_DOLLAR_VOLUME].index)
print(f"  {len(passing)} pairs pass ${MIN_DOLLAR_VOLUME:,.0f} avg daily dollar volume:")
for s in sorted(passing, key=lambda x: -dv[x]):
    print(f"      {s:<12} ${dv[s]:>18,.0f}/day")
rejected = sorted(set(dv.index) - passing, key=lambda x: -dv[x])
if rejected:
    print(f"  rejected ({len(rejected)}): " + ", ".join(rejected[:12])
          + (" …" if len(rejected) > 12 else ""))

# SPY forward-filled onto crypto's 7-day calendar
all_days = pd.DatetimeIndex(sorted(cry["date"].unique()))
spy_ff = spy_all["close"].reindex(all_days.union(spy_all.index)).ffill().reindex(all_days)

print("\nPrecomputing indicators…", flush=True)
t0 = time.time()
records = []
for symbol, df in cry[cry["symbol"].isin(passing)].groupby("symbol"):
    df = df.set_index("date").sort_index()
    if len(df) < MIN_BARS_HISTORY:
        print(f"  skip {symbol}: only {len(df)} bars")
        continue

    close, high, low_s, volume = df["close"], df["high"], df["low"], df["volume"]

    sma50  = ta.sma(close, 50);   sma200 = ta.sma(close, 200)
    ema9   = ta.ema(close, 9);    ema21  = ta.ema(close, 21)
    rsi    = ta.rsi(close, 14)
    macd_r = ta.macd(close, 12, 26, 9)
    adx_r  = ta.adx(high, low_s, close, 14)
    atr_r  = ta.atr(high, low_s, close, 14)
    vol_ma = volume.rolling(20).mean()
    vol_3d = volume.rolling(3).mean()
    roc20  = (close / close.shift(ROC_PERIOD) - 1) * 100

    hist       = macd_r["MACDh_12_26_9"]
    adx_series = adx_r["ADX_14"]
    prev_hist  = hist.shift(1)
    prev_adx   = adx_series.shift(1)

    combined  = pd.DataFrame({"tk": close, "spy": spy_ff}).dropna()
    rs_long   = (combined["tk"] / combined["tk"].shift(RS_LOOKBACK) - 1) * 100
    spy_long  = (combined["spy"] / combined["spy"].shift(RS_LOOKBACK) - 1) * 100
    rs_short  = (combined["tk"] / combined["tk"].shift(RS_SHORT) - 1) * 100
    spy_short = (combined["spy"] / combined["spy"].shift(RS_SHORT) - 1) * 100

    for dt in df.index:
        def g(s):
            return s.get(dt)

        s50 = g(sma50); s200 = g(sma200); e9 = g(ema9); e21 = g(ema21)
        r   = g(rsi);   mc  = g(macd_r["MACD_12_26_9"])
        ms  = g(macd_r["MACDs_12_26_9"]); mh = g(hist); mhp = g(prev_hist)
        adx = g(adx_series); padx = g(prev_adx)
        atr = g(atr_r); vm = g(vol_ma); v3 = g(vol_3d); v = g(volume)
        cl  = g(close); op = g(df["open"]); lo = g(df["low"]); hi = g(df["high"])
        roc = g(roc20)
        rs_l = g(rs_long); spy_l = g(spy_long)
        rs_s = g(rs_short); spy_s = g(spy_short)

        if any(x is None or (isinstance(x, float) and math.isnan(x))
               for x in [s50, s200, e9, e21, r, mc, ms, mh, mhp,
                         adx, padx, atr, vm, v, cl]):
            continue

        vol_ratio   = v / vm if vm > 0 else 0.0
        vol_3d_rat  = v3 / vm if (v3 and vm and vm > 0) else 0.0
        adx_falling = adx < padx and adx < ADX_THRESHOLD
        vol_drying  = vol_ratio < 0.8
        macd_shrink = mh < mhp and mh > 0
        rsi_ob      = r > 70

        above_sma50  = cl > s50
        above_sma200 = cl > s200
        e9_gt_e21    = e9 > e21
        rsi_range    = 50 <= r <= 70
        macd_bull    = mc > ms and mh > 0
        adx_strong   = adx > ADX_THRESHOLD
        vol_above    = vol_ratio >= VOLUME_MULT
        out_spy_long = (rs_l > spy_l) if rs_l is not None and spy_l is not None else False

        score = sum([above_sma50, above_sma200, e9_gt_e21, rsi_range,
                     macd_bull, adx_strong, vol_above, out_spy_long])

        warnings  = sum([rsi_ob, adx_falling, vol_drying, macd_shrink])
        exit_mode = "trailing_stop" if (warnings < 2 and not rsi_ob) else "fixed_take_profit"

        out_spy_short = (rs_s > spy_s) if rs_s is not None and spy_s is not None else False
        records.append({
            "symbol": symbol, "date": dt, "score": score, "exit_mode": exit_mode,
            "adx": adx, "rsi": r, "atr": atr,
            "vol_ratio": vol_ratio, "vol_3d_ratio": vol_3d_rat,
            "rs_return": rs_l if rs_l is not None else 0.0,
            "adx_strong": adx_strong, "vol_drying": vol_drying,
            "macd_shrink": macd_shrink,
            "roc_pass": (roc >= ROC_MIN_PCT) if roc is not None and not math.isnan(roc) else False,
            "dual_rs": out_spy_long and out_spy_short,
            "vol_3d_pass": vol_3d_rat >= VOLUME_MULT,
            "close": cl, "open": op, "low": lo, "high": hi,
        })

print(f"  Done in {time.time()-t0:.1f}s — {len(records):,} rows", flush=True)
if not records:
    raise SystemExit("No rows produced — check MIN_DOLLAR_VOLUME / MIN_BARS_HISTORY.")
sdf = pd.DataFrame(records)
sdf["date"] = pd.to_datetime(sdf["date"])
sdf.to_pickle(CACHE_PATH)
print(f"  {sdf['symbol'].nunique()} symbols | "
      f"{str(sdf['date'].min())[:10]} → {str(sdf['date'].max())[:10]}")
print(f"  Cached to {CACHE_PATH}")
