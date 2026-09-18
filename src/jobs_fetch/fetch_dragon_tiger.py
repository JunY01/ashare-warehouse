# -*- coding: utf-8 -*-
"""龙虎榜每日明细抓取（东财 datacenter 报表）

数据源: datacenter-web RPT_DAILYBILLBOARD_DETAILSNEW，走系统 curl
  （东财对 urllib TLS 指纹拦截）。龙虎榜 T 日盘后（约17:00）公布：盘中跑落昨日，收盘后
  跑落当日；脚本从当日起试、无数据向前回溯最多 7 个自然日取最近有数据的交易日。
  仅收录股票（SECURITY_TYPE_CODE 以 '058' 开头），剔除可转债('060')等非股票品种。

落库 market_history.db（同日重跑幂等覆盖，按 date+code 主键）：
  dragon_tiger_daily(date, code, name, close, chg_pct, market,
                     buy_amt, sell_amt, net_amt, explanation)

用法: python fetch_dragon_tiger.py   （update_data.py 每日自动调用）
"""
import os
import sys
import time
from datetime import date, datetime, timedelta

# 统一寻址：无论从哪个目录运行都能定位仓库根
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common import history_db
from src.common.fetch_util import curl_json, num as _num, wan as _wan

REPORT = "RPT_DAILYBILLBOARD_DETAILSNEW"


def fetch_json(url):
    return curl_json(url)


def fetch_billboard(tdate):
    """拉取指定交易日（YYYY-MM-DD）龙虎榜全量（分页）。返回股票行列表。"""
    rows, pn = [], 1
    while True:
        flt = "(TRADE_DATE='%s')" % tdate
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=%s"
               "&columns=ALL&pageSize=500&pageNumber=%d"
               "&sortColumns=TRADE_DATE&sortTypes=-1&filter=%s") % (REPORT, pn, flt)
        d = fetch_json(url)
        data = ((d or {}).get("result") or {}).get("data") or []
        if not data:
            break
        rows.extend(data)
        if len(rows) >= ((d.get("result") or {}).get("count") or 0):
            break
        pn += 1
        time.sleep(1)
    return rows


def _store(conn, tdate, rows):
    sql = ("INSERT OR REPLACE INTO dragon_tiger_daily"
           "(date, code, name, close, chg_pct, market, buy_amt, sell_amt, net_amt, explanation) "
           "VALUES(?,?,?,?,?,?,?,?,?,?)")
    out = []
    for r in rows:
        stype = (r.get("SECURITY_TYPE_CODE") or "")
        if not stype.startswith("058"):      # 仅股票；剔除可转债等
            continue
        code = r.get("SECURITY_CODE")
        if not code:
            continue
        out.append((tdate, code, r.get("SECURITY_NAME_ABBR"),
                    _num(r.get("CLOSE_PRICE")), _num(r.get("CHANGE_RATE")),
                    r.get("MARKET"),
                    _wan(r.get("BILLBOARD_BUY_AMT")), _wan(r.get("BILLBOARD_SELL_AMT")),
                    _wan(r.get("BILLBOARD_NET_AMT")), r.get("EXPLANATION")))
    # 先删当日旧行再插入，保证当日快照干净
    conn.execute("DELETE FROM dragon_tiger_daily WHERE date=?", (tdate,))
    conn.executemany(sql, out)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM dragon_tiger_daily WHERE date=?", (tdate,)).fetchone()[0]
    print("  龙虎榜(%s): 股票 %d 条（含非股票已剔除）" % (tdate, n))
    return n


def main():
    # 龙虎榜 T 日盘后（约17:00）公布：14:30 盘中跑当日未出→落昨日；收盘后跑当日已出→落当日。
    # 故从当日(back=0)起试，无数据再向前回溯最多 7 个自然日，取最近一个有数据的交易日。
    today = date.today()
    conn = history_db.connect()
    try:
        for back in range(0, 8):
            tdate = (today - timedelta(days=back)).isoformat()
            rows = fetch_billboard(tdate)
            if not rows:
                print("  %s 无龙虎榜（当日未公布/非交易日），继续回溯…" % tdate)
                time.sleep(1)
                continue
            print("== 抓取龙虎榜（%s） ==" % tdate)
            _store(conn, tdate, rows)
            return
        print("  近 8 个自然日均无龙虎榜数据，退出")
    finally:
        conn.close()


if __name__ == "__main__":
    main()