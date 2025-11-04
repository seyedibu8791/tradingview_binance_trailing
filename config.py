# config.py (final)
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
#  BINANCE SIGNED REQUEST HELPERS
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


# =============================
#  FUNCTION: GET UNREALIZED PNL %
# =============================
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
#  Wrapper: GET LIVE PNL FOR MONITOR
#  (safe, predictable helper for the 2-bar monitor)
# =============================
def get_live_pnl_for_monitor(symbol: str):
    """
    Lightweight wrapper used by the 2-bar loss monitor.
    Returns current unrealized PnL % as a rounded float (positive or negative),
    or None if there is no active position / error.
    This calls the existing get_unrealized_pnl_pct() so other code remains unchanged.
    """
    try:
        pnl = get_unrealized_pnl_pct(symbol)
        if pnl is None:
            return None
        return round(float(pnl), 4)
    except Exception as e:
        if DEBUG:
            print(f"⚠️ get_live_pnl_for_monitor error for {symbol}: {e}")
        return None


# =============================
#  FUNCTION: GET LAST FILLS (REAL EXECUTIONS)
# =============================
def get_latest_fills(symbol: str, limit: int = 10):
    """Fetch recent user trades for the symbol."""
    try:
        trades = _signed_get("/fapi/v1/userTrades", {"symbol": symbol.upper(), "limit": limit})
        if not trades:
            return []
        return trades
    except Exception as e:
        if DEBUG:
            print(f"⚠️ get_latest_fills error for {symbol}: {e}")
        return []


def compute_real_pnl(symbol: str):
    """
    Compute actual realized PnL from Binance fills.
    Returns dict with entry_price, exit_price, pnl_usd, pnl_pct
    """
    try:
        fills = get_latest_fills(symbol)
        if not fills:
            return None

        # Split buy/sell trades
        buys = [f for f in fills if f.get("side") == "BUY"]
        sells = [f for f in fills if f.get("side") == "SELL"]

        if not buys or not sells:
            return None

        total_buy_qty = sum(float(f["qty"]) for f in buys)
        total_sell_qty = sum(float(f["qty"]) for f in sells)
        avg_buy_price = sum(float(f["price"]) * float(f["qty"]) for f in buys) / total_buy_qty
        avg_sell_price = sum(float(f["price"]) * float(f["qty"]) for f in sells) / total_sell_qty

        # Use smaller of two sides for matched quantity
        qty = min(total_buy_qty, total_sell_qty)
        direction = "LONG" if total_buy_qty > total_sell_qty else "SHORT"

        # Calculate PnL
        if direction == "LONG":
            pnl_usd = (avg_sell_price - avg_buy_price) * qty
            pnl_pct = ((avg_sell_price - avg_buy_price) / avg_buy_price) * 100 * LEVERAGE
        else:
            pnl_usd = (avg_buy_price - avg_sell_price) * qty
            pnl_pct = ((avg_buy_price - avg_sell_price) / avg_sell_price) * 100 * LEVERAGE

        return {
            "symbol": symbol.upper(),
            "entry_price": round(avg_buy_price if direction == "LONG" else avg_sell_price, 4),
            "exit_price": round(avg_sell_price if direction == "LONG" else avg_buy_price, 4),
            "pnl_usd": round(pnl_usd, 2),
            "pnl_pct": round(pnl_pct, 2),
            "qty": round(qty, 4),
            "direction": direction,
        }
    except Exception as e:
        if DEBUG:
            print(f"⚠️ compute_real_pnl error for {symbol}: {e}")
        return None


# =============================
#  LOG CONFIGURATION DETAILS
# =============================
print("📘 CONFIGURATION LOADED")
print("------------------------------")
print(f"Environment:           {ENVIRONMENT}")
print(f"Leverage:              {LEVERAGE}x ({MARGIN_TYPE})")
print(f"Trade Amount:          ${TRADE_AMOUNT}")
print("------------------------------")
