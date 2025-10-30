# app.py (Final - with trailing monitor active for all exits, market exit only)
from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os
from config import (
    BINANCE_API_KEY, BINANCE_SECRET_KEY, BASE_URL,
    TRADE_AMOUNT, LEVERAGE, MARGIN_TYPE, MAX_ACTIVE_TRADES,
    EXIT_MARKET_DELAY, OPPOSITE_CLOSE_DELAY,
    TRAILING_ACTIVATION_PCT, TS_LOW_OFFSET_PCT, TS_HIGH_OFFSET_PCT
)
from trade_notifier import log_trade_entry, log_trade_exit, trades  # ✅ include trades dict

app = Flask(__name__)

from threading import Lock
trades_lock = Lock()

# ===== Binance Helpers =====
def binance_signed_request(http_method, path, params=None):
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    query += f"&signature={signature}"
    url = f"{BASE_URL}{path}?{query}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        if http_method == "POST":
            return requests.post(url, headers=headers).json()
        elif http_method == "DELETE":
            return requests.delete(url, headers=headers).json()
        else:
            return requests.get(url, headers=headers).json()
    except Exception as e:
        print("❌ Binance request failed:", e)
        return {"error": str(e)}


def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("❌ Failed to set leverage/margin:", e)


def get_symbol_info(symbol):
    info = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo").json()
    for s in info.get("symbols", []):
        if s["symbol"] == symbol:
            return s
    return None


def round_quantity(symbol, qty):
    info = get_symbol_info(symbol)
    if not info:
        return round(qty, 3)
    step_size = float([f["stepSize"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"][0])
    min_qty = float([f["minQty"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"][0])
    qty = (qty // step_size) * step_size
    if qty < min_qty:
        qty = min_qty
    return round(qty, 8)


# ===== Active Trades =====
def count_active_trades():
    try:
        positions = binance_signed_request("GET", "/fapi/v2/positionRisk")
        active_positions = [p for p in positions if abs(float(p["positionAmt"])) > 0]
        return len(active_positions)
    except Exception as e:
        print("❌ Failed to fetch active trades:", e)
        return 0


# ===== Calculate Quantity =====
def calculate_quantity(symbol):
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}).json()
        price = float(price_data["price"])
        position_value = TRADE_AMOUNT * LEVERAGE
        qty = position_value / price
        qty = round_quantity(symbol, qty)
        return qty
    except Exception as e:
        print("❌ Failed to calculate quantity:", e)
        return 0.001


# ===== Open Position =====
def open_position(symbol, side, limit_price):
    active_count = count_active_trades()
    if active_count >= MAX_ACTIVE_TRADES:
        print(f"🚫 Max active trades reached ({active_count}/{MAX_ACTIVE_TRADES})")
        return {"status": "max_trades_reached"}

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol)

    if symbol not in trades or trades[symbol].get("closed", True):
        log_trade_entry(symbol, side, "PENDING", limit_price)

    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": limit_price
    })

    if "orderId" in response:
        order_id = response["orderId"]
        threading.Thread(target=wait_and_notify_filled_entry, args=(symbol, side, order_id), daemon=True).start()

    return response


def wait_and_notify_filled_entry(symbol, side, order_id):
    notified = False
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        status = order_status.get("status")
        executed_qty = float(order_status.get("executedQty", 0))
        avg_price = float(order_status.get("avgPrice") or order_status.get("price") or 0)

        if not notified and status in ("PARTIALLY_FILLED", "FILLED") and executed_qty > 0:
            with trades_lock:
                trades[symbol] = {
                    "side": side,
                    "entry_price": avg_price,
                    "order_id": order_id,
                    "closed": False,
                    "exit_price": None,
                    "pnl": 0,
                    "pnl_percent": 0,
                    "peak": avg_price,
                    "trough": avg_price,
                    "trailing_monitor_started": False
                }
            log_trade_entry(symbol, side, order_id, avg_price)
            threading.Thread(target=monitor_trailing_and_exit, args=(symbol, side), daemon=True).start()
            notified = True

        if status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            break
        time.sleep(1)


# ===== Market Exit =====
def execute_market_exit(symbol, side):
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or abs(float(pos_data[0]["positionAmt"])) == 0:
        print(f"⚠️ No active position for {symbol} to close.")
        return {"status": "no_position"}

    qty = abs(float(pos_data[0]["positionAmt"]))
    qty = round_quantity(symbol, qty)
    close_side = "SELL" if side == "BUY" else "BUY"

    if EXIT_MARKET_DELAY and EXIT_MARKET_DELAY > 0:
        time.sleep(EXIT_MARKET_DELAY)

    response = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": close_side,
        "type": "MARKET",
        "quantity": qty
    })

    if "orderId" in response:
        threading.Thread(target=wait_and_notify_filled_exit, args=(symbol, response["orderId"]), daemon=True).start()
    return response


def wait_and_notify_filled_exit(symbol, order_id):
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        if order_status.get("status") == "FILLED":
            filled_price = float(order_status.get("avgPrice") or order_status.get("price") or 0)
            with trades_lock:
                if symbol in trades:
                    trades[symbol]["exit_price"] = filled_price
                    trades[symbol]["closed"] = True
            log_trade_exit(symbol, order_id, filled_price)
            clean_residual_positions(symbol)
            break
        time.sleep(1)


# ===== Clean residuals =====
def clean_residual_positions(symbol):
    try:
        binance_signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if pos_data and abs(float(pos_data[0]["positionAmt"])) > 0.00001:
            amt = abs(float(pos_data[0]["positionAmt"]))
            side = "SELL" if float(pos_data[0]["positionAmt"]) > 0 else "BUY"
            binance_signed_request("POST", "/fapi/v1/order", {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": round_quantity(symbol, amt)
            })
            print(f"🧹 Residual position cleaned for {symbol}")
    except Exception as e:
        print("⚠️ Residual cleanup failed:", e)


# ===== Trailing logic =====
def compute_ts_dynamic(profit_pct):
    try:
        ts_dynamic = max((TS_HIGH_OFFSET_PCT - TS_LOW_OFFSET_PCT) / 9.5 * (profit_pct - 0.5) + TS_LOW_OFFSET_PCT,
                         TS_LOW_OFFSET_PCT)
        return ts_dynamic
    except Exception as e:
        print("❌ compute_ts_dynamic error:", e)
        return TS_LOW_OFFSET_PCT


def get_current_price(symbol):
    try:
        p = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5).json()
        return float(p.get("price", 0))
    except Exception as e:
        print("❌ get_current_price error:", e)
        return 0.0


def monitor_trailing_and_exit(symbol, side):
    print(f"🛰️ Starting trailing monitor for {symbol}")
    while True:
        with trades_lock:
            if symbol not in trades or trades[symbol].get("closed"):
                print(f"ℹ️ {symbol} trade closed, stopping monitor.")
                return
            trade = trades[symbol]
            entry_price = float(trade.get("entry_price", 0))
            peak = float(trade.get("peak", entry_price))
            trough = float(trade.get("trough", entry_price))

        current_price = get_current_price(symbol)
        if current_price <= 0:
            time.sleep(1)
            continue

        if side.upper() == "BUY":
            if current_price > peak:
                with trades_lock:
                    trades[symbol]["peak"] = current_price
                peak = current_price
            profit_pct = (peak - entry_price) / entry_price * 100
            ts = compute_ts_dynamic(abs(profit_pct))
            if profit_pct >= TRAILING_ACTIVATION_PCT and current_price <= peak * (1 - ts / 100):
                print(f"🔔 Trailing stop hit for LONG {symbol}")
                execute_market_exit(symbol, side)
                return
        else:
            if current_price < trough:
                with trades_lock:
                    trades[symbol]["trough"] = current_price
                trough = current_price
            profit_pct = (entry_price - trough) / entry_price * 100
            ts = compute_ts_dynamic(abs(profit_pct))
            if profit_pct >= TRAILING_ACTIVATION_PCT and current_price >= trough * (1 + ts / 100):
                print(f"🔔 Trailing stop hit for SHORT {symbol}")
                execute_market_exit(symbol, side)
                return

        time.sleep(1)


# ===== Async Exit + Re-entry =====
def async_exit_and_open(symbol, side, entry_price):
    """Exit opposite position and open new one asynchronously"""
    def worker():
        try:
            print(f"🔄 Exiting opposite & opening new {side} for {symbol}")
            execute_market_exit(symbol, "SELL" if side == "BUY" else "BUY")
            time.sleep(OPPOSITE_CLOSE_DELAY)
            open_position(symbol, side, entry_price)
            print(f"✅ Opposite closed, new {side} trade opened for {symbol}")
        except Exception as e:
            print("❌ async_exit_and_open error:", e)
    threading.Thread(target=worker, daemon=True).start()


# ===== Webhook =====
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_data(as_text=True)
    try:
        parts = [p.strip() for p in data.split("|")]
        if len(parts) >= 6:
            ticker, comment, close_price, bar_high, bar_low, interval = parts[:6]
        else:
            ticker, comment, close_price, interval = parts[0], parts[1], parts[2], parts[-1]

        symbol = ticker.replace("USDT", "") + "USDT"
        close_price = float(close_price)
        comment = comment.upper().strip()

        print(f"📩 Alert: {symbol} | {comment} | {close_price}")

        if comment == "BUY_ENTRY":
            open_position(symbol, "BUY", close_price)
        elif comment == "SELL_ENTRY":
            open_position(symbol, "SELL", close_price)
        elif comment in ("EXIT_LONG", "CROSS_EXIT_LONG"):
            threading.Thread(target=monitor_trailing_and_exit, args=(symbol, "BUY"), daemon=True).start()
        elif comment in ("EXIT_SHORT", "CROSS_EXIT_SHORT"):
            threading.Thread(target=monitor_trailing_and_exit, args=(symbol, "SELL"), daemon=True).start()
        else:
            print(f"⚠️ Unknown comment: {comment}")
            return jsonify({"error": f"Unknown comment: {comment}"}), 400

        return jsonify({"status": "ok"})
    except Exception as e:
        print("❌ Webhook Error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


def self_ping():
    while True:
        try:
            requests.get("https://tradingview-binance-trailing.onrender.com/ping")
        except:
            pass
        time.sleep(5 * 60)


threading.Thread(target=self_ping, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
