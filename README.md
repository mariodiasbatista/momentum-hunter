# momentum-hunter

A Python-based momentum scanner for equities, options, forex, and crypto markets. Identifies assets exhibiting strong momentum signals to surface high-probability trading opportunities.

## Features

- Multi-market scanning: stocks, options/derivatives, forex, and crypto
- Momentum signal detection and ranking
- Configurable screener criteria (lookback period, signal thresholds, universe filters)
- Output to terminal, CSV, or downstream systems

## Requirements

- Python 3.10+
- pip

## Installation

```bash
git clone https://github.com/yourusername/momentum-hunter.git
cd momentum-hunter
pip install -r requirements.txt
```

## Usage

```bash
python main.py --market stocks --lookback 20 --top 10
```

| Flag | Default | Description |
|------|---------|-------------|
| `--market` | `stocks` | Market to scan: `stocks`, `options`, `forex`, `crypto` |
| `--lookback` | `20` | Lookback period in days for momentum calculation |
| `--top` | `20` | Number of top results to return |
| `--output` | `terminal` | Output format: `terminal`, `csv` |

## How It Works

Momentum is calculated over a configurable lookback window using rate-of-change (ROC) and relative strength metrics. Assets are ranked by signal strength and filtered by liquidity and volatility thresholds before being surfaced.

## Project Structure

```
momentum-hunter/
├── main.py           # Entry point
├── scanner/          # Core scanning logic
├── data/             # Data fetching and caching
├── signals/          # Momentum indicator implementations
├── config.py         # Configuration
└── requirements.txt
```

## License

MIT
