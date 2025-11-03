# app.py ((UPDATED - integrated with trade_notifier))
from flask import Flask, request, jsonify
import requests, hmac, hashlib, time, threading, os
from threading import Lock

from config import (
    BINANCE_API_KEY, BINANCE_SECRET_KEY, BASE_URL,
    TRADE_AMOUNT, LEVERAGE, MARGIN_TYPE,
    MAX_ACTIVE_TRADES, EXIT_MARKET_DELAY, OPPOSITE_CLOSE_DELAY,
    TRAILING_ACTIVATION_PCT, TRAILING_DISTANCE_PCT,
    TS_LOW_OFFSET_PCT, TS_HIGH_OFFSET_PCT,
    TSI_PRIMARY_TRIGGER_PCT, TSI_LOW_PROFIT_OFFSET_PCT, TSI_HIGH_PROFIT_OFFSET_PCT,
    TRAILING_UPDATE_INTERVAL, STOP_LOSS_PCT,    # added STOP_LOSS_PCT and TRAILING_UPDATE_INTERVAL
    # optional toggle you asked about:
    # set USE_TRAILING_STOP=True/False in env if you want to switch off trailing
    # NOTE: if USE_TRAILING_STOP not present in config, you can add it there.
    # We'll try to import; if absent, default to True.
)

# Import notifier & shared trades from trade_notifier so both modules operate on same state
from trade_notifier import (
    log_trade_entry,
    log_trade_exit,
    send_telegram_message,
    get_unrealized_pnl_pct,
    log_trailing_start,
    trades as notifier_trades
)

# If config didn't provide USE_TRAILING_STOP, default True
try:
    from config import USE_TRAILING_STOP
except Exception:
    USE_TRAILING_STOP = True

# =========================
# Flask Initialization
# =========================
app = Flask(__name__)

# =========================
# GLOBAL STATE MANAGEMENT
# =========================
# Use trade_notifier's trades dict so all Telegrams are formatted consistently
trades = notifier_trades

# Thread lock for safe multi-threaded updates
trades_lock = Lock()

# =======================
# Tick-size cache & hysteresis config
# =======================
_symbol_tick_cache = {}
# Multiplier (number of ticks) used as hysteresis before accepting an updated stop.
# Follow TradingView style: keep it minimal; make configurable via env if you want.
TRAILING_HYSTERESIS_TICKS = int(os.getenv("TRAILING_HYSTERESIS_TICKS", "1"))

def get_symbol_tick_size(symbol):
    """Fetch and cache the tick size for symbol precision."""
    symbol = symbol.upper()
    if symbol in _symbol_tick_cache:
        return _symbol_tick_cache[symbol]
    try:
        url = f"{BASE_URL}/fapi/v1/exchangeInfo"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        for s in data["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "PRICE_FILTER":
                        tick = float(f["tickSize"])
                        _symbol_tick_cache[symbol] = tick
                        return tick
    except Exception as e:
        print(f"⚠️ Tick size fetch error for {symbol}: {e}")
    # fallback default tick
    return 0.0001

def round_to_tick(value, symbol):
    """Round value to nearest valid tick step for the symbol."""
    tick = get_symbol_tick_size(symbol)
    if tick <= 0:
        return round(value, 8)
    return round(round(value / tick) * tick, 8)

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
    # floor to step size
    try:
        qty = (qty // step_size) * step_size
    except Exception:
        # fallback
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
    """
    Place a LIMIT order (GTC). Start thread to wait for fill -> notify via trade_notifier.log_trade_entry.
    The function now writes into the shared `trades` dict used by the notifier so messages remain formatted.
    """
    active_count = count_active_trades()
    if active_count >= MAX_ACTIVE_TRADES:
        print(f"🚫 Max active trades reached ({active_count}/{MAX_ACTIVE_TRADES})")
        return {"status": "max_trades_reached"}

    set_leverage_and_margin(symbol)
    qty = calculate_quantity(symbol)

    # store initial state in shared trades dict
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
                "dynamic_trail": {
                    "active": False,
                    "activation_pct": TRAILING_ACTIVATION_PCT,
                    "peak": limit_price,
                    "trough": limit_price,
                    "stop_price": None,
                    "notified": False   # to ensure trailing start logged once
                },
                "trailing_monitor_started": False,
                "loss_bars": 0,
                "forced_exit": False,
                "entry_time": time.time(),
                "interval": "1h",  # will be overwritten if webhook provides
                # available to accept bar_high/bar_low from webhook
                "last_bar_high": limit_price,
                "last_bar_low": limit_price,
                "quantity": qty  # store quantity for locked pnl computations
            }

    # send a minimal alert (this is allowed/expected) — filled / exit messages will be sent by notifier in formatted style
    send_telegram_message(f"📩 Alert: {symbol} | {side}_ENTRY | {limit_price}")

    # place order on Binance
    resp = binance_signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": str(limit_price)
    })

    # if order was created, wait for fill in background thread
    if "orderId" in resp:
        order_id = resp["orderId"]
        threading.Thread(target=wait_and_notify_filled_entry, args=(symbol, side, order_id), daemon=True).start()
    else:
        # if API returned an error, send message via notifier so formatting is consistent
        send_telegram_message(f"❌ Order create failed for {symbol}: {resp}")

    return resp

# --------- Wait for entry fill and start trailing ----------
def wait_and_notify_filled_entry(symbol, side, order_id):
    notified = False
    while True:
        order_status = binance_signed_request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        status = order_status.get("status")
        executed_qty = float(order_status.get("executedQty", 0)) if order_status.get("executedQty") else 0
        avg_price = 0.0
        # compute filled avg price from fills if available
        if "fills" in order_status and order_status["fills"]:
            num = 0.0; den = 0.0
            for f in order_status["fills"]:
                num += float(f.get("price", 0)) * float(f.get("qty", 0))
                den += float(f.get("qty", 0))
            if den > 0:
                avg_price = num/den
        avg_price = avg_price or float(order_status.get("avgPrice") or order_status.get("price") or 0)

        if not notified and status in ("PARTIALLY_FILLED", "FILLED") and executed_qty > 0:
            # update shared trade state
            with trades_lock:
                if symbol not in trades:
                    trades[symbol] = {}
                trades[symbol]["entry_price"] = avg_price
                trades[symbol]["order_id"] = order_id
                dyn = trades[symbol].setdefault("dynamic_trail", {})
                dyn["peak"] = avg_price
                dyn["trough"] = avg_price

            # notify via trade_notifier (this will send the nicely formatted entry message)
            try:
                log_trade_entry(symbol, side, order_id, avg_price, trades[symbol].get("interval", "1h"))
            except Exception as e:
                # fallback minimal message but still via notifier send function so formatting consistent
                send_telegram_message(f"📩 Filled: {symbol} | {side} | {avg_price}")

            # ensure trailing monitor is running (single monitor safeguard)
            with trades_lock:
                if not trades[symbol].get("trailing_monitor_started"):
                    trades[symbol]["trailing_monitor_started"] = True
                    # don't call log_trailing_start here with a string; the monitor will call it on activation with real pnl
                    send_telegram_message(f"🛰️ Starting dynamic trailing monitor for {symbol}")
                    threading.Thread(target=monitor_trailing_and_exit, args=(symbol, side), daemon=True).start()

            notified = True

        if status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            break
        time.sleep(1)

# --------- Market exit ----------
def execute_market_exit(symbol, side):
    """
    Execute a market close for the position on Binance and let wait_and_notify_filled_exit handle final notifications.
    Uses the provided side (the original side) to determine closing side.
    """
    pos_data = binance_signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    if not pos_data or len(pos_data) == 0 or abs(float(pos_data[0].get("positionAmt", 0))) == 0:
        # Send via notifier to keep formatting consistent
        send_telegram_message(f"⚠️ No active position for {symbol} to close.")
        return {"status": "no_position"}

    qty = abs(float(pos_data[0]["positionAmt"]))
    qty = round_quantity(symbol, qty)
    close_side = "SELL" if side == "BUY" else "BUY"

    # optional delay before exit
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
            filled_price = float(order_status.get("avgPrice") or order_status.get("price") or 0)
            # mark closed in shared dict and call notifier
            with trades_lock:
                if symbol in trades:
                    trades[symbol]["exit_price"] = filled_price
                    trades[symbol]["closed"] = True
                    trades[symbol]["trailing_monitor_started"] = False
            try:
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

# ==============================
# 🔧 Symbol tick & trailing helpers
# ==============================

def compute_ts_dynamic(entry_price, side, current_price,
                       activation_pct=TRAILING_ACTIVATION_PCT,
                       trail_pct=TRAILING_DISTANCE_PCT):
    """
    Compute dynamic trailing stop price (adaptive to current price).
    - activation_pct: threshold % at which trailing activates.
    - trail_pct: % distance used once trailing is active.
    Returns a stop price if active, else None.
    """
    try:
        if side.upper() == "BUY":
            activation_price = entry_price * (1 + activation_pct / 100.0)
            if current_price >= activation_price:
                stop_price = current_price * (1 - trail_pct / 100.0)
                return round(stop_price, 8)
        elif side.upper() == "SELL":
            activation_price = entry_price * (1 - activation_pct / 100.0)
            if current_price <= activation_price:
                stop_price = current_price * (1 + trail_pct / 100.0)
                return round(stop_price, 8)
        return None
    except Exception as e:
        print(f"⚠️ compute_ts_dynamic error: {e}")
        return None


def calculate_trailing_offsets(entry_price, side,
                               distance_pct=TRAILING_DISTANCE_PCT,
                               low_offset_pct=TS_LOW_OFFSET_PCT,
                               high_offset_pct=TS_HIGH_OFFSET_PCT):
    """
    Compute high/low offsets for trailing stop calculations.
    Returns a dictionary for easier usage downstream.
    """
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
    """
    Compute locked (unrealized) PnL in USDT and % for message display.
    """
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
        
# ==============================

# --------- Dynamic trailing monitor ----------
def monitor_trailing_and_exit(symbol, side):
    """
    Monitor that calculates dynamic trailing stop and executes market exit when hit.
    Calls log_trailing_start once (with real pnl%) and uses execute_market_exit() for closings.
    """
    print(f"🛰️ Dynamic trailing monitor started for {symbol} (side: {side})")
    start_time = time.time()
    max_run = max(60, int(os.getenv("MAX_TRAILING_RUNTIME_MIN", "120"))) * 60  # safety cap

    while True:
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
            dyn = trade.setdefault("dynamic_trail", {})
            interval = trade.get("interval", "1h")

        current_price = get_current_price(symbol)
        if current_price <= 0:
            time.sleep(1)
            continue

        # Try to get accurate pnl from notifier helper
        try:
            pnl_percent = get_unrealized_pnl_pct(symbol) or 0.0
        except Exception:
            if side.upper() == "BUY":
                pnl_percent = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE if entry_price > 0 else 0.0
            else:
                pnl_percent = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE if entry_price > 0 else 0.0

        # Immediate stoploss
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
                return
        except Exception as e:
            print("❌ Immediate stoploss check error:", e)

        # Optional external loss checks
        try:
            check_loss_conditions(symbol, current_price=current_price)
        except Exception as e:
            print("⚠️ check_loss_conditions error:", e)

        # Update peak/trough
        if side.upper() == "BUY":
            if current_price > dyn.get("peak", entry_price):
                dyn["peak"] = current_price
        else:
            if current_price < dyn.get("trough", entry_price):
                dyn["trough"] = current_price

        # Trailing activation
        activated = abs(pnl_percent) >= dyn.get("activation_pct", TRAILING_ACTIVATION_PCT)

        if activated:
            # Log trailing start once
            if not dyn.get("notified", False):
                try:
                    log_trailing_start(symbol, round(pnl_percent, 2))
                except Exception:
                    send_telegram_message(f"🎯 <b>{symbol}</b> Trailing Started @ {round(pnl_percent,2)}%")
                dyn["notified"] = True

            # Compute dynamic trailing stop (fixed)
            raw_stop = compute_ts_dynamic(entry_price, side, current_price)

            if raw_stop is None:
                time.sleep(1)
                continue

            # raw_stop is numeric stop price; apply tick rounding & hysteresis (minimal movement threshold)
            tick = get_symbol_tick_size(symbol)
            try:
                new_stop = round_to_tick(raw_stop, symbol)
            except Exception:
                new_stop = round(raw_stop, 8)

            prev_stop = dyn.get("stop_price")
            hysteresis = max(tick * TRAILING_HYSTERESIS_TICKS, tick)

            # If previous stop exists and change is less than hysteresis, skip update to avoid micro churn
            if prev_stop is not None and abs(new_stop - prev_stop) < hysteresis:
                # don't update; wait for a meaningful move
                time.sleep(max(1, TRAILING_UPDATE_INTERVAL))
                continue

            dyn["stop_price"] = new_stop
            dyn["locked_pnl_usd"] = compute_locked_pnl(entry_price, dyn["stop_price"], side, trade.get("quantity", 0))

            # If stop hit
            if side.upper() == "BUY" and current_price <= dyn["stop_price"]:
                send_telegram_message(f"🎯 <b>{symbol}</b> Dynamic trailing stop hit (BUY). Stop: {dyn['stop_price']} Current: {current_price}")
                execute_market_exit(symbol, side)
                return

            if side.upper() == "SELL" and current_price >= dyn["stop_price"]:
                send_telegram_message(f"🎯 <b>{symbol}</b> Dynamic trailing stop hit (SELL). Stop: {dyn['stop_price']} Current: {current_price}")
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
                print(f"🔄 Existing active — forcing market close for {symbol} ({existing_side}) to replace with {new_side}")
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

        print(f"📩 Alert: {symbol} | {comment} | {close_price} | interval={interval}")

        # =============== ENTRY SIGNALS ===============
        if comment == "BUY_ENTRY":
            with trades_lock:
                existing = trades.get(symbol)
            if existing and not existing.get("closed", True):
                # Force close existing then open BUY
                async_exit_and_open(symbol, "BUY", close_price)
            else:
                # store last-bar context if present
                with trades_lock:
                    trades.setdefault(symbol, {})["interval"] = interval.lower()
                    trades.setdefault(symbol, {})["last_bar_high"] = float(bar_high) if bar_high else close_price
                    trades.setdefault(symbol, {})["last_bar_low"] = float(bar_low) if bar_low else close_price
                open_position(symbol, "BUY", close_price)

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

        # =============== EXIT SIGNALS (monitor only) ===============
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

        # =============== CROSS EXIT + REVERSE ENTRY ===============
        elif comment == "CROSS_EXIT_LONG":
            # Close BUY, open SELL
            print(f"🔁 CROSS_EXIT_LONG → Close BUY, Open SELL for {symbol}")
            async_exit_and_open(symbol, "SELL", close_price)

        elif comment == "CROSS_EXIT_SHORT":
            # Close SELL, open BUY
            print(f"🔁 CROSS_EXIT_SHORT → Close SELL, Open BUY for {symbol}")
            async_exit_and_open(symbol, "BUY", close_price)

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
            requests.get(os.getenv("SELF_PING_URL", "https://tradingview-binance-trailing-dhhf.onrender.com/ping"), timeout=5)
        except:
            pass
        time.sleep(5 * 60)

threading.Thread(target=self_ping, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
