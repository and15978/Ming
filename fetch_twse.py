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


# ── 加權指數歷史（給 STEP1 大盤方向自動判斷用）──────────────────────────────
FMTQIK_URL = "https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date={date}"


def fetch_taiex_history(target_date_str):
    """
    抓大盤加權指數的每日收盤與成交值歷史。
    target_date_str: 西元年 YYYYMMDD

    TWSE FMTQIK 這支 API 一次會回傳「該年度至今」全部交易日，
    欄位依序是：日期(民國年/MM/DD)、成交股數、成交金額、成交筆數、
    發行量加權股價指數、漲跌點數。
    """
    def fetch_year(date_str):
        url = FMTQIK_URL.format(date=date_str)
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        rows = data.get("data") or []
        out = []
        for r in rows:
            try:
                y, m, d = r[0].strip().split("/")
                iso_date = f"{int(y)+1911:04d}-{int(m):02d}-{int(d):02d}"
                trade_value = float(str(r[2]).replace(",", ""))
                close = float(str(r[4]).replace(",", ""))
                chg_raw = str(r[5]).replace(",", "").replace("+", "").strip()
                chg = float(chg_raw) if chg_raw not in ("", "X", "--") else 0.0
                out.append({"date": iso_date, "close": close, "tradeValue": trade_value, "changePts": chg})
            except (ValueError, IndexError, ZeroDivisionError):
                continue
        return out

    records = fetch_year(target_date_str)
    if len(records) < 15:
        # 年初交易日不夠算10日均線，補抓去年最後幾筆
        prev_year_date = f"{int(target_date_str[:4]) - 1}1231"
        records = fetch_year(prev_year_date) + records
    return records[-30:]  # 只保留最近30筆，夠算5MA/10MA就好


MI_5MINS_HIST_URL = "https://www.twse.com.tw/indicesReport/MI_5MINS_HIST?response=json&date={date}"


def fetch_taiex_today_hl(date_str):
    """
    抓當天加權指數的開高低收（MI_5MINS_HIST，資料是逐月的，
    傳入某天日期會連當月其他天一起回來，這裡只挑出目標那天）。
    用 (最高+最低+收盤)/3 當大盤「近似均價」，接近但不等於官方VWAP
    （證交所沒有公布指數本身的成交量加權平均價，指數不是一個可成交商品）。
    """
    url = MI_5MINS_HIST_URL.format(date=date_str)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
        raw = resp.read()
    data = json.loads(raw.decode("utf-8"))
    rows = data.get("data") or []
    want_iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    for r in rows:
        try:
            y, m, d = r[0].strip().split("/")
            iso = f"{int(y)+1911:04d}-{int(m):02d}-{int(d):02d}"
            if iso == want_iso:
                return {
                    "open": float(str(r[1]).replace(",", "")),
                    "high": float(str(r[2]).replace(",", "")),
                    "low": float(str(r[3]).replace(",", "")),
                    "close": float(str(r[4]).replace(",", "")),
                }
        except (ValueError, IndexError):
            continue
    return None


def compute_group_momentum(data_dir="data", lookback_days=25):
    """
    最強族群/最弱族群 選股條件與統計（收盤後，逐檔個股先篩選，再依族群統計符合檔數）。

    每檔股票先檢查（1~8項全部符合才算數）：
    最強：成交值>3億、成交量>5日均量×2、收盤>5日均線、收盤>10日均線、
          5日均線>10日均線、收盤>=今日最高價×98%、近5日漲幅>8%、近10日漲幅>15%
    最弱：方向相反（收盤<5MA<10MA、收盤<=今日最低價×102%、近5日跌幅>8%、近10日跌幅>15%）
    加分項（不影響是否入選，只是額外標記）：近10日內創20日新高/新低

    再依 data/industry_map.json 把過關的股票分族群，統計每個族群有幾檔符合：
    3檔以上 → 強勢/弱勢族群；5檔以上 → 主流族群/資金持續流出族群
    """
    history, dates_used = _load_price_history(data_dir, lookback_days, min_days=11)
    if history is None:
        return None
    industry_map = _load_industry_map(data_dir)
    if not industry_map:
        print("[族群資金流向] 找不到 data/industry_map.json，略過")
        return None

    latest_date = dates_used[-1]
    strong_stocks = []  # [{code,name,industry,chg5,chg10,newHigh20}]
    weak_stocks = []

    for code, series in history.items():
        series = [r for r in series if r["close"] is not None]
        if not series or series[-1]["date"] != latest_date:
            continue
        industry = industry_map.get(code)
        if not industry:
            continue
        idx = len(series) - 1
        if idx < 10:
            continue

        today = series[idx]
        closes = [r["close"] for r in series]
        vols = [r["volume"] for r in series]

        value_approx = (today["volume"] or 0) * (today["close"] or 0)
        value_ok = value_approx > 300_000_000

        vol5avg = _sma(vols, 5, idx - 1) if idx - 1 >= 4 else None
        vol_ok = bool(vol5avg and today["volume"] and today["volume"] > vol5avg * 2)

        ma5 = _sma(closes, 5, idx)
        ma10 = _sma(closes, 10, idx)
        above_ma5 = bool(ma5 and today["close"] > ma5)
        below_ma5 = bool(ma5 and today["close"] < ma5)
        above_ma10 = bool(ma10 and today["close"] > ma10)
        below_ma10 = bool(ma10 and today["close"] < ma10)
        bull_align = bool(ma5 and ma10 and ma5 > ma10)
        bear_align = bool(ma5 and ma10 and ma5 < ma10)

        strong_close = bool(today["high"] and today["close"] >= today["high"] * 0.98)
        weak_close = bool(today["low"] and today["close"] <= today["low"] * 1.02)

        close5ago = series[idx - 5]["close"]
        chg5 = ((today["close"] - close5ago) / close5ago * 100) if close5ago else None
        close10ago = series[idx - 10]["close"]
        chg10 = ((today["close"] - close10ago) / close10ago * 100) if close10ago else None
        chg5_strong = bool(chg5 is not None and chg5 > 8)
        chg5_weak = bool(chg5 is not None and chg5 < -8)
        chg10_strong = bool(chg10 is not None and chg10 > 15)
        chg10_weak = bool(chg10 is not None and chg10 < -15)

        new_high20 = False
        new_low20 = False
        if idx >= 20:
            prior20_highs = [r["high"] for r in series[idx - 20:idx] if r["high"] is not None]
            prior20_lows = [r["low"] for r in series[idx - 20:idx] if r["low"] is not None]
            if prior20_highs:
                new_high20 = today["close"] > max(prior20_highs)
            if prior20_lows:
                new_low20 = today["close"] < min(prior20_lows)

        is_strong = value_ok and vol_ok and above_ma5 and above_ma10 and bull_align and strong_close and chg5_strong and chg10_strong
        is_weak = value_ok and vol_ok and below_ma5 and below_ma10 and bear_align and weak_close and chg5_weak and chg10_weak

        if is_strong:
            strong_stocks.append({"code": code, "name": today.get("name"), "industry": industry, "chg5": chg5, "chg10": chg10, "newHigh20": new_high20})
        if is_weak:
            weak_stocks.append({"code": code, "name": today.get("name"), "industry": industry, "chg5": chg5, "chg10": chg10, "newLow20": new_low20})

    def aggregate(stocks, chg5_key="chg5", chg10_key="chg10"):
        groups = {}
        for s in stocks:
            groups.setdefault(s["industry"], []).append(s)
        rows = []
        for industry, items in groups.items():
            count = len(items)
            avg5 = sum(x["chg5"] for x in items if x["chg5"] is not None) / count
            avg10 = sum(x["chg10"] for x in items if x["chg10"] is not None) / count
            if count >= 5:
                tier = "mainstream"
            elif count >= 3:
                tier = "confirmed"
            else:
                tier = "watch"
            rows.append({"industry": industry, "count": count, "avgChg5": avg5, "avgChg10": avg10, "tier": tier, "stocks": items})
        rows.sort(key=lambda r: (r["count"], abs(r["avgChg10"])), reverse=True)
        return rows

    return {
        "trade_date": latest_date,
        "history_days": len(dates_used),
        "strongCount": len(strong_stocks),
        "weakCount": len(weak_stocks),
        "strongGroups": aggregate(strong_stocks),
        "weakGroups": aggregate(weak_stocks),
        "note": "個股先逐檔篩選（成交值/量能/均線排列/收盤強弱位置/5日與10日漲跌幅全部符合），再依族群統計符合檔數；3檔以上算強勢/弱勢族群，5檔以上算主流族群/資金持續流出族群。",
    }


def save_group_momentum(data_dir="data"):
    """把族群資金流向統計存成 data/group_momentum.json 與帶日期快照，失敗不影響其他資料抓取。"""
    try:
        result = compute_group_momentum(data_dir=data_dir)
        if result is None:
            return
        out_path = os.path.join(data_dir, "group_momentum.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        dated_path = os.path.join(data_dir, f"group_momentum_{result['trade_date']}.json")
        with open(dated_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        print(f"[族群資金流向] 已寫入 {out_path} 與 {dated_path}，最強族群符合股 {result['strongCount']} 檔，最弱族群符合股 {result['weakCount']} 檔")
    except Exception as e:
        print(f"[族群資金流向] 計算失敗（{e}），略過（不影響個股資料）")


def save_market_history(date_iso, data_dir="data"):
    """把大盤指數歷史存成 data/market.json，失敗不影響個股資料抓取。"""
    try:
        history = fetch_taiex_history(date_iso.replace("-", ""))
        if not history:
            print("[大盤指數] 沒抓到任何資料，略過")
            return

        out_data = {"updated_trade_date": date_iso, "history": history}

        try:
            today_hl = fetch_taiex_today_hl(date_iso.replace("-", ""))
            if today_hl:
                out_data["today_hl"] = today_hl
                print(f"[大盤VWAP近似] 已取得 {date_iso} 開高低收，可自動估算VWAP")
            else:
                print(f"[大盤VWAP近似] 沒找到 {date_iso} 這天的高低點資料，VWAP條件維持手動")
        except Exception as e:
            print(f"[大盤VWAP近似] 抓取失敗（{e}），VWAP條件維持手動")

        out_path = os.path.join(data_dir, "market.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out_data, f, ensure_ascii=False)
        print(f"[大盤指數] 已寫入 {out_path}，共 {len(history)} 筆")
    except Exception as e:
        print(f"[大盤指數] 抓取失敗（{e}），略過（不影響個股資料）")


def _to_f(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _sma(vals, n, end_idx):
    """vals 是收盤價或量的list，算 index end_idx（含）往前數n筆的平均，資料不夠回傳None"""
    start = end_idx - n + 1
    if start < 0:
        return None
    window = vals[start:end_idx + 1]
    if any(v is None for v in window):
        return None
    return sum(window) / n


def _load_industry_map(data_dir="data"):
    """讀取 fetch_industry.py 產生的 data/industry_map.json，回傳 {code: industry}。找不到就回傳空dict。"""
    path = os.path.join(data_dir, "industry_map.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {code: info.get("industry") for code, info in raw.items() if info.get("industry")}
    except Exception:
        return {}


def _load_price_history(data_dir="data", lookback_days=25, min_days=6):
    """
    讀取 data/ 裡逐日累積的 YYYY-MM-DD.json，組成：
    history[code] = [{date, open, high, low, close, volume, name}, ...]（依日期由舊到新）
    回傳 (history, dates_used)，資料不足回傳 (None, [])
    """
    files = sorted(glob.glob(os.path.join(data_dir, "????-??-??.json")))
    files = files[-lookback_days:]
    if len(files) < min_days:
        print(f"[篩選雷達] 累積的歷史資料只有 {len(files)} 天，至少要{min_days}天才能算，先略過")
        return None, []

    history = {}
    dates_used = []
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                day = json.load(f)
        except Exception:
            continue
        d = day.get("trade_date")
        stocks = day.get("stocks") or []
        if not d or not stocks:
            continue
        dates_used.append(d)
        for s in stocks:
            code = s.get("Code")
            if not code:
                continue
            rec = {
                "date": d,
                "open": _to_f(s.get("OpeningPrice")),
                "high": _to_f(s.get("HighestPrice")),
                "low": _to_f(s.get("LowestPrice")),
                "close": _to_f(s.get("ClosingPrice")),
                "volume": _to_f(s.get("TradeVolume")),
                "instNet": _to_f(s.get("TotalInstNet")),
                "name": s.get("Name"),
            }
            history.setdefault(code, []).append(rec)

    if not dates_used:
        print("[篩選雷達] 沒有可用的歷史資料，略過")
        return None, []
    return history, dates_used


def compute_screener(data_dir="data", lookback_days=25):
    """
    明朝 A++ 隔日當沖雷達 v1.0：對每檔股票算 5MA/10MA 趨勢、K棒強弱、量能倍數、
    20日新高低/10日平台突破、3日累計漲跌排除條件。換手率因為缺流通股本資料，暫不計入判斷。
    只回傳「全部條件通過」的做多/放空候選（通常個位數到十幾檔），不是全市場列表。
    """
    history, dates_used = _load_price_history(data_dir, lookback_days, min_days=6)
    if history is None:
        return None

    latest_date = dates_used[-1]
    long_picks = []
    short_picks = []

    for code, series in history.items():
        series = [r for r in series if r["close"] is not None]
        if not series or series[-1]["date"] != latest_date:
            continue
        idx = len(series) - 1
        today = series[idx]
        closes = [r["close"] for r in series]
        vols = [r["volume"] for r in series]

        ma5 = _sma(closes, 5, idx)
        ma5_prev = _sma(closes, 5, idx - 1) if idx - 1 >= 4 else None
        ma10 = _sma(closes, 10, idx)

        trend_long = bool(ma5 and ma10 and ma5_prev and today["close"] > ma5 > ma10 and ma5 > ma5_prev)
        trend_short = bool(ma5 and ma10 and ma5_prev and today["close"] < ma5 < ma10 and ma5 < ma5_prev)
        if not (trend_long or trend_short):
            continue  # 第一關沒過，不用再算後面，省時間

        rng = None
        close_pos = None
        body_ratio = None
        if today["high"] is not None and today["low"] is not None and today["open"] is not None:
            rng = today["high"] - today["low"]
            if rng and rng > 0:
                close_pos = (today["close"] - today["low"]) / rng
                body_ratio = abs(today["close"] - today["open"]) / rng
        red_k = today["open"] is not None and today["close"] > today["open"]
        black_k = today["open"] is not None and today["close"] < today["open"]
        k_long = bool(rng and rng > 0 and red_k and close_pos >= 0.85 and body_ratio >= 0.6)
        k_short = bool(rng and rng > 0 and black_k and close_pos <= 0.15 and body_ratio >= 0.6)

        vol5avg = _sma(vols, 5, idx - 1) if idx - 1 >= 4 else None
        vol_ok = bool(vol5avg and today["volume"] and today["volume"] > vol5avg * 1.8)
        value_approx = (today["volume"] or 0) * (today["close"] or 0)
        value_ok = value_approx > 300_000_000

        high20 = max((r["high"] for r in series[max(0, idx - 20):idx] if r["high"] is not None), default=None) if idx >= 20 else None
        low20 = min((r["low"] for r in series[max(0, idx - 20):idx] if r["low"] is not None), default=None) if idx >= 20 else None
        close10max = max((r["close"] for r in series[max(0, idx - 10):idx]), default=None) if idx >= 10 else None
        close10min = min((r["close"] for r in series[max(0, idx - 10):idx]), default=None) if idx >= 10 else None

        breakout_long = bool((high20 is not None and today["close"] > high20) or (close10max is not None and today["close"] > close10max))
        breakout_short = bool((low20 is not None and today["close"] < low20) or (close10min is not None and today["close"] < close10min))

        pct_today = None
        if idx >= 1 and series[idx - 1]["close"]:
            pct_today = (today["close"] - series[idx - 1]["close"]) / series[idx - 1]["close"] * 100
        chg3 = None
        if idx >= 3 and series[idx - 3]["close"]:
            chg3 = (today["close"] - series[idx - 3]["close"]) / series[idx - 3]["close"] * 100

        exclude_long = bool((pct_today is not None and pct_today >= 9.5) or (chg3 is not None and chg3 > 20))
        exclude_short = bool((pct_today is not None and pct_today <= -9.5) or (chg3 is not None and chg3 < -20))

        support = _find_support_level(series, idx)

        base = {
            "costChart": _build_cost_chart(series, idx),
            "code": code, "name": today.get("name"),
            "prevClose": today["close"], "prevHigh": today["high"], "prevLow": today["low"],
            "todayOpen": today["open"], "pctToday": pct_today,
            "ma5": ma5, "ma10": ma10, "vol5avg": vol5avg,
            "volToday": today["volume"], "valueApprox": value_approx,
            "high20": high20, "low20": low20, "close10max": close10max, "close10min": close10min,
            "chg3": chg3, "historyDays": len(series),
            "supportLevel": support["level"] if support else None,
            "supportTouches": support["touches"] if support else None,
            "supportBroken": support["broken"] if support else None,
        }

        if trend_long and k_long and vol_ok and value_ok and breakout_long and not exclude_long:
            long_picks.append(base)
        if trend_short and k_short and vol_ok and value_ok and breakout_short and not exclude_short:
            short_picks.append(base)

    long_picks.sort(key=lambda x: (x["volToday"] or 0) / (x["vol5avg"] or 1), reverse=True)
    short_picks.sort(key=lambda x: (x["volToday"] or 0) / (x["vol5avg"] or 1), reverse=True)

    return {
        "trade_date": latest_date,
        "history_days": len(dates_used),
        "checked_count": len(history),
        "long": long_picks,
        "short": short_picks,
        "note": "換手率條件因缺流通股本資料，未計入判斷；突破條件為20日新高/低或10日收盤高低點近似判斷",
    }


def compute_reversal_screener(data_dir="data", lookback_days=65):
    """
    隔日做多/放空反轉雷達（收盤後）：找超跌/超漲後帶量反轉的股票。
    做多：近20日跌幅>20% 或 近10日跌幅>15%（符合任一即可）＋爆量＋收盤明顯高於當日低點＋收紅K＋高振幅
    放空：近20日漲幅>20% 或 近10日漲幅>15%（符合任一即可）＋爆量＋收盤明顯低於當日高點＋收黑K＋高振幅

    另外附帶KD/MACD當「動能確認」參考，不放進主要AND邏輯（怕篩得太少），
    只用來標警示：如果價格位置符合條件、但KD/MACD根本沒同步轉弱，代表動能還很強，
    放空容易軋空、做多可能還沒止跌，前端會顯示警示提醒。
    """
    history, dates_used = _load_price_history(data_dir, lookback_days, min_days=11)
    if history is None:
        return None

    latest_date = dates_used[-1]
    long_picks = []
    short_picks = []

    for code, series in history.items():
        series = [r for r in series if r["close"] is not None]
        if not series or series[-1]["date"] != latest_date:
            continue
        idx = len(series) - 1
        if idx < 10:
            continue  # 至少要有近10日資料
        today = series[idx]
        closes = [r["close"] for r in series]
        highs = [r["high"] for r in series]
        lows = [r["low"] for r in series]
        vols = [r["volume"] for r in series]
        prev_close = series[idx - 1]["close"] if idx >= 1 else None

        close20ago = series[idx - 20]["close"] if idx >= 20 else None
        chg20 = ((today["close"] - close20ago) / close20ago * 100) if close20ago else None
        close10ago = series[idx - 10]["close"]
        chg10 = ((today["close"] - close10ago) / close10ago * 100) if close10ago else None

        cond1_long = bool((chg20 is not None and chg20 <= -20) or (chg10 is not None and chg10 <= -15))
        cond1_short = bool((chg20 is not None and chg20 >= 20) or (chg10 is not None and chg10 >= 15))
        if not (cond1_long or cond1_short):
            continue

        vol5avg = _sma(vols, 5, idx - 1) if idx - 1 >= 4 else None
        vol_ok = bool(vol5avg and today["volume"] and today["volume"] > vol5avg * 1.8)

        long_close_strong = bool(today["low"] and today["close"] > today["low"] * 1.03)
        short_close_weak = bool(today["high"] and today["close"] < today["high"] * 0.97)

        red_k = bool(today["open"] is not None and today["close"] > today["open"])
        black_k = bool(today["open"] is not None and today["close"] < today["open"])

        amplitude = None
        if today["high"] is not None and today["low"] is not None and prev_close:
            amplitude = (today["high"] - today["low"]) / prev_close * 100
        amp_ok = bool(amplitude is not None and amplitude > 6)

        # ── 動能確認（僅供警示，不影響上面主要AND邏輯）──
        k_series, d_series = _compute_kd(highs, lows, closes)
        k_today, d_today = k_series[idx], d_series[idx]
        kd_ready = bool(k_today is not None and d_today is not None)
        # 放空但KD還在死亡交叉之前（K還在D上面）＝動能還沒轉弱，軋空風險
        short_squeeze_warning = bool(kd_ready and k_today >= d_today)
        # 做多但KD還在黃金交叉之前（K還在D下面）＝還沒止跌，續跌風險
        long_keepfall_warning = bool(kd_ready and k_today <= d_today)

        _, _, hist_series = _compute_macd(closes)
        macd_hist_today = hist_series[idx]
        macd_ready = macd_hist_today is not None
        # 放空但MACD柱狀圖還是紅的（正值）＝多方力道還沒消退
        short_macd_warning = bool(macd_ready and macd_hist_today > 0)
        # 做多但MACD柱狀圖還是綠的（負值）＝空方力道還沒消退
        long_macd_warning = bool(macd_ready and macd_hist_today < 0)

        # ── 嚴重風險檢查（比上面的動能警示更嚴重）──
        # 這是為了抓「結構性重挫」的股票：不是短期超跌，是長期崩跌中的技術性反彈，
        # 這種股票的KD/MACD訊號常常失真，一根反彈就轉正，但不代表主跌段結束。
        close60ago = series[idx - 60]["close"] if idx >= 60 else None
        chg60 = ((today["close"] - close60ago) / close60ago * 100) if close60ago else None
        deep_crash = bool(chg60 is not None and chg60 <= -40)
        deep_surge = bool(chg60 is not None and chg60 >= 100)

        window10 = series[max(0, idx - 9):idx + 1]
        limit_down_count = 0
        limit_up_count = 0
        for j in range(max(1, idx - 9), idx + 1):
            pc = series[j - 1]["close"]
            cc = series[j]["close"]
            if pc:
                daily_pct = (cc - pc) / pc * 100
                if daily_pct <= -9.5:
                    limit_down_count += 1
                elif daily_pct >= 9.5:
                    limit_up_count += 1

        k_low_stuck_count = 0
        k_high_stuck_count = 0
        for j in range(max(0, idx - 9), idx + 1):
            kv = k_series[j]
            if kv is None:
                continue
            if kv < 20:
                k_low_stuck_count += 1
            elif kv > 80:
                k_high_stuck_count += 1

        severe_long_risk = bool(deep_crash or limit_down_count >= 2 or k_low_stuck_count >= 5)
        severe_short_risk = bool(deep_surge or limit_up_count >= 2 or k_high_stuck_count >= 5)
        severe_long_reasons = []
        if deep_crash: severe_long_reasons.append(f"近60日累計{chg60:.0f}%，結構性重挫")
        if limit_down_count >= 2: severe_long_reasons.append(f"近10日跌停{limit_down_count}次")
        if k_low_stuck_count >= 5: severe_long_reasons.append(f"KD近10日有{k_low_stuck_count}天卡在20以下（低檔鈍化）")
        severe_short_reasons = []
        if deep_surge: severe_short_reasons.append(f"近60日累計+{chg60:.0f}%，長期強勢")
        if limit_up_count >= 2: severe_short_reasons.append(f"近10日漲停{limit_up_count}次")
        if k_high_stuck_count >= 5: severe_short_reasons.append(f"KD近10日有{k_high_stuck_count}天卡在80以上（高檔鈍化）")

        support = _find_support_level(series, idx)

        base = {
            "costChart": _build_cost_chart(series, idx),
            "code": code, "name": today.get("name"),
            "prevClose": today["close"], "prevHigh": today["high"], "prevLow": today["low"],
            "todayOpen": today["open"],
            "pctToday": ((today["close"] - prev_close) / prev_close * 100) if prev_close else None,
            "chg20": chg20, "chg10": chg10, "chg60": chg60,
            "vol5avg": vol5avg, "volToday": today["volume"],
            "amplitude": amplitude, "historyDays": len(series),
            "kToday": k_today, "dToday": d_today, "macdHistToday": macd_hist_today,
            "shortSqueezeWarning": short_squeeze_warning, "shortMacdWarning": short_macd_warning,
            "longKeepfallWarning": long_keepfall_warning, "longMacdWarning": long_macd_warning,
            "severeLongRisk": severe_long_risk, "severeLongReasons": severe_long_reasons,
            "severeShortRisk": severe_short_risk, "severeShortReasons": severe_short_reasons,
            "supportLevel": support["level"] if support else None,
            "supportTouches": support["touches"] if support else None,
            "supportBroken": support["broken"] if support else None,
        }

        if cond1_long and vol_ok and long_close_strong and red_k and amp_ok:
            long_picks.append(base)
        if cond1_short and vol_ok and short_close_weak and black_k and amp_ok:
            short_picks.append(base)

    long_picks.sort(key=lambda x: x["chg10"] if x["chg10"] is not None else 0)
    short_picks.sort(key=lambda x: -(x["chg10"] if x["chg10"] is not None else 0))

    return {
        "trade_date": latest_date,
        "history_days": len(dates_used),
        "checked_count": len(history),
        "long": long_picks,
        "short": short_picks,
        "note": "條件1為「近20日累計>20%」或「近10日累計>15%」符合任一即算通過；振幅=(當日高-當日低)/昨收",
    }


def _ema_series(values, span):
    """回傳跟values等長的EMA序列。前面暖身不足的位置是None，
    從第span個值開始，先用簡單移動平均當種子，之後才用EMA遞迴平滑。"""
    n = len(values)
    result = [None] * n
    if n < span:
        return result
    window = values[:span]
    if any(v is None for v in window):
        return result
    seed = sum(window) / span
    result[span - 1] = seed
    alpha = 2 / (span + 1)
    prev = seed
    for i in range(span, n):
        if values[i] is None:
            break
        prev = values[i] * alpha + prev * (1 - alpha)
        result[i] = prev
    return result


def _compute_macd(closes, fast=12, slow=26, signal_span=9):
    """回傳 (macd_line, signal_line, histogram)，三個list跟closes等長"""
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    macd_line = [(a - b) if (a is not None and b is not None) else None for a, b in zip(ema_fast, ema_slow)]

    valid_idx = [i for i, v in enumerate(macd_line) if v is not None]
    signal = [None] * len(closes)
    if len(valid_idx) >= signal_span:
        vals = [macd_line[i] for i in valid_idx]
        sig_vals = _ema_series(vals, signal_span)
        for j, i in enumerate(valid_idx):
            signal[i] = sig_vals[j]
    hist = [(m - s) if (m is not None and s is not None) else None for m, s in zip(macd_line, signal)]
    return macd_line, signal, hist


def _build_cost_chart(series, idx, price_days=30, vwma_window=20):
    """
    給前端畫「成本線」迷你圖用的資料。
    這裡的成本線是「20日成交量加權平均價(VWMA)」，是業界常見的近似做法，
    不是用真實籌碼分布反推的主力成本（那個需要更細的資料，我們沒有），
    前端顯示時要標註清楚這是近似值。
    """
    start = max(0, idx - price_days + 1)
    window = series[start:idx + 1]
    dates = [r["date"][5:] for r in window]  # 只留 MM-DD
    closes = [round(r["close"], 2) if r["close"] is not None else None for r in window]

    cost_line = []
    for i in range(len(window)):
        abs_i = start + i
        w_start = max(0, abs_i - vwma_window + 1)
        w = series[w_start:abs_i + 1]
        total_v = sum((r["volume"] or 0) for r in w)
        if total_v > 0:
            wsum = sum((r["close"] or 0) * (r["volume"] or 0) for r in w)
            cost_line.append(round(wsum / total_v, 2))
        else:
            cost_line.append(None)

    return {"dates": dates, "closes": closes, "costLine": cost_line}


def _find_support_level(series, idx, lookback=20, tolerance=0.015):
    """
    找「近期低點聚類」支撐位：把 lookback 天內相近的低點（誤差 tolerance 內）
    歸成同一群，同一群出現 2 次以上算「測試過」，且期間收盤沒有明顯跌破，
    才算有效支撐。回傳離目前收盤最近、測試次數最多的那個支撐價位。
    找不到就回傳 None。
    """
    start = max(0, idx - lookback)
    window = series[start:idx + 1]
    lows = [r["low"] for r in window if r["low"] is not None]
    if len(lows) < 2:
        return None

    sorted_lows = sorted(lows)
    clusters = []
    current = [sorted_lows[0]]
    for v in sorted_lows[1:]:
        if v <= current[0] * (1 + tolerance):
            current.append(v)
        else:
            clusters.append(current)
            current = [v]
    clusters.append(current)

    current_close = window[-1]["close"]
    candidates = []
    for c in clusters:
        if len(c) < 2:
            continue
        level = sum(c) / len(c)
        # 跌破判斷：期間有沒有收盤明顯低於這個支撐（給一點緩衝，不要太敏感）
        broken = any(
            r["close"] is not None and r["close"] < level * (1 - tolerance * 1.5)
            for r in window
        )
        candidates.append({"level": level, "touches": len(c), "broken": broken})

    if not candidates:
        return None
    valid = [c for c in candidates if not c["broken"]] or candidates
    valid.sort(key=lambda v: (-v["touches"], abs((current_close or 0) - v["level"])))
    best = valid[0]
    return {"level": round(best["level"], 2), "touches": best["touches"], "broken": best["broken"]}


def _compute_kd(highs, lows, closes, n=9):
    """標準KD(9,3,3)，回傳 (k_series, d_series)"""
    length = len(closes)
    rsv = [None] * length
    for i in range(length):
        if i >= n - 1:
            window_h = highs[i - n + 1:i + 1]
            window_l = lows[i - n + 1:i + 1]
            if any(v is None for v in window_h) or any(v is None for v in window_l) or closes[i] is None:
                continue
            hh, ll = max(window_h), min(window_l)
            rsv[i] = ((closes[i] - ll) / (hh - ll) * 100) if (hh - ll) > 0 else 50.0

    k = [None] * length
    d = [None] * length
    prev_k = prev_d = 50.0
    started = False
    for i in range(length):
        if rsv[i] is None:
            continue
        if not started:
            prev_k = prev_d = rsv[i]
            started = True
        else:
            prev_k = prev_k * 2 / 3 + rsv[i] * 1 / 3
            prev_d = prev_d * 2 / 3 + prev_k * 1 / 3
        k[i], d[i] = prev_k, prev_d
    return k, d


def compute_v2_screener(data_dir="data", lookback_days=60):
    """
    V2 隔日多空雷達（收盤後）：在V1反轉雷達的基礎上，多加KD跟MACD兩個技術指標。
    這兩個指標需要比較長的歷史（尤其MACD的26日EMA），資料累積不夠長時，
    早期會有股票因為算不出來被跳過，之後資料越多越準。
    """
    history, dates_used = _load_price_history(data_dir, lookback_days, min_days=21)
    if history is None:
        return None

    latest_date = dates_used[-1]
    long_picks = []
    short_picks = []

    for code, series in history.items():
        series = [r for r in series if r["close"] is not None]
        if not series or series[-1]["date"] != latest_date:
            continue
        idx = len(series) - 1
        if idx < 20:
            continue  # 至少要有近20日資料算跌幅/漲幅

        today = series[idx]
        closes = [r["close"] for r in series]
        highs = [r["high"] for r in series]
        lows = [r["low"] for r in series]
        vols = [r["volume"] for r in series]
        prev_close = series[idx - 1]["close"] if idx >= 1 else None

        close20ago = series[idx - 20]["close"]
        chg20 = ((today["close"] - close20ago) / close20ago * 100) if close20ago else None
        cond1_long = bool(chg20 is not None and chg20 <= -20)
        cond1_short = bool(chg20 is not None and chg20 >= 20)
        if not (cond1_long or cond1_short):
            continue

        vol10avg = _sma(vols, 10, idx - 1) if idx - 1 >= 9 else None
        vol_ok = bool(vol10avg and today["volume"] and today["volume"] > vol10avg * 2)

        red_k = bool(today["open"] is not None and today["close"] > today["open"])
        black_k = bool(today["open"] is not None and today["close"] < today["open"])

        low_support_ok = bool(today["low"] and today["close"] > today["low"] * 1.03)
        near_high_ok = bool(today["high"] and today["close"] > today["high"] * 0.98)
        near_low_pressure_ok = bool(today["high"] and today["close"] < today["high"] * 0.97)

        ma5 = _sma(closes, 5, idx)
        above_ma5 = bool(ma5 and today["close"] > ma5)
        below_ma5 = bool(ma5 and today["close"] < ma5)

        k_series, d_series = _compute_kd(highs, lows, closes)
        k_today, k_yday = k_series[idx], (k_series[idx - 1] if idx >= 1 else None)
        kd_up = bool(k_today is not None and k_yday is not None and k_today > k_yday)
        kd_down = bool(k_today is not None and k_yday is not None and k_today < k_yday)

        _, _, hist_series = _compute_macd(closes)
        hist_today, hist_yday = hist_series[idx], (hist_series[idx - 1] if idx >= 1 else None)
        macd_green_shrink = bool(hist_today is not None and hist_yday is not None and hist_yday < 0 and hist_today > hist_yday)
        macd_red_shrink = bool(hist_today is not None and hist_yday is not None and hist_yday > 0 and hist_today < hist_yday)

        amplitude = None
        if today["high"] is not None and today["low"] is not None and prev_close:
            amplitude = (today["high"] - today["low"]) / prev_close * 100
        amp_ok = bool(amplitude is not None and amplitude > 6)

        # ── 以下是可選的額外篩選條件，不影響上面主要5/9項AND邏輯，
        # 只是多算好附在候選股資料裡，前端可以讓使用者勾選要不要再篩一次 ──
        bband_mid = _sma(closes, 20, idx)  # 布林中軌通常是20日均線
        above_bband_mid = bool(bband_mid and today["close"] > bband_mid)
        below_bband_mid = bool(bband_mid and today["close"] < bband_mid)

        inst_today = today.get("instNet")
        inst_yday = series[idx - 1].get("instNet") if idx >= 1 else None
        inst_data_available = bool(inst_today is not None and inst_yday is not None)
        inst_streak_buy = bool(inst_data_available and inst_today > 0 and inst_yday > 0)
        inst_streak_sell = bool(inst_data_available and inst_today < 0 and inst_yday < 0)

        yday = series[idx - 1] if idx >= 1 else None
        yday_limit_locked = False
        if yday and yday["high"] is not None and yday["low"] is not None and yday["close"]:
            yday_range_ratio = (yday["high"] - yday["low"]) / yday["close"]
            yday_limit_locked = yday_range_ratio < 0.002  # 幾乎沒有振幅，當作鎖死判斷
        not_limit_locked_yday = (not yday_limit_locked) if yday else None

        base = {
            "costChart": _build_cost_chart(series, idx),
            "code": code, "name": today.get("name"),
            "prevClose": today["close"], "prevHigh": today["high"], "prevLow": today["low"],
            "pctToday": ((today["close"] - prev_close) / prev_close * 100) if prev_close else None,
            "chg20": chg20, "vol10avg": vol10avg, "volToday": today["volume"],
            "ma5": ma5, "kToday": k_today, "kYesterday": k_yday,
            "macdHistToday": hist_today, "macdHistYesterday": hist_yday,
            "amplitude": amplitude, "historyDays": len(series),
            "bbandMid": bband_mid, "aboveBbandMid": above_bband_mid, "belowBbandMid": below_bband_mid,
            "instDataAvailable": inst_data_available,
            "instStreakBuy": inst_streak_buy, "instStreakSell": inst_streak_sell,
            "notLimitLockedYesterday": not_limit_locked_yday,
        }

        if cond1_long and vol_ok and red_k and low_support_ok and near_high_ok and above_ma5 and kd_up and macd_green_shrink and amp_ok:
            long_picks.append(base)
        if cond1_short and vol_ok and black_k and near_low_pressure_ok and below_ma5 and kd_down and macd_red_shrink and amp_ok:
            short_picks.append(base)

    long_picks.sort(key=lambda x: x["chg20"] if x["chg20"] is not None else 0)
    short_picks.sort(key=lambda x: -(x["chg20"] if x["chg20"] is not None else 0))

    return {
        "trade_date": latest_date,
        "history_days": len(dates_used),
        "checked_count": len(history),
        "long": long_picks,
        "short": short_picks,
        "note": "V2版在V1反轉雷達基礎上多加KD(9,3,3)與MACD(12,26,9)判斷；MACD/KD需要較長歷史資料才準確，資料累積不足60天時精準度會隨天數增加而改善。",
    }


def compute_v5_screener(data_dir="data", lookback_days=60):
    """
    V5 隔日多空雷達（收盤後）第一層（必要條件，全部符合）：
    做多：收盤>BBAND中軌、收盤>5日均線、成交量>10日均量×2、收盤>今日最高價×98%、
          振幅>6%、成交金額>5億、KD黃金交叉
    放空：收盤<BBAND中軌、收盤<5日均線、成交量>10日均量×2、收盤<今日最低價×102%、
          振幅>6%、成交金額>5億、KD死亡交叉
    """
    history, dates_used = _load_price_history(data_dir, lookback_days, min_days=21)
    if history is None:
        return None

    latest_date = dates_used[-1]
    long_picks = []
    short_picks = []

    for code, series in history.items():
        series = [r for r in series if r["close"] is not None]
        if not series or series[-1]["date"] != latest_date:
            continue
        idx = len(series) - 1
        if idx < 20:
            continue

        today = series[idx]
        closes = [r["close"] for r in series]
        highs = [r["high"] for r in series]
        lows = [r["low"] for r in series]
        vols = [r["volume"] for r in series]
        prev_close = series[idx - 1]["close"] if idx >= 1 else None

        bband_mid = _sma(closes, 20, idx)
        ma5 = _sma(closes, 5, idx)
        above_bband = bool(bband_mid and today["close"] > bband_mid)
        below_bband = bool(bband_mid and today["close"] < bband_mid)
        above_ma5 = bool(ma5 and today["close"] > ma5)
        below_ma5 = bool(ma5 and today["close"] < ma5)

        vol10avg = _sma(vols, 10, idx - 1) if idx - 1 >= 9 else None
        vol_ok = bool(vol10avg and today["volume"] and today["volume"] > vol10avg * 2)

        near_high_ok = bool(today["high"] and today["close"] > today["high"] * 0.98)
        near_low_ok = bool(today["low"] and today["close"] < today["low"] * 1.02)

        amplitude = None
        if today["high"] is not None and today["low"] is not None and prev_close:
            amplitude = (today["high"] - today["low"]) / prev_close * 100
        amp_ok = bool(amplitude is not None and amplitude > 6)

        value_approx = (today["volume"] or 0) * (today["close"] or 0)
        value_ok = value_approx > 500_000_000

        k_series, d_series = _compute_kd(highs, lows, closes)
        k_today, d_today = k_series[idx], d_series[idx]
        k_yday, d_yday = (k_series[idx - 1], d_series[idx - 1]) if idx >= 1 else (None, None)
        kd_ready = all(v is not None for v in [k_today, d_today, k_yday, d_yday])
        kd_golden = bool(kd_ready and k_yday <= d_yday and k_today > d_today)
        kd_death = bool(kd_ready and k_yday >= d_yday and k_today < d_today)

        # ── 以下是可選的額外篩選條件，不影響上面第一層AND邏輯，
        # 只是多算好附在候選股資料裡，前端讓使用者勾選要不要再篩一次 ──
        macd_line, _, hist_series = _compute_macd(closes)
        dif_today, dif_yday = macd_line[idx], (macd_line[idx - 1] if idx >= 1 else None)
        hist_today, hist_yday = hist_series[idx], (hist_series[idx - 1] if idx >= 1 else None)
        macd_ready = dif_today is not None and dif_yday is not None
        dif_up = bool(macd_ready and dif_today > dif_yday)
        dif_down = bool(macd_ready and dif_today < dif_yday)
        green_shrink = bool(hist_today is not None and hist_yday is not None and hist_yday < 0 and hist_today > hist_yday)
        red_shrink = bool(hist_today is not None and hist_yday is not None and hist_yday > 0 and hist_today < hist_yday)
        macd_bull_ok = bool(green_shrink or dif_up)
        macd_bear_ok = bool(red_shrink or dif_down)

        vol_yday = series[idx - 1]["volume"] if idx >= 1 else None
        vol_up_dod = bool(vol_yday and today["volume"] and today["volume"] > vol_yday)

        prior5_highs = [r["high"] for r in series[max(0, idx - 5):idx] if r["high"] is not None]
        prior5_lows = [r["low"] for r in series[max(0, idx - 5):idx] if r["low"] is not None]
        high5 = max(prior5_highs) if len(prior5_highs) >= 5 else None
        low5 = min(prior5_lows) if len(prior5_lows) >= 5 else None
        breakout5_high = bool(high5 is not None and today["close"] > high5)
        breakout5_low = bool(low5 is not None and today["close"] < low5)

        base = {
            "costChart": _build_cost_chart(series, idx),
            "code": code, "name": today.get("name"),
            "prevClose": today["close"], "prevHigh": today["high"], "prevLow": today["low"],
            "pctToday": ((today["close"] - prev_close) / prev_close * 100) if prev_close else None,
            "bbandMid": bband_mid, "ma5": ma5,
            "vol10avg": vol10avg, "volToday": today["volume"], "valueApprox": value_approx,
            "amplitude": amplitude,
            "kToday": k_today, "dToday": d_today, "kYesterday": k_yday, "dYesterday": d_yday,
            "historyDays": len(series),
            "macdBullOk": macd_bull_ok, "macdBearOk": macd_bear_ok,
            "volUpDod": vol_up_dod,
            "high5": high5, "low5": low5,
            "breakout5High": breakout5_high, "breakout5Low": breakout5_low,
        }

        if above_bband and above_ma5 and vol_ok and near_high_ok and amp_ok and value_ok and kd_golden:
            long_picks.append(base)
        if below_bband and below_ma5 and vol_ok and near_low_ok and amp_ok and value_ok and kd_death:
            short_picks.append(base)

    long_picks.sort(key=lambda x: (x["volToday"] or 0) / (x["vol10avg"] or 1), reverse=True)
    short_picks.sort(key=lambda x: (x["volToday"] or 0) / (x["vol10avg"] or 1), reverse=True)

    return {
        "trade_date": latest_date,
        "history_days": len(dates_used),
        "checked_count": len(history),
        "long": long_picks,
        "short": short_picks,
        "note": "V5第一層必要條件（7項全符合）：BBAND中軌位置、5日均線位置、10日均量2倍爆量、收盤貼近當日高/低、振幅>6%、成交金額>5億、KD黃金/死亡交叉。",
    }


def compute_v4_screener(data_dir="data", lookback_days=60):
    """
    隔日當沖策略 V4.0（收盤後篩選）：
    第1層 核心條件（全部必須符合）：
      做多：收盤>BBAND中軌、收盤>5日均線、成交量>5日均量×1.3、MACD柱狀圖增加、收盤>開盤
      放空：方向相反
    第2層 加分條件（6項符合3項以上）：收盤位置、KD交叉、BBAND中軌方向、振幅4~8%、
      影線<實體50%、量不能爆量(<5日均量×3)
    第3層 避開洗盤（符合任一項就直接排除）：振幅>10%、爆量>5日均量×3、
      上下影線合計>實體、近3日平均振幅>8%、收盤卡在當日中間48~52%
    第4層（隔日開盤5分K確認）跟後面的進出場建議，需要盤中資料或屬於執行面，
    不在這裡計算，只在前端顯示參考文字。
    """
    history, dates_used = _load_price_history(data_dir, lookback_days, min_days=21)
    if history is None:
        return None
    industry_map = _load_industry_map(data_dir)

    latest_date = dates_used[-1]
    long_picks = []
    short_picks = []

    for code, series in history.items():
        series = [r for r in series if r["close"] is not None]
        if not series or series[-1]["date"] != latest_date:
            continue
        idx = len(series) - 1
        if idx < 20:
            continue

        today = series[idx]
        closes = [r["close"] for r in series]
        highs = [r["high"] for r in series]
        lows = [r["low"] for r in series]
        vols = [r["volume"] for r in series]
        prev_close = series[idx - 1]["close"] if idx >= 1 else None

        bband_mid = _sma(closes, 20, idx)
        bband_mid_yday = _sma(closes, 20, idx - 1) if idx - 1 >= 19 else None
        ma5 = _sma(closes, 5, idx)
        vol5avg = _sma(vols, 5, idx - 1) if idx - 1 >= 4 else None

        above_bband = bool(bband_mid and today["close"] > bband_mid)
        below_bband = bool(bband_mid and today["close"] < bband_mid)
        above_ma5 = bool(ma5 and today["close"] > ma5)
        below_ma5 = bool(ma5 and today["close"] < ma5)
        vol_ok = bool(vol5avg and today["volume"] and today["volume"] > vol5avg * 1.3)

        _, _, hist_series = _compute_macd(closes)
        hist_today, hist_yday = hist_series[idx], (hist_series[idx - 1] if idx >= 1 else None)
        macd_ready = hist_today is not None and hist_yday is not None
        macd_up = bool(macd_ready and hist_today > hist_yday)   # 紅柱擴大或綠柱縮短＝值變大
        macd_down = bool(macd_ready and hist_today < hist_yday)  # 綠柱擴大或紅柱縮短＝值變小

        red_k = bool(today["open"] is not None and today["close"] > today["open"])
        black_k = bool(today["open"] is not None and today["close"] < today["open"])

        core_long = above_bband and above_ma5 and vol_ok and macd_up and red_k
        core_short = below_bband and below_ma5 and vol_ok and macd_down and black_k
        if not (core_long or core_short):
            continue

        # ── 第2層 加分條件 ──
        rng = None
        body = None
        upper_shadow = None
        lower_shadow = None
        close_pos = None
        if today["high"] is not None and today["low"] is not None and today["open"] is not None:
            rng = today["high"] - today["low"]
            body = abs(today["close"] - today["open"])
            upper_shadow = today["high"] - max(today["open"], today["close"])
            lower_shadow = min(today["open"], today["close"]) - today["low"]
            if rng > 0:
                close_pos = (today["close"] - today["low"]) / rng

        amplitude = None
        if rng is not None and prev_close:
            amplitude = rng / prev_close * 100

        k_series, d_series = _compute_kd(highs, lows, closes)
        k_today, d_today = k_series[idx], d_series[idx]

        near_high = bool(today["high"] and today["close"] >= today["high"] * 0.98)
        near_low = bool(today["low"] and today["close"] <= today["low"] * 1.02)
        kd_up = bool(k_today is not None and d_today is not None and k_today > d_today)
        kd_down = bool(k_today is not None and d_today is not None and k_today < d_today)
        bband_rising = bool(bband_mid and bband_mid_yday and bband_mid > bband_mid_yday)
        bband_falling = bool(bband_mid and bband_mid_yday and bband_mid < bband_mid_yday)
        amp_in_range = bool(amplitude is not None and 4 <= amplitude <= 8)
        upper_shadow_small = bool(body is not None and upper_shadow is not None and body > 0 and upper_shadow < body * 0.5)
        lower_shadow_small = bool(body is not None and lower_shadow is not None and body > 0 and lower_shadow < body * 0.5)
        not_overheated_vol = bool(vol5avg and today["volume"] and today["volume"] < vol5avg * 3)

        bonus_long_count = sum([near_high, kd_up, bband_rising, amp_in_range, upper_shadow_small, not_overheated_vol])
        bonus_short_count = sum([near_low, kd_down, bband_falling, amp_in_range, lower_shadow_small, not_overheated_vol])

        # ── 第3層 避開洗盤（任一項觸發就直接排除，不分方向）──
        chop_amp_too_big = bool(amplitude is not None and amplitude > 10)
        chop_overvolume = bool(vol5avg and today["volume"] and today["volume"] > vol5avg * 3)
        chop_shadow_too_long = bool(body is not None and upper_shadow is not None and lower_shadow is not None and (upper_shadow + lower_shadow) > body)
        chop_recent3_amp = None
        if idx >= 2:
            amps3 = []
            for j in range(idx - 2, idx + 1):
                r = series[j]
                pc = series[j - 1]["close"] if j >= 1 else None
                if r["high"] is not None and r["low"] is not None and pc:
                    amps3.append((r["high"] - r["low"]) / pc * 100)
            if len(amps3) == 3:
                chop_recent3_amp = sum(amps3) / 3
        chop_recent3_too_big = bool(chop_recent3_amp is not None and chop_recent3_amp > 8)
        chop_middle_position = bool(close_pos is not None and 0.48 <= close_pos <= 0.52)

        chop_reasons = []
        if chop_amp_too_big: chop_reasons.append("當日振幅>10%")
        if chop_overvolume: chop_reasons.append("成交量異常爆量(>5日均量×3)")
        if chop_shadow_too_long: chop_reasons.append("上下影線合計>實體")
        if chop_recent3_too_big: chop_reasons.append("近3日平均振幅>8%")
        if chop_middle_position: chop_reasons.append("收盤卡在當日中間區(48~52%)")
        is_chop = bool(chop_reasons)

        support = _find_support_level(series, idx)

        base = {
            "costChart": _build_cost_chart(series, idx),
            "code": code, "name": today.get("name"),
            "industry": industry_map.get(code),
            "prevClose": today["close"], "prevHigh": today["high"], "prevLow": today["low"],
            "todayOpen": today["open"],
            "pctToday": ((today["close"] - prev_close) / prev_close * 100) if prev_close else None,
            "bbandMid": bband_mid, "ma5": ma5, "vol5avg": vol5avg, "volToday": today["volume"],
            "amplitude": amplitude, "kToday": k_today, "dToday": d_today,
            "macdHistToday": hist_today, "macdHistYesterday": hist_yday,
            "historyDays": len(series), "chopReasons": chop_reasons,
            "supportLevel": support["level"] if support else None,
            "supportTouches": support["touches"] if support else None,
            "supportBroken": support["broken"] if support else None,
        }

        if core_long and bonus_long_count >= 3 and not is_chop:
            entry = dict(base)
            entry["bonusCount"] = bonus_long_count
            long_picks.append(entry)
        if core_short and bonus_short_count >= 3 and not is_chop:
            entry = dict(base)
            entry["bonusCount"] = bonus_short_count
            short_picks.append(entry)

    long_picks.sort(key=lambda x: x["bonusCount"], reverse=True)
    short_picks.sort(key=lambda x: x["bonusCount"], reverse=True)

    return {
        "trade_date": latest_date,
        "history_days": len(dates_used),
        "checked_count": len(history),
        "long": long_picks,
        "short": short_picks,
        "note": "第1層核心條件全符合＋第2層加分條件6項符合3項以上＋第3層避開洗盤（任一項觸發即排除）。第4層隔日開盤5分K確認、進出場建議屬於執行面，前端只顯示參考，不參與篩選。",
    }


def save_v4_screener(data_dir="data"):
    """把V4當沖策略結果存成 data/v4_screener.json 與帶日期快照，失敗不影響其他資料抓取。"""
    try:
        result = compute_v4_screener(data_dir=data_dir)
        if result is None:
            return None
        out_path = os.path.join(data_dir, "v4_screener.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        dated_path = os.path.join(data_dir, f"v4_screener_{result['trade_date']}.json")
        with open(dated_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        print(f"[V4策略] 已寫入 {out_path} 與 {dated_path}，做多候選 {len(result['long'])} 檔，放空候選 {len(result['short'])} 檔")
        return result
    except Exception as e:
        print(f"[V4策略] 計算失敗（{e}），略過（不影響個股資料）")
        return None


def save_v5_screener(data_dir="data"):
    """把V5雷達結果存成 data/v5_screener.json 與帶日期快照，失敗不影響其他資料抓取。"""
    try:
        result = compute_v5_screener(data_dir=data_dir)
        if result is None:
            return
        out_path = os.path.join(data_dir, "v5_screener.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        dated_path = os.path.join(data_dir, f"v5_screener_{result['trade_date']}.json")
        with open(dated_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        print(f"[V5雷達] 已寫入 {out_path} 與 {dated_path}，做多候選 {len(result['long'])} 檔，放空候選 {len(result['short'])} 檔")
        return result
    except Exception as e:
        print(f"[V5雷達] 計算失敗（{e}），略過（不影響個股資料）")
        return None


def save_v2_screener(data_dir="data"):
    """把V2雷達結果存成 data/v2_screener.json 與帶日期快照，失敗不影響其他資料抓取。"""
    try:
        result = compute_v2_screener(data_dir=data_dir)
        if result is None:
            return
        out_path = os.path.join(data_dir, "v2_screener.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        dated_path = os.path.join(data_dir, f"v2_screener_{result['trade_date']}.json")
        with open(dated_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        print(f"[V2雷達] 已寫入 {out_path} 與 {dated_path}，做多候選 {len(result['long'])} 檔，放空候選 {len(result['short'])} 檔")
        return result
    except Exception as e:
        print(f"[V2雷達] 計算失敗（{e}），略過（不影響個股資料）")
        return None


def save_reversal_screener(data_dir="data"):
    """把反轉雷達結果存成 data/reversal_screener.json 與帶日期快照，失敗不影響其他資料抓取。"""
    try:
        result = compute_reversal_screener(data_dir=data_dir)
        if result is None:
            return
        out_path = os.path.join(data_dir, "reversal_screener.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        dated_path = os.path.join(data_dir, f"reversal_screener_{result['trade_date']}.json")
        with open(dated_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        print(f"[反轉雷達] 已寫入 {out_path} 與 {dated_path}，做多候選 {len(result['long'])} 檔，放空候選 {len(result['short'])} 檔")
        return result
    except Exception as e:
        print(f"[反轉雷達] 計算失敗（{e}），略過（不影響個股資料）")
        return None


def save_screener(data_dir="data"):
    """把明朝A++篩選結果存成 data/screener.json（最新一筆）
    以及 data/screener_YYYY-MM-DD.json（帶日期，供之後回顧選日期用）。
    失敗不影響其他資料抓取。"""
    try:
        result = compute_screener(data_dir=data_dir)
        if result is None:
            return
        out_path = os.path.join(data_dir, "screener.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        dated_path = os.path.join(data_dir, f"screener_{result['trade_date']}.json")
        with open(dated_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        print(f"[篩選雷達] 已寫入 {out_path} 與 {dated_path}，做多候選 {len(result['long'])} 檔，放空候選 {len(result['short'])} 檔（用了 {result['history_days']} 天歷史資料）")
        return result
    except Exception as e:
        print(f"[篩選雷達] 計算失敗（{e}），略過（不影響個股資料）")
        return None


def save_watchlist(screener_results, data_dir="data", max_size=50):
    """
    把當天各雷達（STEP3/4/5/6）選出來的候選股代號聯集起來，存成
    data/watchlist.json，供隔天開盤的即時5分K追蹤程式（intraday_capture.py）
    知道要盯哪些股票，不用追全市場。
    """
    try:
        codes = {}
        for result in screener_results:
            if not result:
                continue
            for side in ("long", "short"):
                for item in result.get(side, []):
                    code = item.get("code")
                    name = item.get("name")
                    if code and code not in codes:
                        codes[code] = name

        watchlist = [{"code": c, "name": n} for c, n in codes.items()][:max_size]
        out_path = os.path.join(data_dir, "watchlist.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"generated_at": datetime.datetime.utcnow().isoformat() + "Z", "stocks": watchlist}, f, ensure_ascii=False)
        print(f"[追蹤清單] 已寫入 {out_path}，共 {len(watchlist)} 檔（明天開盤會追蹤這些股票的5分K）")
    except Exception as e:
        print(f"[追蹤清單] 產生失敗（{e}），略過")


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
        save_market_history(date_iso)
        save_group_momentum()
        r3 = save_screener()
        r4 = save_reversal_screener()
        r5 = save_v2_screener()
        r6 = save_v5_screener()
        r7 = save_v4_screener()
        save_watchlist([r3, r4, r5, r6, r7])
        return

    # 排程執行（不帶參數）：
    # 先用 MI_INDEX 明確指定「台北時間今天」這個日期去抓，這樣才不會被
    # openapi 的 STOCK_DAY_ALL（公布時間常常延遲、有時候會停在前一天）誤導，
    # 造成程式顯示執行成功、但其實抓到的是舊資料。
    # 只有在 MI_INDEX 失敗時（例如非交易日、TWSE 尚未公布）才退回 openapi。
    taipei_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today_str = taipei_now.strftime("%Y%m%d")

    try:
        stocks, date_iso = fetch_by_date_via_mi_index(today_str)
        source = MI_INDEX_URL.format(date=today_str)
        print(f"[主要來源] MI_INDEX {today_str} 抓取成功，交易日 {date_iso}")
    except Exception as e:
        print(f"[主要來源] MI_INDEX {today_str} 抓取失敗（{e}），改用備援來源 openapi")
        stocks, date_iso = fetch_latest_via_openapi()
        source = OPENAPI_URL
        print(f"[備援來源] openapi 抓取成功，交易日 {date_iso}")

    merge_extra_data(stocks, date_iso)
    save(stocks, date_iso, source, update_latest=True)
    save_market_history(date_iso)
    save_group_momentum()
    r3 = save_screener()
    r4 = save_reversal_screener()
    r5 = save_v2_screener()
    r6 = save_v5_screener()
    r7 = save_v4_screener()
    save_watchlist([r3, r4, r5, r6, r7])


if __name__ == "__main__":
    main()
