# ============================================
# ✅ trade_notifier.py (FINAL - Production Ready)
# ============================================
import requests
import threading
import time
import datetime
import hmac
import hashlib
from typing import Optional

# ===============================
# 🔧 IMPORTS FROM CONFIG
# ===============================
from config import (
    BINANCE_API_KEY,
    BINANCE_SECRET_KEY,
    BASE_URL,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    DEBUG,
    LEVERAGE,
    TRADE_AMOUNT,
    TRAILING_ACTIVATION_PCT,
    TRAILING_DISTANCE_PCT,
    TS_LOW_OFFSET_PCT,
    TS_HIGH_OFFSET_PCT,
    TSI_PRIMARY_TRIGGER_PCT,
    TSI_LOW_PROFIT_OFFSET_PCT,
    TSI_HIGH_PROFIT_OFFSET_PCT
)

# =======================
# 📦 STORAGE
# =======================
trades = {}
notified_orders = set()
_symbol_tick_cache = {}  # cache tick size per symbol


# =======================
# 📢 TELEGRAM HELPER
# =======================
def send_telegram_message(message: str):
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            if DEBUG:
                print("⚠️ Missing Telegram credentials.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200 and DEBUG:
            print("❌ Telegram Error:", r.status_code, r.text)
    except Exception as e:
        print("❌ Telegram Exception:", e)


# =======================
# 🔑 BINANCE SIGNED HELPERS
# =======================
def _signed_get(path: str, params: dict = None):
    params = params.copy() if params else {}
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{path}?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _signed_post(path: str, params: dict):
    params = params.copy()
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{path}?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    resp = requests.post(url, headers=headers, timeout=10)
    if DEBUG:
        print("🧾 POST:", path, resp.text)
    return resp.json()


# =======================
# ⚙️ SYMBOL TICK SIZE
# =======================
def get_symbol_tick_size(symbol: str) -> float:
    """Fetch and cache Binance symbol tick size to apply hysteresis."""
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
        if DEBUG:
            print(f"⚠️ Tick size fetch error for {symbol}: {e}")
    return 0.01


def round_to_tick(value: float, symbol: str) -> float:
    """Round value to nearest valid tick step."""
    tick = get_symbol_tick_size(symbol)
    if tick <= 0:
        return round(value, 8)
    return round(round(value / tick) * tick, 8)


# =======================
# 🔎 POSITION HELPERS
# =======================
def get_position_info(symbol: str) -> Optional[dict]:
    try:
        data = _signed_get("/fapi/v2/positionRisk")
        for p in data:
            if p.get("symbol", "").upper() == symbol.upper() and abs(float(p.get("positionAmt", 0))) > 0:
                return p
        return None
    except Exception as e:
        if DEBUG:
            print("⚠️ get_position_info error:", e)
        return None


def get_unrealized_pnl_pct(symbol: str) -> Optional[float]:
    try:
        p = get_position_info(symbol)
        if not p:
            return None
        entry_price = float(p.get("entryPrice", 0) or 0)
        mark_price = float(p.get("markPrice", 0) or 0)
        pos_amt = float(p.get("positionAmt", 0))
        if entry_price == 0 or abs(pos_amt) == 0 or mark_price == 0:
            return None
        if pos_amt > 0:
            pct = ((mark_price - entry_price) / entry_price) * 100 * LEVERAGE
        else:
            pct = ((entry_price - mark_price) / entry_price) * 100 * LEVERAGE
        return round(pct, 2)
    except Exception as e:
        if DEBUG:
            print("⚠️ get_unrealized_pnl_pct error:", e)
        return None


# =======================
# 🔒 CLOSE TRADE
# =======================
def close_trade_on_binance(symbol: str, side: str):
    try:
        pos = get_position_info(symbol)
        if not pos:
            if DEBUG:
                print(f"⚠️ No open position for {symbol}.")
            return {"status": "no_position"}
        amt = abs(float(pos.get("positionAmt", 0)))
        close_side = "SELL" if side.upper() == "BUY" else "BUY"
        params = {"symbol": symbol, "side": close_side, "type": "MARKET", "quantity": round(amt, 8)}
        resp = _signed_post("/fapi/v1/order", params)
        if DEBUG:
            print("🧾 close_trade_on_binance:", resp)
        return resp
    except Exception as e:
        print("❌ close_trade_on_binance error:", e)
        return {"error": str(e)}


# =======================
# 🟩 ENTRY LOGIC
# =======================
def log_trade_entry(symbol, side, order_id, filled_price, interval):
    if symbol in trades and trades[symbol].get("closed"):
        trades.pop(symbol, None)
    if order_id in notified_orders:
        return
    notified_orders.add(order_id)

    trades[symbol] = {
        "side": side.upper(),
        "entry_price": filled_price,
        "order_id": order_id,
        "closed": False,
        "interval": interval.lower(),
        "trail_active": False,
        "dynamic_offset": 0.0,
        "stop_price": None,
        "entry_time": time.time()
    }

    emoji = "🟩⬆️" if side.upper() == "BUY" else "🟥⬇️"
    send_telegram_message(
        f"{emoji} <b>{side.upper()} ENTRY</b>\n"
        f"┇#{symbol}\n"
        f"┇Entry: {filled_price}\n"
        f"┇Interval: {interval}\n"
        f"┇Leverage: {LEVERAGE}x | Amount: ${TRADE_AMOUNT}\n"
        f"┇<i>Dynamic Trailing Active</i>"
    )

# =======================
# 🟥 EXIT LOGIC
# =======================
def log_trade_exit(symbol, exit_price, reason="EXIT"):
    """Logs and notifies when a trade is closed."""
    if symbol not in trades:
        return

    t = trades[symbol]
    t["closed"] = True
    t["exit_price"] = exit_price
    t["exit_reason"] = reason
    t["exit_time"] = time.time()

    emoji = "✅" if reason != "STOP_LOSS" else "⚠️"
    pnl = get_unrealized_pnl_pct(symbol)
    send_telegram_message(
        f"{emoji} <b>{symbol}</b> EXIT\n"
        f"┇Reason: {reason}\n"
        f"┇Exit Price: {exit_price}\n"
        f"┇Entry: {t.get('entry_price')}\n"
        f"┇PnL%: {pnl if pnl is not None else 'N/A'}\n"
        f"┇Duration: {round((time.time() - t.get('entry_time', time.time())) / 60, 1)} min"
    )


# =======================
# 🎯 compute_ts_dynamic
# =======================
def compute_ts_dynamic(symbol, entry_price, side, current_price):
    try:
        pnl_pct = get_unrealized_pnl_pct(symbol)
        if pnl_pct is None:
            return None, None
        profit_abs = abs(pnl_pct)

        if profit_abs < TSI_PRIMARY_TRIGGER_PCT:
            return None, None

        lower_bound = TSI_PRIMARY_TRIGGER_PCT
        upper_bound = 10.0
        if profit_abs <= lower_bound:
            offset_pct = TSI_LOW_PROFIT_OFFSET_PCT
        elif profit_abs >= upper_bound:
            offset_pct = TSI_HIGH_PROFIT_OFFSET_PCT
        else:
            span = TSI_HIGH_PROFIT_OFFSET_PCT - TSI_LOW_PROFIT_OFFSET_PCT
            scale = (profit_abs - lower_bound) / (upper_bound - lower_bound)
            offset_pct = TSI_LOW_PROFIT_OFFSET_PCT + span * scale

        # Symmetric stop calc
        if side.upper() == "BUY":
            stop_price = current_price * (1 - offset_pct / 100.0)
            stop_price = max(entry_price, stop_price)
        else:
            stop_price = current_price * (1 + offset_pct / 100.0)
            stop_price = min(entry_price, stop_price)

        # 🧭 Apply tick-size hysteresis rounding
        stop_price = round_to_tick(stop_price, symbol)
        offset_pct = round(offset_pct, 6)

        return stop_price, offset_pct
    except Exception as e:
        if DEBUG:
            print("⚠️ compute_ts_dynamic error:", e)
        return None, None


# =======================
# 📉 LOSS & TRAIL MONITOR
# =======================
def check_loss_conditions(symbol, current_price=None):
    if symbol not in trades or trades[symbol].get("closed"):
        return
    t = trades[symbol]
    pnl = get_unrealized_pnl_pct(symbol)
    if pnl is None:
        return

    # 2-bar forced exit
    dur = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    if time.time() - t["entry_time"] >= 2 * dur.get(t["interval"], 3600) and pnl < 0 and not t.get("forced_exit"):
        t["forced_exit"] = True
        send_telegram_message(f"⚠️ <b>{symbol}</b> 2-bar negative → Force closing")
        close_trade_on_binance(symbol, t["side"])
        return

    # Activate trailing
    if not t.get("trail_active") and pnl >= TRAILING_ACTIVATION_PCT:
        t["trail_active"] = True
        send_telegram_message(f"🎯 {symbol} Trailing Activated @ {pnl}%")

    if not t.get("trail_active"):
        return

    if current_price is None:
        pos = get_position_info(symbol)
        current_price = float(pos.get("markPrice")) if pos and pos.get("markPrice") else None
        if not current_price:
            return

    stop_price, offset_pct = compute_ts_dynamic(symbol, t["entry_price"], t["side"], current_price)
    if stop_price is None:
        return

    t["stop_price"] = stop_price
    t["dynamic_offset"] = offset_pct

    # Hit logic symmetric
    if t["side"] == "BUY" and current_price <= stop_price:
        send_telegram_message(f"🎯 {symbol} BUY Trailing Stop Hit\nStop: {stop_price}\nOffset: {offset_pct}%")
        close_trade_on_binance(symbol, t["side"])
        t["trail_active"] = False
    elif t["side"] == "SELL" and current_price >= stop_price:
        send_telegram_message(f"🎯 {symbol} SELL Trailing Stop Hit\nStop: {stop_price}\nOffset: {offset_pct}%")
        close_trade_on_binance(symbol, t["side"])
        t["trail_active"] = False


# =======================
# 📊 DAILY SUMMARY
# =======================
def send_daily_summary():
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))
        nxt = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        time.sleep((nxt - now).total_seconds())

        closed = [t for t in trades.values() if t.get("closed")]
        msg = f"📅 Daily Summary ({len(closed)} closed)"
        send_telegram_message(msg)


threading.Thread(target=send_daily_summary, daemon=True).start()
