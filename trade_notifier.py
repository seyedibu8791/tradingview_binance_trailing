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
# 🧾 STORAGE
# =======================
trades = {}  # {symbol: {...}}
notified_orders = set()  # prevent duplicate entry notifications

# =======================
# 📢 TELEGRAM HELPER
# =======================
def send_telegram_message(message: str):
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            if DEBUG:
                print("⚠️ Missing Telegram credentials. Skipping message.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print("❌ Telegram Error:", r.status_code, r.text)
    except Exception as e:
        print("❌ Telegram Exception:", e)

# =======================
# 📡 BINANCE SIGNED HELPERS (for notifier to execute closes)
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

def get_position_info(symbol: str):
    """Return the first non-zero position dict for symbol or None"""
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

def close_trade_on_binance(symbol: str, side: str):
    """
    Force market close of the open position for symbol.
    side = original side ("BUY" or "SELL") -> close by sending opposite side MARKET order
    """
    try:
        pos = get_position_info(symbol)
        if not pos:
            if DEBUG:
                print(f"⚠️ No open position found for {symbol} to close.")
            return {"status": "no_position"}
        amt = abs(float(pos.get("positionAmt", 0)))
        # quantity must be rounded according to lot size; place exact amt (Binance will accept)
        close_side = "SELL" if side.upper() == "BUY" else "BUY"
        params = {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": round(amt, 8)
        }
        resp = _signed_post("/fapi/v1/order", params)
        if DEBUG:
            print("🧾 close_trade_on_binance:", resp)
        return resp
    except Exception as e:
        print("❌ close_trade_on_binance error:", e)
        return {"error": str(e)}

# =======================
# 🟩 TRADE ENTRY (notifier)
# =======================
def log_trade_entry(symbol: str, side: str, order_id: str, filled_price: float):
    """Log entry when Binance confirms fill (notifier-side)"""
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

    arrow = "⬆️" if side.upper() == "BUY" else "⬇️"
    msg = f"""{arrow} <b>{side.upper()} Trade Opened</b>
Symbol: <b>#{symbol}</b>
Entry: {filled_price}
Leverage: {LEVERAGE}x | Amount: ${TRADE_AMOUNT}
🕐 Trailing & Risk Monitor Started..."""
    send_telegram_message(msg)

# =======================
# 🟥 TRADE EXIT (notifier)
# =======================
def log_trade_exit(symbol: str, *args, **kwargs):
    """
    Flexible log_trade_exit:
    - Called as log_trade_exit(symbol, filled_price, reason="X")
    - Or log_trade_exit(symbol, order_id, filled_price) (older call signature)
    We'll handle both variants gracefully.
    """
    # Parse args/kwargs
    reason = kwargs.get("reason", "NORMAL")
    filled_price = None
    # possible call patterns:
    # (symbol, filled_price)
    # (symbol, order_id, filled_price)
    if len(args) == 1:
        filled_price = args[0]
    elif len(args) >= 2:
        # args[0] may be order_id, args[1] filled_price
        filled_price = args[1]
    elif "filled_price" in kwargs:
        filled_price = kwargs["filled_price"]

    if filled_price is None:
        # can't proceed without exit price
        if DEBUG:
            print("⚠️ log_trade_exit called without filled_price, skipping.")
        return

    t = trades.get(symbol)
    if not t:
        # create minimal entry
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
        # already closed; avoid duplicate messages
        return

    t["closed"] = True
    t["exit_price"] = filled_price

    # Try to fetch live pnl% from Binance (preferred)
    live_pct = None
    try:
        live_pct = get_unrealized_pnl_pct(symbol)
    except Exception:
        live_pct = None

    pnl_percent = live_pct if live_pct is not None else t.get("pnl_percent", 0.0)
    pnl_value = (pnl_percent / 100.0) * TRADE_AMOUNT

    t["pnl"] = round(pnl_value, 2)
    t["pnl_percent"] = round(pnl_percent, 2)

    if reason == "STOP_LOSS":
        header = "🚨 <b>Immediate Stoploss Triggered</b>"
    elif reason == "FORCE_CLOSE":
        header = "⚠️ <b>2-Bar Consecutive Loss Exit</b>"
    elif reason == "NORMAL":
        header = "✅ <b>Trade Closed Normally</b>"
    else:
        header = f"ℹ️ <b>{reason}</b>"

    msg = f"""{header}
Symbol: <b>#{symbol}</b>
Side: <b>{t.get('side')}</b>
Entry: {t.get('entry_price')}
Exit: {filled_price}
PnL: {t['pnl']}$ | {t['pnl_percent']}%
Reason: <b>{reason}</b>"""
    send_telegram_message(msg)

# =======================
# 🟦 LOSS MONITOR
# =======================
def check_loss_conditions(symbol: str, current_price: float = None):
    """
    Monitor open trades using live unrealized pnl from Binance when available.
    - If live PnL percent <= -STOP_LOSS_PCT * LEVERAGE -> immediate market close.
    - If consecutive LOSS_BARS_LIMIT bars negative -> forced market close.
    - If recovered (pnl >= 0) after losing bars -> reset loss_bars and send recovered message.
    """
    if symbol not in trades:
        return
    t = trades[symbol]
    if t.get("closed"):
        return

    # prefer live PnL%
    pnl_percent = get_unrealized_pnl_pct(symbol)
    if pnl_percent is None:
        # fallback to local calc using current_price if provided
        if current_price is None:
            # if we don't have any price, try to fetch mark price quickly
            try:
                r = _signed_get("/fapi/v1/ticker/price", {"symbol": symbol})
                current_price = float(r.get("price", 0))
            except Exception:
                current_price = None

        if current_price is None:
            if DEBUG:
                print(f"⚠️ No price available to compute fallback pnl for {symbol}.")
            return

        entry = t["entry_price"]
        if t["side"].upper() == "BUY":
            pnl_percent = ((current_price - entry) / entry) * 100 * LEVERAGE
        else:
            pnl_percent = ((entry - current_price) / entry) * 100 * LEVERAGE

    # Immediate stop-loss threshold is configured STOP_LOSS_PCT multiplied by LEVERAGE (per your spec)
    immediate_threshold = -(STOP_LOSS_PCT * LEVERAGE)
    if pnl_percent <= immediate_threshold and not t.get("forced_exit", False):
        t["forced_exit"] = True
        send_telegram_message(f"🚨 #{symbol} PnL {round(pnl_percent,2)}% <= −{STOP_LOSS_PCT}×{LEVERAGE} → Immediate exit")
        close_trade_on_binance(symbol, t["side"])
        # call notifier exit - use flexible signature
        log_trade_exit(symbol, current_price or 0, reason="STOP_LOSS")
        return "STOP_LOSS"

    # Consecutive loss bars logic
    if pnl_percent < 0:
        t["loss_bars"] = t.get("loss_bars", 0) + 1
        if t["loss_bars"] >= LOSS_BARS_LIMIT and not t.get("forced_exit", False):
            t["forced_exit"] = True
            send_telegram_message(f"⚠️ #{symbol} remained negative for {t['loss_bars']} bars → Forced exit")
            close_trade_on_binance(symbol, t["side"])
            log_trade_exit(symbol, current_price or 0, reason="FORCE_CLOSE")
            return "FORCE_CLOSE"
    else:
        # recovered
        if t.get("loss_bars", 0) > 0:
            t["recovered"] = True
            send_telegram_message(f"✅ #{symbol} recovered after loss — Trailing resumed")
        t["loss_bars"] = 0

    return None

# =======================
# 🕐 TRAILING START LOG
# =======================
def log_trailing_start(symbol: str, trailing_type: str = "Primary"):
    send_telegram_message(f"🕐 {symbol}: <b>{trailing_type} Trailing Monitoring Started...</b>")

# =======================
# 📅 DAILY SUMMARY (thread)
# =======================
def send_daily_summary():
    """Auto-send EOD summary (runs in background thread)."""
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
                details += f"#{s} {icon} {t.get('side')} | PnL% {t.get('pnl_percent')} | ${t.get('pnl')}\n"

        msg = f"""{details}
👇🏻 <b>Daily Summary</b>
➕ Trades: {total}
✔️ Profitable: {win}
✖️ Lost: {loss}
◼️ Open: {open_}
📊 Net PnL %: {net_pnl}%"""
        send_telegram_message(msg)
        trades.clear()

# start summary thread
threading.Thread(target=send_daily_summary, daemon=True).start()
