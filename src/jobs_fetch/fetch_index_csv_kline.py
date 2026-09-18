# -*- coding: utf-8 -*-
"""补齐 data/kline_10y 与 data/kline_full 的指数 CSV 收盘（这两个目录没有写入者）。

消费者：holding_advice（持仓建议引擎）、pe_percentile、research/* 回测。

数据源：主源腾讯日K（东财 push2his 是 IP 级限流，被限时 curl 与浏览器一起失败）；
腾讯未收录的指数（中证2000）退到东财 push2 的 f18（昨收 = 上一交易日收盘）。

写入门槛（缺一不可，防 secid 串号与「把上一交易日的值写成今天」）：
  1. 会话日期由数据源推断（腾讯序列末日 / 显式 --target），绝不默认取今天；
  2. 该日期须晚于 CSV 的末日；
  3. 腾讯路径：与 CSV 最近 5 个交易日逐位一致（历史不足 5 行时用该指数的「昨收」校验）；
     东财 f18 路径：只有单值，无历史可校验，故额外要求 f18 与 CSV 末日收盘**不相等**
     （相等说明拿到的还是已有那一天，日期无法确认，宁可不写）。

用法:
  python src/jobs_fetch/fetch_index_csv_kline.py              # 会话日由腾讯推断
  python src/jobs_fetch/fetch_index_csv_kline.py --target 2026-09-15
  python src/jobs_fetch/fetch_index_csv_kline.py --dry-run
"""
import csv
import datetime
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common.fetch_util import curl_json, num
from src.common.paths import DATA_ROOT

DIRS = [os.path.join(DATA_ROOT, "kline_10y"), os.path.join(DATA_ROOT, "kline_full")]

# 指数名 -> (腾讯代码, 东财 secid)；secid 取自 fetch_index_valuation.INDEXES 权威表
INDEXES = {
    "上证指数": ("sh000001", "1.000001"), "深成指": ("sz399001", "0.399001"),
    "创业板指": ("sz399006", "0.399006"), "科创50": ("sh000688", "1.000688"),
    "恒生指数": ("hkHSI", "100.HSI"), "恒生科技": ("hkHSTECH", "124.HSTECH"),
    "中证A500": ("sh000510", "1.000510"), "中证500": ("sh000905", "1.000905"),
    "上证50": ("sh000016", "1.000016"), "沪深300": ("sh000300", "1.000300"),
    "中证1000": ("sh000852", "1.000852"), "中证2000": ("sh932000", "2.932000"),
    "北证50": ("bj899050", "0.899050"), "中证银行": ("sz399986", "0.399986"),
    "红利指数": ("sh000015", "1.000015"),
    # ── 2026-09-16 增补：蛋卷有 10 年估值历史、但本地缺点位历史的行业指数 ──
    # 它们的估值序列已由 pe_percentile 入库（research/validate_advice_rule 按行业做历史验证要用）
    "国证地产": ("sz399393", "0.399393"), "养老产业": ("sz399812", "0.399812"),
    "证券公司": ("sz399975", "0.399975"), "中证白酒": ("sz399997", "0.399997"),
    "国证食品": ("sz399396", "0.399396"), "中证传媒": ("sz399971", "0.399971"),
    "新能源车": ("sz399417", "0.399417"), "中证煤炭": ("sz399998", "0.399998"),
    "中证环保": ("sh000827", "1.000827"), "TMT50": ("sz399610", "0.399610"),
    "中证军工": ("sz399967", "0.399967"),
    # 消费红利/5G通讯/中证电子：腾讯未收录（中证系代码），点位历史待东财解除限流后补
}

OVERLAP_DAYS = 5
TOL = 0.005
DEVIATION_PCT = 10.0


def unfinished(date_iso, now=None):
    """当日 bar 在 15:05 收盘确认前视为未完成，不得当作收盘写入。"""
    now = now or datetime.datetime.now()
    return date_iso == now.date().isoformat() and now.time() < datetime.time(15, 5)


def tencent(code, n=300):
    """腾讯日K -> (rows, 昨收)。rows 每行 [日期, 开, 收, 高, 低, 量]，昨收取 qt 块第 5 位。"""
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (code, n)
    rows, prev = [], None
    try:
        r = subprocess.run(["curl", "-s", "-m", "20", url], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
        block = ((json.loads(r.stdout) or {}).get("data") or {}).get(code) or {}
        rows = block.get("qfqday") or block.get("day") or []
        qt = (block.get("qt") or {}).get(code) or []
        prev = num(qt[4]) if len(qt) > 4 else None
    except (json.JSONDecodeError, IndexError, TypeError, ValueError):
        pass
    return rows, prev


def eastmoney_prev_close(secid):
    """东财 push2 的 f18（昨收）；push2 不受 K线端点那类限流影响。"""
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?ut=fa5fd1943c7b386f172d6893dbfba10b"
           "&fltt=2&invt=2&secids=%s&fields=f2,f12,f14,f18" % secid)
    for it in ((curl_json(url, timeout=15) or {}).get("data") or {}).get("diff") or []:
        v = num(it.get("f18"))
        if v is not None:
            return v
    return None


def read_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    return rows[0], [r for r in rows[1:] if r]


def append_row(path, head, body, date_iso, close):
    body.append([date_iso, "%.2f" % close])
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(body)


def plan(name, rows, prev, target, dry):
    """返回 (状态, 说明)。状态 ok=已（或将）写入 / skip / bad。"""
    if not rows and target is None:
        return "bad", "腾讯无该指数数据"
    if target is None:
        target = rows[-1][0]
    if not rows:
        return "bad", "腾讯无该指数数据（需 --target 才能走东财 f18 兜底）"
    if unfinished(target):
        return "skip", "%s 当日未收盘（15:05 前不写半截bar）" % target

    # 会话日必须落在这份 CSV 的后一日
    out, bad = [], []
    for d in DIRS:
        path = os.path.join(d, name + ".csv")
        if not os.path.exists(path):
            continue
        head, body = read_csv(path)
        if not body:
            continue
        if body[-1][0] >= target:
            continue
        last_close = float(body[-1][1])
        idx = {r[0]: float(r[2]) for r in rows}
        if target not in idx:
            bad.append((os.path.basename(d), "腾讯序列无 %s" % target))
            continue
        close = idx[target]
        # 校验：优先与 CSV 最近 5 日逐位比对；历史不足时用「昨收」对齐 CSV 末日
        tail = body[-OVERLAP_DAYS:]
        if len(rows) > len(tail):
            diffs = [(dt, cl, idx.get(dt)) for dt, cl in tail if abs(idx.get(dt, -1) - float(cl)) > TOL]
            how = "重叠%d日一致" % len(tail)
        else:
            diffs = [] if (prev is not None and abs(prev - last_close) <= TOL) else [("昨收", last_close, prev)]
            how = "昨收%s对齐" % prev
        if diffs:
            bad.append((os.path.basename(d), "校验不符 %s" % diffs[:2]))
            continue
        dev = abs(close - last_close) / last_close * 100 if last_close else 0
        if dev >= DEVIATION_PCT:
            bad.append((os.path.basename(d), "偏离 %.2f%% 过大" % dev))
            continue
        if not dry:
            append_row(path, head, body, target, close)
        out.append((os.path.basename(d), "%.2f（前收 %s，%s）" % (close, last_close, how)))
    if bad:
        return "bad", bad
    if out:
        return "ok", out
    return "skip", "已有 %s" % target


def main():
    argv = sys.argv
    dry = "--dry-run" in argv
    forced = argv[argv.index("--target") + 1] if "--target" in argv else None

    series = {}
    for name, (tcode, _) in INDEXES.items():
        rows, prev = tencent(tcode)
        series[name] = (rows, prev)
        time.sleep(0.4)

    # 会话日：优先用显式 --target，否则取腾讯各指数末日的最大值（跨指数交叉印证）
    if forced:
        target = forced
    else:
        dates = [r[-1][0] for r, _ in series.values() if r]
        target = max(dates) if dates else None
    print("会话日 %s%s\n" % (target, "（--target 指定）" if forced else "（腾讯推断）"))

    ok, bad = [], []
    for name, (tcode, secid) in INDEXES.items():
        rows, prev = series[name]
        if not rows:
            v = eastmoney_prev_close(secid)
            if v is None:
                bad.append((name, "-", "腾讯与东财都无数据"))
                continue
            # f18 只有单值：必须与 CSV 末日收盘不相等，否则无法确认是新的一个交易日
            if unfinished(target):
                print("  %-8s %-10s %s 当日未收盘（15:05 前不写半截bar）" % (name, "-", target))
                continue
            resolved = True
            for d in DIRS:
                path = os.path.join(d, name + ".csv")
                if not os.path.exists(path):
                    continue
                head, body = read_csv(path)
                if not body or body[-1][0] >= target:
                    continue
                if abs(v - float(body[-1][1])) <= TOL:
                    bad.append((name, os.path.basename(d), "f18 与末日收盘相同，无法确认新交易日"))
                    resolved = False
                    break
                dev = abs(v - float(body[-1][1])) / float(body[-1][1]) * 100
                if dev >= DEVIATION_PCT:
                    bad.append((name, os.path.basename(d), "偏离 %.2f%% 过大" % dev))
                    resolved = False
                    break
                if not dry:
                    append_row(path, head, body, target, v)
                ok.append((name, os.path.basename(d), "%.2f（前收 %s，偏离 %.2f%%）[东财f18]"
                           % (v, body[-1][1], dev)))
            continue
        st, msg = plan(name, rows, prev, target, dry)
        if st == "ok":
            for tag, m in msg:
                ok.append((name, tag, m))
        elif st == "bad":
            if isinstance(msg, list):
                for tag, m in msg:
                    bad.append((name, tag, m))
            else:
                bad.append((name, "-", msg))

    for n, t, m in ok:
        print("  %-8s %-10s %s" % (n, t, m))
    if bad:
        print("\n未写入：")
        for n, t, m in bad:
            print("  %s %s: %s" % (n, t, m))
    print("\n%s：写入 %d 处，问题 %d 项" % ("试算" if dry else "完成", len(ok), len(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
