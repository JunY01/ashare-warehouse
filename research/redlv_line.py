# -*- coding: utf-8 -*-
"""中证红利低波动（H30269）买卖算法验证台 —— "这只到底能不能择时"

背景：40 日收益差那套尺子（相对全市场的均值回复）在红利低波动上**三种口径全为负**
（reports/红利40日收益差十万次迭代_20260916.md）。本台子换一批候选信号重问同一个问题：
在没有估值历史、只有一条价格序列（2013-12-19 起）的条件下，**能不能找到一只
"买了能多赚、卖了能少亏"的规则**？

候选信号（全部无前视：只用 ≤T 收盘，分位用扩张窗口，最少 500 个观测）：
  ① 相对强弱均值回复：红利低波 − 基准 的 N 日收益差（基准 = 中证全指 / 中证银行 / 沪深300）
  ② 自身超跌：N 日跌幅、距 250 日高点回撤
  ③ 趋势乖离：收盘/MA20、/MA60、/MA250 的偏离度
  ④ 波动恐慌：20 日已实现波动率分位
  ⑤ 强弱指标：RSI(14)

评估口径：
  · 触发日 → 未来 20/60/120 日**含分红**收益（红利低波股息率 4.33%/年按日计提），
    与"全样本同期收益中位数"比，得到该信号的超额；
  · **独立轮次**：同一信号的触发日间隔 ≤20 交易日算同一轮，只取首日（防一个持续状态
    被当成 60 条独立样本）；
  · **两段同号**：2013-12~2020-06 与 2020-07~2026-09 各自算一遍，符号必须一致——
    红利在 2016 前后与 2021 后是两个完全不同的市场；
  · 判定门槛：独立轮次 ≥8 且两段同号且 |中位超额| ≥ 1%，才算"能验证出来"。

纯标准库。用法:
  python research/redlv_line.py --scan          # 全部候选信号扫一遍
  python research/redlv_line.py --detail rsi    # 看某个信号/家族的逐轮明细
"""
import argparse
import datetime
import os
import statistics as st
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "src", "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from src.common import history_db  # noqa: E402

DIV = "csH30269"                     # 中证红利低波动
DIV_YIELD = 4.33                     # 股息率（%，含分红口径用）
BENCHES = {"全指": "sh000985", "银行": "sz399986", "沪深300": "sh000300"}
FWD = (20, 60, 120)
EP_GAP = 20                          # 独立轮次：间隔 ≤20 交易日算同一轮
MIN_EP = 8                           # 独立轮次下限
MIN_PCT_HIST = 500                   # 扩张分位最少观测
SPLIT = "2020-07-01"                 # 两段分割点
EDGE = 1.0                           # 中位超额门槛（%）


def load(code):
    with history_db.connect(readonly=True) as conn:
        return {d: c for d, c in conn.execute(
            "SELECT date, close FROM index_daily WHERE code=? ORDER BY date", (code,)) if c}


def pctile_run(series):
    """扩张分位：第 i 个交易日在其自身历史中的分位（0~100），历史不足 500 个返回 None。"""
    out = [None] * len(series)
    hist = []
    for i, v in enumerate(series):
        hist.append(v)
        if len(hist) >= MIN_PCT_HIST:
            base = hist[:-1]
            out[i] = 100.0 * sum(1 for x in base if x <= v) / len(base)
    return out


def cluster(dates_idx, is_hit):
    """把命中日聚成独立轮次（间隔 ≤EP_GAP 合并），返回每轮首日的下标列表。"""
    idx = [i for i in range(len(is_hit)) if is_hit[i]]
    out = []
    last = None
    for i in idx:
        if last is not None and dates_idx[i] - dates_idx[last] <= EP_GAP:
            last = i
            continue
        last = i
        out.append(i)
    return out


def evaluate(dates, div, fields, is_hit, label):
    """is_hit[i]=True 表示第 i 日处于该状态（买/卖）。返回统计 dict。"""
    n = len(dates)
    tr = [(div[dates[i]] / div[dates[i - 1]] - 1 + (DIV_YIELD / 100.0) / 250) for i in range(1, n)]
    fw = {}
    for h in FWD:
        arr = [None] * n
        for i in range(n - h):
            e = 1.0
            for k in range(i + 1, i + h + 1):
                e *= 1 + tr[k - 1]
            arr[i] = (e - 1) * 100
        fw[h] = arr
    res = {"label": label, "n_hit": sum(1 for x in is_hit if x)}
    for h in FWD:
        valid = [fw[h][i] for i in range(n) if fw[h][i] is not None]
        med_all = st.median(valid)
        hits = [(i, fw[h][i]) for i in cluster(list(range(n)), is_hit) if fw[h][i] is not None]
        if len(hits) < 2:
            res[h] = None
            continue
        exc = [v - med_all for _i, v in hits]
        a = [v - med_all for i, v in hits if dates[i] < SPLIT]
        b = [v - med_all for i, v in hits if dates[i] >= SPLIT]
        res[h] = {"ep": len(hits), "med_ex": st.median(exc),
                  "pos": sum(1 for x in exc if x > 0) / len(exc),
                  "seg": (st.median(a) if a else None, st.median(b) if b else None)}
    res["first_dates"] = [dates[i] for i in cluster(list(range(n)), is_hit)]
    return res


def signals(dates, div, bench_map):
    """构造候选信号 → {名字: (是否命中数组, 方向)}。方向 buy=低位买 / sell=高位卖。"""
    n = len(dates)
    closes = [div[d] for d in dates]
    out = []

    def add(name, side, hit):
        out.append((name, side, hit))

    def ma(w):
        s = [None] * n
        for i in range(w - 1, n):
            s[i] = sum(closes[i - w + 1:i + 1]) / w
        return s

    def pct_ok(arr, lo=None, hi=None):
        p = pctile_run([x if x is not None else 0.0 for x in arr])
        hit = [False] * n
        for i in range(n):
            if arr[i] is None or p[i] is None:
                continue
            if lo is not None and p[i] <= lo:
                hit[i] = True
            elif hi is not None and p[i] >= hi:
                hit[i] = True
        return hit

    # ① 相对强弱均值回复
    for bname, b in bench_map.items():
        dates_b = [d for d in dates if d in b]
        if len(dates_b) < 300:
            continue
        for w in (20, 40, 60, 120):
            n_b = len(dates_b)
            rs = [None] * n_b
            for i in range(w, n_b):
                rs[i] = ((div[dates_b[i]] / div[dates_b[i - w]] - 1)
                         - (b[dates_b[i]] / b[dates_b[i - w]] - 1)) * 100
            idx = {d: i for i, d in enumerate(dates_b)}
            arr = [rs[idx[d]] if d in idx else None for d in dates]
            add("rs_%s_%d" % (bname, w), "buy", pct_ok(arr, lo=20))
            add("rs_%s_%d" % (bname, w), "sell", pct_ok(arr, hi=80))
    # ② 自身超跌 / 回撤
    for w in (20, 60, 120):
        ret = [None] * n
        for i in range(w, n):
            ret[i] = (closes[i] / closes[i - w] - 1) * 100
        add("ret%d" % w, "buy", pct_ok(ret, lo=20))
    dd = [None] * n
    for i in range(250, n):
        dd[i] = (closes[i] / max(closes[i - 250:i + 1]) - 1) * 100
    add("dd250", "buy", pct_ok(dd, lo=20))
    add("dd250_hard", "buy", [bool(d is not None and d <= -15) for d in dd])
    # ③ 趋势乖离
    for w in (20, 60, 250):
        m = ma(w)
        bias = [((closes[i] / m[i] - 1) * 100 if m[i] else None) for i in range(n)]
        add("bias%d" % w, "buy", pct_ok(bias, lo=20))
        add("bias%d" % w, "sell", pct_ok(bias, hi=80))
    # ④ 波动恐慌
    vol = [None] * n
    for i in range(20, n):
        rs = [closes[k] / closes[k - 1] - 1 for k in range(i - 19, i + 1)]
        vol[i] = st.pstdev(rs) * 100
    add("vol20_high", "buy", pct_ok(vol, hi=80))
    add("vol20_low", "sell", pct_ok(vol, lo=20))
    # ⑤ RSI(14)
    rsi = [None] * n
    ag = al = None
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        g, l = max(ch, 0), max(-ch, 0)
        if i == 14:
            ag, al = g / 14, l / 14
        elif i > 14:
            ag = (ag * 13 + g) / 14
            al = (al * 13 + l) / 14
        if ag is not None and al is not None and (ag + al) > 0:
            rsi[i] = 100 * ag / (ag + al)
    add("rsi14", "buy", [bool(x is not None and x <= 35) for x in rsi])
    add("rsi14", "sell", [bool(x is not None and x >= 70) for x in rsi])
    return out


def scan():
    div = load(DIV)
    bench = {k: load(v) for k, v in BENCHES.items()}
    dates = sorted(div)
    print("红利低波动 %s~%s 共 %d 个交易日；分割点 %s" % (dates[0], dates[-1], len(dates), SPLIT))
    print("基准：" + "、".join("%s(%s)" % (k, v) for k, v in BENCHES.items()))
    rows = []
    for name, side, hit in signals(dates, div, bench):
        r = evaluate(dates, div, None, hit, "%s_%s" % (name, side))
        m = r.get(60)
        if not m:
            continue
        s1, s2 = m["seg"]
        ok = (m["ep"] >= MIN_EP and abs(m["med_ex"]) >= EDGE and s1 is not None
              and s2 is not None and s1 * s2 > 0)
        rows.append((name, side, m["ep"], m["med_ex"], m["pos"], s1, s2, r[20], r[120], ok))
    rows.sort(key=lambda x: -abs(x[3]))
    print("\n%-16s %-5s %5s %9s %7s %9s %9s %8s  %s" % (
        "信号", "侧", "轮次", "60日超额", "为正", "前段超额", "后段超额", "20日超额", "判定"))
    for name, side, ep, med, pos, s1, s2, r20, r120, ok in rows:
        print("%-16s %-5s %5d %+8.2f%% %6.0f%% %+8s %+8s %+7s  %s" % (
            name, side, ep, med, pos * 100,
            ("%.2f%%" % s1) if s1 is not None else "—",
            ("%.2f%%" % s2) if s2 is not None else "—",
            ("%.2f%%" % r20["med_ex"]) if r20 else "—",
            "★可用" if ok else "不达标"))
    good = [r for r in rows if r[9]]
    print("\n达到门槛（轮次≥%d、两段同号、|60日超额|≥%.0f%%）的信号：%d 个%s" % (
        MIN_EP, EDGE, len(good),
        "" if good else " —— 这条序列上没有可用的择时信号"))
    return rows


def detail(name):
    div = load(DIV)
    bench = {k: load(v) for k, v in BENCHES.items()}
    dates = sorted(div)
    for nm, side, hit in signals(dates, div, bench):
        if not nm.startswith(name):
            continue
        r = evaluate(dates, div, None, hit, "%s_%s" % (nm, side))
        print("\n=== %s %s：%d 轮独立触发 ===" % (nm, side, len(r["first_dates"])))
        print("  首触日: " + "、".join(r["first_dates"]))
        for h in FWD:
            m = r[h]
            if m:
                print("  未来%3d日: 超额中位 %+.2f%%  为正 %.0f%%  前段 %s / 后段 %s" % (
                    h, m["med_ex"], m["pos"] * 100,
                    "%.2f%%" % m["seg"][0] if m["seg"][0] is not None else "—",
                    "%.2f%%" % m["seg"][1] if m["seg"][1] is not None else "—"))


def overlay():
    """把"深回撤加仓"当成仓位叠加规则来测（不是清仓式择时）。

    基线：一直满仓持有。叠加：常持 50%，触发日（250 日回撤 ≤ -15%）加到 100%。
    只有年化更高**且**回撤不更深，才算这条规则有实用价值。
    """
    div = load(DIV)
    dates = sorted(div)
    n = len(dates)
    closes = [div[d] for d in dates]
    dd = [None] * n
    for i in range(250, n):
        dd[i] = (closes[i] / max(closes[i - 250:i + 1]) - 1) * 100
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = closes[i] / closes[i - 1] - 1 + (DIV_YIELD / 100.0) / 250
    cash = (1.2 / 100.0) / 250

    def bt(trigger, base_w):
        eq, peak, mdd, hits, days_in = 1.0, 1.0, 0.0, 0, 0
        for i in range(1, n):
            w = 1.0 if (trigger(i) or base_w >= 1.0) else base_w
            if trigger(i):
                hits += 1
            days_in += w
            eq *= 1 + w * tr[i] + (1 - w) * cash
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
        return eq ** (250.0 / (n - 1)) - 1, mdd, hits, days_in / (n - 1)

    trig = lambda i: dd[i] is not None and dd[i] <= -15     # noqa: E731
    a1, m1, _h1, _d1 = bt(lambda i: True, 1.0)
    a2, m2, hits, din = bt(trig, 0.5)
    print("红利低波动 %s~%s（含分红口径，空仓按现金 1.2%%）" % (dates[0], dates[-1]))
    print("  基线·一直满仓        年化 %+.2f%%  最大回撤 %.1f%%" % (a1 * 100, m1 * 100))
    print("  叠加·半仓+深回撤加满   年化 %+.2f%%  最大回撤 %.1f%%  触发 %d 天（占 %.0f%%）"
          % (a2 * 100, m2 * 100, hits, din * 100))
    print("  → 差额 %+.2f%%/年%s" % ((a2 - a1) * 100,
                                    "（不如一直满仓）" if a2 < a1 else "（略好，但要看回撤）"))
    yrs = {}
    for i in range(n):
        if trig(i):
            yrs[dates[i][:4]] = yrs.get(dates[i][:4], 0) + 1
    print("  触发日年份分布：" + "、".join("%s:%d天" % (k, v) for k, v in sorted(yrs.items())))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--overlay", action="store_true")
    ap.add_argument("--detail", default=None)
    a = ap.parse_args()
    if a.detail:
        detail(a.detail)
    elif a.overlay:
        overlay()
    else:
        scan()


if __name__ == "__main__":
    main()
