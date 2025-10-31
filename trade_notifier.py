# trade_notifier.py
import requests
import threading
import time
import datetime
import hmac
import hashlib
from typing import Optional

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TRADE_AMOUNT, LEVERAGE, STOP_LOSS_PCT, LOSS_BARS_LIMIT,
    BASE_URL, BINANCE_API_KEY, BINANCE_SECRET_KEY, DEBUG,
    TRAILING_COMPARE_PNL, TS_LOW_OFFSET_PCT, TS_HIGH_OFFSET_PCT
)

# =======================
# 📦 STORAGE
# =======================
trades = {}              # { symbol: {side, entry_price, order_id, closed, exit_price, pnl, pnl_percent, loss_bars, forced_exit, recovered} }
notified_orders = set()  # prevent duplicate entry notifications


# =======================
# 📢 TELEGRAM HELPER
# =======================
def send_telegram_message(message: str):
    """Send formatted HTML message to configured Telegram chat."""
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
# 🔎 Position helpers
# =======================
def get_position_info(symbol: str) -> Optional[dict]:
    """Return position dict for symbol from /fapi/v2/positionRisk or None."""
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
    """
    Compute unrealized PnL% *including leverage* for the open position on symbol.
    Formula:
      - For LONG: ((markPrice - entryPrice) / entryPrice) * 100 * LEVERAGE
      - For SHORT: ((entryPrice - markPrice) / entryPrice) * 100 * LEVERAGE
    """
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
# 🔒 Close on Binance
# =======================
def close_trade_on_binance(symbol: str, side: str):
    """Force market close of the open position for symbol."""
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
    """Record entry and send Telegram notification. Prevent duplicate messages by order_id."""
    # --- cleanup for same-direction re-entry ---
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
        "loss_bars": 0,
        "forced_exit": False,
        "recovered": False
    }

    direction_emoji = "🟩⬆️" if side.upper() == "BUY" else "🟥⬇️"
    msg = (
        f"{direction_emoji} <b>{side.upper()} ENTRY</b>\n"
        f"┇#{symbol}\n"
        f"┇Entry: {filled_price}\n"
        f"┇Leverage: {LEVERAGE}x | Amount: ${TRADE_AMOUNT}\n"
        f"┇<i>Trailing & Risk Monitor Initiated</i>"
    )
    send_telegram_message(msg)


# =======================
# 🟥 TRADE EXIT
# =======================
def log_trade_exit(symbol: str, filled_price: float, reason: str = "NORMAL"):
    """Send close notification and update trade record (with accurate live PnL fetch)."""
    t = trades.get(symbol)
    if not t or t.get("closed"):
        return

    t["closed"] = True
    t["exit_price"] = filled_price

    # --- Fetch live PnL percent from Binance ---
    live_pct = get_unrealized_pnl_pct(symbol)
    pnl_percent = live_pct if live_pct is not None else t.get("pnl_percent", 0.0)
    pnl_value = (pnl_percent / 100.0) * TRADE_AMOUNT

    t["pnl"] = round(pnl_value, 2)
    t["pnl_percent"] = round(pnl_percent or 0.0, 2)

    # --- Emoji mapping based on PnL ---
    if t["pnl_percent"] > 0:
        emoji = "💰✅"
    elif t["pnl_percent"] < 0:
        emoji = "💔⛔️"
    else:
        emoji = "⚪️"

    # --- Reason text mapping ---
    reason_text = {
        "STOP_LOSS": "🚨 Stoploss Triggered",
        "FORCE_CLOSE": "⚠️ 2-Bar Forced Exit",
        "TRAIL_CLOSE": "🎯 Trailing Stop Hit",
        "MARKET_CLOSE": "✅ Market Close",
        "SAME_DIRECTION_REENTRY": "🔁 Same-Direction Re-entry Close",
        "NORMAL": "✅ Normal Close"
    }.get(reason, reason)

    # --- Telegram message ---
    msg = (
        f"{emoji} <b>{reason_text}</b>\n"
        f"┇#{symbol}\n"
        f"┇{t['side']} | Entry: {t['entry_price']} → Exit: {filled_price}\n"
        f"┇PnL: <b>{t['pnl']}$</b> | {t['pnl_percent']}%\n"
        f"┇Reason: <i>{reason}</i>"
    )

    # --- Highlight Forced Exit PnL ---
    if reason in ["FORCE_CLOSE", "STOP_LOSS"]:
        msg += f"\n┇<b>Force PnL</b>: {t['pnl_percent']}% ({t['pnl']}$)"

    send_telegram_message(msg)


# =======================
# 📉 LOSS MONITOR
# =======================
def check_loss_conditions(symbol: str, current_price: float = None):
    """Evaluate ongoing trades for stoploss, 2-bar forced close, or recovery."""
    if symbol not in trades or trades[symbol].get("closed"):
        return

    t = trades[symbol]
    pnl_percent = get_unrealized_pnl_pct(symbol)
    if pnl_percent is None:
        if current_price is None:
            if DEBUG:
                print(f"⚠️ No live PnL for {symbol} and no price provided.")
            return
        entry = t["entry_price"]
        if t["side"] == "BUY":
            pnl_percent = ((current_price - entry) / entry) * 100 * LEVERAGE
        else:
            pnl_percent = ((entry - current_price) / entry) * 100 * LEVERAGE

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
            send_telegram_message(f"⚠️ <b>{symbol}</b> remained negative for {t['loss_bars']} bars → Forced Exit")
            close_trade_on_binance(symbol, t["side"])
            log_trade_exit(symbol, current_price or 0, reason="FORCE_CLOSE")
            return "FORCE_CLOSE"
    else:
        if t.get("loss_bars", 0) > 0:
            t["recovered"] = True
            send_telegram_message(f"💪 <b>{symbol}</b> recovered — Trailing Resumed")
        t["loss_bars"] = 0

    return None


# =======================
# 🕐 TRAILING & COMPARISON
# =======================
def log_trailing_start(symbol: str, trailing_type: str = "Primary", extra: str = ""):
    txt = f"🕐 <b>#{symbol}</b>: {trailing_type} Trailing Activated"
    if extra:
        txt += f" ┇{extra}"
    send_telegram_message(txt)


def notify_trail_comparison(symbol: str, primary_pct: float, secondary_pct: Optional[float]):
    sec_text = f" | Secondary: {round(secondary_pct,2)}%" if secondary_pct is not None else ""
    send_telegram_message(f"⚖️ <b>#{symbol}</b> Trail PnL — Primary: {round(primary_pct,2)}%{sec_text}")

    if TRAILING_COMPARE_PNL and secondary_pct is not None:
        if primary_pct - secondary_pct >= 0.3:
            send_telegram_message(
                f"🎯 <b>#{symbol}</b> Primary protects more — prefer Primary stop (diff {round(primary_pct-secondary_pct,2)}%)"
            )


# =======================
# 📊 DAILY SUMMARY THREAD
# =======================
def send_daily_summary():
    """Runs background thread to send EOD summary (IST midnight)."""
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


# start background summary thread
threading.Thread(target=send_daily_summary, daemon=True).start()
