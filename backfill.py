#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill.py — 一次補抓過去N天的歷史收盤資料。

STEP3（20天）、STEP4（10~20天）、STEP5 V2雷達（最好60天，MACD才準）
這些篩選都要靠 data/ 裡逐日累積的 YYYY-MM-DD.json 才能算，
如果你的 pipeline 才剛開始跑沒多久，歷史天數不夠，篩選結果會不準或是抓不到股票。

這支腳本會自動往回找過去N個平日，把還沒有存檔的日期一天一天補抓回來，
已經存在的日期會自動跳過，不會重複抓、不會覆蓋。
週末或國定假日本來就沒有交易資料，會抓失敗然後自動跳過，這是正常現象，不用理它。

用法：
  python3 backfill.py 60      # 補抓過去60個交易日（預設值，抓完MACD才會準）
  python3 backfill.py 30      # 只補抓過去30天

補完之後，會順便重新計算一次 market / screener / reversal_screener / v2_screener，
不用再手動跑一次 fetch_twse.py。
"""
import sys
import os
import time
import datetime

import fetch_twse as ft


def main():
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    data_dir = "data"
    os.makedirs(data_dir, exist_ok=True)

    taipei_today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()

    # 往回掃平日，抓夠 days_back 個候選日期（多掃一點當緩衝，扣掉假日還夠湊）
    candidates = []
    d = taipei_today - datetime.timedelta(days=1)  # 今天讓平常的排程去處理，這裡只補過去的
    scanned = 0
    buffer_limit = days_back * 2 + 20
    while len(candidates) < days_back and scanned < buffer_limit:
        if d.weekday() < 5:  # 只挑週一~週五
            candidates.append(d)
        d -= datetime.timedelta(days=1)
        scanned += 1

    ok_count = 0
    skip_count = 0
    fail_count = 0

    for d in candidates:
        date_iso = d.isoformat()
        out_path = os.path.join(data_dir, f"{date_iso}.json")
        if os.path.exists(out_path):
            skip_count += 1
            continue
        date_str = d.strftime("%Y%m%d")
        try:
            stocks, real_date_iso = ft.fetch_by_date_via_mi_index(date_str)
            ft.merge_extra_data(stocks, real_date_iso, data_dir=data_dir)
            ft.save(stocks, real_date_iso, ft.MI_INDEX_URL.format(date=date_str), update_latest=False, data_dir=data_dir)
            print(f"OK {date_iso}，共 {len(stocks)} 檔")
            ok_count += 1
        except Exception as e:
            print(f"略過 {date_iso}（{e}，可能是非交易日）")
            fail_count += 1
        time.sleep(1.5)  # 對TWSE伺服器客氣一點，避免連續狂打被擋

    print(f"補抓完成：成功 {ok_count} 天、跳過(已存在) {skip_count} 天、失敗或非交易日 {fail_count} 天")

    # 補完歷史資料後，用目前最新一天的資料重新算一次篩選雷達
    try:
        json_files = sorted([
            f for f in os.listdir(data_dir)
            if len(f) == 15 and f.endswith(".json") and f[:4].isdigit() and f[4] == "-"
        ])
        if json_files:
            latest_date_iso = json_files[-1].replace(".json", "")
            ft.save_market_history(latest_date_iso, data_dir=data_dir)
            ft.save_screener(data_dir=data_dir)
            ft.save_reversal_screener(data_dir=data_dir)
            ft.save_v2_screener(data_dir=data_dir)
            print(f"已用 {latest_date_iso} 重新計算 market / screener / reversal_screener / v2_screener")
    except Exception as e:
        print(f"補完後重新計算篩選雷達失敗（{e}）")


if __name__ == "__main__":
    main()
