from flask import Flask, request, jsonify
import requests
import hmac
import hashlib
import time
import threading
import os
from threading import Lock

# import config values
from config import (
    BINANCE_API_KEY, BINANCE_SECRET_KEY, BASE_URL,
    TRADE_AMOUNT, LEVERAGE, MARGIN_TYPE, MAX_ACTIVE_TRADES,
    EXIT_MARKET_DELAY, OPPOSITE_CLOSE_DELAY,
    TRAILING_ACTIVATION_PCT, TS_LOW_OFFSET_PCT, TS_HIGH_OFFSET_PCT,
    TRAILING_UPDATE_INTERVAL, DUAL_TRAILING_ENABLED, TRAILING_DISTANCE_PCT, TRAILING_COMPARE_PNL
)

# import notifier helpers
from trade_notifier import (
    send_telegram_message, log_trade_entry, log_trade_exit, log_trailing_start, trades
)

app = Flask(__name__)
trades_lock = Lock()

# --------- Binance signed request helper ----------
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
            return requests.post(url, headers=headers, timeout=10).json()
        elif http_method == "DELETE":
            return requests.delete(url, headers=headers, timeout=10).json()
        else:
            return requests.get(url, headers=headers, timeout=10).json()
    except Exception as e:
        print("❌ Binance request failed:", e)
        return {"error": str(e)}

# --------- Exchange helpers ----------
def set_leverage_and_margin(symbol):
    try:
        binance_signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": LEVERAGE})
        binance_signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": MARGIN_TYPE})
    except Exception as e:
        print("❌ Failed to set leverage/margin:", e)

def get_symbol_info(symbol):
    info = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo", timeout=10).json()
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

def get_current_price(symbol):
    try:
        p = requests.get(f"{BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5).json()
        return float(p.get("price", 0))
    except Exception as e:
        print("❌ get_current_price error:", e)
        return 0.0

# --------- Active trades and qty ----------
def count_active_trades():
    try:
        positions = binance_signed_request("GET", "/fapi/v2/positionRisk")
        active_positions = [p for p in positions if abs(float(p["positionAmt"])) > 0]
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

# --------- Entry placement ----------
def open_position(symbol, side, limit_price):
    active_count = count_active_trades()
    if active_count >= MAX_ACTIVE_TRADES:
        print(f"🚫 Max active trades reached ({active_count}/{MAX_ACTIVE_TRADES})")
        return {"status": "max_trades_reached"}

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol)

    # Immediate old-format entry Telegram message (user requested)
    send_telegram_message(f"📩 Alert: {symbol} | {side}_ENTRY | {limit_price}")

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
                "trails": {
                    "primary": {
                        "active": True,
                        "activation_pct": TRAILING_ACTIVATION_PCT,
                        "distance_pct": TS_LOW_OFFSET_PCT,
                        "peak": limit_price,
                        "trough": limit_price,
                        "stop": None,
                        "locked_pnl_usd": 0.0,
                        "locked_pnl_pct": 0.0
                    },
                    "secondary": {
                        "active": False,
                        "activation_pct": TRAILING_ACTIVATION_PCT/2,
                        "distance_pct": TS_LOW_OFFSET_PCT/2,
                        "peak": limit_price,
                        "trough": limit_price,
                        "stop": None,
                        "locked_pnl_usd": 0.0,
                        "locked_pnl_pct": 0.0
                    },
                    "final": "primary"
                },
                "trailing_monitor_started": False,
                # LOSS CONTROL fields
                "loss_bars": 0,
                "forced_exit": False
            }

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

    return resp

# --------- Wait for entry fill and start trailing ----------
def wait_and_notify_filled_entry(symbol, side, order_id):
    notified = False
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        status = order_status.get("status")
        executed_qty = float(order_status.get("executedQty", 0)) if order_status.get("executedQty") else 0
        avg_price = 0.0
        # best effort avg price from fills
        if "fills" in order_status and order_status["fills"]:
            num = 0.0; den = 0.0
            for f in order_status["fills"]:
                num += float(f.get("price", 0)) * float(f.get("qty", 0))
                den += float(f.get("qty", 0))
            if den > 0:
                avg_price = num/den
        avg_price = avg_price or float(order_status.get("avgPrice") or order_status.get("price") or 0)

        if not notified and status in ("PARTIALLY_FILLED", "FILLED") and executed_qty > 0:
            # record actual entry details
            with trades_lock:
                trades[symbol]["entry_price"] = avg_price
                trades[symbol]["order_id"] = order_id
                # initialize peaks/troughs
                trades[symbol]["trails"]["primary"]["peak"] = avg_price
                trades[symbol]["trails"]["primary"]["trough"] = avg_price
                trades[symbol]["trails"]["secondary"]["peak"] = avg_price
                trades[symbol]["trails"]["secondary"]["trough"] = avg_price

            # notify via trade_notifier
            try:
                log_trade_entry(symbol, side, order_id, avg_price)
            except Exception:
                send_telegram_message(f"📩 Filled: {symbol} | {side} | {avg_price}")

            # ensure trailing monitor is running (single monitor safeguard)
            with trades_lock:
                if not trades[symbol].get("trailing_monitor_started"):
                    trades[symbol]["trailing_monitor_started"] = True
                    # announce primary trailing start
                    try:
                        log_trailing_start(symbol, "1st")
                    except Exception:
                        send_telegram_message(f"🛰️ Starting 1st trailing monitor for {symbol}")
                    threading.Thread(target=monitor_trailing_and_exit, args=(symbol, side), daemon=True).start()

            notified = True

        if status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            break
        time.sleep(1)

# --------- Market exit ----------
def execute_market_exit(symbol, side):
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or len(pos_data) == 0 or abs(float(pos_data[0].get("positionAmt", 0))) == 0:
        print(f"⚠️ No active position for {symbol} to close.")
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
    return resp

def wait_and_notify_filled_exit(symbol, order_id):
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        if order_status.get("status") == "FILLED":
            filled_price = float(order_status.get("avgPrice") or order_status.get("price") or 0)
            # mark closed and call notifier
            with trades_lock:
                if symbol in trades:
                    trades[symbol]["exit_price"] = filled_price
                    trades[symbol]["closed"] = True
                    trades[symbol]["trailing_monitor_started"] = False
            try:
                # call notifier's flexible log_trade_exit
                log_trade_exit(symbol, filled_price, reason="MARKET_CLOSE")
            except Exception:
                send_telegram_message(f"💰 Closed {symbol} | Exit: {filled_price}")
            clean_residual_positions(symbol)
            break
        time.sleep(1)

# --------- Clean residual positions ----------
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

# --------- Trailing utilities ----------
def compute_ts_dynamic(profit_pct):
    """Mimic pine ts_dynamic linear interpolation"""
    try:
        ts_dynamic = max((TS_HIGH_OFFSET_PCT - TS_LOW_OFFSET_PCT) / 9.5 * (profit_pct - 0.5) + TS_LOW_OFFSET_PCT,
                         TS_LOW_OFFSET_PCT)
        return ts_dynamic
    except Exception as e:
        print("❌ compute_ts_dynamic error:", e)
        return TS_LOW_OFFSET_PCT

def compute_locked_pnl(entry_price, stop_price, side):
    """Return (pnl_usd, pnl_pct) for an exit at stop_price using current TRADE_AMOUNT & LEVERAGE"""
    try:
        if entry_price == 0:
            return 0.0, 0.0
        if side.upper() == "BUY":
            pnl_pct = ((stop_price - entry_price) / entry_price) * 100 * LEVERAGE
            pnl_usd = ((stop_price - entry_price) * TRADE_AMOUNT * LEVERAGE) / entry_price
        else:
            pnl_pct = ((entry_price - stop_price) / entry_price) * 100 * LEVERAGE
            pnl_usd = ((entry_price - stop_price) * TRADE_AMOUNT * LEVERAGE) / entry_price
        return round(pnl_usd, 2), round(pnl_pct, 2)
    except Exception as e:
        print("❌ compute_locked_pnl error:", e)
        return 0.0, 0.0

# --------- Unified trailing monitor (primary + secondary) ----------
def monitor_trailing_and_exit(symbol, side):
    """
    Single monitor per symbol. Maintains primary and optionally secondary trails.
    Selects final trail by locked PnL (USD) if TRAILING_COMPARE_PNL True, else by locked_pnl_pct.
    Closes in market when final stop is hit.

    Also includes loss-control:
    - Immediate Stoploss: if pnl_percent <= -STOP_LOSS_PCT * LEVERAGE => immediate market exit
    - Consecutive negative checks: increments loss_bars when pnl < 0; when loss_bars >= LOSS_BARS_LIMIT => force close
    - Recovery: if pnl >= 0 after being negative, reset loss_bars and notify recovery
    """
    print(f"🛰️ Trailing monitor started for {symbol} (side: {side})")
    start_time = time.time()
    max_run = max(60, int(os.getenv("MAX_TRAILING_RUNTIME_MIN", "120"))) * 60  # safety cap in seconds

    # announce primary trail start
    try:
        log_trailing_start(symbol, "primary")
    except Exception:
        send_telegram_message(f"🛰️ Starting primary trailing monitor for {symbol}")

    while True:
        # safety: stop if exceeded runtime
        if time.time() - start_time > max_run:
            print(f"⚠️ Trailing monitor max runtime reached for {symbol}. Stopping monitor.")
            with trades_lock:
                if symbol in trades:
                    trades[symbol]["trailing_monitor_started"] = False
            return

        with trades_lock:
            if symbol not in trades or trades[symbol].get("closed"):
                print(f"ℹ️ {symbol} trade closed or missing; stopping trailing.")
                if symbol in trades:
                    trades[symbol]["trailing_monitor_started"] = False
                return
            trade = trades[symbol]
            entry_price = float(trade.get("entry_price", 0))
            trails = trade.setdefault("trails", {})
            # loss metadata
            loss_bars = trade.get("loss_bars", 0)

        current = get_current_price(symbol)
        if current <= 0:
            time.sleep(1)
            continue

        # prefer live unrealized pnl percent from config helper
        try:
            from config import get_unrealized_pnl_pct
            pnl_percent = get_unrealized_pnl_pct(symbol) or 0.0
        except Exception:
            # fallback local calc
            if side.upper() == "BUY":
                pnl_percent = ((current - entry_price) / entry_price) * 100 * LEVERAGE if entry_price > 0 else 0.0
            else:
                pnl_percent = ((entry_price - current) / entry_price) * 100 * LEVERAGE if entry_price > 0 else 0.0

        # --------- LOSS CONTROL: Immediate Stoploss ----------
        try:
            immediate_threshold = -STOP_LOSS_PCT * LEVERAGE
            if pnl_percent <= immediate_threshold and not trade.get("forced_exit", False):
                with trades_lock:
                    trades[symbol]["forced_exit"] = True
                msg = (f"🚨 Immediate Stoploss Triggered for {symbol} | PnL%: {round(pnl_percent,2)} "
                       f"<= -{STOP_LOSS_PCT} x {LEVERAGE} => closing market position.")
                try:
                    log_trade_exit(symbol, current, reason="STOP_LOSS")
                except Exception:
                    send_telegram_message(msg)
                execute_market_exit(symbol, side)
                return
        except Exception as e:
            print("❌ Immediate stoploss check error:", e)

        # --------- LOSS CONTROL: Consecutive negative bars & recovery logic ----------
        try:
            with trades_lock:
                trade = trades[symbol]
                if pnl_percent < 0:
                    trade["loss_bars"] = trade.get("loss_bars", 0) + 1
                    if trade["loss_bars"] >= LOSS_BARS_LIMIT and not trade.get("forced_exit", False):
                        trade["forced_exit"] = True
                        msg = (f"⚠️ Consecutive-Loss Exit for {symbol} | Loss bars: {trade['loss_bars']} "
                               f"| PnL%: {round(pnl_percent,2)} — executing market close.")
                        try:
                            log_trade_exit(symbol, current, reason="FORCE_CLOSE")
                        except Exception:
                            send_telegram_message(msg)
                        execute_market_exit(symbol, side)
                        return
                else:
                    # recovered (pnl >= 0)
                    if trade.get("loss_bars", 0) > 0:
                        trade["loss_bars"] = 0
                        if trade.get("forced_exit", False):
                            trade["forced_exit"] = False
                        recovery_msg = (f"✅ Recovered — Trailing resumed for {symbol} | PnL%: {round(pnl_percent,2)}")
                        try:
                            log_trailing_start(symbol, "recovered")
                        except Exception:
                            send_telegram_message(recovery_msg)
        except Exception as e:
            print("❌ Consecutive-loss/recovery logic error:", e)

        # Ensure primary/secondary trails present
        if "primary" not in trails:
            trails["primary"] = {
                "active": True,
                "activation_pct": TRAILING_ACTIVATION_PCT,
                "distance_pct": TS_LOW_OFFSET_PCT,
                "peak": entry_price,
                "trough": entry_price,
                "stop": None,
                "locked_pnl_usd": 0.0,
                "locked_pnl_pct": 0.0
            }
        if "secondary" not in trails:
            trails["secondary"] = {
                "active": False,
                "activation_pct": TRAILING_ACTIVATION_PCT/2,
                "distance_pct": TS_LOW_OFFSET_PCT/2,
                "peak": entry_price,
                "trough": entry_price,
                "stop": None,
                "locked_pnl_usd": 0.0,
                "locked_pnl_pct": 0.0
            }

        # Update trails
        for key in ("primary", "secondary"):
            t = trails[key]
            if not t.get("active"):
                continue
            if side.upper() == "BUY":
                if current > t["peak"]:
                    t["peak"] = current
                profit_pct = (t["peak"] - entry_price) / entry_price * 100 if entry_price > 0 else 0
                activated = profit_pct >= t.get("activation_pct", TRAILING_ACTIVATION_PCT)
                if activated:
                    ts = compute_ts_dynamic(abs(profit_pct))
                    distance_pct = t.get("distance_pct") or ts
                    stop_price = t["peak"] * (1 - distance_pct / 100)
                    t["stop"] = round(stop_price, 8)
                    usd, pct = compute_locked_pnl(entry_price, t["stop"], side)
                    t["locked_pnl_usd"], t["locked_pnl_pct"] = usd, pct
            else:  # SELL
                if current < t["trough"]:
                    t["trough"] = current
                profit_pct = (entry_price - t["trough"]) / entry_price * 100 if entry_price > 0 else 0
                activated = profit_pct >= t.get("activation_pct", TRAILING_ACTIVATION_PCT/2)
                if activated:
                    ts = compute_ts_dynamic(abs(profit_pct))
                    distance_pct = t.get("distance_pct") or ts
                    stop_price = t["trough"] * (1 + distance_pct / 100)
                    t["stop"] = round(stop_price, 8)
                    usd, pct = compute_locked_pnl(entry_price, t["stop"], side)
                    t["locked_pnl_usd"], t["locked_pnl_pct"] = usd, pct

        # Choose final trail and check final stop trigger (as before)
        with trades_lock:
            p = trails["primary"]
            s = trails["secondary"]
            p_locked = p.get("locked_pnl_usd", 0.0)
            s_locked = s.get("locked_pnl_usd", -999999.0) if s.get("active") else -999999.0
            if TRAILING_COMPARE_PNL:
                final_key = "secondary" if (s.get("active") and s_locked > p_locked) else "primary"
            else:
                p_pct = p.get("locked_pnl_pct", 0.0)
                s_pct = s.get("locked_pnl_pct", -9999.0) if s.get("active") else -9999.0
                final_key = "secondary" if (s.get("active") and s_pct > p_pct) else "primary"
            final_stop = trails[final_key].get("stop")
            trades[symbol]["trails"]["final"] = final_key

        if final_stop:
            if side.upper() == "BUY" and current <= final_stop:
                print(f"🔔 Final stop ({final_key}) hit for LONG {symbol} current={current}, stop={final_stop}")
                execute_market_exit(symbol, side)
                return
            elif side.upper() == "SELL" and current >= final_stop:
                print(f"🔔 Final stop ({final_key}) hit for SHORT {symbol} current={current}, stop={final_stop}")
                execute_market_exit(symbol, side)
                return

        time.sleep(max(1, TRAILING_UPDATE_INTERVAL))

# --------- Async exit + re-entry (force close then open) ----------
def async_exit_and_open(symbol, new_side, entry_price):
    def worker():
        try:
            with trades_lock:
                existing = trades.get(symbol)
            if existing and not existing.get("closed", True):
                existing_side = existing.get("side")
                print(f"🔄 Opposite signal detected — forcing market close for {symbol} ({existing_side})")
                execute_market_exit(symbol, existing_side)
                time.sleep(OPPOSITE_CLOSE_DELAY)
            open_position(symbol, new_side, entry_price)
        except Exception as e:
            print("❌ async_exit_and_open error:", e)
    threading.Thread(target=worker, daemon=True).start()

# --------- Webhook endpoint ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_data(as_text=True)
    try:
        parts = [p.strip() for p in data.split("|")]
        if len(parts) >= 6:
            ticker, comment, close_price, bar_high, bar_low, interval = parts[:6]
        else:
            ticker, comment, close_price, interval = parts[0], parts[1], parts[2], parts[-1]
            bar_high = bar_low = None

        symbol = ticker.replace("USDT", "") + "USDT"
        close_price = float(close_price)
        comment = comment.upper().strip()

        print(f"📩 Alert: {symbol} | {comment} | {close_price}")

        # Entry signals
        if comment == "BUY_ENTRY":
            with trades_lock:
                existing = trades.get(symbol)
            if existing and not existing.get("closed", True):
                if existing.get("side", "").upper() == "BUY":
                    print(f"ℹ️ BUY_ENTRY ignored for {symbol} (same direction active).")
                else:
                    async_exit_and_open(symbol, "BUY", close_price)
            else:
                open_position(symbol, "BUY", close_price)

        elif comment == "SELL_ENTRY":
            with trades_lock:
                existing = trades.get(symbol)
            if existing and not existing.get("closed", True):
                if existing.get("side", "").upper() == "SELL":
                    print(f"ℹ️ SELL_ENTRY ignored for {symbol} (same direction active).")
                else:
                    async_exit_and_open(symbol, "SELL", close_price)
            else:
                open_position(symbol, "SELL", close_price)

        # EXIT signals: do not market close immediately; activate secondary trailing
        elif comment in ("EXIT_LONG", "CROSS_EXIT_LONG"):
            with trades_lock:
                if symbol in trades and not trades[symbol].get("closed", True) and DUAL_TRAILING_ENABLED:
                    trails = trades[symbol].setdefault("trails", {})
                    sec = trails.setdefault("secondary", {})
                    if not sec.get("active"):
                        sec["active"] = True
                        cp = get_current_price(symbol) or close_price
                        sec["peak"] = cp
                        sec["trough"] = cp
                        try:
                            log_trailing_start(symbol, "secondary")
                        except Exception:
                            send_telegram_message(f"🛰️ Starting 2nd trailing monitor for {symbol} (exit signal detected)")
                    if not trades[symbol].get("trailing_monitor_started"):
                        trades[symbol]["trailing_monitor_started"] = True
                        threading.Thread(target=monitor_trailing_and_exit, args=(symbol, "BUY"), daemon=True).start()
                else:
                    print(f"⚠️ EXIT received for {symbol} but no active trade to attach secondary trailing, or dual trailing disabled.")

        elif comment in ("EXIT_SHORT", "CROSS_EXIT_SHORT"):
            with trades_lock:
                if symbol in trades and not trades[symbol].get("closed", True) and DUAL_TRAILING_ENABLED:
                    trails = trades[symbol].setdefault("trails", {})
                    sec = trails.setdefault("secondary", {})
                    if not sec.get("active"):
                        sec["active"] = True
                        cp = get_current_price(symbol) or close_price
                        sec["peak"] = cp
                        sec["trough"] = cp
                        try:
                            log_trailing_start(symbol, "secondary")
                        except Exception:
                            send_telegram_message(f"🛰️ Starting 2nd trailing monitor for {symbol} (exit signal detected)")
                    if not trades[symbol].get("trailing_monitor_started"):
                        trades[symbol]["trailing_monitor_started"] = True
                        threading.Thread(target=monitor_trailing_and_exit, args=(symbol, "SELL"), daemon=True).start()
                else:
                    print(f"⚠️ EXIT received for {symbol} but no active trade to attach secondary trailing, or dual trailing disabled.")

        else:
            print(f"⚠️ Unknown comment: {comment}")
            return jsonify({"error": f"Unknown comment: {comment}"}), 400

        return jsonify({"status": "ok"})
    except Exception as e:
        print("❌ Webhook Error:", e)
        return jsonify({"error": str(e)}), 500

# --------- Ping & self-ping ----------
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

def self_ping():
    while True:
        try:
            requests.get(os.getenv("SELF_PING_URL", "https://tradingview-binance-trailing.onrender.com/ping"), timeout=5)
        except:
            pass
        time.sleep(5 * 60)

threading.Thread(target=self_ping, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
