#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_twse.py — 在 GitHub Actions 伺服器上執行，抓取證交所全市場收盤資料。

用法：
  python3 fetch_twse.py            # 不帶參數：抓「目前可取得的最新一個交易日」
  python3 fetch_twse.py 20260623   # 帶日期參數：抓指定那一天的歷史資料（西元或民國年皆可）

資料來源策略（不帶參數時）：
  證交所其實有兩套互相獨立的系統：
    1) openapi.twse.com.tw  — 開放資料平台，由批次匯出程式定期更新
    2) www.twse.com.tw      — 證交所網站原本的即時查詢系統
  這兩套系統各自維護，其中一套故障不會連帶影響另一套，但對外看起來
  就會出現「同一天，一邊有資料、一邊沒有」的情況。
  這裡兩邊都嘗試抓一次，比較回傳的交易日期，自動採用比較新的那一筆，
  這樣不管哪一邊先恢復，都能自動跟上，不需要人工判斷或介入。

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
# 省略 date 參數＝抓「目前已公告的最新交易日」，這是即時查詢系統自己的最新值
MI_INDEX_LATEST_URL = "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALLBUT0999"

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


def parse_mi_index_payload(payload):
    """把 MI_INDEX 回傳的原始表格，轉成跟 OpenAPI 一樣的欄位格式"""
    fields, data = find_table(payload)
    if not fields:
        raise RuntimeError("找不到個股收盤行情表（可能非交易日，或剛好還沒公布）")

    idx = {name: i for i, name in enumerate(fields)}

    def col(row, *names):
        for n in names:
            if n in idx:
                return row[idx[n]]
        return None

    stocks = []
    found_date = None
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
    return stocks


def extract_mi_index_date(payload):
    """
    從 MI_INDEX 回傳內容裡找出實際的交易日期。
    證交所 MI_INDEX 可能有以下幾種格式存放日期：
      1) payload["date"] = "1150630"（民國7碼）
      2) title字串裡有「115年06月30日」
      3) tables陣列或 fields數字 的標題
    三種都試，只要找到一個就回傳。
    """
    import re
    # 格式1：直接有 "date" 欄位，值為民國7碼
    date_raw = payload.get("date", "")
    if date_raw and re.match(r"^\d{7}$", str(date_raw)):
        return normalize_roc_date_to_iso(str(date_raw))

    # 格式2/3：掃所有字串值（包含 title, stat, 以及 tables 裡的 title）
    def scan_string(s):
        m = re.search(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", str(s))
        if m:
            roc_year, month, day = m.groups()
            year = int(roc_year) + 1911
            return f"{year:04d}-{int(month):02d}-{int(day):02d}"
        return None

    def deep_scan(obj, depth=0):
        if depth > 4:
            return None
        if isinstance(obj, str):
            return scan_string(obj)
        if isinstance(obj, list):
            for item in obj[:5]:  # 只看前幾個，避免太慢
                r = deep_scan(item, depth + 1)
                if r:
                    return r
        if isinstance(obj, dict):
            for k, v in obj.items():
                r = deep_scan(v, depth + 1)
                if r:
                    return r
        return None

    return deep_scan(payload)


def fetch_latest_via_mi_index():
    """不指定日期：抓即時查詢系統(MI_INDEX)目前公告的最新一天"""
    payload = http_get_json(MI_INDEX_LATEST_URL)
    stocks = parse_mi_index_payload(payload)
    date_iso = extract_mi_index_date(payload)
    if not date_iso:
        raise RuntimeError("MI_INDEX 回傳內容裡找不到交易日期")
    return stocks, date_iso


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
    stocks = parse_mi_index_payload(payload)

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
    print(f"OK，交易日 {date_iso}，共 {len(stocks)} 檔，來源 {source}，已寫入 data/{date_iso}.json"
          + ("（同時更新 data/latest.json）" if update_latest else "（未更動 data/latest.json）"))


def fetch_latest_best():
    """
    不指定日期時：兩個來源都嘗試抓一次，比較交易日期，自動採用比較新的那一筆。
    任一來源失敗都不中斷，只要有一邊成功就能繼續；兩邊都失敗才報錯。
    """
    candidates = []

    try:
        stocks, date_iso = fetch_latest_via_openapi()
        if date_iso:
            print(f"[OpenAPI] 抓到交易日 {date_iso}，共 {len(stocks)} 檔")
            candidates.append((date_iso, stocks, OPENAPI_URL))
        else:
            print("[OpenAPI] 回傳內容無法解析出交易日期，略過")
    except Exception as e:
        print(f"[OpenAPI] 抓取失敗：{e}")

    try:
        stocks, date_iso = fetch_latest_via_mi_index()
        if date_iso:
            print(f"[MI_INDEX] 抓到交易日 {date_iso}，共 {len(stocks)} 檔")
            candidates.append((date_iso, stocks, MI_INDEX_LATEST_URL))
        else:
            print("[MI_INDEX] 回傳內容無法解析出交易日期，略過")
    except Exception as e:
        print(f"[MI_INDEX] 抓取失敗：{e}")

    if not candidates:
        raise RuntimeError("OpenAPI 與 MI_INDEX 兩個來源都抓取失敗，本次無法更新")

    # 兩邊都成功的話，採用日期比較新的那一筆
    candidates.sort(key=lambda c: c[0], reverse=True)
    best_date, best_stocks, best_source = candidates[0]
    if len(candidates) > 1:
        print(f"兩個來源都有回應，採用較新的一筆：{best_date}（來源：{best_source}）")
    return best_stocks, best_date, best_source


def main():
    date_arg = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else None
    if date_arg:
        # 指定歷史日期：只回補那一天自己的檔案，絕對不要動 latest.json
        stocks, date_iso = fetch_by_date_via_mi_index(date_arg)
        save(stocks, date_iso, MI_INDEX_URL.format(date=date_arg), update_latest=False)
    else:
        # 沒指定日期：兩個來源都試，採用較新的一筆，這才是該更新 latest.json 的時候
        stocks, date_iso, source = fetch_latest_best()
        save(stocks, date_iso, source, update_latest=True)


if __name__ == "__main__":
    main()
