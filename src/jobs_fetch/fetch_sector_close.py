# -*- coding: utf-8 -*-
"""盘前用板块快照 f2 补齐 sector_kline 的上一交易日收盘（东财K线端点限流时的可靠替代）。

原理：次日 09:25 前，东财板块榜单（clist m:90+t:2）的 f2 仍是上一交易日收盘、f3 被重置为 0，
一次请求即可拿全 496 个板块的收盘价；而 K线端点 push2his 是 IP 级限流，被限时 curl 与浏览器
一起失败（每请求 4 次重试约 6.2s、冷却 15~20 分钟、每窗口约百次请求），补一天要抓 496 次得不偿失。

安全闸：库内已有该日真K线的板块必须与 f2 逐位一致，否则整体拒写（防盘中值混入）。
窗口外（09:25 后）运行会告警——那时 f2 已切成盘中价。

产物：kline_raw.json 该日行 -> sector_300d.csv 重算 -> sector_kline 落库。

用法:
  python src/jobs_fetch/fetch_sector_close.py --target 2026-09-15
  python src/jobs_fetch/fetch_sector_close.py --target 2026-09-15 --dry-run
"""
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common import history_db
from src.common.kline_csv import write_sector_300d
from src.common.paths import DATA_ROOT
from src.jobs_fetch import update_data as U

KLINE_RAW = os.path.join(DATA_ROOT, "cache", "kline_raw.json")
PREOPEN_DEADLINE = (9, 15)   # 09:15 起集合竞价，f2 变撮合价（不是上一交易日收盘）
TOL = 0.005


def board_snapshot():
    """返回 {code: (名称, f2)}，pz=200 分页取全板块。"""
    out, pn = {}, 1
    while True:
        url = ("http://push2delay.eastmoney.com/api/qt/clist/get?pn=%d&pz=200&po=1&np=1&fltt=2&invt=2"
               "&fid=f3&fs=m:90+t:2&fields=f2,f12,f14&_=%d") % (pn, int(time.time() * 1000))
        d = U.get(url)
        if not d or not d.get("data") or not d["data"].get("diff"):
            break
        items = d["data"]["diff"]
        if isinstance(items, dict):
            items = [items]
        for it in items:
            try:
                out[it["f12"]] = (it["f14"], float(it["f2"]))
            except (KeyError, TypeError, ValueError):
                continue
        if len(out) >= d["data"]["total"]:
            break
        pn += 1
        time.sleep(1)
    return out


def names_and_leaders():
    """名称与领涨股取库内快照最近一日（盘前榜单的 f128 是 '-'，不能现抓）。"""
    con = history_db.connect(readonly=True)
    try:
        rows = con.execute(
            "SELECT code, name, leader FROM sector_daily "
            "WHERE date=(SELECT MAX(date) FROM sector_daily)").fetchall()
    finally:
        con.close()
    return ({c: n for c, n, _ in rows}, {c: (l or "") for c, _, l in rows})


def main():
    argv = sys.argv
    if "--target" not in argv:
        print("必须显式指定 --target YYYY-MM-DD（要补哪一天的收盘）")
        return 2
    target = argv[argv.index("--target") + 1]
    dry = "--dry-run" in argv

    now = datetime.now()
    if (now.hour, now.minute) >= PREOPEN_DEADLINE:
        print("警告：当前 %s 已过盘前窗口 %02d:%02d，榜单 f2 可能已是当日盘中值，继续前请确认。"
              % (now.strftime("%H:%M"), *PREOPEN_DEADLINE))

    snap = board_snapshot()
    if not snap:
        print("榜单无返回，退出")
        return 1
    print("榜单板块 %d 个" % len(snap))

    with open(KLINE_RAW, encoding="utf-8") as f:
        raw = json.load(f)
    klines = raw["klines"]

    # 安全闸：库内已有该日真K线的板块必须与 f2 逐位一致
    existing = {c: s[-1][1] for c, s in klines.items() if s and s[-1][0] == target}
    diffs = [(c, existing[c], snap[c][1]) for c in existing
             if c in snap and abs(snap[c][1] - existing[c]) > TOL]
    if diffs:
        print("库内已有该日K线的板块与 f2 不符（%d 个），拒写：%s" % (len(diffs), diffs[:5]))
        return 1
    print("安全闸：库内已有 %d 个板块该日K线，与 f2 逐位一致" % len(existing))

    filled = 0
    for code, (_, close) in snap.items():
        series = klines.get(code)
        if not series or series[-1][0] >= target:
            continue
        if not dry:
            klines[code] = sorted({**dict(series), target: close}.items())
        filled += 1
    print("补入 %d 个板块的 %s 收盘" % (filled, target))
    if dry or not filled:
        return 0

    raw["meta"]["last_date"] = target
    raw["meta"]["last_update"] = "%s 收盘（盘前快照f2补齐，%d个板块已有K线逐位校验）" % (target, len(existing))
    with open(KLINE_RAW, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)

    names, leaders = names_and_leaders()
    print("sector_300d.csv 重算: %d 个板块" % write_sector_300d(klines, names, leaders))
    history_db.import_klines()
    return 0


if __name__ == "__main__":
    sys.exit(main())
