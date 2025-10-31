import requests
import threading
import time
import datetime
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TRADE_AMOUNT, LEVERAGE

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
    """Log entry when Binance confirms fill (even partial)"""
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
🕐 Trailing Monitor Started...
"""
    send_telegram_message(message)

# =======================
# 🟥 TRADE EXIT
# =======================
def log_trade_exit(symbol: str, order_id: str, filled_price: float):
    """Send final Telegram message when position closes in Binance"""
    if symbol not in trades:
        trades[symbol] = {
            "side": "UNKNOWN",
            "entry_price": filled_price,
            "closed": True,
            "exit_price": filled_price,
            "pnl": 0,
            "pnl_percent": 0,
        }

    trade = trades[symbol]
    trade["exit_price"] = filled_price
    trade["closed"] = True

    entry_price = trade["entry_price"]
    side = trade["side"].upper()
    qty = TRADE_AMOUNT

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

    header = "✅ Profit Achieved!" if pnl >= 0 else "⛔️ Ended in Loss!"

    message = f"""{header}
Symbol: <b>#{symbol}</b>
Side: <b>{side}</b>
--- ⌁ ---
Entry: {entry_price}
Exit: {filled_price}
--- ⌁ ---
PnL: {trade['pnl']}$ | {trade['pnl_percent']}%
"""
    send_telegram_message(message)

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
