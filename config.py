"""Bot configuration. Secrets come from the .env file; everything else is tunable here."""
import os

from dotenv import load_dotenv

load_dotenv()

# --- Credentials & connection (set in .env, never commit them) ---
API_KEY = os.getenv("ROOSTOO_API_KEY", "")
SECRET_KEY = os.getenv("ROOSTOO_SECRET_KEY", "")
BASE_URL = os.getenv("ROOSTOO_BASE_URL", "https://mock-api.roostoo.com")

# DRY_RUN=1 -> paper trading against live Roostoo prices, no real orders sent.
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
PAPER_STARTING_USD = 100_000.0

# --- Loop timing ---
POLL_SECONDS = 60            # ticker poll interval (one request for all pairs)
BAR_MINUTES = 15             # price bars built from polled prices
REBALANCE_MINUTES = 60       # how often the strategy re-evaluates and trades
EXCHANGE_INFO_REFRESH_HOURS = 6

# --- Universe ---
UNIVERSE_SIZE = 10           # most liquid USD pairs (by 24h traded value) considered
REGIME_PAIR = "BTC/USD"      # market regime filter

# --- Signal parameters (in bars of BAR_MINUTES) ---
FAST_EMA = 24                # 6h
SLOW_EMA = 96                # 24h
MOM_LOOKBACK = 96            # 24h momentum
VOL_LOOKBACK = 96            # 24h realised volatility
HOLD_BONUS = 0.25            # score bonus for coins already held (reduces churn/fees)

# --- Portfolio construction ---
MAX_POSITIONS = 3
MAX_GROSS_EXPOSURE = 0.90    # max fraction of equity invested when BTC regime is risk-on
RISK_OFF_EXPOSURE = 0.30     # max fraction invested when BTC is below its slow EMA
MAX_WEIGHT_PER_COIN = 0.35
MIN_TRADE_FRACTION = 0.02    # ignore rebalances smaller than 2% of equity (fee control)
DUST_USD = 5.0               # holdings worth less than this are ignored

# --- Risk management ---
STOP_VOL_MULT = 1.5          # trailing stop = 1.5 x daily volatility ...
STOP_MIN = 0.04              # ... clamped between 4%
STOP_MAX = 0.12              # ... and 12% below the post-entry peak
STOP_COOLDOWN_HOURS = 6      # no re-entry into a stopped-out coin for this long
MAX_DRAWDOWN = 0.10          # portfolio drawdown from peak that triggers going flat
BREAKER_COOLDOWN_HOURS = 12  # stay flat this long after the breaker fires

# --- Daily activity safeguard (competition needs trades on >= 8 days) ---
KEEPALIVE_ENABLED = True
KEEPALIVE_TZ_OFFSET_HOURS = 8   # trading day measured in HKT (UTC+8)
KEEPALIVE_LOCAL_HOUR = 18       # if no fill yet today by 18:00 HKT, place a small probe trade
KEEPALIVE_FRACTION = 0.02       # probe size as fraction of equity

# --- Files ---
DATA_DIR = "data"
LOG_DIR = "logs"
BARS_FILE = os.path.join(DATA_DIR, "bars.csv")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
PAPER_WALLET_FILE = os.path.join(DATA_DIR, "paper_wallet.json")
TRADES_FILE = os.path.join(LOG_DIR, "trades.csv")
EQUITY_FILE = os.path.join(LOG_DIR, "equity.csv")
MAX_BARS_PER_PAIR = 600

# Bootstrap price history from Binance public klines so the bot can trade
# immediately instead of waiting 24h for its own bars to accumulate.
BOOTSTRAP_FROM_BINANCE = True
