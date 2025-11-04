# trade_notifier.py (updated with 2-bar continuous negative PnL exit)
import requests
import threading
import time
import datetime
import hmac
import hashlib
from typing import Optional

# ===============================
# ✅ IMPORTS FROM CONFIG
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
    get_unrealized_pnl_pct,
    LOSS_BARS_LIMIT,
)

# =======================
# 📦 STORAGE
# =======================
trades = {}              # {symbol: {...trade info...}}
notified_orders = set()  # prevent duplicate entry notifications
pnl_neg_counter = {}     # Track consecutive negative pnl bars


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


# =======================
# 💰 REAL TRADE PRICE FETCHER
# =======================
def get_last_trade_prices(symbol: str):
    """Fetch last executed buy/sell trade prices from Binance."""
    try:
        data = _signed_get("/fapi/v1/userTrades", {"symbol": symbol.upper(), "limit": 50})
        buys = [float(x["price"]) for x in data if x["isBuyer"]]
        sells = [float(x["price"]) for x in data if not x["isBuyer"]]
        entry = round(sum(buys) / len(buys), 4) if buys else 0.0
        exit = round(sum(sells) / len(sells), 4) if sells else 0.0
        return entry, exit
    except Exception as e:
        if DEBUG:
            print("⚠️ get_last_trade_prices error:", e)
        return 0.0, 0.0


# =======================
# 🔒 CLOSE TRADE ON BINANCE
# =======================
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
def log_trade_entry(symbol: str, side: str, order_id: str, filled_price: float, interval: str):
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
        "exit_price": None,
        "pnl": 0.0,
        "pnl_percent": 0.0,
        "entry_time": time.time(),
        "interval": interval.lower(),
    }

    # Start monitoring negative PnL after entry
    threading.Thread(target=monitor_negative_pnl, args=(symbol,), daemon=True).start()

    direction_emoji = "🟩⬆️" if side.upper() == "BUY" else "🟥⬇️"
    msg = (
        f"{direction_emoji} <b>{side.upper()} ENTRY</b>\n"
        f"┇#{symbol}\n"
        f"┇Entry: {filled_price}\n"
        f"┇Interval: {interval}\n"
        f"┇Leverage: {LEVERAGE}x | Amount: ${TRADE_AMOUNT}\n"
        f"┇<i>Backend Monitored</i>"
    )
    send_telegram_message(msg)


# =======================
# 🟥 TRADE EXIT
# =======================
def log_trade_exit(symbol: str, filled_price: float, reason: str = "MARKET_CLOSE"):
    try:
        entry_price, exit_price = get_last_trade_prices(symbol)
        if not exit_price:
            exit_price = filled_price

        t = trades.get(symbol, {})
        side = t.get("side", "BUY")
        entry_price = entry_price or t.get("entry_price", filled_price)
        exit_price = exit_price or filled_price

        pnl_dollar = (
            (exit_price - entry_price) / entry_price * TRADE_AMOUNT * LEVERAGE
            if side == "BUY"
            else (entry_price - exit_price) / entry_price * TRADE_AMOUNT * LEVERAGE
        )
        pnl_percent = (pnl_dollar / TRADE_AMOUNT) * 100

        emoji = "💰✅" if pnl_dollar > 0 else "💔⛔️" if pnl_dollar < 0 else "⚪️"

reason_text = {
    "TRAIL_CLOSE": "🎯 Trailing Stop Hit",
    "OPPOSITE_SIGNAL_CLOSE": "🔄 Opposite Signal Exit",
    "SAME_DIRECTION_REENTRY": "🔁 Same Direction Signal Exit",
    "CROSS_EXIT": "⚔️ Cross Exit",
    "STOP_LOSS": "🚨 Stop Loss Hit",
    "MARKET_CLOSE": "✅ Market Close",
    "TWO_BAR_CLOSE_EXIT": "⏱️ 2 Bar Close Exit",   # ✅ unified key
}.get(reason, reason)

        msg = (
            f"{emoji} <b>{reason_text}</b>\n"
            f"┇#{symbol}\n"
            f"┇{side} | Entry: {entry_price} → Exit: {exit_price}\n"
            f"┇PnL: <b>{round(pnl_dollar, 2)}$</b> | {round(pnl_percent, 2)}%\n"
            f"┇Reason: <i>{reason_text}</i>"
        )

        t.update({
            "exit_price": exit_price,
            "pnl": round(pnl_dollar, 2),
            "pnl_percent": round(pnl_percent, 2),
            "closed": True,
        })
        trades[symbol] = t

        send_telegram_message(msg)

    except Exception as e:
        print("❌ log_trade_exit error:", e)
        send_telegram_message(f"⚠️ Error logging trade exit for {symbol}: {e}")


# =======================
# 🕒 INTERVAL PARSER
# =======================
def parse_interval_to_seconds(interval: str) -> int:
    """Convert TradingView interval (like '1m', '5m', '1h') into seconds."""
    unit = interval[-1]
    value = int(interval[:-1])
    if unit == "m":
        return value * 60
    elif unit == "h":
        return value * 3600
    elif unit == "d":
        return value * 86400
    else:
        return 300  # default 5 min


# =======================
# 🔁 MONITOR NEGATIVE PNL
# =======================
def monitor_negative_pnl(symbol: str):
    """Check PnL for open trade every bar; close if negative for LOSS_BARS_LIMIT bars."""
    try:
        while symbol in trades and not trades[symbol].get("closed"):
            interval = trades[symbol].get("interval", "5m")
            interval_sec = parse_interval_to_seconds(interval)
            pnl = get_unrealized_pnl_pct(symbol)

            if pnl is not None:
                if pnl < 0:
                    pnl_neg_counter[symbol] = pnl_neg_counter.get(symbol, 0) + 1
                    if pnl_neg_counter[symbol] >= LOSS_BARS_LIMIT:
                        t = trades[symbol]
                        close_trade_on_binance(symbol, t["side"])
                        log_trade_exit(symbol, t["entry_price"], reason="LOSS_BAR_EXIT")
                        send_telegram_message(f"⚠️ 2 bar close exit triggered for {symbol}")
                        break
                else:
                    pnl_neg_counter[symbol] = 0
            time.sleep(interval_sec)
    except Exception as e:
        if DEBUG:
            print(f"⚠️ monitor_negative_pnl error for {symbol}: {e}")


# =======================
# 📊 DAILY SUMMARY
# =======================
def send_daily_summary():
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        time.sleep((next_run - now).total_seconds())

        closed = [t for t in trades.values() if t.get("closed")]
        total = len(trades)
        win = sum(1 for t in closed if t.get("pnl", 0) > 0)
        loss = sum(1 for t in closed if t.get("pnl", 0) < 0)
        open_ = sum(1 for t in trades.values() if not t.get("closed"))
        net_pnl = round(sum(t.get("pnl_percent", 0) for t in closed), 2)

        details = ""
        for s, t in trades.items():
            if t.get("closed"):
                icon = "✅" if t.get("pnl", 0) > 0 else "⛔️"
                details += f"#{s} {icon} {t['side']} | {t.get('pnl_percent')}% | ${t.get('pnl')}\n"

        msg = (
            f"{details}\n"
            f"📅 <b>Daily Summary</b>\n"
            f"📊 Total Trades: {total}\n"
            f"✔️ Profitable: {win} | ✖️ Lost: {loss}\n"
            f"◼️ Open Trades: {open_}\n"
            f"✅ Net PnL %: {net_pnl}%"
        )
        send_telegram_message(msg)

        # cleanup closed trades
        for s in list(trades.keys()):
            if trades[s].get("closed"):
                trades.pop(s, None)


threading.Thread(target=send_daily_summary, daemon=True).start()
