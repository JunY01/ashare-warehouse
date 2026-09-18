# -*- coding: utf-8 -*-
"""板块K线扩历史到2023-01（十万次迭代的弹药库）

给未来亚亚：东财单次lmt=400，一年约242根，所以按年分三段抓，
kline_raw按日期字典合并（幂等，可重跑）。已有覆盖的段自动跳过，
进度记 backfill_3y_progress.json，中断后直接重跑同命令续上。
连续失败5次自动睡15分钟（东财限流），醒了继续，不用人守。

用法:
  python backfill_kline_3y.py --start 0 --count 100   # 切片抓
  python backfill_kline_3y.py --retry                 # 重抓失败段
  python backfill_kline_3y.py --finish                # 落库+覆盖率报告
纯标准库。
"""
import json
import os
import sys
import time

# 统一寻址：K线缓存/进度/历史库统一走 data/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common.paths import DATA_ROOT

DATA_DIR = DATA_ROOT
PROGRESS = os.path.join(DATA_DIR, "cache", "backfill_3y_progress.json")

from src.jobs_fetch import update_data as U  # 复用 get/fetch_klines/drop_incomplete/save/load（cookie逻辑一致）

# 年分段：一年约242根 < lmt=400，单段一次拉完；2025H1与现存2025-06起点重叠，合并去重
SEGMENTS = [("2023", "20230101", "20231231"),
            ("2024", "20240101", "20241231"),
            ("2025H1", "20250101", "20250602")]
SLEEP_S = 1.0
FAIL_PAUSE_N = 5
FAIL_PAUSE_S = 900


def load_progress():
    try:
        return json.load(open(PROGRESS, encoding="utf-8"))
    except Exception:
        return {"done": {}, "fail": []}


def save_progress(p):
    json.dump(p, open(PROGRESS, "w", encoding="utf-8"), ensure_ascii=False)


def sector_codes():
    """东财板块名单顺序（分片稳定）；失败则回退kline_raw已有keys。"""
    all_items, pn = [], 1
    try:
        while True:
            url = ("http://push2delay.eastmoney.com/api/qt/clist/get?pn=%d&pz=200&po=1&np=1&fltt=2&invt=2"
                   "&fid=f3&fs=m:90+t:2&fields=f2,f12,f14&_=%d") % (pn, int(time.time() * 1000))
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
    except Exception as e:
        print("名单异常: %s" % e)
    if all_items:
        return [it["f12"] for it in all_items]
    return sorted(U.load_kline_raw().get("klines", {}).keys())


def seg_covered(merged_sorted, beg, end):
    """该年段是否已覆盖：合并序列里只要有一根落在段内即算（年段一次拉完，要么全有要么全无）。"""
    b = "%s-%s-%s" % (beg[:4], beg[4:6], beg[6:8])
    e = "%s-%s-%s" % (end[:4], end[4:6], end[6:8])
    return any(b <= d <= e for d, _ in merged_sorted)


def raw_earliest():
    """kline_raw各板块当前序列（落盘前内存态由调用方保证已存盘）。"""
    try:
        kl = U.load_kline_raw().get("klines", {})
        return {c: v for c, v in kl.items()}
    except Exception:
        return {}


def run_slice(start, count, retry_only=False):
    raw = U.load_kline_raw()
    klines_map = raw["klines"]
    prog = load_progress()
    done, fail = prog.get("done", {}), prog.get("fail", [])
    fail = [x for x in fail if x[1] not in done.get(x[0], [])]  # 已记完的去重，自愈旧失败
    # 证伪失败：板块最早日若晚于段尾（如2024年新设板块缺2023段），说明该段本就不存在
    seg_ends = {s: e for s, _, e in SEGMENTS}
    earliest = {c: (v[0][0] if v else "") for c, v in raw_earliest().items()}
    kept = []
    for c, s in fail:
        e = seg_ends.get(s, "")
        iso_end = "%s-%s-%s" % (e[:4], e[4:6], e[6:8]) if e else ""
        if earliest.get(c, "") and iso_end and earliest[c] > iso_end:
            done.setdefault(c, [])
            if s not in done[c]:
                done[c].append(s)
        else:
            kept.append([c, s])
    fail = kept
    codes = sector_codes()
    todo = codes[start:start + count] if not retry_only else sorted({c for c, _ in fail})
    print("切片 [%d:%d] 待跑%d个" % (start, start + count, len(todo)), flush=True)
    ok, fails, consec = 0, 0, 0
    for i, code in enumerate(todo):
        old = klines_map.get(code, [])
        merged = dict(old)
        hit = False
        for seg, beg, end in SEGMENTS:
            if seg in done.get(code, []):
                continue
            if seg_covered(sorted(merged.items()), beg, end):
                done.setdefault(code, [])
                if seg not in done[code]:
                    done[code].append(seg)
                continue
            try:
                closes = U.fetch_klines("90." + code, beg, end)
            except Exception:
                closes = None
            if closes:
                closes = U.drop_incomplete(closes)
            if closes:
                merged.update(dict(closes))
                done.setdefault(code, [])
                if seg not in done[code]:
                    done[code].append(seg)
                consec, hit = 0, True
            elif merged:
                # 该板块其他段有数、本段拉空：多为2023年后新设板块，直接记完不重试
                done.setdefault(code, [])
                if seg not in done[code]:
                    done[code].append(seg)
            else:
                fails += 1
                consec += 1
                if (code, seg) not in fail:
                    fail.append([code, seg])
                if consec >= FAIL_PAUSE_N:
                    print("  连续失败%d，睡15分钟避限流…" % consec, flush=True)
                    U.save_kline_raw(raw)
                    save_progress({"done": done, "fail": fail})
                    time.sleep(FAIL_PAUSE_S)
                    consec = 0
            time.sleep(SLEEP_S)
        if merged and hit:
            klines_map[code] = sorted(merged.items())
            ok += 1
        if (i + 1) % 25 == 0:
            U.save_kline_raw(raw)
            save_progress({"done": done, "fail": fail})
            print("  片内 %d/%d 已存（成功板块%d）" % (i + 1, len(todo), ok), flush=True)
    U.save_kline_raw(raw)
    save_progress({"done": done, "fail": fail})
    print("切片完成: 成功板块%d/%d，失败段%d" % (ok, len(todo), len(fail)))


def finish():
    """落库 + 覆盖率摘要（fetch 层不生成报告文件，仅打印摘要，符合分层约定）。"""
    from src.common import history_db
    raw = U.load_kline_raw()
    klines_map = raw["klines"]
    n = sum(len(v) for v in klines_map.values())
    codes = sorted(klines_map.keys())
    early = sum(1 for c in codes if klines_map[c] and klines_map[c][0][0] <= "2023-02-01")
    print("kline_raw: %d板块 %d行，早于2023-02开局%d个" % (len(codes), n, early))
    history_db.import_klines()
    conn = history_db.connect(readonly=True)
    try:
        span = conn.execute("SELECT MIN(date),MAX(date),COUNT(*),COUNT(DISTINCT code) FROM sector_kline").fetchone()
    finally:
        conn.close()
    print("覆盖率: sector_kline %s~%s，共%d行，%d板块；2023年开局板块%d/%d。" % (
        span[0], span[1], span[2], span[3], early, len(codes)))


if __name__ == "__main__":
    if "--finish" in sys.argv:
        finish()
    elif "--retry" in sys.argv:
        run_slice(0, 0, retry_only=True)
    else:
        start = int(sys.argv[sys.argv.index("--start") + 1]) if "--start" in sys.argv else 0
        count = int(sys.argv[sys.argv.index("--count") + 1]) if "--count" in sys.argv else 100
        run_slice(start, count)
