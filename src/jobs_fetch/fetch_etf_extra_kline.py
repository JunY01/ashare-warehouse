# -*- coding: utf-8 -*-
"""补齐「非股票类 ETF」日K —— 债券 ETF 与黄金 ETF（组合分散化验证的数据地基）。

为什么需要它：2026-09-17 验证「沪深300+红利」两只是同一件事（月度相关 0.90）后，
给出「股+债+金」组合建议前，必须先有债券与黄金的**长历史**才能验证分散是否真实。
现有 kline_etf 里只有股票 ETF（510300/510880 等），etf_kline 表也是 2020 起。

数据源：腾讯 fqkline（东财 push2his 是 IP 级限流）。腾讯单次最多返回约 650 根，
故按「end 日」反向分页：取 650 根 → 以首行日期前一天为新的 end → 直到目标起点。

写入门槛（照抄 fetch_index_csv_kline 的口径，防「把上一交易日的值写成今天」）：
  1. 会话日由数据源末日推断，绝不默认取今天；
  2. 当日 bar 在 15:05 前视为未完成，不写；
  3. 与库中已有末日重叠区间逐位比对，偏差 >0.5% 则拒写并报警（防复权口径漂移）。
落库：etf_kline（date 主键幂等覆盖）。

用法:
  python src/jobs_fetch/fetch_etf_extra_kline.py              # 全部
  python src/jobs_fetch/fetch_etf_extra_kline.py --dry-run
  python src/jobs_fetch/fetch_etf_extra_kline.py --code 518880
"""
import argparse
import csv
import datetime
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common import history_db
from src.common.fetch_util import num

# 非股票 ETF：code -> (腾讯代码, 名称, 目标起点)
TARGETS = {
    "518880": ("sh518880", "黄金ETF华安", "2013-07-29"),
    "159934": ("sz159934", "黄金ETF易方达", "2013-08-20"),
    "511010": ("sh511010", "国债ETF国泰(5年)", "2013-03-25"),
    "511260": ("sh511260", "十年国债ETF国泰", "2017-08-04"),
}
PAGE = 650
TOL = 0.005          # 重叠日收盘偏差容差


def tencent(code, beg, end):
    """腾讯 fqkline 取 [beg, end] 内最多 PAGE 根日K -> [(date, o,h,l,c), ...]"""
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,%d,qfq"
           % (code, beg, end, PAGE))
    try:
        r = subprocess.run(["curl", "-s", "-m", "30", url], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=40)
        block = ((json.loads(r.stdout) or {}).get("data") or {}).get(code) or {}
        rows = block.get("qfqday") or block.get("day") or []
    except (json.JSONDecodeError, TypeError, ValueError):
        rows = []
    out = []
    for x in rows:
        try:
            # 腾讯数组序 = 日期,开,收,高,低,量（与东财 f51..f57 同为"开收高低"，勿按列名顺序塞）
            out.append((x[0], num(x[1]), num(x[3]), num(x[4]), num(x[2])))
        except IndexError:
            continue
    return out


def fetch_all(code, tcode, start, today):
    """从 today 反向分页抓到 start，返回按日期升序去重的行"""
    seen, end = {}, today
    for _ in range(40):                       # 40 页 × 650 ≈ 26000 根，足够
        rows = tencent(tcode, "2000-01-01", end)
        if not rows:
            break
        for r in rows:
            if r[0] >= start:
                seen[r[0]] = r
        first = rows[0][0]
        if first <= start:
            break
        end = (datetime.date.fromisoformat(first) - datetime.timedelta(days=1)).isoformat()
        time.sleep(0.8)
    return [seen[d] for d in sorted(seen)]


def db_last(code):
    with history_db.connect(readonly=True) as c:
        r = c.execute("SELECT date, close FROM etf_kline WHERE code=? ORDER BY date DESC LIMIT 1",
                      (code,)).fetchone()
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", help="只跑某个 ETF 代码")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.datetime.now()
    today = now.date().isoformat()
    done = 0
    for code, (tcode, name, start) in TARGETS.items():
        if args.code and code != args.code:
            continue
        rows = fetch_all(code, tcode, start, today)
        if not rows:
            print("%-8s %-16s 无数据" % (code, name))
            continue
        last = rows[-1]
        if last[0] == today and now.time() < datetime.time(15, 5):
            rows = rows[:-1]                   # 当日未收盘，不写半截 bar
        # 重叠校验
        dl = db_last(code)
        warn = ""
        if dl:
            m = {r[0]: r[4] for r in rows}
            if dl[0] in m and dl[1] and abs(m[dl[0]] / dl[1] - 1) > TOL:
                warn = " ⚠重叠日偏差>%.1f%%(库%.3f/新%.3f)" % (
                    TOL * 100, dl[1], m[dl[0]])
        print("%-8s %-16s %d 行  %s ~ %s%s" % (
            code, name, len(rows), rows[0][0], rows[-1][0], warn))
        if args.dry_run or warn:
            continue
        with history_db.connect() as c:
            for d, o, h, l, cl in rows:
                c.execute("INSERT OR REPLACE INTO etf_kline"
                          "(code,date,open,high,low,close,close_raw) VALUES(?,?,?,?,?,?,?)",
                          (code, d, o, h, l, cl, None))
            c.commit()
        done += 1
    print("完成，写入 %d 只" % done)


if __name__ == "__main__":
    main()
