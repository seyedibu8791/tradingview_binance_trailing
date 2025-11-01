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
    TRAILING_UPDATE_INTERVAL, STOP_LOSS_PCT
)

from trade_notifier import (
    log_trade_entry,
    log_trade_exit,
    check_loss_conditions,
    send_telegram_message,
    get_unrealized_pnl_pct
)


# =========================
# Flask Initialization
# =========================
app = Flask(__name__)

# =========================
# GLOBAL STATE MANAGEMENT
# =========================
# Dictionary to store live trade info
trades = {}

# Thread lock for safe multi-threaded updates
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
                # remove pre-existing complex trail fields, replace with dynamic-trail fields
                "dynamic_trail": {
                    "active": False,
                    "activation_pct": TRAILING_ACTIVATION_PCT,
                    "peak": limit_price,
                    "trough": limit_price,
                    "stop_price": None
                },
                "trailing_monitor_started": False,
                # LOSS CONTROL fields
                "loss_bars": 0,
                "forced_exit": False,
                # store entry bookkeeping for 2-bar rule
                "entry_time": time.time(),
                "interval": "1h"  # will be updated from webhook if passed
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
                trades[symbol]["dynamic_trail"]["peak"] = avg_price
                trades[symbol]["dynamic_trail"]["trough"] = avg_price

            # notify via trade_notifier
            try:
                log_trade_entry(symbol, side, order_id, avg_price)
            except Exception:
                send_telegram_message(f"📩 Filled: {symbol} | {side} | {avg_price}")

            # ensure trailing monitor is running (single monitor safeguard)
            with trades_lock:
                if not trades[symbol].get("trailing_monitor_started"):
                    trades[symbol]["trailing_monitor_started"] = True
                    # announce dynamic trailing start
                    try:
                        log_trailing_start(symbol, "dynamic")
                    except Exception:
                        send_telegram_message(f"🛰️ Starting dynamic trailing monitor for {symbol}")
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

# ==============================
# Symbol tick & trailing helpers
# ==============================
def get_symbol_tick_size(symbol):
    """
    Fetch symbol tick size (minimum price step) from Binance exchange info.
    Returns float tick size (e.g. 0.01).
    """
    try:
        info = get_symbol_info(symbol)
        if info:
            for f in info.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    return float(f.get("tickSize", 0.01))
    except Exception as e:
        print(f"❌ get_symbol_tick_size error for {symbol}: {e}")
    return 0.01

def calculate_trailing_offsets(symbol, entry_price, tsi_pct, ts_pct, bar_high, bar_low):
    """
    Reproduce Pine calculations:
      pips_correction = 1 / syminfo.mintick
      trail_long = abs(entry * (1 + tsi/100) - entry) * pips_correction
      offset_long = high * (ts/100) * pips_correction
    Returns values in price-units (not ticks) and ticks as needed.
    """
    tick = get_symbol_tick_size(symbol)
    pips_correction = 1.0 / tick if tick > 0 else 1.0

    # price diffs
    trail_long_ticks = abs(entry_price * (1 + tsi_pct / 100.0) - entry_price) * pips_correction
    trail_short_ticks = abs(entry_price * (1 - tsi_pct / 100.0) - entry_price) * pips_correction

    offset_long_ticks = (bar_high * (ts_pct / 100.0)) * pips_correction
    offset_short_ticks = (bar_low * (ts_pct / 100.0)) * pips_correction

    # convert ticks back to price-units for stop calculations
    trail_long_price = trail_long_ticks * tick
    trail_short_price = trail_short_ticks * tick
    offset_long_price = offset_long_ticks * tick
    offset_short_price = offset_short_ticks * tick

    return {
        "tick_size": tick,
        "pips_correction": pips_correction,
        "trail_long_ticks": trail_long_ticks,
        "trail_short_ticks": trail_short_ticks,
        "offset_long_ticks": offset_long_ticks,
        "offset_short_ticks": offset_short_ticks,
        "trail_long_price": trail_long_price,
        "trail_short_price": trail_short_price,
        "offset_long_price": offset_long_price,
        "offset_short_price": offset_short_price
    }

# --------- compute_ts_dynamic kept (linear interpolation) ----------
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

# --------- Dynamic trailing monitor (replaces unified trailing) ----------
def monitor_trailing_and_exit(symbol, side):
    """
    Dynamic trailing monitor modeled after the Pine Script logic you supplied.
    - Uses compute_ts_dynamic(profit_pct) (linear interpolation between TS_LOW_OFFSET_PCT and TS_HIGH_OFFSET_PCT)
    - Uses pips/tick calculation via get_symbol_tick_size() and calculate_trailing_offsets()
    - Activates trailing once profit >= TRAILING_ACTIVATION_PCT
    - Uses peak (for longs) / trough (for shorts) to compute stop
    - Still honors immediate STOP_LOSS and 2-bar force-close via check_loss_conditions()
    """
    print(f"🛰️ Dynamic trailing monitor started for {symbol} (side: {side})")
    start_time = time.time()
    max_run = max(60, int(os.getenv("MAX_TRAILING_RUNTIME_MIN", "120"))) * 60  # safety cap

    # notify start
    try:
        log_trailing_start(symbol, "dynamic")
    except Exception:
        send_telegram_message(f"🛰️ Starting dynamic trailing monitor for {symbol}")

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
            dyn = trade.setdefault("dynamic_trail", {})
            # loss metadata
            loss_bars = trade.get("loss_bars", 0)
            interval = trade.get("interval", "1h")

        current = get_current_price(symbol)
        if current <= 0:
            time.sleep(1)
            continue

        # prefer live unrealized pnl percent from notifier helper
        try:
            pnl_percent = get_unrealized_pnl_pct(symbol) or 0.0
        except Exception:
            # fallback local calc
            if side.upper() == "BUY":
                pnl_percent = ((current - entry_price) / entry_price) * 100 * LEVERAGE if entry_price > 0 else 0.0
            else:
                pnl_percent = ((entry_price - current) / entry_price) * 100 * LEVERAGE if entry_price > 0 else 0.0

        # ---------- Immediate STOP LOSS check ----------
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

        # ---------- 2-bar/force-close check via trade_notifier helper ----------
        # check_loss_conditions will handle 2-bar forced close when called (it expects trade entry_time & interval to be set in trade_notifier)
        # We call it as a safety; it may close the trade and log the exit.
        try:
            res = check_loss_conditions(symbol, current_price=current)
            if res == "FORCE_CLOSE" or res == "STOP_LOSS":
                # check_loss_conditions already executed close; monitor should stop
                return
        except Exception as e:
            if DEBUG:
                print("⚠️ check_loss_conditions error:", e)

        # ---------- Dynamic trailing computations ----------
        # Update peaks/troughs
        if side.upper() == "BUY":
            if current > dyn.get("peak", entry_price):
                dyn["peak"] = current
        else:  # SELL
            if current < dyn.get("trough", entry_price):
                dyn["trough"] = current

        # Activation: start trailing when profit reaches activation pct
        activated = abs(pnl_percent) >= dyn.get("activation_pct", TRAILING_ACTIVATION_PCT)

        if activated:
            # compute distance % from profit using compute_ts_dynamic (mimic pine)
            profit_for_ts = abs(((dyn.get("peak", entry_price) - entry_price) / entry_price * 100) if side.upper() == "BUY"
                                else ((entry_price - dyn.get("trough", entry_price)) / entry_price * 100))
            distance_pct = compute_ts_dynamic(abs(profit_for_ts))

            # Use bar high/low for offset calculation if available via webhook; fallback to current price
            # We try to use available last-bar highs/lows from trade data (if you pass them via webhook/entry)
            bar_high = trade.get("last_bar_high", current)
            bar_low = trade.get("last_bar_low", current)

            # Calculate pips/tick based offsets
            offsets = calculate_trailing_offsets(symbol, entry_price, TRAILING_ACTIVATION_PCT, distance_pct, bar_high, bar_low)
            # offsets contain both price and tick representations
            # We'll compute stop_price in price units:
            if side.upper() == "BUY":
                # prefer stop based on peak minus dynamic distance
                stop_by_distance = dyn.get("peak", entry_price) - offsets["offset_long_price"]
                # also compute stop based on distance_pct% of peak (backup)
                stop_by_pct = dyn.get("peak", entry_price) * (1 - distance_pct / 100.0)
                stop_price = max(stop_by_distance, stop_by_pct)  # choose tighter stop (higher price) to protect profit
            else:
                stop_by_distance = dyn.get("trough", entry_price) + offsets["offset_short_price"]
                stop_by_pct = dyn.get("trough", entry_price) * (1 + distance_pct / 100.0)
                stop_price = min(stop_by_distance, stop_by_pct)  # choose tighter stop (lower price for shorts)

            # store stop
            dyn["stop_price"] = round(stop_price, 8)

            # compute locked pnl for messaging
            usd_locked, pct_locked = compute_locked_pnl(entry_price, dyn["stop_price"], side)
            dyn["locked_pnl_usd"], dyn["locked_pnl_pct"] = usd_locked, pct_locked

            # If stop hit -> close
            if side.upper() == "BUY" and current <= dyn["stop_price"]:
                send_telegram_message(f"🎯 <b>{symbol}</b> Dynamic trailing stop hit (BUY). Stop: {dyn['stop_price']} Current: {current}")
                execute_market_exit(symbol, side)
                return
            if side.upper() == "SELL" and current >= dyn["stop_price"]:
                send_telegram_message(f"🎯 <b>{symbol}</b> Dynamic trailing stop hit (SELL). Stop: {dyn['stop_price']} Current: {current}")
                execute_market_exit(symbol, side)
                return

            # Optional: debug/send periodic trail update
            # send_telegram_message(f"🛰️ {symbol} Trail update | PnL%: {round(pnl_percent,2)} | Stop: {dyn['stop_price']} | Locked: {dyn['locked_pnl_usd']}$")
        # end activated block

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
                async_exit_and_open(symbol, "BUY", close_price)
            else:
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

        # =============== EXIT SIGNALS ===============
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
