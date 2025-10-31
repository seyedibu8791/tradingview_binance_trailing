import requests
import threading
import time
import datetime
import hmac
import hashlib
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TRADE_AMOUNT, LEVERAGE, STOP_LOSS_PCT, LOSS_BARS_LIMIT,
    BASE_URL, BINANCE_API_KEY, BINANCE_SECRET_KEY, DEBUG, get_unrealized_pnl_pct
)

# =======================
# 📦 STORAGE
# =======================
trades = {}
notified_orders = set()

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
        if r.status_code != 200:
            print("❌ Telegram Error:", r.status_code, r.text)
    except Exception as e:
        print("❌ Telegram Exception:", e)

# =======================
# 🔑 BINANCE SIGNED HELPERS
# =======================
def _signed_post(path: str, params: dict):
    params = params.copy()
    params["timestamp"] = int(time.time() * 1000)
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{path}?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    resp = requests.post(url, headers=headers, timeout=10)
    return resp.json()

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

def get_position_info(symbol: str):
    try:
        data = _signed_get("/fapi/v2/positionRisk")
        for p in data:
            if p["symbol"].upper() == symbol.upper() and abs(float(p.get("positionAmt", 0))) > 0:
                return p
        return None
    except Exception as e:
        if DEBUG:
            print("⚠️ get_position_info error:", e)
        return None

def close_trade_on_binance(symbol: str, side: str):
    try:
        pos = get_position_info(symbol)
        if not pos:
            if DEBUG:
                print(f"⚠️ No open position found for {symbol}.")
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
# 🟩 TRADE ENTRY
# =======================
def log_trade_entry(symbol: str, side: str, order_id: str, filled_price: float):
    if order_id in notified_orders:
        return
    notified_orders.add(order_id)

    trades[symbol] = {
        "side": side.upper(),
        "entry_price": filled_price,
        "order_id": order_id,
        "closed": False,
        "exit_price": None,
        "pnl": 0.0,
        "pnl_percent": 0.0,
        "loss_bars": 0,
        "forced_exit": False,
        "recovered": False
    }

    direction_emoji = "🟩⬆️" if side.upper() == "BUY" else "🟥⬇️"
    msg = (
        f"{direction_emoji} <b>{side.upper()} ENTRY</b>\n"
        f"┇Symbol: <b>{symbol}</b>\n"
        f"┇Entry: {filled_price}\n"
        f"┇Leverage: {LEVERAGE}x | Amount: ${TRADE_AMOUNT}\n"
        f"┇<i>Trailing & Risk Monitoring Initiated</i>"
    )
    send_telegram_message(msg)

# =======================
# 🟥 TRADE EXIT
# =======================
def log_trade_exit(symbol: str, *args, **kwargs):
    reason = kwargs.get("reason", "NORMAL")
    filled_price = None
    if len(args) == 1:
        filled_price = args[0]
    elif len(args) >= 2:
        filled_price = args[1]
    elif "filled_price" in kwargs:
        filled_price = kwargs["filled_price"]

    if filled_price is None:
        if DEBUG:
            print("⚠️ log_trade_exit called without filled_price.")
        return

    t = trades.get(symbol)
    if not t:
        trades[symbol] = {
            "side": "UNKNOWN",
            "entry_price": filled_price,
            "order_id": None,
            "closed": True,
            "exit_price": filled_price,
            "pnl": 0.0,
            "pnl_percent": 0.0,
            "forced_exit": True
        }
        t = trades[symbol]

    if t.get("closed"):
        return

    t["closed"] = True
    t["exit_price"] = filled_price

    live_pct = None
    try:
        live_pct = get_unrealized_pnl_pct(symbol)
    except Exception:
        live_pct = None

    pnl_percent = live_pct if live_pct is not None else t.get("pnl_percent", 0.0)
    pnl_value = (pnl_percent / 100.0) * TRADE_AMOUNT
    t["pnl"], t["pnl_percent"] = round(pnl_value, 2), round(pnl_percent, 2)

    if pnl_percent > 0:
        emoji = "💰✅"
    elif pnl_percent < 0:
        emoji = "💔⛔️"
    else:
        emoji = "⚪️"

    reason_map = {
        "STOP_LOSS": "🚨 Stoploss Triggered",
        "FORCE_CLOSE": "⚠️ 2-Bar Loss Exit",
        "NORMAL": "✅ Normal Close"
    }
    reason_text = reason_map.get(reason, f"ℹ️ {reason}")

    msg = (
        f"{emoji} <b>{reason_text}</b>\n"
        f"┇Symbol: <b>{symbol}</b>\n"
        f"┇Side: <b>{t['side']}</b>\n"
        f"┇Entry: {t['entry_price']} → Exit: {filled_price}\n"
        f"┇PnL: <b>{t['pnl']}$</b> | {t['pnl_percent']}%\n"
        f"┇Reason: <i>{reason}</i>"
    )
    send_telegram_message(msg)

# =======================
# 📉 LOSS MONITOR
# =======================
def check_loss_conditions(symbol: str, current_price: float = None):
    if symbol not in trades or trades[symbol].get("closed"):
        return

    t = trades[symbol]
    pnl_percent = get_unrealized_pnl_pct(symbol)
    if pnl_percent is None:
        if current_price is None:
            try:
                r = _signed_get("/fapi/v1/ticker/price", {"symbol": symbol})
                current_price = float(r.get("price", 0))
            except Exception:
                return
        entry = t["entry_price"]
        pnl_percent = ((current_price - entry) / entry) * 100 * LEVERAGE if t["side"] == "BUY" else ((entry - current_price) / entry) * 100 * LEVERAGE

    immediate_threshold = -(STOP_LOSS_PCT * LEVERAGE)
    if pnl_percent <= immediate_threshold and not t.get("forced_exit"):
        t["forced_exit"] = True
        send_telegram_message(f"🚨 <b>{symbol}</b> PnL {round(pnl_percent,2)}% ≤ −{STOP_LOSS_PCT}×{LEVERAGE} → Immediate Exit")
        close_trade_on_binance(symbol, t["side"])
        log_trade_exit(symbol, current_price or 0, reason="STOP_LOSS")
        return "STOP_LOSS"

    if pnl_percent < 0:
        t["loss_bars"] = t.get("loss_bars", 0) + 1
        if t["loss_bars"] >= LOSS_BARS_LIMIT and not t.get("forced_exit"):
            t["forced_exit"] = True
            send_telegram_message(f"⚠️ <b>{symbol}</b> negative for {t['loss_bars']} bars → Forced Exit")
            close_trade_on_binance(symbol, t["side"])
            log_trade_exit(symbol, current_price or 0, reason="FORCE_CLOSE")
            return "FORCE_CLOSE"
    else:
        if t.get("loss_bars", 0) > 0:
            t["recovered"] = True
            send_telegram_message(f"💪 <b>{symbol}</b> recovered — Trailing Resumed")
        t["loss_bars"] = 0

# =======================
# 🕐 TRAILING START
# =======================
def log_trailing_start(symbol: str, trailing_type: str = "Primary"):
    send_telegram_message(f"🕐 <b>{symbol}</b>: {trailing_type} Trailing Activated")

# =======================
# 📊 DAILY SUMMARY THREAD
# =======================
def send_daily_summary():
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        time.sleep((next_run - now).total_seconds())

        closed = [t for t in trades.values() if t.get("closed")]
        total, win, loss = len(trades), sum(1 for t in closed if t["pnl"] > 0), sum(1 for t in closed if t["pnl"] < 0)
        open_ = sum(1 for t in trades.values() if not t.get("closed"))
        net_pnl = round(sum(t.get("pnl_percent", 0) for t in closed), 2)

        details = "".join(
            f"#{s} {'✅' if t['pnl']>0 else '⛔️'} {t['side']} | {t['pnl_percent']}% | ${t['pnl']}\n"
            for s, t in trades.items() if t.get("closed")
        )

        msg = (
            f"{details}\n"
            f"📅 <b>Daily Summary</b>\n"
            f"📈 Trades: {total}\n"
            f"💹 Wins: {win} | ❌ Losses: {loss}\n"
            f"📊 Net PnL %: {net_pnl}%\n"
            f"🕐 Open Trades: {open_}"
        )
        send_telegram_message(msg)
        trades.clear()

threading.Thread(target=send_daily_summary, daemon=True).start()
