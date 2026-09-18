# -*- coding: utf-8 -*-
"""K线增量分片补抓（update_data.update_klines 太慢会被超时杀掉，用分片+每片落盘续跑）。

用法:
  python update_kline_chunk.py --start 0 --count 25   # 跑第0~24个板块，落盘
  跑完 0/25/50/…/475 共20片后执行:
  python update_kline_chunk.py --finish               # 重算sector_300d + 同步sector_kline

纯标准库。merge 按日期字典幂等，可重复跑。
"""
import os
import sys
import time

# 统一寻址：无论从哪个目录运行都能定位仓库根（本脚本为独立入口，不做导入用）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.jobs_fetch import update_data as U


def finish():
    raw = U.load_kline_raw()
    klines_map = raw["klines"]
    # 板块名（尽力，能取到就取）
    names, leads = {}, {}
    try:
        pn, all_items = 1, []
        while True:
            url = ("http://push2delay.eastmoney.com/api/qt/clist/get?pn=%d&pz=200&po=1&np=1&fltt=2&invt=2"
                   "&fid=f3&fs=m:90+t:2&fields=f2,f12,f14,f128&_=%d") % (pn, int(time.time() * 1000))
            d = U.get(url)
            if not d or not d.get("data") or not d["data"].get("diff"):
                break
            items = d["data"]["diff"]
            if isinstance(items, dict):
                items = [items]
            all_items.extend(items)
            if len(all_items) >= d["data"]["total"]:
                break
            pn += 1
            time.sleep(1)
        names = {it["f12"]: it["f14"] for it in all_items}
        leads = {it["f12"]: it.get("f128", "") for it in all_items}
    except Exception as e:
        print("板块名单失败，用空名继续: %s" % e)
    from src.common.kline_csv import write_sector_300d
    n = write_sector_300d(klines_map, names, leads)
    print("sector_300d.csv 重算完成: %d个板块" % n)
    from src.common import history_db as _hdb
    _hdb.import_klines()


def main():
    if "--finish" in sys.argv:
        finish()
        return

    start = int(sys.argv[sys.argv.index("--start") + 1]) if "--start" in sys.argv else 0
    count = int(sys.argv[sys.argv.index("--count") + 1]) if "--count" in sys.argv else 0

    raw = U.load_kline_raw()
    klines_map = raw["klines"]
    last_date = raw.get("meta", {}).get("last_date", "")
    beg_incr = last_date.replace("-", "") or "20250601"

    # 板块顺序固定（东财名单顺序，分片稳定；含新板块）
    all_items, pn = [], 1
    while True:
        url = ("http://push2delay.eastmoney.com/api/qt/clist/get?pn=%d&pz=200&po=1&np=1&fltt=2&invt=2"
               "&fid=f3&fs=m:90+t:2&fields=f2,f12,f14,f128&_=%d") % (pn, int(time.time() * 1000))
        d = U.get(url)
        if not d or not d.get("data") or not d["data"].get("diff"):
            break
        items = d["data"]["diff"]
        if isinstance(items, dict):
            items = [items]
        all_items.extend(items)
        if len(all_items) >= d["data"]["total"]:
            break
        pn += 1
        time.sleep(1)
    codes = [it["f12"] for it in all_items]
    if "--retry-missing" in sys.argv:
        target = raw.get("meta", {}).get("last_date", "")
        todo = [c for c in codes
                if not klines_map.get(c) or klines_map[c][-1][0] < target]
        print("重试缺口板块 %d个（目标 %s）" % (len(todo), target), flush=True)
    else:
        todo = codes[start:start + count]
    print("分片 [%d:%d] 共%d个，增量起点 %s" % (start, start + count, len(todo), beg_incr), flush=True)
    ok = 0
    for i, code in enumerate(todo):
        old = klines_map.get(code, [])
        beg = beg_incr if old else "20250601"
        try:
            closes = U.fetch_klines("90." + code, beg, U.TODAY)
        except Exception:
            closes = None
        if closes:
            closes = U.drop_incomplete(closes)
        if closes:
            klines_map[code] = sorted({**dict(old), **dict(closes)}.items())
            ok += 1
        time.sleep(0.8)
        if (i + 1) % 25 == 0:
            U.save_kline_raw(raw)
            print("  片内 %d/%d 已存" % (i + 1, len(todo)), flush=True)
    U.save_kline_raw(raw)
    new_last = max((c[-1][0] for c in klines_map.values() if c), default=last_date)
    raw["meta"]["last_date"] = new_last
    U.save_kline_raw(raw)
    print("分片完成: 成功%d/%d，全局截止 %s" % (ok, len(todo), new_last))


if __name__ == "__main__":
    main()
