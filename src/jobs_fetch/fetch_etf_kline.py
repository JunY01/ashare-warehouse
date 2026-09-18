# -*- coding: utf-8 -*-
"""ETF MACD宇宙K线抓取（前复权OHLC + 不复权收盘，双坐标系一次抓全）。

宇宙唯一来源: config/etf_macd_universe.json（A股7 + 跨境4 + 对照1 = 12只）。
接口: 东财push2his kline/get，fqt=1前复权（信号）+ fqt=0不复权（止损同坐标系对账），
klt=101日K，分页lmt=400从20200101抓到今天，串行+sleep防限流，失败重试4次（走update_data.get）。
落盘: data/kline_etf/<code>.csv（日期,开,高,低,收盘,收盘_不复权，前复权口径）
落库: etf_kline(code,date,open,high,low,close,close_raw) 主键幂等，可重跑。
纯标准库。用法: python src/jobs_fetch/fetch_etf_kline.py [--only 510300,513100]
"""
import csv
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common.paths import DATA_ROOT, CONFIG_ROOT
from src.common.market_map import cfg_path, load_json
from src.jobs_fetch import update_data as U

UNI = load_json(cfg_path("etf_macd_universe.json"))
KDIR = os.path.join(DATA_ROOT, "kline_etf")
BEG = "20200101"
TODAY = datetime.date.today().strftime("%Y%m%d")


def all_codes():
    out = []
    for grp in ("A股宽基", "跨境", "弹性对照"):
        out.extend(UNI.get(grp, []))
    return out


def fetch_ohlc(secid, beg, end, fqt):
    """返回 {date: (o,h,l,c)}，fqt=1前复权 / 0不复权。分页由调用方驱动单页lmt=400。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
           "&klt=101&fqt=%d&beg=%s&end=%s&lmt=400&_=%d") % (secid, fqt, beg, end, int(time.time() * 1000))
    d = U.get(url)
    if not d or not d.get("data") or not d["data"].get("klines"):
        return {}
    out = {}
    for k in d["data"]["klines"]:
        p = k.split(",")
        try:
            # fields2=f51..f57 = 日期,开,收,高,低,量,额 → (开,高,低,收)
            out[p[0]] = (float(p[1]), float(p[3]), float(p[4]), float(p[2]))
        except (ValueError, IndexError):
            continue
    return out


def fetch_full(secid):
    """从BEG分页抓到TODAY，返回 {date:(o,h,l,c_adj)} + {date:c_raw}。"""
    adj, raw = {}, {}
    beg = BEG
    for _ in range(12):
        page = fetch_ohlc(secid, beg, TODAY, 1)
        if not page:
            break
        adj.update(page)
        last = max(page)
        if last >= TODAY or len(page) < 400:
            break
        beg = last.replace("-", "")
        time.sleep(1)
    time.sleep(1)
    beg = BEG
    for _ in range(12):
        page = fetch_ohlc(secid, beg, TODAY, 0)
        if not page:
            break
        for d, (_, _, _, c) in page.items():
            raw[d] = c
        last = max(page)
        if last >= TODAY or len(page) < 400:
            break
        beg = last.replace("-", "")
        time.sleep(1)
    return adj, raw


def drop_today(rows):
    if rows and rows[-1][0] == datetime.date.today().isoformat() \
            and datetime.datetime.now().time() < datetime.time(15, 5):
        return rows[:-1]
    return rows


def main():
    only = set()
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
    os.makedirs(KDIR, exist_ok=True)
    targets = [t for t in all_codes() if not only or t["code"] in only]
    from src.common import history_db
    conn = history_db.connect()
    ok, fail = 0, []
    for i, t in enumerate(targets, 1):
        code, secid = t["code"], t["secid"]
        try:
            adj, raw = fetch_full(secid)
        except Exception as e:
            print("  %s 异常: %s" % (code, e))
            fail.append(code)
            continue
        dates = sorted(set(adj) & set(raw)) or sorted(adj)
        rows = [(d, adj[d][0], adj[d][1], adj[d][2], adj[d][3], raw.get(d)) for d in dates]
        rows = drop_today(rows)
        if len(rows) < 100:
            print("  %s 数据不足(%d行)，跳过" % (code, len(rows)))
            fail.append(code)
            time.sleep(1)
            continue
        with open(os.path.join(KDIR, code + ".csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["日期", "开", "高", "低", "收盘", "收盘_不复权"])
            w.writerows(rows)
        conn.executemany(
            "INSERT OR REPLACE INTO etf_kline VALUES(?,?,?,?,?,?,?)",
            [(code, d, o, h, l, c, r) for d, o, h, l, c, r in rows])
        conn.commit()
        print("  [%d/%d] %s %s %d行 %s~%s" % (i, len(targets), code, t["name"], len(rows), rows[0][0], rows[-1][0]))
        ok += 1
        time.sleep(1)
    n = conn.execute("SELECT COUNT(*), COUNT(DISTINCT code), MIN(date), MAX(date) FROM etf_kline").fetchone()
    conn.close()
    print("完成: 成功%d/%d，全表%d行%d只 %s~%s；失败: %s" % (ok, len(targets), n[0], n[1], n[2], n[3], fail or "无"))


if __name__ == "__main__":
    main()
