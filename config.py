#config.py
import os
import time
import hmac
import hashlib
import requests

# =============================
#  ENVIRONMENT CONFIGURATION
# =============================

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

ENVIRONMENT = os.getenv("ENVIRONMENT", "TESTNET").upper()
BASE_URL = (
    "https://testnet.binancefuture.com"
    if ENVIRONMENT == "TESTNET"
    else "https://fapi.binance.com"
)

USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() == "true"

# =============================
#  TRADING PARAMETERS
# =============================
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", 50))
LEVERAGE = int(os.getenv("LEVERAGE", 20))
MARGIN_TYPE = os.getenv("MARGIN_TYPE", "ISOLATED").upper()
MAX_ACTIVE_TRADES = int(os.getenv("MAX_ACTIVE_TRADES", 5))
EXIT_MARKET_DELAY = int(os.getenv("EXIT_MARKET_DELAY", 10))
OPPOSITE_CLOSE_DELAY = int(os.getenv("OPPOSITE_CLOSE_DELAY", 3))

# =============================
#  TRAILING STOP PARAMETERS
# =============================
TRAILING_ACTIVATION_PCT = float(os.getenv("TRAILING_ACTIVATION_PCT", 0.002))
TS_LOW_OFFSET_PCT = float(os.getenv("TS_LOW_OFFSET_PCT", 0.001))
TS_HIGH_OFFSET_PCT = float(os.getenv("TS_HIGH_OFFSET_PCT", 0.001))
TRAILING_DISTANCE_PCT = float(os.getenv("TRAILING_DISTANCE_PCT", 0.003))
TRAILING_UPDATE_INTERVAL = int(os.getenv("TRAILING_UPDATE_INTERVAL", 5))

# =============================
#  TSI TRAILING CONFIG (NEW)
# =============================
TSI_PRIMARY_TRIGGER_PCT = float(os.getenv("TSI_PRIMARY_TRIGGER_PCT", 0.003))       # activation threshold %
TSI_LOW_PROFIT_OFFSET_PCT = float(os.getenv("TSI_LOW_PROFIT_OFFSET_PCT", 0.001))   # offset at 0.5% profit
TSI_HIGH_PROFIT_OFFSET_PCT = float(os.getenv("TSI_HIGH_PROFIT_OFFSET_PCT", 0.001)) # offset at 10% profit

# =============================
#  LOSS CONTROL PARAMETERS
# =============================
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 2.0))
LOSS_BARS_LIMIT = int(os.getenv("LOSS_BARS_LIMIT", 2))

# =============================
#  TELEGRAM CONFIGURATION
# =============================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =============================
#  DAILY SUMMARY (Optional)
# =============================
DAILY_SUMMARY_TIME_IST = os.getenv("DAILY_SUMMARY_TIME_IST", "21:30")

# =============================
#  FLASK CONFIGURATION
# =============================
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT_RAW = os.getenv("FLASK_PORT", "5000").replace("$", "")
FLASK_PORT = int(FLASK_PORT_RAW) if FLASK_PORT_RAW.isdigit() else 5000

# =============================
#  MISC / APP CONFIGURATION
# =============================
DEBUG = os.getenv("DEBUG", "False").lower() == "true"
LOG_FILE = os.getenv("LOG_FILE", "trades.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# =============================
#  FUNCTION: GET UNREALIZED PNL
# =============================
def _signed_get(path: str, params: dict = None, timeout: int = 10):
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{path}?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()

def get_unrealized_pnl_pct(symbol: str):
    try:
        data = _signed_get("/fapi/v2/positionRisk")
        for pos in data:
            if pos["symbol"].upper() == symbol.upper() and abs(float(pos.get("positionAmt", 0))) > 0:
                unpnl = float(pos.get("unRealizedProfit", 0.0))
                entry_price = float(pos.get("entryPrice", 0.0))
                position_amt = abs(float(pos.get("positionAmt", 0.0)))
                if entry_price <= 0 or position_amt <= 0:
                    return None
                notional = entry_price * position_amt
                pnl_pct = (unpnl / notional) * 100 * LEVERAGE
                return pnl_pct
        return None
    except Exception as e:
        if DEBUG:
            print("⚠️ get_unrealized_pnl_pct error:", e)
        return None

# =============================
#  LOG CONFIGURATION DETAILS
# =============================
print("📘 CONFIGURATION LOADED")
print("------------------------------")
print(f"Environment:           {ENVIRONMENT}")
print(f"Leverage:              {LEVERAGE}x ({MARGIN_TYPE})")
print(f"Trade Amount:          ${TRADE_AMOUNT}")
print(f"Trailing Activation:   {TRAILING_ACTIVATION_PCT}%")
print(f"TSI Activation:        {TSI_PRIMARY_TRIGGER_PCT}%")
print(f"TSI Low Offset:        {TSI_LOW_PROFIT_OFFSET_PCT}%")
print(f"TSI High Offset:       {TSI_HIGH_PROFIT_OFFSET_PCT}%")
print("------------------------------")
