#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_twse.py — 在 GitHub Actions 伺服器上執行，抓取證交所全市場收盤資料。

用法：
  python3 fetch_twse.py            # 不帶參數：抓「目前公告的最新一個交易日」
  python3 fetch_twse.py 20260623   # 帶日期參數：抓指定那一天的歷史資料（西元或民國年皆可）

輸出：
  - data/latest.json          永遠是「最新一次成功抓取」的資料（給網頁預設使用）
  - data/{YYYY-MM-DD}.json    依交易日存檔，之後選同一天可以重複使用，不用每次重抓
"""
import json
import os
import re
import ssl
import sys
import urllib.request
import datetime

OPENAPI_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
MI_INDEX_URL = "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date}&type=ALLBUT0999"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}

# 證交所網站的SSL證書缺少 Subject Key Identifier，較新版本Python驗證較嚴格會直接擋下連線。
# 這裡只針對這個網域放寬憑證驗證，不影響其他網路請求的安全性。
SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE


def http_get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def normalize_roc_date_to_iso(date_str):
    """民國年日期(例如 1150624) 轉成西元 YYYY-MM-DD"""
    digits = re.sub(r"\D", "", date_str)
    if len(digits) == 7:
        roc_year = int(digits[:3])
        month = digits[3:5]
        day = digits[5:7]
    else:
        return None
    year = roc_year + 1911
    return f"{year:04d}-{month}-{day}"


def fetch_latest_via_openapi():
    """不指定日期：抓OpenAPI目前公告的最新一天"""
    stocks = http_get_json(OPENAPI_URL)
    if not stocks:
        raise RuntimeError("OpenAPI 回傳空資料")
    date_iso = normalize_roc_date_to_iso(str(stocks[0].get("Date", "")))
    return stocks, date_iso


def find_table(payload):
    """相容證交所 MI_INDEX 兩種可能的回傳格式（tables陣列 或 fields數字/data數字 成對）"""
    if isinstance(payload.get("tables"), list):
        for t in payload["tables"]:
            fields = t.get("fields") or []
            if any("證券代號" in f for f in fields) and any("收盤" in f for f in fields):
                return fields, t.get("data") or []
    for key in payload:
        m = re.match(r"^fields(\d*)$", key)
        if not m:
            continue
        suffix = m.group(1)
        fields = payload[key]
        data = payload.get("data" + suffix)
        if isinstance(fields, list) and isinstance(data, list):
            if any("證券代號" in f for f in fields) and any("收盤" in f for f in fields):
                return fields, data
    return None, None


def fetch_by_date_via_mi_index(date_str):
    """指定日期：抓舊版 MI_INDEX，並轉成跟OpenAPI一樣的欄位格式"""
    digits = re.sub(r"\D", "", date_str)
    if len(digits) == 8:
        twse_date = digits  # 西元 YYYYMMDD
    elif len(digits) == 7:
        roc_year = int(digits[:3])
        twse_date = f"{roc_year + 1911:04d}{digits[3:]}"
    else:
        raise ValueError(f"無法判斷的日期格式：{date_str}")

    url = MI_INDEX_URL.format(date=twse_date)
    payload = http_get_json(url)
    fields, data = find_table(payload)
    if not fields:
        raise RuntimeError(f"{twse_date} 找不到個股收盤行情表（可能非交易日）")

    idx = {name: i for i, name in enumerate(fields)}

    def col(row, *names):
        for n in names:
            if n in idx:
                return row[idx[n]]
        return None

    stocks = []
    for row in data:
        code = col(row, "證券代號")
        name = col(row, "證券名稱")
        close = col(row, "收盤價")
        high = col(row, "最高價")
        low = col(row, "最低價")
        vol = col(row, "成交股數")
        diff = col(row, "漲跌價差")
        sign_raw = str(col(row, "漲跌(+/-)", "漲跌(+-)") or "")

        if not code or close in (None, "", "--"):
            continue

        try:
            change = abs(float(str(diff).replace(",", "")))
        except (TypeError, ValueError):
            change = 0.0
        if "-" in sign_raw or "跌" in sign_raw:
            change = -change

        stocks.append({
            "Code": code,
            "Name": name,
            "OpeningPrice": col(row, "開盤價"),
            "HighestPrice": high,
            "LowestPrice": low,
            "ClosingPrice": close,
            "Change": f"{change:.4f}",
            "TradeVolume": vol,
        })

    y, m, d = twse_date[:4], twse_date[4:6], twse_date[6:8]
    return stocks, f"{y}-{m}-{d}"


def save(stocks, date_iso, source, update_latest):
    os.makedirs("data", exist_ok=True)
    out = {
        "fetched_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "trade_date": date_iso,
        "source": source,
        "count": len(stocks),
        "stocks": stocks,
    }
    if update_latest:
        with open("data/latest.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    if date_iso:
        with open(f"data/{date_iso}.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    print(f"OK，交易日 {date_iso}，共 {len(stocks)} 檔，已寫入 data/{date_iso}.json"
          + ("（同時更新 data/latest.json）" if update_latest else "（未更動 data/latest.json）"))


def main():
    date_arg = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else None
    if date_arg:
        # 指定歷史日期：只回補那一天自己的檔案，絕對不要動 latest.json
        stocks, date_iso = fetch_by_date_via_mi_index(date_arg)
        save(stocks, date_iso, MI_INDEX_URL.format(date=date_arg), update_latest=False)
    else:
        # 沒指定日期：抓「目前最新」，這才是該更新 latest.json 的時候
        stocks, date_iso = fetch_latest_via_openapi()
        save(stocks, date_iso, OPENAPI_URL, update_latest=True)


if __name__ == "__main__":
    main()
