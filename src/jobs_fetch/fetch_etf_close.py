# -*- coding: utf-8 -*-
"""用腾讯行情补齐 etf_kline 最近缺失日的收盘（东财K线端点限流时的替代，一次带全 OHLC）。

腾讯 qt.gtimg.cn 一次批量查全部宇宙（GBK）：字段 [3]=最新价、[4]=昨收、[5]=今开，
时间戳（形如 20260915161443，收盘后快照）后 3/4 位 = 最高/最低。指数不做复权，
故当日的前复权价 = 不复权价 = 实际收盘，close 与 close_raw 同值。

安全闸：时间戳日期即成交日，未到当日 15:05 时腾讯给的是集合竞价/盘中价（高/低为 0），一律拒绝；
[4]昨收须与库内该标的前一交易日收盘逐位一致，否则整体拒写。
日期无需传参——由腾讯时间戳推断，已经是库内最新日时自动跳过。

用法:
  python src/jobs_fetch/fetch_etf_close.py
  python src/jobs_fetch/fetch_etf_close.py --target 2026-09-15
  python src/jobs_fetch/fetch_etf_close.py --dry-run
"""
import datetime
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common import history_db
from src.common.market_map import cfg_path, load_json
from src.common.paths import DATA_ROOT

KDIR = os.path.join(DATA_ROOT, "kline_etf")
TOL = 0.0005


def universe():
    """宇宙唯一来源 config/etf_macd_universe.json：返回 [(code, name, secid)]。"""
    uni = load_json(cfg_path("etf_macd_universe.json"))
    out = []
    for grp in ("A股宽基", "跨境", "弹性对照"):
        for e in uni.get(grp, []):
            out.append((e["code"], e["name"], e["secid"]))
    return out


def tencent_quotes(ents):
    """返回 {code: {close, prev, open, high, low, ts}}；字段定位按 14 位时间戳相对偏移。"""
    q = ",".join(("sh" if s.startswith("1.") else "sz") + c for c, _, s in ents)
    r = subprocess.run(["curl", "-s", "-m", "20", "https://qt.gtimg.cn/q=" + q],
                       capture_output=True, timeout=30)
    out = {}
    for line in r.stdout.decode("gbk", errors="replace").split(";"):
        if "=" not in line or '"' not in line:
            continue
        f = line.split('"')[1].split("~")
        if len(f) < 40:
            continue
        try:
            ti = next(i for i, v in enumerate(f) if len(v) == 14 and v.isdigit())
            out[f[2]] = {"close": float(f[3]), "prev": float(f[4]), "open": float(f[5]),
                         "high": float(f[ti + 3]), "low": float(f[ti + 4]),
                         "ts": "%s-%s-%s" % (f[ti][:4], f[ti][4:6], f[ti][6:8])}
        except (StopIteration, IndexError, ValueError):
            continue
    return out


def append_csv(path, row):
    """追加一行（表头与 fetch_etf_kline.py 一致）。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        lines = f.read().strip().split("\n")
    if lines and lines[-1].startswith(row[0]):
        return
    lines.append(",".join(str(v) for v in row))
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("\n".join(lines) + "\n")


def main():
    argv = sys.argv
    dry = "--dry-run" in argv
    forced = argv[argv.index("--target") + 1] if "--target" in argv else None

    ents = universe()
    quotes = tencent_quotes(ents)
    print("腾讯返回 %d/%d 只" % (len(quotes), len(ents)))

    now = datetime.datetime.now()
    if not forced:
        # 未收盘时腾讯给的是集合竞价/盘中价（最高最低为 0），不能当收盘价用
        sessions = {q["ts"] for q in quotes.values()}
        if len(sessions) == 1:
            only = sessions.pop()
            if only == now.date().isoformat() and now.time() < datetime.time(15, 5):
                print("腾讯时间戳为今日 %s 且未到 15:05——最新价是集合竞价/盘中价，不是收盘价，退出。" % only)
                return 1

    con = history_db.connect()
    rows, rejects, skipped = [], [], 0
    try:
        for code, name, _ in ents:
            q = quotes.get(code)
            if not q:
                rejects.append((code, name, "无返回"))
                continue
            target = forced or q["ts"]
            if target != q["ts"]:
                rejects.append((code, name, "时间戳 %s 非指定日 %s" % (q["ts"], forced)))
                continue
            prev_db = con.execute(
                "SELECT date, close FROM etf_kline WHERE code=? AND date<? ORDER BY date DESC LIMIT 1",
                (code, target)).fetchone()
            if not prev_db:
                rejects.append((code, name, "库内无 %s 之前的行" % target))
                continue
            if prev_db[0] == target:
                skipped += 1
                continue
            if abs(prev_db[1] - q["prev"]) > TOL:
                rejects.append((code, name, "昨收 %s 与库内 %s=%s 不符" % (q["prev"], prev_db[0], prev_db[1])))
                continue
            if min(q["high"], q["low"]) <= 0 or not (
                    q["low"] <= min(q["open"], q["close"]) and max(q["open"], q["close"]) <= q["high"]):
                rejects.append((code, name, "OHLC 不自洽（高/低为0 说明当日未收盘）"))
                continue
            rows.append((code, target, q["open"], q["high"], q["low"], q["close"], q["close"]))
            print("  %-7s %-18s O%-8s H%-8s L%-8s C%-8s 昨收%s 与库内一致"
                  % (code, name, q["open"], q["high"], q["low"], q["close"], q["prev"]))
        if rejects:
            print("\n拒写（不落库）：")
            for c, n, why in rejects:
                print("  %s %s: %s" % (c, n, why))
            return 1
        if not rows:
            print("\n无需补写（均已是最新日，跳过 %d 只）" % skipped)
            return 0
        if dry:
            print("\n试算：将写入 %d 行" % len(rows))
            return 0
        con.executemany(
            "INSERT OR REPLACE INTO etf_kline(code,date,open,high,low,close,close_raw) "
            "VALUES(?,?,?,?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()
    for code, dt, o, h, l, c, cr in rows:
        append_csv(os.path.join(KDIR, code + ".csv"), [dt, o, h, l, c, cr])
    print("\n落库 %d 行 -> etf_kline，CSV 同步完成" % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
