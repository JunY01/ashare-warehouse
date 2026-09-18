# -*- coding: utf-8 -*-
"""抓取市场情绪类数据并落库 market_history.db。

数据源（均走系统 curl，绕过东财对 urllib 的 TLS 指纹拦截）：
  - 两市成交额: push2his 指数日K的 amount 字段（沪 1.000001 + 深综 0.399106 合计）
  - 两融余额:   datacenter-web 的 RPTA_RZRQ_LSHJ 报表

落库表（同日重跑幂等覆盖，单位均为亿元）：
  - market_turnover(date PK, sh_amount, sz_amount, total)
  - margin_balance(date PK, rzrq_ye)

用法: python fetch_sentiment.py   （update_data.py 每日自动调用）
"""
import os
import sys

# 统一寻址：无论从哪个目录运行都能定位仓库根（被 update_data.py 以模块方式调用时同样成立）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common.fetch_util import curl_json, drop_incomplete


def fetch_json(url):
    return curl_json(url)


def open_db():
    from src.common import history_db
    return history_db.connect()


def kline_amounts(secid, lmt):
    """指数日K的成交额字段，返回 [(date, 亿元)]"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3&fields2=f51,f57&klt=101&fqt=1&lmt=%d&end=20500101") % (secid, lmt)
    d = fetch_json(url)
    if not d or not d.get("data") or not d["data"].get("klines"):
        return []
    return [(k.split(",")[0], float(k.split(",")[1]) / 1e8) for k in d["data"]["klines"]]


def update_turnover(days=130):
    sh = kline_amounts("1.000001", days)
    sz = kline_amounts("0.399106", days)
    if not sh or not sz:
        print("  成交额接口无返回，跳过")
        return
    sz_map = dict(sz)
    rows = [(d, a, sz_map[d], a + sz_map[d]) for d, a in sh if d in sz_map]
    # 盘中运行会拿到当日未完成的半截值，丢弃以免按当日归档（收盘后重跑自会入库）
    n_all = len(rows)
    rows = drop_incomplete(rows)
    if len(rows) < n_all:
        print("  当日 bar 未收盘确认，丢弃 %d 期（收盘后重跑即入库）" % (n_all - len(rows)))
    if not rows:
        print("  无可入库期次，跳过")
        return
    con = open_db()
    con.executemany("INSERT OR REPLACE INTO market_turnover VALUES(?,?,?,?)", rows)
    con.commit()
    con.close()
    print("  两市成交额落库 %d 天（%s ~ %s），最新 %.0f 亿" % (len(rows), rows[0][0], rows[-1][0], rows[-1][3]))


def update_margin(count=250):
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ"
           "&columns=ALL&pageSize=%d&pageNumber=1&sortColumns=DIM_DATE&sortTypes=-1" % count)
    d = fetch_json(url)
    data = ((d or {}).get("result") or {}).get("data") or []
    rows = [(r["DIM_DATE"][:10], round(r["RZRQYE"] / 1e8, 1)) for r in data if r.get("RZRQYE")]
    if not rows:
        print("  两融接口无返回，跳过")
        return
    rows.reverse()
    con = open_db()
    con.executemany("INSERT OR REPLACE INTO margin_balance VALUES(?,?)", rows)
    con.commit()
    con.close()
    print("  两融余额落库 %d 期（%s ~ %s），最新 %.0f 亿" % (len(rows), rows[0][0], rows[-1][0], rows[-1][1]))


def main():
    print("== 更新情绪数据 ==")
    update_turnover()
    update_margin()


if __name__ == "__main__":
    main()
