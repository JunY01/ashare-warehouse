# -*- coding: utf-8 -*-
"""涨停/跌停池抓取（东财 push2ex 池接口）

数据源: 东财 getTopicZTPool(涨停池) / getTopicDTPool(跌停池)，均走系统 curl
  （push2ex 对 urllib TLS 指纹拦截，与 fetch_sentiment 同法）。
日期参数为自然日 YYYYMMDD；盘中当日未收盘前涨停池会随行情累计，跌停池可能为空属正常。

落库 market_history.db（同日重跑幂等覆盖，按 date+code 主键）：
  zt_pool_daily(date, code, name, first_time, last_time, lb_cnt, zt_days,
                fund_amt, amount, hybk, market)   -- 涨停池明细（lb_cnt=连板数, zt_days=近期涨停天数）
  dt_pool_daily(date, code, name, last_time, dt_cnt, amount, hybk, market) -- 跌停池明细

用法: python fetch_limit_up_down.py [--date YYYY-MM-DD]   （update_data.py 每日自动调用；--date 用于回补历史日）
"""
import os
import sys
import time
from datetime import datetime

# 统一寻址：无论从哪个目录运行都能定位仓库根
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common import history_db
from src.common.fetch_util import curl_json, wan as _wan

BASE = "https://push2ex.eastmoney.com/%s?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"


def fetch_json(url):
    return curl_json(url)


def _time(v):
    """东财时间戳转 HH:MM:SS。小时<10 时不补零（92503=9:25:03，5位）；
    小时≥10 为 6 位（103315=10:33:15）；0/空为占位返回 None"""
    try:
        s = str(int(v))
    except (TypeError, ValueError):
        return None
    if len(s) == 5:
        s = "0" + s
    if len(s) == 6:
        return "%s:%s:%s" % (s[:2], s[2:4], s[4:6])
    return None


def _market(m):
    return "SH" if m == 1 else "SZ"


def _fetch_pool(endpoint, qdate, pagesize=500):
    """拉全量池。返回 (list, 总数)；qdate 格式 YYYYMMDD"""
    url = (BASE % endpoint) + "&Pageindex=0&pagesize=%d&sort=fund:asc&date=%s" % (pagesize, qdate)
    d = fetch_json(url)
    if not d or d.get("rc") != 0 or not d.get("data"):
        return [], 0
    return d["data"].get("pool") or [], d["data"].get("tc") or 0


def fetch_zt(qdate, iso):
    """涨停池 → 落库行 [(date, code, name, first_time, last_time, lb_cnt, zt_days, fund_amt, amount, hybk, market)]
    lb_cnt=连板数(lbc), zt_days=近期涨停天数(zttj.days)；qdate 供接口(YYYYMMDD)，iso 为落库日期(YYYY-MM-DD)"""
    pool, tc = _fetch_pool("getTopicZTPool", qdate)
    rows = []
    for it in pool:
        code, m = it.get("c"), _market(it.get("m"))
        if not code:
            continue
        zt = it.get("zttj") or {}
        rows.append((iso, code, it.get("n"), _time(it.get("fbt")), _time(it.get("lbt")),
                     int(it.get("lbc") or 1), int(zt.get("days") or 1),
                     _wan(it.get("fund")), _wan(it.get("amount")),
                     it.get("hybk"), m))
    return rows, tc


def fetch_dt(qdate, iso):
    """跌停池 → 落库行 [(date, code, name, last_time, dt_cnt, amount, hybk, market)]"""
    pool, tc = _fetch_pool("getTopicDTPool", qdate)
    rows = []
    for it in pool:
        code, m = it.get("c"), _market(it.get("m"))
        if not code:
            continue
        rows.append((iso, code, it.get("n"), _time(it.get("lbt")),
                     int(it.get("days") or 1), _wan(it.get("amount")),
                     it.get("hybk"), m))
    return rows, tc


def _store(conn, table, sql, rows, label, iso):
    # 先删当日旧行再插入：盘中多次运行不残留"曾涨停后炸板"的股，保证当日快照干净
    conn.execute("DELETE FROM %s WHERE date=?" % table, (iso,))
    conn.executemany(sql, rows)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM %s WHERE date=?" % table, (iso,)).fetchone()[0]
    print("  %s: %d 行（本次抓取 %d）" % (label, n, len(rows)))


def main():
    argv = sys.argv
    if "--date" in argv:
        # 回补指定日（接口按自然日查，收盘后重跑该日即得终值）
        iso = argv[argv.index("--date") + 1]
        qdate = iso.replace("-", "")
    else:
        qdate = datetime.now().strftime("%Y%m%d")   # 接口要求 YYYYMMDD
        iso = datetime.now().strftime("%Y-%m-%d")   # 落库统一 ISO，与全库其他表一致
    print("== 抓取涨跌停池（%s） ==" % iso)
    conn = history_db.connect()
    try:
        rows, tc = fetch_zt(qdate, iso)
        _store(conn, "zt_pool_daily",
               "INSERT OR REPLACE INTO zt_pool_daily VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               rows, "涨停池", iso)
        time.sleep(1.5)
        rows, tc = fetch_dt(qdate, iso)
        _store(conn, "dt_pool_daily",
               "INSERT OR REPLACE INTO dt_pool_daily VALUES(?,?,?,?,?,?,?,?)",
               rows, "跌停池", iso)
    finally:
        conn.close()


if __name__ == "__main__":
    main()