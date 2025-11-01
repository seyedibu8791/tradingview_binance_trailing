import requests
import threading
import time
import datetime
import hmac
import hashlib
from typing import Optional

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TRADE_AMOUNT, LEVERAGE, STOP_LOSS_PCT,
    BASE_URL, BINANCE_API_KEY, BINANCE_SECRET_KEY, DEBUG,
    TRAILING_ACTIVATION_PCT, TS_LOW_OFFSET_PCT, TS_HIGH_OFFSET_PCT
)

# =======================
# 📦 STORAGE
# =======================
trades = {}              # {symbol: {...trade info...}}
notified_orders = set()  # prevent duplicate entry notifications


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
        "forced_exit": False,
        "recovered": False,
        "entry_time": time.time(),
        "interval": interval.lower(),
        "trail_active": False
    }

    direction_emoji = "🟩⬆️" if side.upper() == "BUY" else "🟥⬇️"
    msg = (
        f"{direction_emoji} <b>{side.upper()} ENTRY</b>\n"
        f"┇#{symbol}\n"
        f"┇Entry: {filled_price}\n"
        f"┇Interval: {interval}\n"
        f"┇Leverage: {LEVERAGE}x | Amount: ${TRADE_AMOUNT}\n"
        f"┇<i>Dynamic Trailing & Risk Monitor Active</i>"
    )
    send_telegram_message(msg)


# =======================
# 🟥 TRADE EXIT
# =======================
def log_trade_exit(symbol: str, filled_price: float, reason: str = "NORMAL"):
    t = trades.get(symbol)
    if not t or t.get("closed"):
        return

    t["closed"] = True
    t["exit_price"] = filled_price

    live_pct = get_unrealized_pnl_pct(symbol)
    pnl_percent = live_pct if live_pct is not None else t.get("pnl_percent", 0.0)
    pnl_value = (pnl_percent / 100.0) * TRADE_AMOUNT

    t["pnl"] = round(pnl_value, 2)
    t["pnl_percent"] = round(pnl_percent or 0.0, 2)

    emoji = "💰✅" if t["pnl_percent"] > 0 else "💔⛔️" if t["pnl_percent"] < 0 else "⚪️"
    reason_text = {
        "STOP_LOSS": "🚨 Stoploss Triggered",
        "FORCE_CLOSE": "⚠️ 2-Bar Loss Exit",
        "TRAIL_CLOSE": "🎯 Trailing Stop Hit",
        "MARKET_CLOSE": "✅ Market Close",
        "SAME_DIRECTION_REENTRY": "🔁 Same-Direction Re-entry Close",
        "NORMAL": "✅ Normal Close"
    }.get(reason, reason)

    msg = (
        f"{emoji} <b>{reason_text}</b>\n"
        f"┇#{symbol}\n"
        f"┇{t['side']} | Entry: {t['entry_price']} → Exit: {filled_price}\n"
        f"┇PnL: <b>{t['pnl']}$</b> | {t['pnl_percent']}%\n"
        f"┇Reason: <i>{reason}</i>"
    )
    send_telegram_message(msg)


# =======================
# 📉 LOSS & TRAIL MONITOR
# =======================
def check_loss_conditions(symbol: str, current_price: float = None):
    if symbol not in trades or trades[symbol].get("closed"):
        return

    t = trades[symbol]
    pnl_percent = get_unrealized_pnl_pct(symbol)
    if pnl_percent is None:
        return

    # === 2-Bar Exit Check ===
    bar_durations = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    bar_duration = bar_durations.get(t["interval"], 3600)
    elapsed = time.time() - t["entry_time"]

    if elapsed >= 2 * bar_duration and pnl_percent < 0 and not t.get("forced_exit"):
        t["forced_exit"] = True
        send_telegram_message(f"⚠️ <b>{symbol}</b> 2-bar negative → Force closing trade")
        close_trade_on_binance(symbol, t["side"])
        log_trade_exit(symbol, current_price or t["entry_price"], reason="FORCE_CLOSE")
        return

    # === Trailing Stop Activation ===
    if not t.get("trail_active") and pnl_percent >= TRAILING_ACTIVATION_PCT:
        t["trail_active"] = True
        send_telegram_message(f"🎯 <b>{symbol}</b> Trail activated @ {pnl_percent}%")

    # === Dynamic Trailing Logic ===
    if t.get("trail_active"):
        if pnl_percent <= (TRAILING_ACTIVATION_PCT - TS_LOW_OFFSET_PCT):
            send_telegram_message(f"🎯 <b>{symbol}</b> Trailing stop hit — closing")
            close_trade_on_binance(symbol, t["side"])
            log_trade_exit(symbol, current_price or t["entry_price"], reason="TRAIL_CLOSE")
            t["trail_active"] = False
            return


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
        for s in list(trades.keys()):
            if trades[s].get("closed"):
                trades.pop(s, None)


threading.Thread(target=send_daily_summary, daemon=True).start()
