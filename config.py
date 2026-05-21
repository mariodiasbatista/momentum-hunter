from dotenv import load_dotenv
import os

load_dotenv()

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Number of calendar days to fetch (covers 200 trading days + buffer)
BARS_LOOKBACK_DAYS = 320

# Signal thresholds
SMA_SHORT = 50
SMA_LONG = 200
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_MIN = 50
RSI_MAX = 70
RSI_OVERBOUGHT = 70
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ADX_PERIOD = 14
ADX_THRESHOLD = 25
VOLUME_PERIOD = 20
VOLUME_MULTIPLIER = 1.2  # volume must be >= 20% above avg
RS_LOOKBACK_DAYS = 63    # ~3 months relative strength vs SPY

# Minimum score to surface a candidate (out of 8, equal weights)
MIN_SCORE = 6

# Minimum average daily volume to include a symbol in signal computation
MIN_AVG_VOLUME = 500_000

# ATR multiplier range for trailing stop recommendation
ATR_TRAILING_MIN = 1.5
ATR_TRAILING_MAX = 3.0

# Crypto pairs tracked via Alpaca
CRYPTO_PAIRS = [
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "AVAX/USD",
    "DOGE/USD",
    "LINK/USD",
    "MATIC/USD",
    "DOT/USD",
]
