"""
گزارش عملکرد واقعی ربات - بر اساس تاریخچه واقعی watchlist.json
این دقیق‌ترین نوع بک‌تسته چون سوگیری نگاه‌به‌گذشته نداره:
تصمیم‌ها همون لحظه که گرفته شدن ثبت شدن.
اجرا: python report_stats.py
"""

import json
import os

WATCHLIST_PATH = "watchlist.json"


def main():
    if not os.path.exists(WATCHLIST_PATH):
        print("فایل watchlist.json پیدا نشد.")
        return

    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        items = json.load(f)

    closed = [i for i in items if i.get("status") == "closed"]
    open_trades = [i for i in items if i.get("status") == "open"]
    waiting = [i for i in items if i.get("status") == "waiting_entry"]
    expired = [i for i in items if i.get("status") == "expired"]
    errors = [i for i in items if i.get("status") == "error"]

    print("=" * 60)
    print("گزارش کامل عملکرد ربات")
    print("=" * 60)
    print(f"کل سیگنال‌های ثبت‌شده: {len(items)}")
    print(f"  در انتظار پولبک: {len(waiting)}")
    print(f"  باز (منتظر SL/TP): {len(open_trades)}")
    print(f"  فرصت پولبک رد شد: {len(expired)}")
    print(f"  خطا/داده ناقص: {len(errors)}")
    print(f"  بسته‌شده (نتیجه قطعی): {len(closed)}")

    if not closed:
        print("\nهنوز هیچ معامله‌ای به نتیجه قطعی نرسیده.")
        return

    wins = [i for i in closed if i["outcome"] == "TAKE_PROFIT"]
    losses = [i for i in closed if i["outcome"] == "STOP_LOSS"]
    win_rate = len(wins) / len(closed) * 100
    avg_return = sum(i["return_pct"] for i in closed) / len(closed)

    print("\n" + "=" * 60)
    print("آمار معاملات بسته‌شده:")
    print("=" * 60)
    print(f"تعداد کل: {len(closed)}")
    print(f"برد (TAKE_PROFIT): {len(wins)}")
    print(f"باخت (STOP_LOSS): {len(losses)}")
    print(f"وین‌ریت: {win_rate:.1f}%")
    print(f"میانگین بازدهی هر معامله: {avg_return:.1f}%")

    print("\nجزئیات هر معامله:")
    for i in closed:
        icon = "🟢" if i["outcome"] == "TAKE_PROFIT" else "🔴"
        print(f"  {icon} {i['symbol']:15s} {i['outcome']:12s} {i['return_pct']:+.1f}%")


if __name__ == "__main__":
    main()
