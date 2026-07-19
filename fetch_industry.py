#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_industry.py — 抓取證交所「上市證券國際證券辨識號碼一覽表」，
取得每檔股票的「產業別」，用來做資金輪動的族群自動分類。

這張表更新很慢（只有新股上市/產業重分類才會變動），不需要每天抓。
建議一週跑一次就夠（或手動跑一次也可以，之後很久才需要重跑）。

用法：
  python3 fetch_industry.py

輸出：
  data/industry_map.json
  格式：{ "1101": {"name": "台泥", "industry": "水泥工業"}, ... }
"""
import json
import os
import re
import ssl
import urllib.request
from html.parser import HTMLParser

URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE


class TableParser(HTMLParser):
    """把 <tr><td>...</td></tr> 表格解析成一列一列的文字陣列。"""
    def __init__(self):
        super().__init__()
        self.rows = []
        self._row = []
        self._cell = []
        self._in_td = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag == "td":
            self._in_td = True
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "td":
            self._in_td = False
            self._row.append("".join(self._cell).strip())
        elif tag == "tr":
            if self._row:
                self.rows.append(self._row)

    def handle_data(self, data):
        if self._in_td:
            self._cell.append(data)


def fetch_industry_map():
    req = urllib.request.Request(URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
        raw = resp.read()
    # 證交所這張表是 Big5(MS950) 編碼，不是 UTF-8，一定要指定編碼解碼
    html = raw.decode("big5", errors="ignore")

    parser = TableParser()
    parser.feed(html)

    result = {}
    for row in parser.rows:
        if len(row) != 7:
            continue
        code_name, isin, listed_date, market, industry, cfi, note = row
        if "\u3000" not in code_name:
            continue
        code, name = code_name.split("\u3000", 1)
        code = code.strip()
        name = name.strip()
        if not code or not industry:
            continue
        if market != "上市":
            continue
        # 股票代號通常是4碼數字（可能帶一碼英文），排除權證/牛熊證等衍生商品
        if not re.fullmatch(r"\d{4,6}[A-Z]?", code):
            continue
        result[code] = {"name": name, "industry": industry}
    return result


def main():
    data_dir = "data"
    os.makedirs(data_dir, exist_ok=True)
    industry_map = fetch_industry_map()
    if not industry_map:
        print("警告：沒有抓到任何產業別資料，可能是網站格式改變或暫時無法連線，不覆蓋舊檔。")
        return
    out_path = os.path.join(data_dir, "industry_map.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(industry_map, f, ensure_ascii=False)
    print(f"OK，共取得 {len(industry_map)} 檔上市股票的產業別，已寫入 {out_path}")


if __name__ == "__main__":
    main()
