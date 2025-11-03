# app.py (FULL)
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
    TRAILING_ACTIVATION_PCT,
    TRAILING_DISTANCE_PCT,
    TS_LOW_OFFSET_PCT,
    TS_HIGH_OFFSET_PCT,
    TSI_PRIMARY_TRIGGER_PCT,
    TSI_LOW_PROFIT_OFFSET_PCT,
    TSI_HIGH_PROFIT_OFFSET_PCT,
    # TRAILING_UPDATE_INTERVAL may or may not exist in config
    # STOP_LOSS_PCT may or may not exist in config
    DEBUG,
)

# provide safe defaults if not present
try:
    from config import TRAILING_UPDATE_INTERVAL
except Exception:
    TRAILING_UPDATE_INTERVAL = 2  # seconds default

try:
    from config import STOP_LOSS_PCT
except Exception:
    STOP_LOSS_PCT = 2.0  # percent default

# ===========================
# Import trade_notifier helpers & shared trades dict
# ===========================
from trade_notifier import (
    log_trade_entry,
    log_trade_exit,
    send_telegram_message,
    get_unrealized_pnl_pct,
    log_trailing_start,
    trades as notifier_trades,
)

# optional boolean; default True
try:
    from config import USE_TRAILING_STOP
except Exception:
    USE_TRAILING_STOP = True

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
    try:
        qty = (qty // step_size) * step_size
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
                "dynamic_trail": {
                    "active": False,
                    "activation_pct": TRAILING_ACTIVATION_PCT,
                    "peak": limit_price,
                    "trough": limit_price,
                    "stop_price": None,
                    "notified": False,
                },
                "trailing_monitor_started": False,
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
# Wait for entry fill and mark trailing
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
                dyn = trades[symbol].setdefault("dynamic_trail", {})
                dyn["peak"] = avg_price
                dyn["trough"] = avg_price

            try:
                log_trade_entry(symbol, side, order_id, avg_price, trades[symbol].get("interval", "1h"))
            except Exception as e:
                send_telegram_message(f"📩 Filled: {symbol} | {side} | {avg_price}")

            with trades_lock:
                trades[symbol]["trailing_monitor_started"] = True
                send_telegram_message(f"🛰️ Dynamic trailing monitor marked for {symbol}")

            notified = True

        if status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            break
        time.sleep(1)


# ---------------------------
# Market exit flows
# ---------------------------
def execute_market_exit(symbol, side):
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
                    trades[symbol]["trailing_monitor_started"] = False

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


# ==============================
# 🔧 Symbol tick & trailing helpers
# ==============================
def get_symbol_tick_size(symbol):
    try:
        url = f"{BASE_URL}/fapi/v1/exchangeInfo"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        for s in data["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "PRICE_FILTER":
                        return float(f["tickSize"])
    except Exception as e:
        print(f"⚠️ Tick size fetch error for {symbol}: {e}")
    return 0.0001


def compute_ts_dynamic(entry_price, side, current_price,
                       activation_pct=TRAILING_ACTIVATION_PCT,
                       trail_pct=TRAILING_DISTANCE_PCT):
    try:
        if entry_price <= 0 or current_price <= 0:
            return None

        def _to_percent(p):
            try:
                p = float(p)
            except Exception:
                return 0.0
            if abs(p) < 1.0:
                return p * 100.0
            return p

        activation_pct_norm = _to_percent(activation_pct)
        trail_pct_norm = _to_percent(trail_pct)

        try:
            ts_low = _to_percent(TSI_LOW_PROFIT_OFFSET_PCT)
            ts_high = _to_percent(TSI_HIGH_PROFIT_OFFSET_PCT)
        except Exception:
            ts_low = trail_pct_norm
            ts_high = trail_pct_norm

        if side.upper() == "BUY":
            profit_pct = abs((current_price - entry_price) / entry_price) * 100.0
        else:
            profit_pct = abs((entry_price - current_price) / entry_price) * 100.0

        if profit_pct < activation_pct_norm:
            return None

        slope = (ts_high - ts_low) / 9.5 if 9.5 != 0 else 0.0
        dynamic_pct = slope * (profit_pct - 0.5) + ts_low
        if dynamic_pct < ts_low:
            dynamic_pct = ts_low
        if dynamic_pct <= 0:
            dynamic_pct = trail_pct_norm

        if side.upper() == "BUY":
            stop_price = current_price * (1.0 - dynamic_pct / 100.0)
        else:
            stop_price = current_price * (1.0 + dynamic_pct / 100.0)

        return round(stop_price, 8)

    except Exception as e:
        print(f"⚠️ compute_ts_dynamic (slope) error: {e}")
        return None


def calculate_trailing_offsets(entry_price, side,
                               distance_pct=TRAILING_DISTANCE_PCT,
                               low_offset_pct=TS_LOW_OFFSET_PCT,
                               high_offset_pct=TS_HIGH_OFFSET_PCT):
    try:
        if side.upper() == "BUY":
            offset_high = entry_price * (1 + (distance_pct + high_offset_pct) / 100.0)
            offset_low = entry_price * (1 - (distance_pct + low_offset_pct) / 100.0)
        else:
            offset_high = entry_price * (1 + (distance_pct + low_offset_pct) / 100.0)
            offset_low = entry_price * (1 - (distance_pct + high_offset_pct) / 100.0)
        return {
            "offset_high": round(offset_high, 8),
            "offset_low": round(offset_low, 8)
        }
    except Exception as e:
        print(f"⚠️ calculate_trailing_offsets error: {e}")
        return {"offset_high": entry_price, "offset_low": entry_price}


def compute_locked_pnl(entry_price, current_price, side, quantity=1.0):
    try:
        if entry_price <= 0 or quantity <= 0:
            return 0.0, 0.0
        if side.upper() == "BUY":
            pnl_usd = (current_price - entry_price) * quantity
        else:
            pnl_usd = (entry_price - current_price) * quantity
        pnl_pct = (pnl_usd / (entry_price * quantity)) * 100
        return round(pnl_usd, 3), round(pnl_pct, 2)
    except Exception as e:
        print(f"⚠️ compute_locked_pnl error: {e}")
        return 0.0, 0.0


# ---------------------------
# Legacy per-trade monitor (kept for compatibility)
# ---------------------------
def monitor_trailing_and_exit(symbol, side):
    print(f"🛰️ (legacy) Dynamic trailing monitor started for {symbol} (side: {side})")
    start_time = time.time()
    max_run = max(60, int(os.getenv("MAX_TRAILING_RUNTIME_MIN", "120"))) * 60

    while True:
        if time.time() - start_time > max_run:
            with trades_lock:
                if symbol in trades:
                    trades[symbol]["trailing_monitor_started"] = False
            return

        with trades_lock:
            if symbol not in trades or trades[symbol].get("closed"):
                if symbol in trades:
                    trades[symbol]["trailing_monitor_started"] = False
                return
            trade = trades[symbol]
            entry_price = float(trade.get("entry_price", 0))
            dyn = trade.setdefault("dynamic_trail", {})

        current_price = get_current_price(symbol)
        if current_price <= 0:
            time.sleep(1)
            continue

        stop_candidate = compute_ts_dynamic(entry_price, side, current_price)
        if stop_candidate is None:
            time.sleep(max(1, TRAILING_UPDATE_INTERVAL))
            continue

        with trades_lock:
            prev = dyn.get("stop_price")
            if side.upper() == "BUY":
                if prev is None or stop_candidate > prev:
                    dyn["stop_price"] = stop_candidate
                    if DEBUG:
                        msg = f"[DEBUG] {symbol} legacy stop_price updated: {stop_candidate} (prev: {prev})"
                        print(msg)
                        send_telegram_message(msg)
            else:
                if prev is None or stop_candidate < prev:
                    dyn["stop_price"] = stop_candidate
                    if DEBUG:
                        msg = f"[DEBUG] {symbol} legacy stop_price updated: {stop_candidate} (prev: {prev})"
                        print(msg)
                        send_telegram_message(msg)

            if side.upper() == "BUY" and current_price <= dyn.get("stop_price", 0):
                send_telegram_message(f"🎯 <b>{symbol}</b> Dynamic trailing stop hit (BUY). Stop: {dyn['stop_price']} Current: {current_price}")
                execute_market_exit(symbol, side)
                return
            if side.upper() == "SELL" and current_price >= dyn.get("stop_price", 999999999):
                send_telegram_message(f"🎯 <b>{symbol}</b> Dynamic trailing stop hit (SELL). Stop: {dyn['stop_price']} Current: {current_price}")
                execute_market_exit(symbol, side)
                return

        time.sleep(max(1, TRAILING_UPDATE_INTERVAL))


# ---------------------------
# Global trailing monitor (single thread for all trades)
# ---------------------------
def global_trailing_monitor():
    print("🛰️ Global trailing monitor started")
    while True:
        try:
            with trades_lock:
                symbols = [s for s, t in trades.items() if not t.get("closed", True) and t.get("trailing_monitor_started", False)]

            for symbol in symbols:
                try:
                    with trades_lock:
                        trade = trades.get(symbol)
                        if not trade or trade.get("closed", True):
                            continue
                        side = trade.get("side", "BUY").upper()
                        entry_price = float(trade.get("entry_price", 0))
                        dyn = trade.setdefault("dynamic_trail", {})
                        qty = trade.get("quantity", 1.0)

                    current_price = get_current_price(symbol)
                    if current_price <= 0:
                        continue

                    # compute live pnl %
                    try:
                        pnl_percent = get_unrealized_pnl_pct(symbol) or 0.0
                    except Exception:
                        if entry_price > 0:
                            if side == "BUY":
                                pnl_percent = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
                            else:
                                pnl_percent = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE
                        else:
                            pnl_percent = 0.0

                    # immediate hard stoploss:
                    try:
                        immediate_threshold = -STOP_LOSS_PCT * LEVERAGE
                        if pnl_percent <= immediate_threshold and not trade.get("forced_exit", False):
                            with trades_lock:
                                trades[symbol]["forced_exit"] = True
                            msg = f"🚨 Immediate Stoploss Triggered for {symbol} | PnL%: {round(pnl_percent,2)}"
                            try:
                                log_trade_exit(symbol, current_price, reason="STOP_LOSS")
                            except Exception:
                                send_telegram_message(msg)
                            execute_market_exit(symbol, side)
                            continue
                    except Exception as e:
                        print("❌ Immediate stoploss check error:", e)

                    # trailing activation
                    activated = abs(pnl_percent) >= dyn.get("activation_pct", TRAILING_ACTIVATION_PCT)

                    if not dyn.get("active", False) and activated:
                        with trades_lock:
                            dyn["active"] = True
                            dyn["peak_pnl"] = pnl_percent
                            dyn["trough_pnl"] = pnl_percent
                            dyn["notified"] = dyn.get("notified", False)
                        if not dyn.get("notified", False):
                            try:
                                log_trailing_start(symbol, round(pnl_percent, 2))
                            except Exception:
                                send_telegram_message(f"🎯 <b>{symbol}</b> Trailing Started @ {round(pnl_percent,2)}%")
                            with trades_lock:
                                dyn["notified"] = True

                    # active trailing update
                    if dyn.get("active", False):
                        with trades_lock:
                            if side == "BUY":
                                dyn["peak_pnl"] = max(pnl_percent, dyn.get("peak_pnl", -999))
                            else:
                                dyn["trough_pnl"] = min(pnl_percent, dyn.get("trough_pnl", 999))

                        profit_zone = min(max(abs(pnl_percent), 0.0), 10.0)
                        dynamic_offset = (
                            TSI_LOW_PROFIT_OFFSET_PCT +
                            (TSI_HIGH_PROFIT_OFFSET_PCT - TSI_LOW_PROFIT_OFFSET_PCT) * (profit_zone / 10.0)
                        )

                        # ensure peak/trough keys exist and update them (use for price_for_stop_calc)
                        with trades_lock:
                            if "peak" not in dyn:
                                dyn["peak"] = entry_price
                            if "trough" not in dyn:
                                dyn["trough"] = entry_price

                            if side == "BUY":
                                if current_price > dyn.get("peak", entry_price):
                                    dyn["peak"] = current_price
                                price_for_stop_calc = dyn["peak"]
                            else:
                                if current_price < dyn.get("trough", entry_price):
                                    dyn["trough"] = current_price
                                price_for_stop_calc = dyn["trough"]

                        # compute candidate stop (use peak/trough for BUY/SELL)
                        stop_candidate = compute_ts_dynamic(
                            entry_price,
                            side,
                            price_for_stop_calc,
                            activation_pct=TRAILING_ACTIVATION_PCT,
                            trail_pct=dynamic_offset if dynamic_offset > 0 else TRAILING_DISTANCE_PCT
                        )

                        if stop_candidate is None:
                            # not active yet (rare since dyn.active True) or invalid candidate
                            continue

                        # debug: stop_candidate computed
                        if DEBUG:
                            msg = f"[DEBUG] {symbol} stop_candidate computed: {stop_candidate} (price_for_stop_calc: {price_for_stop_calc}, dynamic_offset: {dynamic_offset})"
                            print(msg)
                            send_telegram_message(msg)

                        # tighten trailing stop if better/tighter
                        with trades_lock:
                            prev_stop = dyn.get("stop_price")
                            updated = False
                            if side == "BUY":
                                if prev_stop is None or stop_candidate > prev_stop:
                                    dyn["stop_price"] = stop_candidate
                                    updated = True
                            else:
                                if prev_stop is None or stop_candidate < prev_stop:
                                    dyn["stop_price"] = stop_candidate
                                    updated = True
                            dyn["locked_pnl_usd"] = compute_locked_pnl(entry_price, dyn.get("stop_price"), side, qty)

                        if updated and DEBUG:
                            msg = f"[DEBUG] {symbol} stop_price updated: {dyn.get('stop_price')} (prev: {prev_stop})"
                            print(msg)
                            send_telegram_message(msg)

                        # check exit: compare current price to trailing stop
                        with trades_lock:
                            sp = dyn.get("stop_price")
                        if sp is None:
                            continue

                        if side == "BUY" and current_price <= sp:
                            trigger_msg = f"🎯 <b>{symbol}</b> Dynamic trailing stop hit (BUY). Stop: {sp} Current: {current_price}"
                            print(trigger_msg)
                            send_telegram_message(trigger_msg if not DEBUG else f"[DEBUG-TRIGGER] {trigger_msg}")
                            execute_market_exit(symbol, side)
                            continue

                        if side == "SELL" and current_price >= sp:
                            trigger_msg = f"🎯 <b>{symbol}</b> Dynamic trailing stop hit (SELL). Stop: {sp} Current: {current_price}"
                            print(trigger_msg)
                            send_telegram_message(trigger_msg if not DEBUG else f"[DEBUG-TRIGGER] {trigger_msg}")
                            execute_market_exit(symbol, side)
                            continue

                except Exception as inner_e:
                    print(f"⚠️ Error processing trailing for {symbol}: {inner_e}")

        except Exception as e:
            print(f"⚠️ Global trailing monitor error: {e}")

        time.sleep(max(1, TRAILING_UPDATE_INTERVAL))


# ---------------------------
# async exit + re-entry
# ---------------------------
def async_exit_and_open(symbol, new_side, entry_price):
    def worker():
        try:
            with trades_lock:
                existing = trades.get(symbol)
            if existing and not existing.get("closed", True):
                existing_side = existing.get("side")
                print(f"🔄 Existing active — forcing market close for {symbol} ({existing_side}) to replace with {new_side}")
                execute_market_exit(symbol, existing_side)
                time.sleep(OPPOSITE_CLOSE_DELAY)
            open_position(symbol, new_side, entry_price)
        except Exception as e:
            print("❌ async_exit_and_open error:", e)
    threading.Thread(target=worker, daemon=True).start()


# ---------------------------
# Webhook endpoint
# ---------------------------
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

        print(f"📩 Alert: {symbol} | {comment} | {close_price} | interval={interval}")

        # ENTRY: BUY
        if comment == "BUY_ENTRY":
            with trades_lock:
                existing = trades.get(symbol)
            if existing and not existing.get("closed", True):
                async_exit_and_open(symbol, "BUY", close_price)
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
                async_exit_and_open(symbol, "SELL", close_price)
            else:
                with trades_lock:
                    trades.setdefault(symbol, {})["interval"] = interval.lower()
                    trades.setdefault(symbol, {})["last_bar_high"] = float(bar_high) if bar_high else close_price
                    trades.setdefault(symbol, {})["last_bar_low"] = float(bar_low) if bar_low else close_price
                open_position(symbol, "SELL", close_price)

        # EXIT signals (monitor only)
        elif comment == "EXIT_LONG":
            with trades_lock:
                if symbol in trades and not trades[symbol].get("closed", True):
                    trades[symbol]["exit_signal_active"] = True
                    trades[symbol]["exit_price_signal"] = close_price
                    send_telegram_message(f"📡 EXIT_LONG received for {symbol} — monitoring for best close.")
                else:
                    print(f"⚠️ EXIT_LONG received but no active BUY trade for {symbol}")

        elif comment == "EXIT_SHORT":
            with trades_lock:
                if symbol in trades and not trades[symbol].get("closed", True):
                    trades[symbol]["exit_signal_active"] = True
                    trades[symbol]["exit_price_signal"] = close_price
                    send_telegram_message(f"📡 EXIT_SHORT received for {symbol} — monitoring for best close.")
                else:
                    print(f"⚠️ EXIT_SHORT received but no active SELL trade for {symbol}")

        # CROSS EXIT + reverse entry
        elif comment == "CROSS_EXIT_LONG":
            print(f"🔁 CROSS_EXIT_LONG → Close BUY, Open SELL for {symbol}")
            async_exit_and_open(symbol, "SELL", close_price)

        elif comment == "CROSS_EXIT_SHORT":
            print(f"🔁 CROSS_EXIT_SHORT → Close SELL, Open BUY for {symbol}")
            async_exit_and_open(symbol, "BUY", close_price)

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

# Start the global trailing monitor thread (single instance)
threading.Thread(target=global_trailing_monitor, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
