import os

# =============================
#  BINANCE API CONFIG
# =============================
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
BASE_URL = "https://fapi.binance.com" if os.getenv("USE_TESTNET", "False").lower() != "true" else "https://testnet.binancefuture.com"

# =============================
#  TRADE SETTINGS
# =============================
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", 50))
LEVERAGE = int(os.getenv("LEVERAGE", 20))
MARGIN_TYPE = os.getenv("MARGIN_TYPE", "ISOLATED")
MAX_ACTIVE_TRADES = int(os.getenv("MAX_ACTIVE_TRADES", 3))
MAX_ACTIVE_THREADS = int(os.getenv("MAX_ACTIVE_THREADS", 5))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 12))
LOSS_BARS_LIMIT = int(os.getenv("LOSS_BARS_LIMIT", 3))
EXIT_MARKET_DELAY = int(os.getenv("EXIT_MARKET_DELAY", 10))
OPPOSITE_CLOSE_DELAY = int(os.getenv("OPPOSITE_CLOSE_DELAY", 5))

# =============================
#  TELEGRAM CONFIG
# =============================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =============================
#  LOGGING CONFIG
# =============================
LOG_FILE = os.getenv("LOG_FILE", "trade_log.txt")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
ENVIRONMENT = os.getenv("ENVIRONMENT", "live")

# =============================
#  DAILY SUMMARY TIME
# =============================
DAILY_SUMMARY_TIME_IST = os.getenv("DAILY_SUMMARY_TIME_IST", "22:00")

# =============================
#  TRAILING STOP PARAMETERS
# =============================
TRAILING_ACTIVATION_PCT = float(os.getenv("TRAILING_ACTIVATION_PCT", 0.5))
TS_LOW_OFFSET_PCT = float(os.getenv("TS_LOW_OFFSET_PCT", 0.1))
TS_HIGH_OFFSET_PCT = float(os.getenv("TS_HIGH_OFFSET_PCT", 0.1))
TRAILING_UPDATE_INTERVAL = int(os.getenv("TRAILING_UPDATE_INTERVAL", 5))
DUAL_TRAILING_ENABLED = os.getenv("DUAL_TRAILING_ENABLED", "True").lower() == "true"
TRAILING_DISTANCE_PCT = float(os.getenv("TRAILING_DISTANCE_PCT", 0.3))
TRAILING_COMPARE_PNL = os.getenv("TRAILING_COMPARE_PNL", "True").lower() == "true"

# ============================================
#  ADVANCED TRAILING STOP LOGIC (UNIFIED + AUTO TIGHTEN)
# ============================================
BEST_STOP_TRACKER = os.getenv("BEST_STOP_TRACKER", "True").lower() == "true"
SECOND_TRAILING_TIGHTEN_PCT = float(os.getenv("SECOND_TRAILING_TIGHTEN_PCT", 0.5))
SECOND_TRAILING_MIN_PNL_DIFF = float(os.getenv("SECOND_TRAILING_MIN_PNL_DIFF", 0.2))

# =============================
#  FLASK SETTINGS
# =============================
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.getenv("FLASK_PORT", 5000))

# =============================
#  TESTNET / LIVE KEYS
# =============================
USE_TESTNET = os.getenv("USE_TESTNET", "False").lower() == "true"
LIVE_API_KEY = os.getenv("LIVE_API_KEY", "")
LIVE_SECRET_KEY = os.getenv("LIVE_SECRET_KEY", "")
