# momentum-hunter

A Python-based momentum scanner for US equities (S&P 500 + NASDAQ 100) and crypto markets. Identifies assets exhibiting strong momentum signals based on the **Idea 5: Catch Best Momentum Stocks Strategy** and delivers ranked results via Telegram.

## Features

- Multi-market scanning: full Alpaca universe (~12,400 US equities) and crypto
- Equal-weight momentum scoring (8 criteria × 1pt): trend, RSI, MACD, ADX, volume, relative strength vs SPY
- Exit mode recommendation per candidate: trailing stop or fixed take-profit
- Results delivered directly to Telegram with per-signal breakdown

## Requirements

- Python 3.10+
- Alpaca API key (paper or live) — [alpaca.markets](https://alpaca.markets)
- Telegram bot token + chat ID

## Installation

```bash
git clone https://github.com/mariodiasbatista/momentum-hunter.git
cd momentum-hunter
pip install -r requirements.txt
cp .env.example .env
# fill in your Alpaca keys and Telegram credentials in .env
```

## Usage

```bash
python main.py --market stocks --top 10
python main.py --market crypto --top 5
python main.py --market all --top 10 --min-score 5
```

| Flag | Default | Description |
|------|---------|-------------|
| `--market` | `stocks` | Market to scan: `stocks`, `crypto`, `all` |
| `--top` | `10` | Number of top results to send to Telegram |
| `--min-score` | `7` | Minimum signal score out of 10 |

## Execution Rules (v1 — 2026-05-22)

These rules govern how candidates from the nightly scan are acted on the following trading day.

### Position Sizing

| Condition | Positions | Capital |
|---|---|---|
| Price > $50 | 1 position | $250 |
| Price < $50 | 3 positions | $750 |

Shares bought = `floor(capital / price)`, minimum 1 share.

**Rationale:** Lower-priced stocks in confirmed momentum often have higher percentage upside. 3× allocation matches the risk/reward profile of high-RS, high-volume small caps that meet all 8 criteria.

### Priority Ranking (tiebreaker for equal scores)

When multiple stocks score 8/8, they are ranked by:

1. **RS % vs SPY** — stocks already leading the market the most tend to continue. Core of momentum investing.
2. **ADX strength** — higher ADX = more established trend, less likely to reverse.
3. **Volume ratio** — higher volume spike = more conviction behind the move.

### Entry Timing

Orders are placed at **9:45 AM ET** (after the first 15-min candle closes).

**Rationale:** The first 15 minutes after open are the most volatile. Many gap-ups retrace before continuing. Waiting for the first candle confirms the move is real, not just opening noise. You sacrifice the first few percent but avoid false breakout traps.

**Order type:** Market order with bracket stop loss.

### Stop Loss

Stop price = `last_close − ATR×1.5` (tight end of the trailing stop range), capped at max 1% below entry.

The ATR-based stop scales to each stock's own volatility — wider for volatile stocks, tighter for stable ones.

### Exit Mode

- **Trailing Stop** (1.5–3× ATR): when all signals are aligned — let the winner run
- **Fixed Take-Profit**: when RSI > 70, ADX falling, or 2+ warning signs — take profit before the reversal

---

## Screening Criteria (Idea 5)

Each candidate is scored out of **8** — one point per criterion, equal weight. A ticker surfaces when it scores ≥ 6/8:

| # | Signal | Threshold |
|---|--------|-----------|
| 1 | Price > SMA 50 | Short/medium-term bullish |
| 2 | Price > SMA 200 | Long-term bullish |
| 3 | EMA 9 > EMA 21 | Short-term momentum trigger |
| 4 | RSI 50–70 | Strong but not overbought |
| 5 | MACD line > signal & histogram > 0 | Buying pressure building |
| 6 | ADX > 25 | Confirms real trend strength |
| 7 | Volume ≥ 20% above 20-period avg | Breakout participation |
| 8 | Outperforming SPY (3-month RS) | Leader, not laggard |

Only symbols with average daily volume ≥ 500,000 shares are considered — micro-caps and illiquid tickers are filtered out before signal computation.

## Exit Mode Logic

- **Trailing stop** (1.5–3x ATR): when all signals are aligned and trend is clean
- **Fixed take-profit**: triggered when RSI > 70, bearish divergence, or 2+ warning signs

Warning signs monitored: RSI overbought, ADX falling, volume drying up, MACD histogram shrinking.

## Architecture & Data Flow

The pipeline is split into two decoupled processes:

### 1. Daily Ingestion (`ingest.py`)

Runs once per day after market close. Heavy — takes 7–20 minutes for the full stock universe.

```
ingest.py
 ├── [stocks] Fetch ~12,400 tradable US equity assets from Alpaca → assets table
 ├── [stocks] Fetch OHLCV bars in chunks of 100 → bars table (+ SPY as benchmark)
 ├── [stocks] score_ticker() × ~12,400 symbols → signals table
 ├── [crypto] Fetch ~73 tradable crypto pairs from Alpaca → assets table
 ├── [crypto] Fetch OHLCV bars → bars table (BTC/USD as benchmark)
 └── [crypto] score_ticker() × ~73 pairs → signals table
```

`score_ticker()` requires at least 210 bars per symbol (≈ SMA 200 minimum). Symbols below that threshold are skipped. Signals are always computed from the same batch of bars saved in the same run — the DB is always internally consistent.

### 2. Notification (`main.py`)

Reads pre-computed signals from the DB — near instant. No recalculation.

```
main.py
 └── load_signals() from SQLite → filter by min_score → send top N to Telegram
```

### Scheduling (`scheduler.py`)

A long-running APScheduler process fires both jobs Mon–Fri on a cron trigger:

| Time (UTC) | Job | Why |
|---|---|---|
| 21:30 | `ingest.py` | 30 min after US market close (4pm ET) — bars are final |
| 22:00 | `main.py --market all` | After ingest has time to complete |

Managed as a systemd service (`momentum-hunter.service`) — starts on boot, restarts on failure.

```bash
# Service management
systemctl status momentum-hunter
systemctl restart momentum-hunter
journalctl -u momentum-hunter -f
```

To trigger ingestion manually at any time (safe — full replace, idempotent):

```bash
.venv/bin/python ingest.py
```

## Testing

The test suite covers all core pipeline logic. No external API calls are made — Alpaca and Telegram are mocked.

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

| Test file | What it covers |
|---|---|
| `test_db.py` | Signal history PK, persistence tracking, same-day dedup, schema migration |
| `test_scorer.py` | Weighted scoring, `MAX_SCORE=10`, criteria weights, 210-bar minimum |
| `test_fetcher.py` | NYSE trading calendar, weekend/holiday handling, cache freshness |
| `test_telegram.py` | `send_alert`, stale warning header, streak badge, message splitting |
| `test_ingest_volume_filter.py` | Volume threshold boundary and mixed-universe filtering |

## Project Structure

```
momentum-hunter/
├── main.py                     # CLI entry point — reads signals from DB, sends Telegram
├── ingest.py                   # Daily ingestion — fetches bars, computes all signals
├── scheduler.py                # APScheduler service — fires ingest + notify Mon–Fri
├── config.py                   # Thresholds and .env loading
├── requirements.txt
├── .env.example                # API keys template
├── data/
│   ├── db.py                   # SQLite schema + all read/write helpers
│   ├── alpaca_client.py        # Alpaca REST client (stocks + crypto)
│   ├── universe.py             # Asset universe helpers
│   └── fetcher.py              # Cache-aware OHLCV loader
├── signals/
│   ├── trend.py                # SMA 50/200, EMA 9/21
│   ├── momentum.py             # RSI, MACD, ADX, ATR
│   ├── volume.py               # Volume vs 20-period average
│   ├── relative_strength.py    # Return vs SPY / BTC
│   ├── exit_mode.py            # Trailing stop vs fixed take-profit
│   └── scorer.py               # Aggregate weighted scorer (0–10)
├── scanner/
│   ├── stock_scanner.py        # Equity scan — reads signals from DB
│   └── crypto_scanner.py       # Crypto scan — reads signals from DB
├── notifier/
│   └── telegram.py             # Telegram bot notifier
└── tests/
    ├── test_db.py
    ├── test_scorer.py
    ├── test_fetcher.py
    ├── test_telegram.py
    └── test_ingest_volume_filter.py
```

## First Run Results (2026-05-21)

Pipeline stats from the first live ingestion after all improvements were in place:

**Stocks (US Equity)**

| Stage | Count |
|---|---|
| Tradable symbols from Alpaca | 12,396 |
| Passed 500k avg volume filter | 3,674 (30%) |
| Had sufficient history (210 bars) | 3,540 |
| Signals computed | 3,540 |
| Candidates score ≥ 7/10 | 669 |

Score distribution:

| Score | Symbols |
|---|---|
| 9/10 | 120 |
| 8/10 | 372 |
| 7/10 | 177 |
| 6/10 | 258 |
| ≤ 5/10 | 2,613 |

Notable top candidates by relative strength vs SPY (3-month): BKSY (+134%), BW (+127%), AERT (+100%).

Ingestion time: **13m 33s** (bars fetch + signal computation for ~3.5k symbols).

**Crypto**

| Stage | Count |
|---|---|
| Pairs from Alpaca | 73 |
| Had sufficient history | 62 |
| Candidates score ≥ 7/10 | 0 |

Crypto market was broadly weak on this date — highest score was 6/10 across 4 pairs.

**Notification**: top 10 stock candidates delivered to Telegram in under 1 second from pre-computed signals.

## License

MIT
