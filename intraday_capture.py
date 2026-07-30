#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
intraday_capture.py — 開盤時間內（09:00~13:30 台北時間），每分鐘輪詢一次
證交所即時報價 API，自己把逐筆報價組成5分鐘K線（開高低收＋量），
存成 data/intraday/YYYY-MM-DD.json。

只追蹤 data/watchlist.json 裡的股票（由 fetch_twse.py 收盤後自動產生，
是STEP3/4/5/6篩選雷達選出來的候選股清單），不是全市場，避免被證交所擋。

設計重點：
- 這支程式是「一次啟動、內部自己跑到收盤」，不是靠 GitHub Actions 排程
  每分鐘各自觸發一次 —— 因為我們已經證實 GitHub 的排程對低流量 repo
  不夠準時，改成單一個 job 內部用迴圈輪詢，比較不會漏資料。
- 用的是 mis.twse.com.tw 的「即時報價」API，這是給網頁前端用的公開端點，
  沒有官方文件保證穩定性，如果證交所改版導致格式跑掉，這支程式會印出
  錯誤訊息然後略過那一輪，不會讓整個 workflow 失敗。
- 每一輪都會把目前為止組好的K線存一次檔（不是等到收盤才存），
  這樣就算 job 中途被中斷，也不會整天的資料都不見。

用法：
  python3 intraday_capture.py
"""
import json
import os
import ssl
import time
import datetime
import urllib.request
import urllib.error

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://mis.twse.com.tw/stock/index.jsp",
}
SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

POLL_INTERVAL_SEC = 60       # 每60秒問一次
MARKET_OPEN = (9, 0)         # 09:00
MARKET_CLOSE = (13, 30)      # 13:30，留一點緩衝到13:35才真正停
MARKET_CLOSE_BUFFER_MIN = 5
MAX_WAIT_FOR_OPEN_MIN = 20   # 如果太早觸發，最多等20分鐘到開盤，避免job空等太久


def taipei_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def load_watchlist(data_dir="data"):
    path = os.path.join(data_dir, "watchlist.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        stocks = raw.get("stocks") or []
        codes = [s["code"] for s in stocks if s.get("code")]
        return codes
    except Exception as e:
        print(f"[即時追蹤] 讀不到 data/watchlist.json（{e}），今天沒有股票可以追")
        return []


def fetch_quotes(codes):
    """一次查多檔即時報價，回傳 {code: {price, cum_vol, high, low, open, prev_close}}"""
    if not codes:
        return {}
    ex_ch = "|".join(f"tse_{c}.tw" for c in codes)
    url = MIS_URL.format(ex_ch=ex_ch)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[即時追蹤] 這一輪抓取失敗（{e}），略過")
        return {}

    out = {}
    for item in data.get("msgArray", []):
        code = item.get("c")
        if not code:
            continue
        z = item.get("z")  # 當前成交價，沒成交時是 "-"
        price = None
        try:
            if z and z != "-":
                price = float(z)
        except ValueError:
            price = None
        if price is None:
            # 成交價是空的，退而求其次用最高/最低/開盤價估一個參考值
            for fallback_key in ("h", "l", "o", "y"):
                fv = item.get(fallback_key)
                try:
                    if fv and fv != "-":
                        price = float(fv)
                        break
                except ValueError:
                    continue
        if price is None:
            continue
        try:
            cum_vol = float(item.get("v") or 0)
        except ValueError:
            cum_vol = 0.0
        out[code] = {"price": price, "cum_vol": cum_vol, "time": item.get("t")}
    return out


def bucket_label(dt):
    """把時間對齊到5分鐘區間的標籤，例如 09:00, 09:05, 09:10..."""
    minute = (dt.minute // 5) * 5
    return dt.replace(minute=minute, second=0, microsecond=0).strftime("%H:%M")


def main():
    data_dir = "data"
    intraday_dir = os.path.join(data_dir, "intraday")
    os.makedirs(intraday_dir, exist_ok=True)

    codes = load_watchlist(data_dir)
    if not codes:
        print("[即時追蹤] 追蹤清單是空的，今天不執行")
        return

    now = taipei_now()
    today_str = now.strftime("%Y-%m-%d")
    out_path = os.path.join(intraday_dir, f"{today_str}.json")

    open_dt = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    close_dt = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0) \
        + datetime.timedelta(minutes=MARKET_CLOSE_BUFFER_MIN)

    if now > close_dt:
        print(f"[即時追蹤] 現在台北時間 {now.strftime('%H:%M')}，已經過了收盤時間，今天不執行")
        return

    if now < open_dt:
        wait_min = (open_dt - now).total_seconds() / 60
        if wait_min > MAX_WAIT_FOR_OPEN_MIN:
            print(f"[即時追蹤] 現在離開盤還有 {wait_min:.0f} 分鐘，太早了，先不等，直接結束")
            return
        print(f"[即時追蹤] 現在離開盤還有 {wait_min:.0f} 分鐘，先等到開盤")
        time.sleep((open_dt - taipei_now()).total_seconds())

    print(f"[即時追蹤] 開始追蹤 {len(codes)} 檔股票：{', '.join(codes)}")

    # candles[code][bucket_label] = {open, high, low, close, vol}
    candles = {}
    prev_cum_vol = {}

    def save_progress():
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({"date": today_str, "watchlist": codes, "candles": candles}, f, ensure_ascii=False)
        except Exception as e:
            print(f"[即時追蹤] 存檔失敗（{e}）")

    poll_count = 0
    while True:
        now = taipei_now()
        if now > close_dt:
            print("[即時追蹤] 已經過收盤緩衝時間，結束追蹤")
            break

        quotes = fetch_quotes(codes)
        label = bucket_label(now)
        for code, q in quotes.items():
            price = q["price"]
            cum_vol = q["cum_vol"]
            prev = prev_cum_vol.get(code)
            interval_vol = max(0.0, cum_vol - prev) if prev is not None else 0.0
            prev_cum_vol[code] = cum_vol

            stock_candles = candles.setdefault(code, {})
            c = stock_candles.get(label)
            if c is None:
                stock_candles[label] = {"open": price, "high": price, "low": price, "close": price, "vol": interval_vol}
            else:
                c["high"] = max(c["high"], price)
                c["low"] = min(c["low"], price)
                c["close"] = price
                c["vol"] += interval_vol

        poll_count += 1
        if poll_count % 5 == 0:
            save_progress()
            print(f"[即時追蹤] 已輪詢 {poll_count} 次，時間 {now.strftime('%H:%M:%S')}，已存進度")

        # 睡到下一個整分鐘，避免累積誤差
        sleep_sec = POLL_INTERVAL_SEC - (taipei_now().second)
        if sleep_sec <= 0:
            sleep_sec = POLL_INTERVAL_SEC
        time.sleep(sleep_sec)

    save_progress()
    total_candles = sum(len(v) for v in candles.values())
    print(f"[即時追蹤] 完成，共 {len(candles)} 檔股票、{total_candles} 根5分K，已寫入 {out_path}")


if __name__ == "__main__":
    main()
