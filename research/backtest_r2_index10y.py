# -*- coding: utf-8 -*-
"""R2动量指数10年穿越验证（2016-2026，真牛熊）

给未来亚亚：回答“策略见过真熊市吗”。用 data/kline_10y 15指数CSV
（与板块代理同口径：收盘>MA20>MA60 + N日加权回归得分=年化×R2，
月度TOP2 + 10%换仓缓冲 + -8%止损 + 空窗切货币），基准=沪深300买持。
分段输出2018/2022熊、2020/2024牛，裸策略回撤若超10%则必须叠仓位灯。
纯标准库。用法: python backtest_r2_index10y.py [--window 40]
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research._engine import wlinreg_stats  # noqa: E402

try:
    from src.common.paths import DATA_ROOT as _DATA_ROOT, REPORT_ROOT as _REPORT_ROOT
    DATA_DIR = _DATA_ROOT
    KDIR = os.path.join(_DATA_ROOT, "kline_10y")
    REPORT_DIR = _REPORT_ROOT
except ImportError:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))
    _alt = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kline_10y")
    KDIR = _alt if os.path.isdir(_alt) else os.path.join(DATA_DIR, "kline_10y")
    REPORT_DIR = os.path.join(os.path.dirname(DATA_DIR), "reports")


def score(cl, window):
    s = wlinreg_stats(cl, window)
    if s is None:
        return None
    ann, r2 = s[0], s[1]
    return None if ann <= 0 else (ann * r2, ann, r2)


def load():
    series = {}
    for fn in os.listdir(KDIR):
        if not fn.endswith(".csv"):
            continue
        name = fn[:-4]
        with open(os.path.join(KDIR, fn), encoding="utf-8-sig") as f:
            rows = []
            for r in csv.DictReader(f):
                try:
                    rows.append((r["日期"].strip().strip("'"), float(r["收盘"].strip().strip("'"))))
                except (ValueError, AttributeError):
                    continue  # 2026-09脏数据（引号/体内重复表头）直接丢
        series[name] = sorted(rows)
    return series


def run(window=40, topn=2, stop=-8.0, buffer=0.10):
    series = load()
    px = {k: dict(v) for k, v in series.items()}
    cal = sorted({d for v in series.values() for d, _ in v})
    months, seen = [], set()
    for d in cal:
        m = d[:7]
        if m not in seen:
            seen.add(m)
            months.append(d)
    warm = window + 70
    months = [m for m in months if cal.index(m) >= warm]
    eq, bench = [1.0], [1.0]
    peak, maxdd, empty, turns = 1.0, 0.0, 0, []
    hold, hs = [], 0.0
    entry = {}
    hs300 = px.get("沪深300", {})
    # 分段统计：熊2018、牛2020-21、熊2022、牛2024后
    segs = {"2018熊": ("2018-01-01", "2018-12-31"), "2022熊": ("2022-01-01", "2022-12-31"),
            "2020牛": ("2020-01-01", "2021-12-31"), "2024牛": ("2024-01-01", "2026-09-01")}
    seg_eq = {k: [1.0] for k in segs}
    for mi in range(len(months) - 1):
        rb, nxt = months[mi], months[mi + 1]
        scored = []
        for name, pts in series.items():
            idx = next((i for i, (d, _) in enumerate(pts) if d > rb), len(pts)) - 1
            if idx < warm or pts[idx][0] != rb:
                continue
            cl = [p[1] for p in pts[:idx + 1]]
            if len(cl) < 60:
                continue
            m20 = sum(cl[-20:]) / 20
            m60 = sum(cl[-60:]) / 60
            if not (cl[-1] > m20 > m60):
                continue
            r = score(cl, window)
            if r:
                scored.append((r[0], name))
        scored.sort(reverse=True)
        cand = [c for _, c in scored[:topn]]
        cs = sum(s for s, _ in scored[:topn]) / topn if cand else 0
        if not cand:
            empty += 1
        if hold and cand and cs <= hs * (1 + buffer):
            cand, cs = list(hold), hs
        turns.append(len(set(cand) - set(hold)) / topn if topn else 0)
        hold, hs = cand, cs
        entry = {c: px[c][rb] for c in hold if rb in px[c]}
        stopped = set()  # 2026-09-09补：止损后当月剩余按货币，不再复活（复活=免费过滤下跌，印假收益）
        i0, i1 = cal.index(rb), cal.index(nxt)
        for d in cal[i0 + 1:i1 + 1]:
            legs = []
            prev = cal[cal.index(d) - 1]
            for c in hold:
                if c in stopped:
                    legs.append(0.0)
                    continue
                if c not in px or d not in px[c] or c not in entry or not entry[c]:
                    continue
                if px[c][d] / entry[c] - 1 <= stop / 100:
                    stopped.add(c)
                    legs.append(0.0)
                    continue
                legs.append(px[c][d] / px[c][prev] - 1 if prev in px[c] else 0.0)
            r = sum(legs) / len(legs) if legs else 0.0
            eq.append(eq[-1] * (1 + r))
            b = hs300[d] / hs300[prev] - 1 if d in hs300 and prev in hs300 and hs300[prev] else 0.0
            bench.append(bench[-1] * (1 + b))
            for k, (s0, s1) in segs.items():
                if s0 <= d <= s1:
                    seg_eq[k].append(seg_eq[k][-1] * (1 + r))
        peak = max(peak, eq[-1])
        maxdd = min(maxdd, eq[-1] / peak - 1)
    years = (datetime.date.fromisoformat(cal[-1]) - datetime.date.fromisoformat(cal[0])).days / 365.25
    cagr = (eq[-1]) ** (1 / years) - 1 if eq[-1] > 0 else -1
    cagr_b = (bench[-1]) ** (1 / years) - 1 if bench[-1] > 0 else -1
    return {"eq": eq[-1] - 1, "bench": bench[-1] - 1, "cagr": cagr, "cagr_b": cagr_b,
            "maxdd": maxdd, "turn": sum(turns) / len(turns) * 100 if turns else 0,
            "empty": empty, "n": len(months) - 1, "span": (months[0], months[-1]),
            "seg": {k: v[-1] - 1 for k, v in seg_eq.items()}}


def main():
    w = int(sys.argv[sys.argv.index("--window") + 1]) if "--window" in sys.argv else 40
    m = run(window=w)
    print("R2指数10年 window=%d TOP2 stop-8%% buf10%%" % w)
    print("区间 %s~%s 调仓%d 空仓%d月" % (m["span"][0], m["span"][1], m["n"], m["empty"]))
    print("策略累计%+.0f%% 年化%.1f%% 回撤%.0f%% 换手%.0f%%" % (
        m["eq"] * 100, m["cagr"] * 100, m["maxdd"] * 100, m["turn"]))
    print("沪深300买持%+.0f%% 年化%.1f%%" % (m["bench"] * 100, m["cagr_b"] * 100))
    for k, v in m["seg"].items():
        print(" %s 策略%+.0f%%" % (k, v * 100))
    rp = os.path.join(REPORT_DIR, "R2指数10年_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(rp, "w", encoding="utf-8") as f:
        f.write("# R210年穿越 %s window=%d\n\n" % (datetime.date.today(), w))
        f.write("区间%s~%s 调仓%d 空仓%d。\n\n" % (m["span"][0], m["span"][1], m["n"], m["empty"]))
        f.write("策略累计%+.0f%% 年化%.1f%% 回撤%.0f%%；沪深300买持%+.0f%% 年化%.1f%%。\n\n" % (
            m["eq"] * 100, m["cagr"] * 100, m["maxdd"] * 100, m["bench"] * 100, m["cagr_b"] * 100))
        for k, v in m["seg"].items():
            f.write("- %s 策略%+.0f%%\n" % (k, v * 100))
    print("报告:%s" % rp)


if __name__ == "__main__":
    main()
