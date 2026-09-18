# -*- coding: utf-8 -*-
"""主题板块成分股快照（白名单见 watchlist.json 的 member_bks）

数据源: 东财 clist fs=b:BKxxxx（push2delay 直连，urllib 可用）
落库 market_history.db（同日重跑幂等覆盖）：
  sector_member_daily(date, bk_code, stock_code, name, price, chg_pct,
                      main_net_wan, float_mv_yi)
个股级资金流强度可在查询时用 main_net_wan/float_mv_yi 现算（万元÷亿元=bp）。

用法: python fetch_sector_members.py   （update_data.py 每日自动调用）
"""
import datetime
import json
import os
import sys
import time

# 统一寻址：无论从哪个目录运行都能定位仓库根（消除回退写错目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common import history_db
from src.common.paths import CONFIG_ROOT
from src.common.fetch_util import urllib_json, num as _num

WATCHLIST = os.path.join(CONFIG_ROOT, "watchlist.json")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0",
           "Referer": "https://quote.eastmoney.com/"}


def get(url, tries=3, sleep_s=2):
    return urllib_json(url, headers=HEADERS, timeout=12, tries=tries, sleep_s=sleep_s)


def fetch_members(bk_code):
    """单板块成分股全量分页；fields: f12代码 f14名称 f2现价 f3涨跌幅 f62主力净流入(元) f21流通市值(元)"""
    items, pn = [], 1
    while True:
        url = ("http://push2delay.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1&fltt=2&invt=2"
               "&fid=f3&fs=b:%s&fields=f12,f14,f2,f3,f62,f21") % (pn, bk_code)
        d = get(url)
        if not d or not d.get("data") or not d["data"].get("diff"):
            break
        page = d["data"]["diff"]
        if isinstance(page, dict):
            page = [page]
        items.extend(page)
        if len(items) >= d["data"]["total"]:
            break
        pn += 1
        time.sleep(1)
    rows = []
    for it in items:
        code = it.get("f12")
        if not code:
            continue
        net, mv = _num(it.get("f62")), _num(it.get("f21"))
        rows.append((code, it.get("f14"), _num(it.get("f2")), _num(it.get("f3")),
                     round(net / 1e4, 2) if net is not None else None,
                     round(mv / 1e8, 2) if mv is not None else None))
    return rows


def main():
    with open(WATCHLIST, encoding="utf-8") as f:
        bks = json.load(f).get("member_bks", [])
    date = datetime.date.today().isoformat()
    print("== 更新板块成分股（白名单%d个） ==" % len(bks))
    conn = history_db.connect()
    try:
        total = 0
        for bk in bks:
            rows = fetch_members(bk["bk_code"])
            if not rows:
                print("  %s %s 无返回" % (bk["bk_code"], bk["name"]))
                continue
            conn.executemany(
                "INSERT OR REPLACE INTO sector_member_daily VALUES(?,?,?,?,?,?,?,?)",
                [(date, bk["bk_code"], c, n, p, chg, net, mv) for c, n, p, chg, net, mv in rows])
            conn.commit()
            total += len(rows)
            print("  %s %s: %d 只" % (bk["bk_code"], bk["name"], len(rows)))
            time.sleep(2)  # 防限流
    finally:
        conn.close()
    print("  成分股落库合计 %d 行" % total)


if __name__ == "__main__":
    main()
