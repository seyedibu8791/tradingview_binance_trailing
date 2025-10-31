import os

# =============================
#  ENVIRONMENT CONFIGURATION
# =============================

# --- Binance API Configuration ---
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

# --- Mode (TESTNET or MAINNET) ---
ENVIRONMENT = os.getenv("ENVIRONMENT", "TESTNET").upper()
BASE_URL = (
    "https://testnet.binancefuture.com"
    if ENVIRONMENT == "TESTNET"
    else "https://fapi.binance.com"
)

# --- Trading Parameters ---
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", 50))       # USD value per trade
LEVERAGE = int(os.getenv("LEVERAGE", 20))
MARGIN_TYPE = os.getenv("MARGIN_TYPE", "ISOLATED")        # CROSS or ISOLATED
MAX_ACTIVE_TRADES = int(os.getenv("MAX_ACTIVE_TRADES", 5))
EXIT_MARKET_DELAY = int(os.getenv("EXIT_MARKET_DELAY", 10))
OPPOSITE_CLOSE_DELAY = int(os.getenv("OPPOSITE_CLOSE_DELAY", 3))

# --- Trailing Stop Parameters ---
TRAILING_ACTIVATION_PCT = float(os.getenv("TRAILING_ACTIVATION_PCT", 0.5))
TS_LOW_OFFSET_PCT = float(os.getenv("TS_LOW_OFFSET_PCT", 0.1))
TS_HIGH_OFFSET_PCT = float(os.getenv("TS_HIGH_OFFSET_PCT", 0.1))
TRAILING_UPDATE_INTERVAL = int(os.getenv("TRAILING_UPDATE_INTERVAL", 5))
DUAL_TRAILING_ENABLED = os.getenv("DUAL_TRAILING_ENABLED", "True").lower() == "true"
TRAILING_DISTANCE_PCT = float(os.getenv("TRAILING_DISTANCE_PCT", 0.3))
TRAILING_COMPARE_PNL = os.getenv("TRAILING_COMPARE_PNL", "True").lower() == "true"

# --- Loss Control Parameters ---
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 3.0))     # Maximum % loss before force close
LOSS_BARS_LIMIT = int(os.getenv("LOSS_BARS_LIMIT", 2))     # Bars to wait before forced loss close

# --- Telegram Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- Misc / App Configuration ---
DEBUG = os.getenv("DEBUG", "False").lower() == "true"

# =============================
#  LOG CONFIGURATION DETAILS
# =============================
print("📘 CONFIGURATION LOADED")
print("------------------------------")
print(f"Environment:           {ENVIRONMENT}")
print(f"Leverage:              {LEVERAGE}x ({MARGIN_TYPE})")
print(f"Trade Amount:          ${TRADE_AMOUNT}")
print(f"Exit Market Delay:     {EXIT_MARKET_DELAY}s")
print(f"Trailing Activation:   {TRAILING_ACTIVATION_PCT}%")
print(f"Trailing Low Offset:   {TS_LOW_OFFSET_PCT}%")
print(f"Trailing High Offset:  {TS_HIGH_OFFSET_PCT}%")
print(f"Opposite Close Delay:  {OPPOSITE_CLOSE_DELAY}s")
print(f"Max Active Trades:     {MAX_ACTIVE_TRADES}")
print(f"Stop Loss %:           {STOP_LOSS_PCT}%")
print(f"Loss Bars Limit:       {LOSS_BARS_LIMIT}")
print("------------------------------")
