"""
ربات مدیریت سیگنال - اجرا می‌شه هر ۱۵ دقیقه توسط GitHub Actions
کاری که می‌کنه:
1. سیگنال‌های تازه در watchlist.json رو می‌خونه و سطوح ورود/حد ضرر/حد سود رو محاسبه می‌کنه
2. سیگنال‌های در انتظار پولبک رو چک می‌کنه، اگه وارد شدن پیام میده
3. معاملات باز رو چک می‌کنه، اگه به SL یا TP خوردن پیام میده و می‌بنده
"""

import os
import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

# ---------- تنظیمات ثابت سیستم (نتیجه بک‌تست) ----------
PULLBACK_FRAC = 0.65
ATR_MULT = 1.5
RISK_REWARD = 2
ATR_PERIOD = 14
ENTRY_WINDOW_DAYS = 10
SUPPORT_LOOKBACK_DAYS = 30

CAPITAL = float(os.environ.get("CAPITAL", "64"))
RISK_PCT = float(os.environ.get("RISK_PCT", "2"))
MAX_CONCURRENT_TRADES = int(os.environ.get("MAX_CONCURRENT_TRADES", "2"))

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
WATCHLIST_PATH = "watchlist.json"

# ساعتی که توی watchlist.json می‌نویسی به‌وقت ایرانه (UTC+3:30)؛
# داده صرافی‌ها به‌وقت جهانیه (UTC)، پس همیشه باید این افست کم بشه
IRAN_OFFSET = timedelta(hours=3, minutes=30)


def to_utc(iran_naive_dt):
    """تبدیل زمان محلی ایران (که توی watchlist.json می‌نویسی) به UTC برای تطبیق با داده صرافی"""
    return iran_naive_dt - IRAN_OFFSET


def to_iran(utc_naive_dt):
    """برعکس - برای نمایش زمان به کاربر در پیام‌های تلگرام"""
    return utc_naive_dt + IRAN_OFFSET


def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        print(f"خطا در ارسال پیام تلگرام: {e}")


def split_symbol(sym):
    return (sym[:-4], "USDT") if sym.endswith("USDT") else (sym, "USDT")


def fetch_mexc(symbol, interval, start_ms, end_ms):
    url = (f"https://api.mexc.com/api/v3/klines?symbol={symbol}&interval={interval}"
           f"&startTime={start_ms}&endTime={end_ms}&limit=1000")
    r = requests.get(url, timeout=15); r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError("داده خالی از MEXC")
    ncols = len(data[0])
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_vol"][:ncols]
    df = pd.DataFrame(data, columns=cols)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    return df[["open_time", "open", "high", "low", "close"]]


def fetch_kucoin(symbol, ktype, start_s, end_s):
    base, quote = split_symbol(symbol)
    kc_symbol = f"{base}-{quote}"
    url = (f"https://api.kucoin.com/api/v1/market/candles?type={ktype}"
           f"&symbol={kc_symbol}&startAt={start_s}&endAt={end_s}")
    r = requests.get(url, timeout=15); r.raise_for_status()
    payload = r.json()
    if payload.get("code") != "200000":
        raise ValueError(f"KuCoin خطا: {payload}")
    data = payload.get("data", [])
    if not data:
        raise ValueError("داده خالی از KuCoin")
    df = pd.DataFrame(data, columns=["open_time", "open", "close", "high", "low", "volume", "turnover"])
    df["open_time"] = pd.to_datetime(df["open_time"].astype(float), unit="s")
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    return df[["open_time", "open", "high", "low", "close"]].sort_values("open_time").reset_index(drop=True)


def get_data(symbol, interval_mexc, interval_kucoin, start_dt, end_dt):
    try:
        return fetch_mexc(symbol, interval_mexc, int(start_dt.timestamp() * 1000),
                           int(end_dt.timestamp() * 1000)), "MEXC"
    except Exception:
        try:
            return fetch_kucoin(symbol, interval_kucoin, int(start_dt.timestamp()),
                                 int(end_dt.timestamp())), "KuCoin"
        except Exception as e:
            return None, str(e)


def compute_atr(daily_df, period=ATR_PERIOD):
    high, low, close = daily_df["high"].values, daily_df["low"].values, daily_df["close"].values
    tr = np.zeros(len(daily_df))
    for i in range(1, len(daily_df)):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    s = pd.Series(tr).rolling(period).mean()
    return s.iloc[-1] if not np.isnan(s.iloc[-1]) else None


def calc_position(entry, stop):
    risk_amount = CAPITAL * (RISK_PCT / 100)
    risk_dist_pct = (entry - stop) / entry
    if risk_dist_pct <= 0:
        return None
    pos_size = risk_amount / risk_dist_pct
    units = pos_size / entry
    return round(pos_size, 2), round(units, 6), round(risk_amount, 2)


def load_watchlist():
    if not os.path.exists(WATCHLIST_PATH):
        return []
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_watchlist(items):
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def process_new_signal(item):
    """محاسبه اولیه سطوح برای سیگنالی که تازه اضافه شده"""
    symbol = item["symbol"]
    # ورودی watchlist.json به‌وقت ایرانه؛ برای تطبیق با API صرافی به UTC تبدیل می‌کنیم
    signal_time = to_utc(pd.Timestamp(item["signal_time"]))

    daily_df, src = get_data(symbol, "1d", "1day",
                              signal_time - timedelta(days=SUPPORT_LOOKBACK_DAYS + ATR_PERIOD + 5), signal_time)
    if daily_df is None or len(daily_df) < ATR_PERIOD + 2:
        item["status"] = "error"
        item["error"] = f"داده روزانه ناکافی: {src}"
        send_message(f"⚠️ {symbol}: نتونستم داده کافی پیدا کنم ({src})")
        return item

    atr = compute_atr(daily_df)
    if atr is None:
        item["status"] = "error"
        item["error"] = "ATR محاسبه نشد"
        return item

    # برای قیمت دقیق لحظه سیگنال، از نزدیک‌ترین کندل ساعتی استفاده می‌کنیم
    # (نه بسته‌شدن روزانه که می‌تونه چند ساعت با لحظه واقعی فاصله داشته باشه)
    hourly_window, src_h = get_data(symbol, "60m", "1hour",
                                     signal_time - timedelta(hours=3), signal_time + timedelta(hours=3))
    if hourly_window is not None and len(hourly_window) > 0:
        hourly_window["diff"] = (hourly_window["open_time"] - signal_time).abs()
        signal_price = float(hourly_window.loc[hourly_window["diff"].idxmin(), "close"])
    else:
        signal_price = float(daily_df.iloc[-1]["close"])  # fallback

    recent_low = float(daily_df.tail(SUPPORT_LOOKBACK_DAYS)["low"].min())

    entry_target = recent_low + (signal_price - recent_low) * PULLBACK_FRAC
    if entry_target >= signal_price:
        entry_target = signal_price * (1 - PULLBACK_FRAC * 0.3)

    stop_loss = entry_target - ATR_MULT * atr
    if stop_loss <= 0:
        stop_loss = entry_target * 0.8
    risk = entry_target - stop_loss
    take_profit = entry_target + RISK_REWARD * risk

    item["signal_price"] = round(signal_price, 8)
    item["entry_target"] = round(entry_target, 8)
    item["stop_loss"] = round(stop_loss, 8)
    item["take_profit"] = round(take_profit, 8)
    item["recent_low"] = round(recent_low, 8)
    item["status"] = "waiting_entry"

    send_message(
        f"📋 سیگنال جدید ثبت شد: <b>{symbol}</b>\n"
        f"قیمت لحظه سیگنال: {signal_price:.6g}\n"
        f"منتظر پولبک به: <b>{entry_target:.6g}</b>\n"
        f"حد ضرر (از اینجا): {stop_loss:.6g}\n"
        f"حد سود: {take_profit:.6g}\n"
        f"تا {ENTRY_WINDOW_DAYS} روز صبر می‌کنیم قیمت به نقطه ورود برسه."
    )
    return item


def check_waiting_entry(item, open_count):
    symbol = item["symbol"]
    signal_time = to_utc(pd.Timestamp(item["signal_time"]))
    entry_target = item["entry_target"]
    recent_low = item.get("recent_low")
    entry_deadline = signal_time + timedelta(days=ENTRY_WINDOW_DAYS)
    now = pd.Timestamp.now()

    if open_count >= MAX_CONCURRENT_TRADES:
        if now > entry_deadline:
            item["status"] = "expired"
            send_message(f"⏱️ {symbol}: فرصت پولبک تموم شد (ظرفیت پوزیشن هم‌زمان هم پر بود).")
        return item

    hourly_df, src = get_data(symbol, "60m", "1hour", signal_time, min(now, entry_deadline))
    if hourly_df is None or len(hourly_df) == 0:
        return item

    # به ترتیب زمانی کندل‌ها رو بررسی می‌کنیم:
    # - اگه قیمت زیر کف حمایت اصلی (recent_low) بسته بشه، یعنی ساختار حمایتی شکسته و سیگنال باطله
    # - اگه یه کندل با بسته‌شدن بالای سطح پولبک ببینیم (نه فقط لمس با سایه)، یعنی بازگشت واقعیه و وارد می‌شیم
    for _, row in hourly_df.iterrows():
        if recent_low is not None and row["close"] < recent_low:
            item["status"] = "invalidated"
            send_message(
                f"🚫 {symbol}: سیگنال باطل شد — قیمت زیر کف حمایت اصلی ({recent_low:.6g}) بسته شد. "
                f"وارد نمی‌شیم، این یه سقوط ادامه‌دار به‌نظر می‌رسه، نه پولبک سالم."
            )
            return item

        if row["low"] <= entry_target <= row["close"]:
            entry_time = row["open_time"]
            item["status"] = "open"
            item["entry_time"] = str(entry_time)

            pos = calc_position(entry_target, item["stop_loss"])
            pos_txt = ""
            if pos:
                pos_size, units, risk_amt = pos
                pos_txt = f"\n💰 حجم پیشنهادی: ${pos_size} (~{units} واحد) | ریسک: ${risk_amt}"

            send_message(
                f"✅ ورود تایید شد (با بسته‌شدن کندل بالای سطح): <b>{symbol}</b>\n"
                f"قیمت ورود: {entry_target:.6g}\n"
                f"حد ضرر: {item['stop_loss']:.6g} | حد سود: {item['take_profit']:.6g}"
                f"{pos_txt}"
            )
            return item

    if now > entry_deadline:
        item["status"] = "expired"
        send_message(f"⏱️ {symbol}: فرصت پولبک تموم شد، وارد نشدیم.")

    return item


def check_open_trade(item):
    symbol = item["symbol"]
    entry_time = pd.Timestamp(item["entry_time"])
    stop_loss, take_profit = item["stop_loss"], item["take_profit"]
    now = pd.Timestamp.now()

    hourly_df, src = get_data(symbol, "60m", "1hour", entry_time, now)
    if hourly_df is None or len(hourly_df) == 0:
        return item

    for _, row in hourly_df.iterrows():
        if row["low"] <= stop_loss:
            item["status"] = "closed"
            item["outcome"] = "STOP_LOSS"
            ret = (stop_loss - item["entry_target"]) / item["entry_target"] * 100
            item["return_pct"] = round(ret, 1)
            send_message(f"🔴 {symbol}: حد ضرر خورد. بازدهی: {ret:.1f}%")
            return item
        if row["high"] >= take_profit:
            item["status"] = "closed"
            item["outcome"] = "TAKE_PROFIT"
            ret = (take_profit - item["entry_target"]) / item["entry_target"] * 100
            item["return_pct"] = round(ret, 1)
            send_message(f"🟢 {symbol}: به حد سود رسید! بازدهی: +{ret:.1f}%")
            return item

    return item


def main():
    items = load_watchlist()
    changed = False

    # اول معاملات باز رو چک می‌کنیم (ممکنه یکی بسته بشه و جا باز کنه)
    for item in items:
        if item.get("status") == "open":
            try:
                items[items.index(item)] = check_open_trade(item)
                changed = True
            except Exception as e:
                print(f"خطا در پردازش {item.get('symbol')}: {e}")

    open_count = sum(1 for i in items if i.get("status") == "open")

    for item in items:
        status = item.get("status", "new")
        try:
            if status == "new":
                items[items.index(item)] = process_new_signal(item)
                changed = True
            elif status == "waiting_entry":
                items[items.index(item)] = check_waiting_entry(item, open_count)
                if items[items.index(item)].get("status") == "open":
                    open_count += 1
                changed = True
        except Exception as e:
            print(f"خطا در پردازش {item.get('symbol')}: {e}")

    if changed:
        save_watchlist(items)


if __name__ == "__main__":
    main()
