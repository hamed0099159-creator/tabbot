# -*- coding: utf-8 -*-
"""
ربات چندارزی تبدیل — نسخه همزمان (برای GitHub Actions)
=======================================================
هر بازار یک خط مستقل دارد که همزمان با بقیه اجرا می‌شود:
سیگنال می‌گیرد، می‌خرد، حد سود/ضرر را دنبال می‌کند و می‌فروشد.
در پایان جلسه، همه پوزیشن‌های باز بسته و گزارش نهایی چاپ می‌شود.

کلیدها از Secrets گیت‌هاب: TABDEAL_API_KEY و TABDEAL_SECRET
ورودی‌های اجرا: RUN_MINUTES (مدت جلسه) و DRY_RUN (آزمایشی؟)
"""

import requests, hmac, hashlib, time, json, os, logging, threading
from urllib.parse import urlencode

# ================= تنظیمات اصلی =================
API_KEY     = os.environ.get("TABDEAL_API_KEY", "")
API_SECRET  = os.environ.get("TABDEAL_SECRET", "")
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "60"))
DRY_RUN     = os.environ.get("DRY_RUN", "1") == "1"   # 1 = آزمایشی، 0 = پول واقعی

BUDGET_USDT   = 20.0    # کل بودجه جلسه (تتر) — کم شروع کن!
MAX_OPEN      = 3       # حداکثر پوزیشن باز همزمان
PER_TRADE     = 0.35    # سهم هر خرید از بودجه کل (۳۵٪)
SYMBOLS       = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]  # بازارها
INTERVAL      = "15m"
FAST, SLOW    = 9, 21
TP_PCT        = 3.0     # حد سود ٪
SL_PCT        = 1.5     # حد ضرر ٪
CHECK_EVERY   = 120     # ثانیه بین هر بررسی هر بازار
MIN_ORDER_USDT = 5.0

BASE = "https://api1.tabdeal.org/api/v1"
# =================================================

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s")
log = logging.getLogger("tabbot")

session = requests.Session()
session.headers.update({"X-MBX-APIKEY": API_KEY})

lock = threading.Lock()          # قفل مشترک برای وضعیت و سفارش‌ها
pos  = {}                        # {symbol: {buy, qty}}
realized = [0.0]
kills   = threading.Event()      # برای پایان جلسه

def signed(path, params=None, method="GET"):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 30000
    qs = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    r = session.request(method, f"{BASE}{path}?{qs}&signature={sig}", timeout=15)
    r.raise_for_status()
    return r.json()

def public(path, params=None):
    r = session.get(f"{BASE}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def get_klines_close(symbol):
    data = public("/klines", {"symbol": symbol, "interval": INTERVAL, "limit": SLOW + 5})
    return [float(k[4]) for k in data]

def get_price(symbol):
    return float(public("/ticker/price", {"symbol": symbol})["price"])

def sma(arr, p):
    s = arr[-p:]
    return sum(s) / len(s)

def usdt_free():
    acct = signed("/account")
    for b in acct.get("balances", []):
        if b.get("asset") == "USDT":
            return float(b["free"])
    return 0.0

def place_order(symbol, side, amount, qty=None):
    if DRY_RUN:
        return {"dry": True, "price": get_price(symbol)}
    params = {"symbol": symbol, "side": side, "type": "MARKET"}
    if side == "BUY":
        params["quoteOrderQty"] = round(amount, 2)
    else:
        params["quantity"] = qty
    return signed("/order", params, method="POST")

def open_count():
    return len(pos)

def buy(symbol, price):
    with lock:
        if symbol in pos or open_count() >= MAX_OPEN:
            return
        spend = BUDGET_USDT * PER_TRADE
        free  = usdt_free()
        spend = min(spend, free)
        if spend < MIN_ORDER_USDT:
            log.info(f"[{symbol}] بودجه آزاد کافی نیست ({free:.2f})")
            return
        res = place_order(symbol, "BUY", spend)
        qty = spend / price if res.get("dry") else float(res.get("executedQty", 0))
        entry = price if res.get("dry") else float(res.get("price", price))
        if qty <= 0:
            return
        pos[symbol] = {"buy": entry, "qty": qty}
        log.info(f"[{symbol}] 🟢 خرید {qty:.6f} در {entry} ({spend:.2f} تتر) | "
                 f"پوزیشن باز: {open_count()}")

def sell(symbol, price, reason):
    with lock:
        p = pos.pop(symbol, None)
        if not p:
            return
    try:
        res = place_order(symbol, "SELL", p["qty"] * price, qty=p["qty"])
        out = p["qty"] * price if res.get("dry") else p["qty"] * float(res.get("price", price))
    except Exception as e:
        log.error(f"[{symbol}] خطا در فروش ({reason}): {e} — دوباره تلاش می‌شود")
        with lock:
            pos[symbol] = p   # برگرداندن پوزیشن برای تلاش مجدد
        return
    diff = out - p["qty"] * p["buy"]
    with lock:
        realized[0] += diff
    log.info(f"[{symbol}] 🔴 فروش ({reason}) | نتیجه {diff:+.2f} | کل جلسه {realized[0]:+.2f}")

def market_worker(symbol):
    """خط مستقل هر بازار — تا پایان جلسه می‌چرخد"""
    log.info(f"[{symbol}] خط بازار فعال شد")
    while not kills.is_set():
        try:
            closes = get_klines_close(symbol)
            price  = closes[-1]
            f, s   = sma(closes, FAST), sma(closes, SLOW)
            p = pos.get(symbol)
            if p:
                if price >= p["buy"] * (1 + TP_PCT / 100):
                    sell(symbol, price, "حد سود 🎯")
                elif price <= p["buy"] * (1 - SL_PCT / 100):
                    sell(symbol, price, "حد ضرر 🛑")
                elif f < s:
                    sell(symbol, price, "کراس نزولی 🔻")
            elif f > s:
                buy(symbol, price)
        except Exception as e:
            log.error(f"[{symbol}] خطا: {e}")
        kills.wait(CHECK_EVERY)
    log.info(f"[{symbol}] خط بازار پایان یافت")

def close_all():
    """بستن همه پوزیشن‌های باز در پایان جلسه"""
    with lock:
        open_syms = list(pos.keys())
    for sym in open_syms:
        try:
            sell(sym, get_price(sym), "پایان جلسه ⏰")
        except Exception as e:
            log.error(f"[{sym}] خطا در بستن: {e}")

def main():
    log.info("=" * 55)
    log.info(f"جلسه همزمان {len(SYMBOLS)} بازار | مدت {RUN_MINUTES} دقیقه | "
             f"حالت: {'آزمایشی' if DRY_RUN else '🔥 پول واقعی'}")
    if not API_KEY or not API_SECRET:
        log.error("کلید API تنظیم نشده! Secrets گیت‌هاب را چک کن.")
        return
    try:
        log.info(f"اتصال به تبدیل موفق ✓ | تتر آزاد: {usdt_free():.2f}")
    except Exception as e:
        log.error(f"اتصال برقرار نشد: {e}")
        return
    try:
        info = public("/exchangeInfo")
        valid = {s["symbol"] for s in info.get("symbols", [])}
        ok_syms = [s for s in SYMBOLS if s in valid]
        for bad in [s for s in SYMBOLS if s not in valid]:
            log.warning(f"نماد {bad} در تبدیل نیست — حذف شد")
        if not ok_syms:
            log.error("هیچ نماد معتبری نماند!")
            return
        SYMBOLS[:] = ok_syms
        log.info(f"نمادهای تاییدشده: {SYMBOLS}")
    except Exception:
        log.warning("exchangeInfo در دسترس نبود؛ ادامه می‌دهم...")

    # راه‌اندازی همزمان همه خط‌های بازار
    threads = [threading.Thread(target=market_worker, args=(s,), daemon=True) for s in SYMBOLS]
    for t in threads: t.start()

    end_time = time.time() + RUN_MINUTES * 60
    while time.time() < end_time:
        time.sleep(30)
        with lock:
            log.info(f"... در حال اجرا | پوزیشن باز: {list(pos.keys()) or '—'} | "
                     f"سود/زیان جلسه: {realized[0]:+.2f} | "
                     f"{max(0, int((end_time - time.time()) / 60))} دقیقه مانده")

    kills.set()
    for t in threads: t.join(timeout=CHECK_EVERY)
    close_all()
    log.info("=" * 55)
    log.info(f"جلسه تمام شد | نتیجه نهایی: {realized[0]:+.2f} USDT | "
             f"پوزیشن باز مانده: {list(pos.keys()) or 'هیچ ✓'}")

if __name__ == "__main__":
    main()