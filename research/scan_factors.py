# -*- coding: utf-8 -*-
"""板块因子候选扫描（IC 筛选）

现有"关注行业"算法只有 5 个因子（底部评分/短动量/资金流/稳定性/信号），实测 IC 全部 ≤0.11。
本脚本换一批**候选因子**做横截面 IC 扫描，用业界口径筛：
    IC 均值 |mean| > 0.03（日频因子的可用门槛）
    IR = IC均值 / IC标准差 > 0.3（稳定性）
    前半 / 后半 同号（不因市场环境翻符号）
四者同时满足才进入下一步验证，避免再走"样本内好看"的老路。

数据源：sector_kline（价格）、sector_flow_daily（主力/大/超大/小单）。
全部按"T 日收盘后可知"构造，无未来函数；收益取 T+1 收盘。

用法: python research/scan_factors.py
"""
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.common.paths import REPORT_ROOT  # noqa: E402
from src.common import history_db  # noqa: E402

IC_MIN = 0.03
IR_MIN = 0.3
HORIZONS = (1, 5, 10, 20)   # 持有期（交易日）：日频噪声大，低频信号的 IC 通常更清楚


def _std(v):
    if len(v) < 2:
        return None
    m = sum(v) / len(v)
    return (sum((x - m) ** 2 for x in v) / (len(v) - 1)) ** 0.5


def _spearman(xs, ys):
    n = len(xs)
    if n < 30:
        return None

    def rk(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0] * n
        for pos, i in enumerate(order):
            r[i] = pos
        return r
    rx, ry = rk(xs), rk(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def factors_at(cs, i, fl, fl_hist):
    """cs: 收盘序列（升序）；i: 当前下标；fl: 当日资金流五元组；fl_hist: 该板块 [(date, 五元组)]"""
    f = {}
    c = cs[i]
    if not c:
        return f
    r1 = [cs[k] / cs[k - 1] - 1 for k in range(max(1, i - 20), i + 1) if cs[k - 1]]
    if len(r1) >= 19:
        f["vol20"] = _std(r1) * 100
        f["amp20"] = sum(abs(x) for x in r1) / len(r1) * 100
        f["upratio20"] = sum(1 for x in r1 if x > 0) / len(r1)
        f["skew20"] = sum(((x - sum(r1) / len(r1)) / (_std(r1) or 1)) ** 3 for x in r1) / len(r1) if _std(r1) else None
        f["maxup20"] = max(r1) * 100
        f["maxdn20"] = min(r1) * 100
    if len(r1) >= 4 and _std(r1[-5:]) is not None and _std(r1) is not None and _std(r1) > 0:
        f["volratio"] = _std(r1[-5:]) / _std(r1)
    for n, key in ((5, "mom5"), (10, "mom10"), (20, "mom20"), (60, "mom60"), (120, "mom120")):
        if i >= n and cs[i - n]:
            f[key] = (c / cs[i - n] - 1) * 100
    if i >= 1 and cs[i - 1]:
        f["rev1"] = -(c / cs[i - 1] - 1) * 100
    if "mom5" in f:
        f["rev5"] = -f["mom5"]
    for n, key in ((20, "dd20"), (60, "dd60"), (250, "dd250"), (120, "dd120")):
        if i >= n - 1:
            hi = max(cs[i - n + 1:i + 1])
            if hi:
                f[key] = (c / hi - 1) * 100
    for n, key in ((20, "magap20"), (60, "magap60")):
        if i >= n - 1:
            ma = sum(cs[i - n + 1:i + 1]) / n
            if ma:
                f[key] = (c / ma - 1) * 100
    if i >= 59:
        ma5 = sum(cs[i - 4:i + 1]) / 5
        ma10 = sum(cs[i - 9:i + 1]) / 10
        ma20 = sum(cs[i - 19:i + 1]) / 20
        ma60 = sum(cs[i - 59:i + 1]) / 60
        f["align"] = sum((ma5 > ma10, ma10 > ma20, ma20 > ma60))
        f["mastack"] = (ma5 / ma60 - 1) * 100
    if "mom5" in f and "mom20" in f:
        f["accel"] = f["mom5"] - f["mom20"] / 4.0
    st = 0
    if i >= 1:
        for k in range(i, 0, -1):
            if cs[k] == cs[k - 1]:
                break
            d = 1 if cs[k] > cs[k - 1] else -1
            if st == 0:
                st = d
            elif (d > 0) == (st > 0):
                st += d
            else:
                break
        f["streak"] = st
    for n, key in ((20, "pos20"), (60, "pos60")):
        if i >= n - 1:
            lo, hi = min(cs[i - n + 1:i + 1]), max(cs[i - n + 1:i + 1])
            f[key] = (c - lo) / (hi - lo) if hi > lo else 0.5
    if i >= 59:
        peak, mdd = cs[i - 59], 0.0
        for k in range(i - 59, i + 1):
            peak = max(peak, cs[k])
            if peak:
                mdd = min(mdd, cs[k] / peak - 1)
        f["mdd60"] = mdd * 100
    # 资金流
    if fl:
        main, small, med, large, sup = fl
        if main is not None:
            f["fmain"] = main
        if sup is not None:
            f["fsuper"] = sup
        if large is not None:
            f["flarge"] = large
        if small is not None:
            f["fsmall"] = small
        if large is not None and sup is not None:
            f["big_ratio"] = (large + sup) / (abs(large) + abs(sup) + 1e-9)
        if main is not None and small is not None and abs(small) > 1e-9:
            f["main_minus_small"] = main - small
    if fl_hist:
        vals = [x[1][0] for x in fl_hist if x[1] and x[1][0] is not None]
        if len(vals) >= 20:
            f["f20"] = sum(vals[-20:])
            f["f5"] = sum(vals[-5:])
            f["f_accel"] = sum(vals[-3:]) - sum(vals[-10:]) / 10 * 3
            f["f_consec"] = 0
            for v in reversed(vals):
                if v > 0:
                    f["f_consec"] += 1
                else:
                    break
            s = _std(vals[-20:])
            if s:
                f["f_z"] = (vals[-1] - sum(vals[-20:]) / len(vals)) / s if len(vals) >= 20 else None
    return f


def build(conn, need_flow=True):
    """need_flow=False 时走长历史模式：只用 sector_kline（2023-01 起 896 个交易日），
    跳过资金流因子 —— 东财资金流历史硬上限约 120 交易日，长窗口只能靠价格类因子。"""
    kl = defaultdict(list)
    for code, d, v in conn.execute(
            "SELECT code, date, close FROM sector_kline WHERE close IS NOT NULL ORDER BY code, date"):
        kl[code].append((d, v))
    flow = defaultdict(dict)
    if need_flow:
        for d, code, m, s, md, lg, sp in conn.execute(
                "SELECT date, code, main_net_wan, small_net_wan, med_net_wan, large_net_wan, "
                "super_net_wan FROM sector_flow_daily"):
            flow[code][d] = (m, s, md, lg, sp)

    panel = defaultdict(dict)          # date -> code -> {factor: v, 'close': c}
    for code, sq in kl.items():
        ds = [d for d, _ in sq]
        cs = [v for _, v in sq]
        fh = flow.get(code, {})
        if need_flow and not fh:
            continue
        hist = [(d, fh[d]) for d in sorted(fh)]
        hi_by_date = {d: j for j, (d, _) in enumerate(hist)}
        for i in range(120, len(ds)):
            d = ds[i]
            if need_flow:
                if d not in fh:
                    continue
                j = hi_by_date[d]
                fl, wh = fh[d], hist[max(0, j - 20):j + 1]
            else:
                fl, wh = None, None
            f = factors_at(cs, i, fl, wh)
            if not f:
                continue
            f["close"] = cs[i]
            panel[d][code] = f
    return panel, []


def main():
    need_flow = "--long" not in sys.argv
    conn = history_db.connect(readonly=True)
    try:
        panel, _ = build(conn, need_flow=need_flow)
    finally:
        conn.close()
    days = sorted(panel)
    years = sorted({d[:4] for d in days})
    half = len(days) // 2
    mode = "长历史·仅价格因子" if not need_flow else "含资金流因子"
    print("因子面板 %d 天（%s ~ %s）｜%s｜日均 %d 板块" % (
        len(days), days[0], days[-1], mode, sum(len(panel[d]) for d in days) // len(days)))

    names = sorted({k for d in days for v in panel[d].values() for k in v if k != "close"})
    rows = []
    for name in names:
        for h in HORIZONS:
            ics, by_year = [], {y: [] for y in years}
            for i, T in enumerate(days):
                if i + h >= len(days):
                    break
                xs, ys = [], []
                nxt = panel[days[i + h]]
                for code, v in panel[T].items():
                    x = v.get(name)
                    b = nxt.get(code, {}).get("close")
                    a = v.get("close")
                    if x is None or not a or not b:
                        continue
                    xs.append(x)
                    ys.append((b / a - 1) * 100)
                ic = _spearman(xs, ys)
                if ic is not None:
                    ics.append(ic)
                    by_year[T[:4]].append(ic)
            if len(ics) < 30:
                continue
            m = sum(ics) / len(ics)
            sd = _std(ics)
            if not sd:
                continue
            # 重叠样本使有效观测数≈n/h，t 值按此收缩（否则持有期越长 t 越虚高）
            t = m / (sd / math.sqrt(len(ics) / h))
            h1 = sum(ics[:len(ics) // 2]) / max(1, len(ics[:len(ics) // 2]))
            h2 = sum(ics[len(ics) // 2:]) / max(1, len(ics[len(ics) // 2:]))
            ym = {y: (sum(v) / len(v) if len(v) >= 20 else None) for y, v in by_year.items()}
            rows.append({"name": name, "hold": h, "ic": m, "ir": m / sd, "t": t,
                         "n": len(ics), "h1": h1, "h2": h2, "ym": ym,
                         "pos": sum(1 for x in ics if x > 0) / len(ics)})
    # 每个因子取 |IC| 最大的持有期
    best_by_factor = {}
    for r in rows:
        k = r["name"]
        if k not in best_by_factor or abs(r["ic"]) > abs(best_by_factor[k]["ic"]):
            best_by_factor[k] = r
    rows = sorted(best_by_factor.values(), key=lambda r: -abs(r["ic"]))

    yhdr = "".join(" %s |" % y for y in years)
    L = ["# 板块因子候选 IC 扫描\n",
         "> 面板 %d 天（%s ~ %s）｜%s｜持有期 %s 日 ｜ 门槛：|IC|>%.2f 且 IR>%.1f 且 跨年同号 ｜ by research/scan_factors.py\n"
         % (len(days), days[0], days[-1], mode, "/".join(map(str, HORIZONS)), IC_MIN, IR_MIN),
         "| 因子 | 最佳持有期 | IC 均值 | IR | t 值(按重叠收缩) | 前半 | 后半 |" + yhdr + " 判定 |",
         "|---|---|---|---|---|---|---|" + "---|" * len(years) + "---|"]
    print("\n%-14s %5s %8s %7s %8s %8s %8s  %s" % (
        "因子", "持有期", "IC均值", "IR", "t值", "前半", "后半",
        " ".join("%7s" % y for y in years)))
    passed = []
    for r in rows:
        ok_ic = abs(r["ic"]) > IC_MIN
        ok_ir = abs(r["ir"]) > IR_MIN
        yy = [v for v in r["ym"].values() if v is not None]
        ok_year = len(yy) >= 2 and (all(v > 0 for v in yy) or all(v < 0 for v in yy))
        verdict = "候选 ✓" if (ok_ic and ok_ir and ok_year) else (
            "IC弱" if not ok_ic else ("不稳" if not ok_ir else "跨年反号"))
        if verdict == "候选 ✓":
            passed.append(r)
        ycells = "".join(" %s |" % ("%+.3f" % r["ym"][y] if r["ym"][y] is not None else "  -")
                         for y in years)
        L.append("| %s | T+%d | %+.4f | %+.2f | %+.2f | %+.4f | %+.4f |%s %s |" % (
            r["name"], r["hold"], r["ic"], r["ir"], r["t"], r["h1"], r["h2"], ycells, verdict))
        print("%-14s T+%-3d %+8.4f %+7.2f %+8.2f %+8.4f %+8.4f  %s  %s" % (
            r["name"], r["hold"], r["ic"], r["ir"], r["t"], r["h1"], r["h2"],
            " ".join("%+7.3f" % (r["ym"][y] if r["ym"][y] is not None else 0) for y in years),
            verdict))

    L.append("\n## 结论\n")
    if passed:
        L.append("通过筛选的因子 **%d 个**：%s\n" % (
            len(passed), "、".join("`%s`(IC %+.3f)" % (r["name"], r["ic"]) for r in passed)))
        L.append("下一步：这些因子去重（两两相关 >0.7 只留一个）后进组合，再走 10 万组网格 + 分块留出验证。\n")
    else:
        L.append("**没有因子通过筛选。** 说明在「板块日频收益」这个目标上，"
                 "价格形态与资金流结构类因子同样没有可用预测力。\n")
    L.append("\n> 仅个人研究，不构成投资建议。\n")

    out = os.path.join(REPORT_ROOT, "因子扫描_20260914.md" if need_flow else "因子扫描_长历史_20260914.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("\n通过筛选 %d 个 | [OK] 报告已写入 %s" % (len(passed), out))


if __name__ == "__main__":
    main()
