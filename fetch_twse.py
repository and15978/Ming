#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_twse.py — 在 GitHub Actions 伺服器上執行，抓取證交所全市場收盤資料。

用法：
  python3 fetch_twse.py            # 不帶參數：抓「目前公告的最新一個交易日」
  python3 fetch_twse.py 20260623   # 帶日期參數：抓指定那一天的歷史資料（西元或民國年皆可）

輸出：
  - data/latest.json          永遠是「最新一次成功抓取」的資料（給網頁預設使用）
  - data/{YYYY-MM-DD}.json    依交易日存檔

附加資料（同時抓取，合併進每一檔股票的資料中）：
  - T86  三大法人買賣超（外資/投信/自營商）
  - TWTB4U 每日當日沖銷交易標的及統計（個股當日沖銷成交股數）
  - TWTB4U 借券賣出餘額（沿用舊邏輯，欄位關鍵字若對不上會是 None，不影響其他資料）
"""
import json
import os
import re
import ssl
import sys
import glob
import urllib.request
import datetime

OPENAPI_URL   = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
MI_INDEX_URL  = "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date}&type=ALLBUT0999"
T86_URL       = "https://www.twse.com.tw/exchangeReport/T86?response=json&date={date}&selectType=ALLBUT0999"
TWTB4U_URL    = "https://www.twse.com.tw/exchangeReport/TWTB4U?response=json&date={date}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE


def http_get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def to_int(s):
    """把含逗號的數字字串轉成整數，失敗回傳 None"""
    try:
        return int(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def normalize_roc_date_to_iso(date_str):
    digits = re.sub(r"\D", "", date_str)
    if len(digits) == 7:
        roc_year = int(digits[:3])
        month = digits[3:5]
        day = digits[5:7]
    else:
        return None
    return f"{roc_year + 1911:04d}-{month}-{day}"


# ── 三大法人買賣超 T86 ──────────────────────────────────────────────────────
def fetch_t86(twse_date):
    """回傳 {股票代號: {外資買賣超, 投信買賣超, 自營商買賣超, 三大法人買賣超}} （單位：股）"""
    try:
        url = T86_URL.format(date=twse_date)
        payload = http_get_json(url)
        if payload.get("stat") != "OK" or not payload.get("data"):
            print(f"  [T86] 無資料（{twse_date}）")
            return {}
        fields = payload["fields"]

        def find(keywords, excludes=()):
            for i, f in enumerate(fields):
                if all(k in f for k in keywords) and all(e not in f for e in excludes):
                    return i
            return None

        idx_code    = find(["代號"])
        idx_foreign = find(["外資", "買賣超"], excludes=["陸資"])
        idx_trust   = find(["投信", "買賣超"])
        idx_dealer  = find(["自營商", "買賣超"], excludes=["避險"])
        idx_total   = find(["三大法人"])

        result = {}
        for row in payload["data"]:
            code = str(row[idx_code]).strip() if idx_code is not None else None
            if not code:
                continue
            result[code] = {
                "ForeignNet":   to_int(row[idx_foreign]) if idx_foreign is not None else None,
                "TrustNet":     to_int(row[idx_trust])   if idx_trust   is not None else None,
                "DealerNet":    to_int(row[idx_dealer])  if idx_dealer  is not None else None,
                "TotalInstNet": to_int(row[idx_total])   if idx_total   is not None else None,
            }
        print(f"  [T86] 取得 {len(result)} 檔三大法人資料")
        return result
    except Exception as e:
        print(f"  [T86] 抓取失敗：{e}")
        return {}


# ── 每日當日沖銷交易標的及統計 TWTB4U（真正的當沖成交量）───────────────────
def fetch_daytrading(twse_date):
    """回傳 {股票代號: 當日沖銷成交股數}（買進+賣出成交股數相加，單位：股）"""
    try:
        url = TWTB4U_URL.format(date=twse_date)
        payload = http_get_json(url)
        if payload.get("stat") != "OK" or not payload.get("data"):
            print(f"  [當沖統計] 無資料（{twse_date}，可能是假日或尚未公布）")
            return {}
        fields = payload["fields"]

        def find(keywords, excludes=()):
            for i, f in enumerate(fields):
                if all(k in f for k in keywords) and all(e not in f for e in excludes):
                    return i
            return None

        idx_code   = find(["代號"])
        idx_buy    = find(["買進", "成交股數"])
        idx_sell   = find(["賣出", "成交股數"])
        idx_total  = find(["當日沖銷交易總成交股數"])

        result = {}
        for row in payload["data"]:
            code = str(row[idx_code]).strip() if idx_code is not None else None
            if not code:
                continue
            if idx_total is not None:
                vol = to_int(row[idx_total])
            else:
                buy = to_int(row[idx_buy]) if idx_buy is not None else None
                sell = to_int(row[idx_sell]) if idx_sell is not None else None
                vol = (buy or 0) + (sell or 0) if (buy is not None or sell is not None) else None
            if vol is not None:
                result[code] = vol
        print(f"  [當沖統計] 取得 {len(result)} 檔當日沖銷成交量資料")
        return result
    except Exception as e:
        print(f"  [當沖統計] 抓取失敗：{e}")
        return {}


# ── 借券餘額 TWTB4U ─────────────────────────────────────────────────────────
def fetch_twtb4u(twse_date):
    """回傳 {股票代號: 借券賣出餘額（張）}"""
    try:
        url = TWTB4U_URL.format(date=twse_date)
        payload = http_get_json(url)
        if payload.get("stat") != "OK" or not payload.get("data"):
            print(f"  [TWTB4U] 無資料（{twse_date}）")
            return {}
        fields = payload["fields"]

        def find(keywords):
            for i, f in enumerate(fields):
                if all(k in f for k in keywords):
                    return i
            return None

        idx_code    = find(["代號"])
        idx_balance = find(["借券", "餘額"])

        result = {}
        for row in payload["data"]:
            code = str(row[idx_code]).strip() if idx_code is not None else None
            if not code:
                continue
            bal = to_int(row[idx_balance]) if idx_balance is not None else None
            # 餘額單位為「股」，轉成「張」
            result[code] = round(bal / 1000) if bal is not None else None
        print(f"  [TWTB4U] 取得 {len(result)} 檔借券資料")
        return result
    except Exception as e:
        print(f"  [TWTB4U] 抓取失敗：{e}")
        return {}


# ── 讀取前一交易日借券餘額（用於計算借券增減）──────────────────────────────
def load_prev_short_balance(data_dir="data"):
    """從已存的日期檔裡找最新一天的借券餘額，回傳 {code: balance}"""
    try:
        files = sorted(glob.glob(os.path.join(data_dir, "????-??-??.json")))
        if not files:
            return {}
        with open(files[-1], encoding="utf-8") as f:
            prev = json.load(f)
        result = {}
        for s in prev.get("stocks", []):
            sb = s.get("ShortBalance")
            if sb is not None:
                result[s["Code"]] = sb
        print(f"  [借券比較] 讀取前一交易日存檔：{os.path.basename(files[-1])}，共 {len(result)} 檔")
        return result
    except Exception as e:
        print(f"  [借券比較] 讀取失敗：{e}")
        return {}


# ── 股票日線資料 ─────────────────────────────────────────────────────────────
def find_table(payload):
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


def fetch_latest_via_openapi():
    stocks = http_get_json(OPENAPI_URL)
    if not stocks:
        raise RuntimeError("OpenAPI 回傳空資料")
    date_iso = normalize_roc_date_to_iso(str(stocks[0].get("Date", "")))
    return stocks, date_iso


def fetch_by_date_via_mi_index(date_str):
    digits = re.sub(r"\D", "", date_str)
    if len(digits) == 8:
        twse_date = digits
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


def iso_to_twse_date(date_iso):
    """2026-06-23 → 20260623"""
    return date_iso.replace("-", "")


def merge_extra_data(stocks, date_iso, data_dir="data"):
    """抓 T86 / TWTB4U 並合併進 stocks（in-place）"""
    twse_date = iso_to_twse_date(date_iso)

    print(f"正在抓取三大法人資料（T86 {twse_date}）...")
    t86_map = fetch_t86(twse_date)

    print(f"正在抓取當日沖銷成交量資料（TWTB4U {twse_date}）...")
    daytrading_map = fetch_daytrading(twse_date)

    print(f"正在抓取借券餘額資料（TWTB4U {twse_date}）...")
    short_map = fetch_twtb4u(twse_date)
    prev_short = load_prev_short_balance(data_dir)

    for s in stocks:
        code = s["Code"]
        t = t86_map.get(code, {})
        s["ForeignNet"]   = t.get("ForeignNet")     # 外資買賣超（股，負=賣超）
        s["TrustNet"]     = t.get("TrustNet")        # 投信買賣超
        s["DealerNet"]    = t.get("DealerNet")       # 自營商買賣超
        s["TotalInstNet"] = t.get("TotalInstNet")    # 三大法人買賣超

        s["DayTradeVolume"] = daytrading_map.get(code)  # 當日沖銷成交股數（股）

        bal = short_map.get(code)
        s["ShortBalance"] = bal                      # 借券餘額（張）

        prev_bal = prev_short.get(code)
        if bal is not None and prev_bal is not None:
            s["ShortBalanceChange"] = bal - prev_bal  # 正=借券增加（張）
        else:
            s["ShortBalanceChange"] = None


def save(stocks, date_iso, source, update_latest, data_dir="data"):
    os.makedirs(data_dir, exist_ok=True)
    out = {
        "fetched_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "trade_date": date_iso,
        "source": source,
        "count": len(stocks),
        "stocks": stocks,
    }
    if update_latest:
        with open(os.path.join(data_dir, "latest.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    if date_iso:
        with open(os.path.join(data_dir, f"{date_iso}.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    print(f"OK，交易日 {date_iso}，共 {len(stocks)} 檔，已寫入 data/{date_iso}.json"
          + ("（同時更新 data/latest.json）" if update_latest else "（未更動 data/latest.json）"))


def main():
    date_arg = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else None
    if date_arg:
        stocks, date_iso = fetch_by_date_via_mi_index(date_arg)
        merge_extra_data(stocks, date_iso)
        save(stocks, date_iso, MI_INDEX_URL.format(date=date_arg), update_latest=False)
    else:
        stocks, date_iso = fetch_latest_via_openapi()
        merge_extra_data(stocks, date_iso)
        save(stocks, date_iso, OPENAPI_URL, update_latest=True)


if __name__ == "__main__":
    main()
