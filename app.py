# app.py (Final) - trailing logic removed, exit alerts close market positions
from flask import Flask, request, jsonify
import requests
import hmac
import hashlib
import time
import threading
import os
from threading import Lock

# ===========================
# Config imports (user confirmed names)
# ===========================
from config import (
    BINANCE_API_KEY,
    BINANCE_SECRET_KEY,
    BASE_URL,
    TRADE_AMOUNT,
    LEVERAGE,
    MARGIN_TYPE,
    MAX_ACTIVE_TRADES,
    EXIT_MARKET_DELAY,
    OPPOSITE_CLOSE_DELAY,
    DEBUG,
)

# ===========================
# Import trade_notifier helpers & shared trades dict
# ===========================
from trade_notifier import (
    log_trade_entry,
    log_trade_exit,
    send_telegram_message,
    get_unrealized_pnl_pct,
    close_trade_on_binance,
    trades as notifier_trades,
)

# ===========================
# Flask + global state
# ===========================
app = Flask(__name__)
trades = notifier_trades             # shared dict with trade_notifier
trades_lock = Lock()

# ---------------------------
# Binance signed request helper
# ---------------------------
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
            r = requests.post(url, headers=headers, timeout=10)
            return r.json()
        elif http_method == "DELETE":
            r = requests.delete(url, headers=headers, timeout=10)
            return r.json()
        else:
            r = requests.get(url, headers=headers, timeout=10)
            return r.json()
    except Exception as e:
        print("❌ Binance request failed:", e)
        return {"error": str(e)}


# ---------------------------
# Exchange helpers
# ---------------------------
def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("❌ Failed to set leverage/margin:", e)


def get_symbol_info(symbol):
    try:
        info = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo", timeout=10).json()
        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                return s
    except Exception as e:
        print("❌ get_symbol_info error:", e)
    return None


def round_quantity(symbol, qty):
    info = get_symbol_info(symbol)
    if not info:
        try:
            return round(qty, 3)
        except Exception:
            return qty
    try:
        step_size = float([f["stepSize"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"][0])
        min_qty = float([f["minQty"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"][0])
        # quantize to step size
        qty = float(int(qty / step_size) * step_size)
    except Exception:
        qty = round(qty, 8)
    if qty < min_qty:
        qty = min_qty
    return round(qty, 8)


def get_current_price(symbol):
    try:
        p = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5).json()
        return float(p.get("price", 0))
    except Exception as e:
        print("❌ get_current_price error:", e)
        return 0.0


# ---------------------------
# Active trades and qty
# ---------------------------
def count_active_trades():
    try:
        positions = binance_signed_request("GET", "/fapi/v2/positionRisk")
        active_positions = [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]
        return len(active_positions)
    except Exception as e:
        print("❌ Failed to fetch active trades:", e)
        return 0


def calculate_quantity(symbol):
    try:
        price_data = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5).json()
        price = float(price_data["price"])
        position_value = TRADE_AMOUNT * LEVERAGE
        qty = position_value / price
        qty = round_quantity(symbol, qty)
        return qty
    except Exception as e:
        print("❌ Failed to calculate quantity:", e)
        return 0.001


# ---------------------------
# Entry placement
# ---------------------------
def open_position(symbol, side, limit_price):
    active_count = count_active_trades()
    if active_count >= MAX_ACTIVE_TRADES:
        print(f"🚫 Max active trades reached ({active_count}/{MAX_ACTIVE_TRADES})")
        return {"status": "max_trades_reached"}

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol)

    with trades_lock:
        if symbol not in trades or trades[symbol].get("closed", True):
            trades[symbol] = {
                "side": side,
                "entry_price": limit_price,
                "order_id": "PENDING",
                "closed": False,
                "exit_price": None,
                "pnl": 0,
                "pnl_percent": 0,
                "quantity": qty,
                "loss_bars": 0,
                "forced_exit": False,
                "entry_time": time.time(),
                "interval": "1h",
                "last_bar_high": limit_price,
                "last_bar_low": limit_price,
            }

    send_telegram_message(f"📩 Alert: {symbol} | {side}_ENTRY | {limit_price}")

    resp = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": str(limit_price)
    })

    if "orderId" in resp:
        order_id = resp["orderId"]
        threading.Thread(target=wait_and_notify_filled_entry, args=(symbol, side, order_id), daemon=True).start()
    else:
        send_telegram_message(f"❌ Order create failed for {symbol}: {resp}")

    return resp


# ---------------------------
# Wait for entry fill and notify
# ---------------------------
def wait_and_notify_filled_entry(symbol, side, order_id):
    notified = False
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        status = order_status.get("status")
        executed_qty = float(order_status.get("executedQty", 0)) if order_status.get("executedQty") else 0
        avg_price = 0.0

        if "fills" in order_status and order_status["fills"]:
            num = 0.0; den = 0.0
            for f in order_status["fills"]:
                num += float(f.get("price", 0)) * float(f.get("qty", 0))
                den += float(f.get("qty", 0))
            if den > 0:
                avg_price = num / den
        avg_price = avg_price or float(order_status.get("avgPrice") or order_status.get("price") or 0)

        if not notified and status in ("PARTIALLY_FILLED", "FILLED") and executed_qty > 0:
            with trades_lock:
                if symbol not in trades:
                    trades[symbol] = {}
                trades[symbol]["entry_price"] = avg_price
                trades[symbol]["order_id"] = order_id

            try:
                log_trade_entry(symbol, side, order_id, avg_price, trades[symbol].get("interval", "1h"))
            except Exception:
                send_telegram_message(f"📩 Filled: {symbol} | {side} | {avg_price}")

            notified = True

        if status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            break
        time.sleep(1)


# ---------------------------
# Market exit flows
# ---------------------------
def execute_market_exit(symbol, side):
    # side = "BUY" means close BUY (long) -> send SELL market
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or len(pos_data) == 0 or abs(float(pos_data[0].get("positionAmt", 0))) == 0:
        send_telegram_message(f"⚠️ No active position for {symbol} to close.")
        return {"status": "no_position"}

    qty = abs(float(pos_data[0]["positionAmt"]))
    qty = round_quantity(symbol, qty)
    close_side = "SELL" if side == "BUY" else "BUY"

    if EXIT_MARKET_DELAY and EXIT_MARKET_DELAY > 0:
        time.sleep(EXIT_MARKET_DELAY)

    resp = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": close_side,
        "type": "MARKET",
        "quantity": qty
    })

    if "orderId" in resp:
        threading.Thread(target=wait_and_notify_filled_exit, args=(symbol, resp["orderId"]), daemon=True).start()
    else:
        send_telegram_message(f"❌ Market close failed for {symbol}: {resp}")

    return resp


def wait_and_notify_filled_exit(symbol, order_id):
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        if order_status.get("status") == "FILLED":
            try:
                filled_price = float(order_status.get("avgPrice") or order_status.get("price") or 0)
            except Exception:
                filled_price = 0.0

            try:
                log_trade_exit(symbol, filled_price, reason="MARKET_CLOSE")
            except Exception as e:
                print(f"⚠️ log_trade_exit failed for {symbol}: {e}")
                send_telegram_message(f"💰 Closed {symbol} | Exit: {filled_price} (fallback notify)")

            with trades_lock:
                if symbol in trades:
                    trades[symbol]["exit_price"] = filled_price
                    trades[symbol]["closed"] = True

            try:
                clean_residual_positions(symbol)
            except Exception as e:
                print(f"⚠️ Residual cleanup error for {symbol}: {e}")

            break
        time.sleep(1)


def clean_residual_positions(symbol):
    try:
        binance_signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if pos_data and abs(float(pos_data[0].get("positionAmt", 0))) > 0.00001:
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


# ---------------------------
# Webhook endpoint
# ---------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_data(as_text=True)
    try:
        if DEBUG:
            print("🔔 Webhook raw payload:", data)

        parts = [p.strip() for p in data.split("|")]
        if len(parts) >= 6:
            ticker, comment, close_price, bar_high, bar_low, interval = parts[:6]
        else:
            # fallback parsing (keeps compatibility with earlier payloads)
            ticker, comment, close_price, interval = parts[0], parts[1], parts[2], parts[-1]
            bar_high = bar_low = None

        # normalize symbol to Binance futures format (e.g., BTC -> BTCUSDT)
        symbol = ticker.replace("USDT", "") + "USDT"
        try:
            close_price = float(close_price)
        except Exception:
            close_price = 0.0
        comment = comment.upper().strip()

        print(f"📩 Alert: {symbol} | {comment} | {close_price} | interval={interval}")

        # ENTRY: BUY
        if comment == "BUY_ENTRY":
            with trades_lock:
                existing = trades.get(symbol)
            if existing and not existing.get("closed", True):
                # Close existing trade then open new BUY
                def worker_replace():
                    execute_market_exit(symbol, existing.get("side"))
                    time.sleep(OPPOSITE_CLOSE_DELAY)
                    open_position(symbol, "BUY", close_price)
                threading.Thread(target=worker_replace, daemon=True).start()
            else:
                with trades_lock:
                    trades.setdefault(symbol, {})["interval"] = interval.lower()
                    trades.setdefault(symbol, {})["last_bar_high"] = float(bar_high) if bar_high else close_price
                    trades.setdefault(symbol, {})["last_bar_low"] = float(bar_low) if bar_low else close_price
                open_position(symbol, "BUY", close_price)

        # ENTRY: SELL
        elif comment == "SELL_ENTRY":
            with trades_lock:
                existing = trades.get(symbol)
            if existing and not existing.get("closed", True):
                # Close existing trade then open new SELL
                def worker_replace():
                    execute_market_exit(symbol, existing.get("side"))
                    time.sleep(OPPOSITE_CLOSE_DELAY)
                    open_position(symbol, "SELL", close_price)
                threading.Thread(target=worker_replace, daemon=True).start()
            else:
                with trades_lock:
                    trades.setdefault(symbol, {})["interval"] = interval.lower()
                    trades.setdefault(symbol, {})["last_bar_high"] = float(bar_high) if bar_high else close_price
                    trades.setdefault(symbol, {})["last_bar_low"] = float(bar_low) if bar_low else close_price
                open_position(symbol, "SELL", close_price)

        # EXIT signals -> immediate market close (TradingView comment: EXIT_LONG / EXIT_SHORT)
        elif comment == "EXIT_LONG":
            # close BUY (long) positions
            with trades_lock:
                if symbol in trades and not trades[symbol].get("closed", True):
                    send_telegram_message(f"📡 EXIT_LONG received for {symbol} — initiating market close.")
                    threading.Thread(target=execute_market_exit, args=(symbol, "BUY"), daemon=True).start()
                else:
                    send_telegram_message(f"📡 EXIT_LONG received for {symbol} but no active BUY position found.")

        elif comment == "EXIT_SHORT":
            # close SELL (short) positions
            with trades_lock:
                if symbol in trades and not trades[symbol].get("closed", True):
                    send_telegram_message(f"📡 EXIT_SHORT received for {symbol} — initiating market close.")
                    threading.Thread(target=execute_market_exit, args=(symbol, "SELL"), daemon=True).start()
                else:
                    send_telegram_message(f"📡 EXIT_SHORT received for {symbol} but no active SELL position found.")

        # CROSS EXIT + reverse entry (close then open opposite)
        elif comment == "CROSS_EXIT_LONG":
            # Close BUY then open SELL
            def worker_cross_long():
                execute_market_exit(symbol, "BUY")
                time.sleep(OPPOSITE_CLOSE_DELAY)
                open_position(symbol, "SELL", close_price)
            threading.Thread(target=worker_cross_long, daemon=True).start()

        elif comment == "CROSS_EXIT_SHORT":
            # Close SELL then open BUY
            def worker_cross_short():
                execute_market_exit(symbol, "SELL")
                time.sleep(OPPOSITE_CLOSE_DELAY)
                open_position(symbol, "BUY", close_price)
            threading.Thread(target=worker_cross_short, daemon=True).start()

        else:
            print(f"⚠️ Unknown comment: {comment}")
            return jsonify({"error": f"Unknown comment: {comment}"}), 400

        return jsonify({"status": "ok"})

    except Exception as e:
        print("❌ Webhook Error:", e)
        return jsonify({"error": str(e)}), 500


# ---------------------------
# Ping & self-ping
# ---------------------------
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


def self_ping():
    while True:
        try:
            requests.get(os.getenv("SELF_PING_URL", "https://tradingview-binance-trailing-dhhf.onrender.com/ping"), timeout=5)
        except Exception:
            pass
        time.sleep(5 * 60)


threading.Thread(target=self_ping, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
