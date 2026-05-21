# momentum-hunter

A Python-based momentum scanner for US equities (S&P 500 + NASDAQ 100) and crypto markets. Identifies assets exhibiting strong momentum signals based on the **Idea 5: Catch Best Momentum Stocks Strategy** and delivers ranked results via Telegram.

## Features

- Multi-market scanning: stocks (S&P 500 + NASDAQ 100) and crypto via Alpaca
- 8-criterion momentum scoring: trend, RSI, MACD, ADX, volume, and relative strength vs SPY
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
| `--min-score` | `6` | Minimum signal score out of 8 |

## Screening Criteria (Idea 5)

Each candidate is scored out of 8. A ticker surfaces when it meets at least 6:

| # | Signal | Threshold |
|---|--------|-----------|
| 1 | Price > SMA 50 | Short/medium-term bullish |
| 2 | Price > SMA 200 | Long-term bullish |
| 3 | EMA 9 > EMA 21 | Short-term momentum trigger |
| 4 | RSI | 50–70 (strong but not overbought) |
| 5 | MACD line > signal line & histogram > 0 | Buying pressure building |
| 6 | ADX > 25 | Confirms real trend strength |
| 7 | Volume >= 20% above 20-period avg | Breakout participation |
| 8 | Outperforming SPY (3-month RS) | Leaders, not laggards |

## Exit Mode Logic

- **Trailing stop** (1.5–3x ATR): when all signals are aligned and trend is clean
- **Fixed take-profit**: triggered when RSI > 70, bearish divergence, or 2+ warning signs

Warning signs monitored: RSI overbought, ADX falling, volume drying up, MACD histogram shrinking.

## Project Structure

```
momentum-hunter/
├── main.py                     # CLI entry point
├── config.py                   # Thresholds and .env loading
├── requirements.txt
├── .env.example                # API keys template
├── data/
│   ├── alpaca_client.py        # Alpaca REST client (stocks + crypto)
│   ├── universe.py             # S&P 500 + NASDAQ 100 tickers; crypto pairs
│   └── fetcher.py              # Batch OHLCV fetch
├── signals/
│   ├── trend.py                # SMA 50/200, EMA 9/21
│   ├── momentum.py             # RSI, MACD, ADX, ATR
│   ├── volume.py               # Volume vs 20-period average
│   ├── relative_strength.py    # Return vs SPY
│   ├── exit_mode.py            # Trailing stop vs fixed take-profit
│   └── scorer.py               # Aggregate scorer (0–8)
├── scanner/
│   ├── stock_scanner.py        # Equity scan pipeline
│   └── crypto_scanner.py       # Crypto scan pipeline
└── notifier/
    └── telegram.py             # Telegram bot notifier
```

## License

MIT
