#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_twse.py — 在 GitHub Actions 伺服器上執行，抓取證交所全市場最新收盤資料，
存成 data/latest.json，給網頁工具讀取（網頁讀自己repo裡的靜態檔案，不會有CORS問題）。
"""
import json
import os
import ssl
import urllib.request
import datetime

URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}

# 證交所網站的SSL證書缺少 Subject Key Identifier 這個欄位，較新版本的Python
# (3.13+) 驗證較嚴格會直接擋下連線（CERTIFICATE_VERIFY_FAILED）。
# 這裡只針對這個網域放寬憑證驗證，不影響其他網路請求的安全性。
SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE


def main():
    req = urllib.request.Request(URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
        raw = resp.read().decode("utf-8")
    stocks = json.loads(raw)

    out = {
        "fetched_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "source": URL,
        "count": len(stocks),
        "stocks": stocks,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print(f"OK, 共 {len(stocks)} 檔，已寫入 data/latest.json")


if __name__ == "__main__":
    main()
