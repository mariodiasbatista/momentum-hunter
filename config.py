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
RSI_OVERBOUGHT = 65
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ADX_PERIOD = 14
ADX_THRESHOLD = 30
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

# ── Execution rules ────────────────────────────────────────────────────────
# Base dollar amount per 1 position
POSITION_SIZE_DOLLARS = 250

# Price threshold: above → 1 position, below → POSITION_MULTIPLIER positions
POSITION_PRICE_THRESHOLD = 50
POSITION_MULTIPLIER = 3

# Auto-order: how many top candidates to place orders for each morning
AUTO_ORDER_TOP_N = 10

# Maximum number of concurrent open positions allowed.
# No new orders are placed once this ceiling is reached.
# Backtest showed 43-position pile-up degraded P&L; max 15 doubled it.
MAX_CONCURRENT_POSITIONS = 15

# Cooldown: a stock bought within this many calendar days cannot be re-bought
# 0 = no cooldown (only today's dedup applies)
# 1 = skip yesterday's buys (default — increases daily variety)
# 5 = skip anything bought in the last 5 days
ORDER_COOLDOWN_DAYS = 1

# Trailing stop management (runs every morning at 9:40 AM ET)
# Minimum gain above entry before we start trailing the stop up
STOP_TRAIL_MIN_GAIN_PCT = 0.03   # 3% gain required before trailing
# Fraction of the gain above entry to lock in (0.5 = protect 50% of gains)
STOP_TRAIL_LOCK_RATIO   = 0.50

# Exit thresholds
MAX_LOSS_PCT      = 5.0   # Close if unrealized loss exceeds this % from entry
MIN_GAIN_TAKE_PCT = 8.0   # Lock in profits when gain exceeds this % from entry
MAX_HOLD_DAYS     = 7     # Force-close any position held longer than this many calendar days

# Priority tiebreaker sort order for equal-score candidates:
# primary = RS % vs SPY, secondary = ADX, tertiary = volume ratio
PRIORITY_SORT = ["rs_return", "adx", "volume_ratio"]

# Market regime guard: skip all orders if SPY is down >= this % from prior close at 9:45 AM
SPY_BEAR_THRESHOLD = 0.5   # percent (positive value — triggers when SPY return <= -0.5%)

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
