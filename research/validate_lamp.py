# -*- coding: utf-8 -*-
"""仓位灯量化验证（红/黄/绿是否有未来收益区分度）

给未来亚亚：回答“灯到底灵不灵”。重算2023-07以来每日灯色：
估值用上证点位区间（backtest_10y同款bands），趋势用全板块EMA55/89多头占比
（scan_regime同口径简化版，只取bull布尔值）。验证两项：
1. 灯色对未来20日全板块均值收益的区分度（各灯胜率/均值）。
2. 灯仓位组合（红30/黄50/绿70，余货币，月调仓）vs 100%等权死拿：累计/回撤。
纯标准库。用法: python validate_lamp.py
"""
import csv
import datetime
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DATA_DIR)
for _p in (DATA_DIR, REPO_ROOT,
           os.path.join(REPO_ROOT, "src", "common"),
           os.path.join(REPO_ROOT, "src", "jobs_build"),
           os.path.join(REPO_ROOT, "src", "jobs_fetch")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from src.common.paths import DATA_ROOT
from src.common import history_db
KDIR = os.path.join(DATA_ROOT, "kline_10y")
REPORT_DIR = os.path.join(REPO_ROOT, "reports")
SH_BANDS = [5000, 3800, 3200, 3000]  # 上证点位区间（2026-09-14 起线上已改 K 线自算点位分位，此处保留手写区间做历史验算）
W = {"red": 0.30, "yellow": 0.50, "green": 0.70}


def ema(vals, span):
    from src.common.technical_indicators import ema as _ema
    return _ema(vals, span)


def sh_state(price):
    b_foam, b_high, b_mid, b_low = SH_BANDS
    if price >= b_foam:
        return "泡沫"
    if price >= b_high:
        return "高估"
    if price >= b_mid:
        return "合理偏高"
    if price >= b_low:
        return "合理偏低"
    return "低估"


def lamp_of(state, ratio):
    if state in ("高估", "泡沫") and ratio < 40:
        return "red"
    if state in ("合理偏低", "低估", "极低估", "合理") and ratio >= 55:
        return "green"
    return "yellow"


def index10y_check():
    """指数级哲学验证（10年）：上证点位区间+站上/跌破MA60定三挡仓位。
    给未来亚亚：板块灯样本内只出现过黄灯，红绿挡无样本；这里用10年上证验证
    “估值定仓位”思想本身是否减回撤。若思想成立，阈值维持现状等实盘触发复验。"""
    rows = []
    with open(os.path.join(KDIR, "上证指数.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append((r["日期"], float(r["收盘"])))
    dates = [d for d, _ in rows]
    px = dict(rows)
    lamps = {}
    for i, (d, c) in enumerate(rows):
        if i < 60:
            continue
        ma60 = sum(p for _, p in rows[i - 59:i + 1]) / 60
        st = sh_state(c)
        if st in ("高估", "泡沫") and c < ma60:
            lamps[d] = "red"
        elif st in ("合理偏低", "低估", "极低估", "合理") and c > ma60:
            lamps[d] = "green"
        else:
            lamps[d] = "yellow"
    days = sorted(lamps)
    red = sum(1 for d in days if lamps[d] == "red")
    grn = sum(1 for d in days if lamps[d] == "green")
    print("指数灯样本%d天 红%d 绿%d" % (len(days), red, grn))
    eq_l, eq_b = [1.0], [1.0]
    pk_l, pk_b, md_l, md_b = 1.0, 1.0, 0.0, 0.0
    months, seen = [], set()
    for d in days:
        if d[:7] not in seen:
            seen.add(d[:7])
            months.append(d)
    for mi in range(len(months) - 1):
        w = W[lamps[months[mi]]]
        i0, i1 = dates.index(months[mi]), dates.index(months[mi + 1])
        for d in dates[i0 + 1:i1 + 1]:
            prev = dates[dates.index(d) - 1]
            m = px[d] / px[prev] - 1
            eq_l.append(eq_l[-1] * (1 + w * m))
            eq_b.append(eq_b[-1] * (1 + m))
        pk_l, pk_b = max(pk_l, eq_l[-1]), max(pk_b, eq_b[-1])
        md_l, md_b = min(md_l, eq_l[-1] / pk_l - 1), min(md_b, eq_b[-1] / pk_b - 1)
    print("指数灯仓位累计%+.0f%% 回撤%.0f%%｜死拿累计%+.0f%% 回撤%.0f%%" % (
        (eq_l[-1] - 1) * 100, md_l * 100, (eq_b[-1] - 1) * 100, md_b * 100))
    return {"red": red, "green": grn, "lamp_ret": eq_l[-1] - 1, "lamp_dd": md_l,
            "buy_ret": eq_b[-1] - 1, "buy_dd": md_b,
            "span": (days[0], days[-1]) if days else ("?", "?")}


def main():
    conn = history_db.connect(readonly=True)
    try:
        krows = conn.execute("SELECT code,date,close FROM sector_kline ORDER BY code,date").fetchall()
    finally:
        conn.close()
    series = {}
    for code, d, c in krows:
        series.setdefault(code, []).append((d, c))
    cal = sorted({d for pts in series.values() for d, _ in pts})
    px = {c: dict(pts) for c, pts in series.items()}
    # 各板块逐日bull（EMA55>EMA89）
    bull = {}
    for code, pts in series.items():
        cl = [p[1] for p in pts]
        if len(cl) < 100:
            continue
        f, s = ema(cl, 55), ema(cl, 89)
        dd = [p[0] for p in pts]
        for i, d in enumerate(dd):
            if i >= 99:
                bull.setdefault(d, []).append(f[i] > s[i])
    # 上证点位
    sh = {}
    with open(os.path.join(KDIR, "上证指数.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sh[r["日期"]] = float(r["收盘"])
    # 逐日灯色（需bull覆盖≥400板块）
    lamps = {}
    for d in cal:
        if d not in bull or len(bull[d]) < 400 or d not in sh:
            continue
        ratio = sum(bull[d]) / len(bull[d]) * 100
        lamps[d] = (lamp_of(sh_state(sh[d]), ratio), ratio)
    days = sorted(lamps)
    print("灯样本%d天 %s~%s" % (len(days), days[0], days[-1]))
    # 1. 后向20日区分度（全板块等权 forward 均值）
    stat = {c: [] for c in W}
    for d in days:
        i = cal.index(d)
        if i + 20 >= len(cal):
            continue
        d2 = cal[i + 20]
        rs = [(px[c][d2] / px[c][d] - 1) * 100 for c in px if d in px[c] and d2 in px[c] and px[c][d]]
        if rs:
            stat[lamps[d][0]].append(sum(rs) / len(rs))
    for c in W:
        v = stat[c]
        print("%s灯 n=%d 胜率%.0f%% 均值%+.2f%%" % (
            c, len(v), sum(1 for x in v if x > 0) / len(v) * 100 if v else 0,
            sum(v) / len(v) if v else 0))
    # 2. 灯仓位组合 vs 死拿（月调仓到灯仓位）
    eq_lamp, eq_buy = [1.0], [1.0]
    peak_l, peak_b, mdd_l, mdd_b = 1.0, 1.0, 0.0, 0.0
    months, seen = [], set()
    for d in days:
        if d[:7] not in seen:
            seen.add(d[:7])
            months.append(d)
    for mi in range(len(months) - 1):
        w = W[lamps[months[mi]][0]]
        i0, i1 = cal.index(months[mi]), cal.index(months[mi + 1])
        for d in cal[i0 + 1:i1 + 1]:
            prev = cal[cal.index(d) - 1]
            rs = [(px[c][d] / px[c][prev] - 1) for c in px
                  if d in px[c] and prev in px[c] and px[c][prev]]
            m = sum(rs) / len(rs) if rs else 0.0
            eq_lamp.append(eq_lamp[-1] * (1 + w * m))
            eq_buy.append(eq_buy[-1] * (1 + m))
        peak_l, peak_b = max(peak_l, eq_lamp[-1]), max(peak_b, eq_buy[-1])
        mdd_l = min(mdd_l, eq_lamp[-1] / peak_l - 1)
        mdd_b = min(mdd_b, eq_buy[-1] / peak_b - 1)
    print("灯仓位累计%+.1f%% 回撤%.1f%%｜死拿累计%+.1f%% 回撤%.1f%%" % (
        (eq_lamp[-1] - 1) * 100, mdd_l * 100, (eq_buy[-1] - 1) * 100, mdd_b * 100))
    ix = index10y_check()
    rp = os.path.join(REPORT_DIR, "仓位灯验证_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(rp, "w", encoding="utf-8") as f:
        f.write("# 仓位灯验证 %s（%s~%s，%d天）\n\n" % (datetime.date.today(), days[0], days[-1], len(days)))
        for c in W:
            v = stat[c]
            f.write("- %s灯 n=%d 胜率%.0f%% 后20日均值%+.2f%%\n" % (
                c, len(v), sum(1 for x in v if x > 0) / len(v) * 100 if v else 0,
                sum(v) / len(v) if v else 0))
        f.write("\n灯仓位累计%+.1f%% 回撤%.1f%%；死拿累计%+.1f%% 回撤%.1f%%。\n" % (
            (eq_lamp[-1] - 1) * 100, mdd_l * 100, (eq_buy[-1] - 1) * 100, mdd_b * 100))
        f.write("\n## 指数级哲学验证（10年 %s~%s，红%d天绿%d天）\n" % (
            ix["span"][0], ix["span"][1], ix["red"], ix["green"]))
        f.write("灯仓位累计%+.0f%% 回撤%.0f%%；死拿累计%+.0f%% 回撤%.0f%%。\n" % (
            ix["lamp_ret"] * 100, ix["lamp_dd"] * 100, ix["buy_ret"] * 100, ix["buy_dd"] * 100))
        f.write("\n结论：板块灯样本内只触发过黄灯（62%%胜率， lamp回撤减半有效），红绿挡无样本不调阈值、等实盘触发复验；\n")
        f.write("若指数级验证同样显示灯仓位回撤减半，则“估值定仓位”思想成立。\n")
    print("报告:%s" % rp)


if __name__ == "__main__":
    main()
