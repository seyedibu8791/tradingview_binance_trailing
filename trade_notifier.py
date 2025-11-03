# ============================================
# ✅ trade_notifier.py (FINAL - Fixed Dynamic Trailing Closure + compatibility))
# ============================================
import requests, threading, time, datetime, hmac, hashlib
from typing import Optional
from config import (
    BINANCE_API_KEY, BINANCE_SECRET_KEY, BASE_URL,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DEBUG, LEVERAGE, TRADE_AMOUNT,
    TRAILING_ACTIVATION_PCT, TRAILING_DISTANCE_PCT,
    TS_LOW_OFFSET_PCT, TS_HIGH_OFFSET_PCT,
    TSI_PRIMARY_TRIGGER_PCT, TSI_LOW_PROFIT_OFFSET_PCT, TSI_HIGH_PROFIT_OFFSET_PCT
)

trades = {}
notified_orders = set()
_symbol_tick_cache = {}

# ===============================
# 📢 TELEGRAM HELPER
# ===============================
def send_telegram_message(message: str):
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            if DEBUG: print("⚠️ Missing Telegram credentials.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200 and DEBUG:
            print("❌ Telegram Error:", r.status_code, r.text)
    except Exception as e:
        print("❌ Telegram Exception:", e)


# ===============================
# 🔑 BINANCE SIGNED HELPERS
# ===============================
def _signed_get(path, params=None):
    params = params.copy() if params else {}
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    sig = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{path}?{query}&signature={sig}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _signed_post(path, params):
    params = params.copy()
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    sig = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{path}?{query}&signature={sig}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    resp = requests.post(url, headers=headers, timeout=10)
    if DEBUG: print("🧾 POST:", path, resp.text)
    return resp.json()


# ===============================
# ⚙️ SYMBOL TICK SIZE
# ===============================
def get_symbol_tick_size(symbol: str) -> float:
    symbol = symbol.upper()
    if symbol in _symbol_tick_cache:
        return _symbol_tick_cache[symbol]
    try:
        info = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo", timeout=10).json()
        for s in info.get("symbols", []):
            if s.get("symbol") == symbol:
                for f in s.get("filters", []):
                    if f.get("filterType") == "PRICE_FILTER":
                        tick = float(f.get("tickSize"))
                        _symbol_tick_cache[symbol] = tick
                        return tick
    except Exception as e:
        if DEBUG: print(f"⚠️ Tick size fetch error for {symbol}: {e}")
    return 0.01


def round_to_tick(value: float, symbol: str) -> float:
    tick = get_symbol_tick_size(symbol)
    # guard against division by zero
    if tick <= 0:
        return round(value, 8)
    return round(round(value / tick) * tick, 8)


# ===============================
# 🔎 POSITION HELPERS
# ===============================
def get_position_info(symbol: str) -> Optional[dict]:
    try:
        data = _signed_get("/fapi/v2/positionRisk")
        for p in data:
            if p.get("symbol", "").upper() == symbol.upper() and abs(float(p.get("positionAmt", 0))) > 0:
                return p
        return None
    except Exception as e:
        if DEBUG: print("⚠️ get_position_info error:", e)
        return None


def get_unrealized_pnl_pct(symbol: str) -> Optional[float]:
    try:
        p = get_position_info(symbol)
        if not p: return None
        entry, mark = float(p.get("entryPrice", 0) or 0), float(p.get("markPrice", 0) or 0)
        amt = float(p.get("positionAmt", 0))
        if entry == 0 or amt == 0: return None
        pct = ((mark - entry) / entry) * 100 * LEVERAGE if amt > 0 else ((entry - mark) / entry) * 100 * LEVERAGE
        return round(pct, 2)
    except Exception as e:
        if DEBUG: print("⚠️ get_unrealized_pnl_pct error:", e)
        return None


# ===============================
# 🔒 CLOSE TRADE
# ===============================
def close_trade_on_binance(symbol, side):
    try:
        pos = get_position_info(symbol)
        if not pos:
            if DEBUG: print(f"⚠️ No open position for {symbol}.")
            return {"status": "no_position"}
        amt = abs(float(pos.get("positionAmt", 0)))
        close_side = "SELL" if side.upper() == "BUY" else "BUY"
        params = {"symbol": symbol, "side": close_side, "type": "MARKET", "quantity": round(amt, 8)}
        resp = _signed_post("/fapi/v1/order", params)
        if DEBUG: print("🧾 close_trade_on_binance:", resp)
        return resp
    except Exception as e:
        print("❌ close_trade_on_binance error:", e)
        return {"error": str(e)}


# ===============================
# 🟩 ENTRY / EXIT LOGS
# ===============================
def log_trade_entry(symbol, side, order_id, filled_price, interval):
    if symbol in trades and trades[symbol].get("closed"):
        trades.pop(symbol, None)
    if order_id in notified_orders: return
    notified_orders.add(order_id)
    trades[symbol] = {
        "side": side.upper(), "entry_price": filled_price, "closed": False,
        "interval": interval, "trail_active": False, "stop_price": None,
        "entry_time": time.time()
    }
    emoji = "🟩⬆️" if side.upper() == "BUY" else "🟥⬇️"
    send_telegram_message(
        f"{emoji} <b>{side.upper()} ENTRY</b>\n"
        f"┇#{symbol}\n┇Entry: {filled_price}\n┇Interval: {interval}\n"
        f"┇Leverage: {LEVERAGE}x | Amount: ${TRADE_AMOUNT}\n┇<i>Dynamic Trailing Active</i>"
    )


def log_trade_exit(symbol, exit_price, reason="EXIT"):
    if symbol not in trades: return
    t = trades[symbol]
    t.update({"closed": True, "exit_price": exit_price, "exit_reason": reason, "exit_time": time.time()})
    pnl = get_unrealized_pnl_pct(symbol)
    emoji = "✅" if reason != "STOP_LOSS" else "⚠️"
    send_telegram_message(
        f"{emoji} <b>{symbol}</b> EXIT\n"
        f"┇Reason: {reason}\n┇Exit Price: {exit_price}\n┇Entry: {t.get('entry_price')}\n"
        f"┇PnL%: {pnl if pnl is not None else 'N/A'}\n┇Duration: {round((time.time()-t.get('entry_time', time.time()))/60,1)} min"
    )


# ===============================
# 🎯 log_trailing_start (compatibility wrapper expected by app.py)
# ===============================
def log_trailing_start(symbol: str, pnl_percent: float = None):
    """
    Compatibility helper so app.py can import and call log_trailing_start(symbol, pnl_percent).
    Sets trail_active on the trade and records starting pct/time, and notifies via Telegram.
    """
    t = trades.get(symbol)
    if not t:
        # create minimal record if not present
        trades[symbol] = {
            "side": "UNKNOWN",
            "entry_price": 0.0,
            "closed": False,
            "interval": "1h",
            "trail_active": True,
            "trail_start_pct": pnl_percent,
            "trail_start_time": time.time(),
            "stop_price": None,
            "entry_time": time.time()
        }
        send_telegram_message(f"🎯 Trailing started for <b>{symbol}</b> @ {pnl_percent}%")
        return

    t["trail_active"] = True
    t["trail_start_pct"] = pnl_percent
    t["trail_start_time"] = time.time()
    # keep existing stop_price/dynamic_offset if present
    send_telegram_message(f"🎯 Trailing started for <b>{symbol}</b> @ {pnl_percent}%")


# ===============================
# 🎯 compute_ts_dynamic
# ===============================
def compute_ts_dynamic(symbol, entry_price, side, current_price):
    pnl_pct = get_unrealized_pnl_pct(symbol)
    if pnl_pct is None: return None, None
    profit = abs(pnl_pct)
    if profit < TSI_PRIMARY_TRIGGER_PCT: return None, None
    # Linear interpolation between offsets
    lower, upper = TSI_PRIMARY_TRIGGER_PCT, 10.0
    offset = (
        TSI_LOW_PROFIT_OFFSET_PCT if profit <= lower else
        TSI_HIGH_PROFIT_OFFSET_PCT if profit >= upper else
        TSI_LOW_PROFIT_OFFSET_PCT + ((profit - lower)/(upper-lower)) *
        (TSI_HIGH_PROFIT_OFFSET_PCT - TSI_LOW_PROFIT_OFFSET_PCT)
    )
    stop = (current_price * (1 - offset/100) if side.upper() == "BUY"
            else current_price * (1 + offset/100))
    stop = max(stop, entry_price) if side.upper() == "BUY" else min(stop, entry_price)
    return round_to_tick(stop, symbol), round(offset, 4)


# ===============================
# 🧠 TRAILING MONITOR
# ===============================
def check_trailing(symbol):
    """Continuously monitor a single symbol until closed."""
    while True:
        t = trades.get(symbol)
        if not t or t.get("closed"): break

        pos = get_position_info(symbol)
        if not pos:
            time.sleep(3)
            continue

        try:
            mark = float(pos.get("markPrice", 0))
        except Exception:
            time.sleep(3)
            continue

        pnl = get_unrealized_pnl_pct(symbol)
        if pnl is None:
            time.sleep(3)
            continue

        # Activate trailing once activation % reached
        if not t.get("trail_active", False) and pnl >= TRAILING_ACTIVATION_PCT:
            t["trail_active"] = True
            t["trail_start_pct"] = pnl
            t["trail_start_time"] = time.time()
            send_telegram_message(f"🎯 {symbol} Trailing Activated @ {pnl}%")

        if not t.get("trail_active", False):
            time.sleep(3)
            continue

        stop, offset = compute_ts_dynamic(symbol, t.get("entry_price", 0), t.get("side", "BUY"), mark)
        if stop is None:
            # still below TSI activation or compute failed
            time.sleep(3)
            continue

        prev_stop = t.get("stop_price")
        t["stop_price"] = stop
        t["dynamic_offset"] = offset

        if prev_stop != stop and DEBUG:
            print(f"🔄 {symbol} trail updated | Stop: {stop} | Offset: {offset}% | PnL: {pnl}%")

        tick = get_symbol_tick_size(symbol)
        hysteresis = max(2 * tick, tick)  # require at least 1-2 ticks movement to trigger

        # Trigger exit condition (symmetric)
        if t.get("side", "BUY").upper() == "BUY":
            # current price falls to or below stop => close
            if mark <= stop - hysteresis:
                send_telegram_message(f"🎯 {symbol} BUY Trailing Stop Hit\nStop: {stop}\nOffset: {offset}%")
                close_trade_on_binance(symbol, "BUY")
                log_trade_exit(symbol, stop, "TRAIL_STOP")
                break
        else:
            # SELL: current price rises to or above stop => close
            if mark >= stop + hysteresis:
                send_telegram_message(f"🎯 {symbol} SELL Trailing Stop Hit\nStop: {stop}\nOffset: {offset}%")
                close_trade_on_binance(symbol, "SELL")
                log_trade_exit(symbol, stop, "TRAIL_STOP")
                break

        time.sleep(3)


def start_trailing_monitor(symbol):
    """Start a per-symbol trailing monitor thread (keeps backward-compatible start message)."""
    # if a monitor already running for this symbol, we still start a thread; it's cheap and will exit immediately if closed
    threading.Thread(target=check_trailing, args=(symbol,), daemon=True).start()
    send_telegram_message(f"🛰️ Starting dynamic trailing monitor for {symbol}")


# ===============================
# 🕒 DAILY SUMMARY
# ===============================
def send_daily_summary():
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))
        nxt = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        time.sleep((nxt - now).total_seconds())
        closed = [t for t in trades.values() if t.get("closed")]
        send_telegram_message(f"📅 Daily Summary ({len(closed)} closed)")

threading.Thread(target=send_daily_summary, daemon=True).start()
