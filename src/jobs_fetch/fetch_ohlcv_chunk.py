# -*- coding: utf-8 -*-
"""板块OHLCV历史抓取（分片落库，含成交量/额 — 量价特征的数据地基）。

东财kline/get fields2=f51..f57 = 日期,开,收,高,低,量,额（update_data只存了收盘）。
用法:
  python fetch_ohlcv_chunk.py --start 0 --count 100     # 分片回填未入库板块
  python fetch_ohlcv_chunk.py --retry-missing           # 只补缺失板块（可反复跑，幂等）
  python fetch_ohlcv_chunk.py --refresh                 # 刷新已入库板块的最近数据（每日/按需）
  python fetch_ohlcv_chunk.py --refresh --sleep 5       # 东财限流时放慢节奏
  python fetch_ohlcv_chunk.py --patient                 # 限流期补齐：单次尝试+轮询多轮（见 patient）
表 sector_ohlcv(code,date,open,high,low,close,vol,amt) 主键幂等，可重跑。
纯标准库。
"""
import datetime
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# 统一寻址：无论从哪个目录运行都能定位仓库根
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.jobs_fetch import update_data as U
from src.common.fetch_util import drop_incomplete


def fetch_ohlcv(secid, beg, end, tries=4):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
           "&klt=101&fqt=1&beg=%s&end=%s&lmt=400&_=%d") % (secid, beg, end, int(time.time() * 1000))
    d = U.get(url, tries=tries)
    if not d or not d.get("data") or not d["data"].get("klines"):
        return None
    out = []
    for k in d["data"]["klines"]:
        p = k.split(",")
        try:
            out.append((p[0], float(p[1]), float(p[3]), float(p[4]),
                        float(p[2]), float(p[5]), float(p[6])))
        except (ValueError, IndexError):
            continue
    return out


def board_list():
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
    return [it["f12"] for it in all_items]


def patient(conn, codes, sleep_s):
    """耐心回填：单次尝试 + 失败留给下一轮，轮询到缺口清零。

    东财 /stock/kline/get 的限流是成功失败交替（不是硬配额），而失败重试代价高
    （默认 4 次重试约 6.2s）——短冷却多轮比长冷却划算。实测 496 板块约 1.5 小时补齐。
    可随时 Ctrl-C，进度已落库，重跑继续。
    """
    rows = conn.execute("SELECT DISTINCT date FROM sector_kline ORDER BY date DESC LIMIT 2").fetchall()
    if not rows:
        print("sector_kline 为空，无法确定目标日")
        return 1
    target = rows[0][0]
    now = datetime.datetime.now()
    if target == now.date().isoformat() and now.time() < datetime.time(15, 5):
        target = rows[1][0] if len(rows) > 1 else target   # 当日未收盘，退到上一交易日
    print("耐心回填：目标 %s，单次尝试节奏 %.1fs" % (target, sleep_s), flush=True)

    total = 0
    for rnd in range(1, 41):
        last = dict(conn.execute("SELECT code, MAX(date) FROM sector_ohlcv GROUP BY code"))
        todo = [c for c in codes if last.get(c, "") < target]
        if not todo:
            break
        print("== 第 %d 轮：待补 %d 个 ==" % (rnd, len(todo)), flush=True)
        ok = 0
        for i, code in enumerate(todo, 1):
            try:
                got = fetch_ohlcv("90." + code, "20250601", target.replace("-", ""), tries=1)
            except Exception:
                got = None
            if got:
                got = drop_incomplete(got)
            if got:
                conn.executemany("INSERT OR REPLACE INTO sector_ohlcv VALUES(?,?,?,?,?,?,?,?)",
                                 [(code,) + r for r in got])
                ok += 1
            time.sleep(sleep_s)
            if i % 25 == 0:
                conn.commit()
                print("  %d/%d 成功 %d" % (i, len(todo), ok), flush=True)
        conn.commit()
        total += ok
        print("  第 %d 轮结束：成功 %d/%d" % (rnd, ok, len(todo)), flush=True)
        if ok == 0:
            print("  整轮零成功，接口可能被完全封锁，先退出（稍后重跑即可续）", flush=True)
            break
    print("耐心回填累计成功 %d" % total)
    return 0


def main():
    from src.common import history_db
    conn = history_db.connect(check_same_thread=False)
    codes = board_list()
    sleep_s = 1.0
    if "--sleep" in sys.argv:
        sleep_s = float(sys.argv[sys.argv.index("--sleep") + 1])
    if "--patient" in args():
        rc = patient(conn, codes, sleep_s if "--sleep" in sys.argv else 1.6)
        n = conn.execute("SELECT COUNT(*), COUNT(DISTINCT code), MAX(date) FROM sector_ohlcv").fetchone()
        print("全表%d行%d板块 截止%s" % n)
        conn.close()
        return rc
    have = {r[0] for r in conn.execute(
        "SELECT code FROM sector_ohlcv GROUP BY code HAVING COUNT(*) >= 250")}
    if "--refresh" in args():
        # 刷新已入库板块的最近数据（原分片/补缺两条路径都跳过已有板块，故需单独入口）
        todo = [c for c in codes if c in have]
        print("刷新已入库板块 %d个（sleep=%s）" % (len(todo), sleep_s), flush=True)
    elif "--retry-missing" in args():
        todo = [c for c in codes if c not in have]
        print("缺口板块 %d个（sleep=%s）" % (len(todo), sleep_s), flush=True)
    else:
        start = int(sys.argv[sys.argv.index("--start") + 1])
        count = int(sys.argv[sys.argv.index("--count") + 1])
        todo = [c for c in codes[start:start + count] if c not in have]
        print("分片 [%d:%d] 去重后%d个（sleep=%s）" % (start, start + count, len(todo), sleep_s), flush=True)
    ok = 0
    lock = threading.Lock()
    counter = [0]

    def one(code):
        try:
            rows = fetch_ohlcv("90." + code, "20250601", U.TODAY)
        except Exception:
            rows = None
        time.sleep(sleep_s)  # 降速保成功率：并行2线程+每请求sleep（东财限流期可 --sleep 4~6）
        if rows:
            rows = drop_incomplete(rows)
        if rows:
            with lock:
                conn.executemany(
                    "INSERT OR REPLACE INTO sector_ohlcv VALUES(?,?,?,?,?,?,?,?)",
                    [(code,) + r for r in rows])
        with lock:
            counter[0] += 1
            n = counter[0]
            good = rows is not None and len(rows) > 0
        if n % 25 == 0:
            with lock:
                conn.commit()
            print("  片内 %d/%d 已存" % (n, len(todo)), flush=True)
        return 1 if good else 0

    with ThreadPoolExecutor(max_workers=2) as ex:
        ok = sum(ex.map(one, todo))
    conn.commit()
    n = conn.execute("SELECT COUNT(*), COUNT(DISTINCT code) FROM sector_ohlcv").fetchone()
    print("完成: 成功%d/%d，全表%d行%d板块" % (ok, len(todo), n[0], n[1]))
    conn.close()


def args():
    return sys.argv


if __name__ == "__main__":
    main()
