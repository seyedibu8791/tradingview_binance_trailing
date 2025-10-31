import requests
import threading
import time
import datetime
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TRADE_AMOUNT, LEVERAGE, STOP_LOSS_PCT, LOSS_BARS_LIMIT
)

# =======================
# 🧾 STORAGE
# =======================
trades = {}  # {symbol: {...}}
notified_orders = set()  # prevent duplicate entries

# =======================
# 📢 TELEGRAM HELPER
# =======================
def send_telegram_message(message: str):
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("⚠️ Missing Telegram credentials. Skipping message.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        response = requests.post(url, data=payload, timeout=10)
        if response.status_code != 200:
            print("❌ Telegram Error:", response.status_code, response.text)
    except Exception as e:
        print("❌ Telegram Exception:", e)

# =======================
# 🟩 TRADE ENTRY
# =======================
def log_trade_entry(symbol: str, side: str, order_id: str, filled_price: float):
    """Log entry when Binance confirms fill"""
    if order_id in notified_orders:
        return
    notified_orders.add(order_id)

    trades[symbol] = {
        "side": side,
        "entry_price": filled_price,
        "order_id": order_id,
        "closed": False,
        "exit_price": None,
        "pnl": 0,
        "pnl_percent": 0,
        "loss_bars": 0,  # Track how many bars in loss
        "forced_exit": False
    }

    arrow = "⬆️" if side.upper() == "BUY" else "⬇️"
    trade_type = "Long Trade" if side.upper() == "BUY" else "Short Trade"

    message = f"""{arrow} <b>{trade_type}</b>
Symbol: <b>#{symbol}</b>
Side: <b>{side}</b>
--- ⌁ ---
Leverage: {LEVERAGE}x
Trade Amount: {TRADE_AMOUNT}$
--- ⌁ ---
Entry Price: <b>{filled_price}</b>
--- ⌁ ---
🕐 Trailing & Risk Monitor Started...
"""
    send_telegram_message(message)

# =======================
# 🟥 TRADE EXIT
# =======================
def log_trade_exit(symbol: str, order_id: str, filled_price: float, reason: str = "NORMAL"):
    """Send Telegram message when trade closes"""
    if symbol not in trades:
        trades[symbol] = {
            "side": "UNKNOWN",
            "entry_price": filled_price,
            "closed": True,
            "exit_price": filled_price,
            "pnl": 0,
            "pnl_percent": 0,
            "forced_exit": True
        }

    trade = trades[symbol]
    if trade.get("closed"):
        return

    trade["exit_price"] = filled_price
    trade["closed"] = True
    entry_price = trade["entry_price"]
    side = trade["side"].upper()
    qty = TRADE_AMOUNT

    # Calculate PnL
    if side == "BUY":
        pnl = (filled_price - entry_price) * qty * LEVERAGE / entry_price
        pnl_percent = ((filled_price - entry_price) / entry_price) * 100 * LEVERAGE
    elif side == "SELL":
        pnl = (entry_price - filled_price) * qty * LEVERAGE / entry_price
        pnl_percent = ((entry_price - filled_price) / entry_price) * 100 * LEVERAGE
    else:
        pnl = pnl_percent = 0

    trade["pnl"] = round(pnl, 2)
    trade["pnl_percent"] = round(pnl_percent, 2)

    if reason == "STOP_LOSS":
        header = "🚨 Stop Loss Triggered!"
    elif reason == "FORCE_CLOSE":
        header = "⚠️ 2-Bar Loss Closure Executed!"
    elif pnl >= 0:
        header = "✅ Profit Achieved!"
    else:
        header = "⛔️ Ended in Loss!"

    message = f"""{header}
Symbol: <b>#{symbol}</b>
Side: <b>{side}</b>
--- ⌁ ---
Entry: {entry_price}
Exit: {filled_price}
--- ⌁ ---
PnL: {trade['pnl']}$ | {trade['pnl_percent']}%
Reason: <b>{reason}</b>
"""
    send_telegram_message(message)

# =======================
# 🟦 LOSS MONITOR
# =======================
def check_loss_conditions(symbol: str, current_price: float):
    """Evaluate ongoing trades for loss/stoploss conditions"""
    if symbol not in trades or trades[symbol]["closed"]:
        return

    trade = trades[symbol]
    entry = trade["entry_price"]
    side = trade["side"].upper()

    # Calculate current % PnL
    if side == "BUY":
        pnl_percent = ((current_price - entry) / entry) * 100 * LEVERAGE
    else:
        pnl_percent = ((entry - current_price) / entry) * 100 * LEVERAGE

    # --- Stop Loss Check ---
    if pnl_percent <= -STOP_LOSS_PCT:
        trade["forced_exit"] = True
        log_trade_exit(symbol, trade["order_id"], current_price, reason="STOP_LOSS")
        return "STOP_LOSS"

    # --- Consecutive Loss Bars ---
    if pnl_percent < 0:
        trade["loss_bars"] += 1
        if trade["loss_bars"] >= LOSS_BARS_LIMIT:
            trade["forced_exit"] = True
            log_trade_exit(symbol, trade["order_id"], current_price, reason="FORCE_CLOSE")
            return "FORCE_CLOSE"
    else:
        # Reset loss counter if back to profit
        trade["loss_bars"] = 0

    return None

# =======================
# 🟦 TRAILING START LOG
# =======================
def log_trailing_start(symbol: str, trailing_type: str = "Primary"):
    msg = f"🕐 {symbol}: <b>{trailing_type} Trailing Started Monitoring...</b>"
    send_telegram_message(msg)

# =======================
# 📅 DAILY SUMMARY
# =======================
def send_daily_summary():
    """Auto-send EOD summary"""
    while True:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5.5)))
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        time.sleep((next_run - now).total_seconds())

        closed_trades = [t for t in trades.values() if t["closed"]]
        total_signals = len(trades)
        profitable = sum(1 for t in closed_trades if t["pnl"] > 0)
        lost = sum(1 for t in closed_trades if t["pnl"] < 0)
        open_trades = sum(1 for t in trades.values() if not t["closed"])
        net_pnl_percent = round(sum(t["pnl_percent"] for t in closed_trades), 2)

        detailed_msg = ""
        for symbol, t in trades.items():
            if t["closed"]:
                icon = "✅" if t["pnl"] > 0 else "⛔️"
                detailed_msg += f"#{symbol} {t['side']} {icon} | Entry: {t['entry_price']} | Exit: {t['exit_price']} | PnL%: {t['pnl_percent']} | PnL$: {t['pnl']}\n"

        summary_msg = f"""{detailed_msg}
👇🏻 <b>Daily Summary</b>
➕ Total Trades: {total_signals}
✔️ Profitable: {profitable}
✖️ Lost: {lost}
◼️ Open Trades: {open_trades}
✅ Net PnL %: {net_pnl_percent}%"""
        send_telegram_message(summary_msg)
        trades.clear()

# Start summary thread
threading.Thread(target=send_daily_summary, daemon=True).start()
