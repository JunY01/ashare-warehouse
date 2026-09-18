# -*- coding: utf-8 -*-
"""红利 40 日收益差十万次迭代 —— 回答"这套边界到底是真规律，还是我挑的那几段行情"

前情见 reports/红利40日收益差择时_20260916.md：单样本回测下"模型2 固定分位 + 上宽下窄
（买 40% 分位 / 卖 85% 分位）"在 上证红利·中证红利·中证红利低波动 上跑赢买入持有。
但那只建立在**一条时间线上的几次交易**上，任何单点结论都可能是挑出来的。本脚本把结论
摊开成分布：10 万次随机取样（随机指数 + 随机时间窗）叠随机参数，看这套规则在各种各样的
样本里，有多大比例真的赚钱、赚多少。

网格维度（256 种配置 × 约 390 个随机窗口 ≈ 10 万次）：
  指数(3) × 买入分位(8: 10%~45%) × 卖出分位(8: 60%~95%) × 分位口径(2) × 初始状态(2)
  分位口径：exp = 扩张窗口（只用截止当日的历史，**可实盘、无前视**）
            full = 全样本分位（文章口径，含未来数据）→ 两者的差值就是"过拟合的水分"
  初始状态：1 = 一上来就满仓（跟买入持有同起点）／0 = 空仓等信号
  分红口径单列：--div-yield on 时统计"含分红+空仓拿现金息"，因为择时空仓要放弃红利票息

随机时间窗：起点随机、跨度随机取 750 ~ 全部（下探到 3 年，才能测"只给近几年数据"的样本）
门槛：窗口内完整回合 < 6 笔的样本丢弃（笔数太少，年化对比没有意义）

产物：data/cache/sweeps/sweep100k_red40.jsonl + 报告。纯标准库。
用法:
  python research/sweep100k_red40.py --run      # 跑 10 万次（约 1-3 分钟）
  python research/sweep100k_red40.py --finish   # 聚合
"""
import argparse
import json
import os
import random
import statistics as st
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "src", "common"), os.path.join(REPO_ROOT, "research")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from src.common.paths import SWEEP_CACHE, REPORT_ROOT  # noqa: E402
import red40_spread as R  # noqa: E402

DIVS = {"sh000922": "中证红利", "sh000015": "上证红利", "csH30269": "中证红利低波动"}
MKT = "sz399317"                     # 国证A指（全市场，≈Wind全A）
DIV_YIELD = {"sh000922": 4.22, "sh000015": 3.94, "csH30269": 4.33}
A_LEVELS = (10, 15, 20, 25, 30, 35, 40, 45)
B_LEVELS = (60, 65, 70, 75, 80, 85, 90, 95)
MODES = ("exp", "full")
CASH_ANN = 1.2                       # 空仓按货币基金 1.2%/年
MIN_TRADES = 6                       # 完整回合下限
MIN_WIN = 750                        # 窗口最小跨度（交易日，≈3年）
WARMUP = 540                         # 40日收益差 + 扩张分位最少 500 个观测
N_TRIALS = 100000
OUT = os.path.join(SWEEP_CACHE, "sweep100k_red40.jsonl")

_cache = {}


def preload(code, dy):
    """每个指数只算一次：收益差序列 + 两种口径的分位数组 + 两种口径的日收益"""
    if code in _cache:
        return _cache[code]
    dates, diff = R.spread_series(code, MKT, "2013-01-04")
    d = R.load(code, "2013-01-04")
    n = len(dates)
    r_exp, r_full = [None] * n, [None] * n
    valid = [x for x in diff if x is not None]
    hist = []
    for i in range(n):
        if diff[i] is None:
            continue
        hist.append(diff[i])
        if len(hist) >= 500:
            base = hist[:-1]
            r_exp[i] = 100.0 * sum(1 for x in base if x <= diff[i]) / len(base)
        r_full[i] = 100.0 * sum(1 for x in valid if x <= diff[i]) / len(valid)
    rp, rt = [0.0] * n, [0.0] * n
    for i in range(1, n):
        g = d[dates[i]] / d[dates[i - 1]] - 1
        rp[i] = g
        rt[i] = g + (dy / 100.0) / 250
    _cache[code] = dict(dates=dates, diff=diff, exp=r_exp, full=r_full, rp=rp, rt=rt)
    return _cache[code]


def evaluate(code, dy, s, e, A, B, mode, start_pos, use_tr, flat=0.0):
    """flat = 卖点触发后保留的仓位权重（0=清仓，0.5=只减一半）。

    "减仓不清仓"这个维度的由来：10 万次迭代显示价格口径的超额（+1.3%）小于
    空仓期放弃的票息（≈1.45%/年），所以满仓↔空仓二元切换在含分红口径下必然亏。
    降 flat 就能只放弃一部分票息——这一维就是用来验证它的。
    """
    S = preload(code, dy)
    rank = S[mode]
    ret = S["rt"] if use_tr else S["rp"]
    c = (CASH_ANN / 100.0) / 250
    st_, eq, peak, mdd = start_pos, 1.0, 1.0, 0.0
    expo = flat if start_pos == 0 else 1.0
    held = trades = nw = nl = 0
    sw = sl = 0.0
    entry = 1.0
    for i in range(s, e + 1):
        r = rank[i]
        if r is not None:
            if st_ == 0 and r < A:
                st_, entry = 1, eq
                expo = 1.0
            elif st_ == 1 and r > B:
                st_ = 0
                expo = flat
                trades += 1
                t = eq / entry - 1
                if t > 0:
                    sw += t
                    nw += 1
                elif t < 0:
                    sl += t
                    nl += 1
        held += expo
        x = ret[i]
        eq *= 1 + expo * x + (1 - expo) * c
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    if st_ == 1:
        trades += 1
        t = eq / entry - 1
        if t > 0:
            sw += t
            nw += 1
        elif t < 0:
            sl += t
            nl += 1
    nd = e - s
    bh = 1.0
    for i in range(s + 1, e + 1):
        bh *= 1 + ret[i]
    ann = eq ** (250.0 / nd) - 1
    bhan = bh ** (250.0 / nd) - 1
    return dict(ann=ann, bhan=bhan, excess=ann - bhan, mdd=mdd, trades=trades,
                per_year=trades / (nd / 250.0), held=held / nd,
                win=(nw / trades if trades else 0.0),
                pf=((sw / nw) / abs(sl / nl) if (nw and nl and sl) else float("nan")))


def evaluate_ladder(code, dy, s, e, lv, use_tr=True):
    """买卖阶梯版：多档仓位状态机（回答"分几次买、每次减多少"）。

    lv = (A1, A2, w1, B1, B2, w2, w3, init)
      A1/A2 = 第一档/第二档买点分位（A2 为 None 表示只做一档）
      w1    = 跌到 A1 时建到多少仓（1.0 = 一次买满）
      B1/B2 = 第一档/第二档卖点分位；w2/w3 = 对应减到的仓位
      init  = 初始仓位（0 = 空仓等信号，1 = 一上来满仓）
    中段（既没到买点也没到卖点）不动仓位——这是阶梯的本质。
    """
    A1, A2, w1, B1, B2, w2, w3, init = lv
    S = preload(code, dy)
    rank = S["exp"]
    ret = S["rt"] if use_tr else S["rp"]
    c = (CASH_ANN / 100.0) / 250
    w = init
    eq, peak, mdd = 1.0, 1.0, 0.0
    expo = 0.0
    for i in range(s, e + 1):
        r = rank[i]
        if r is not None:
            if A2 is not None and r < A2:
                w = 1.0
            elif r < A1:
                w = max(w, w1)
            elif r > B2:
                w = min(w, w3)
            elif r > B1:
                w = min(w, w2)
        expo += w
        eq *= 1 + w * ret[i] + (1 - w) * c
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    bh = 1.0
    for i in range(s + 1, e + 1):
        bh *= 1 + ret[i]
    nd = e - s
    ann = eq ** (250.0 / nd) - 1
    bhan = bh ** (250.0 / nd) - 1
    return {"ann": ann, "bhan": bhan, "excess": ann - bhan, "mdd": mdd, "expo": expo / nd}


LADDER_W1 = (0.33, 0.5, 0.67, 1.0)
LADDER_A2 = (20, None)
LADDER_W23 = ((0.67, 0.33), (0.5, 0.33), (0.5, 0.0), (0.67, 0.5), (0.33, 0.0),
              (1.0, 1.0), (0.0, 0.0))
LADDER_OUT = os.path.join(SWEEP_CACHE, "sweep100k_red40_ladder.jsonl")


def run_ladder(n_trials=N_TRIALS, code="sh000922"):
    """买卖阶梯的随机取样：中证红利单只、含分红口径、扩张分位、随机时间窗。"""
    dates = preload(code, DIV_YIELD[code])["dates"]
    n = len(dates)
    rng = random.Random(20260917)
    t0, done = time.time(), 0
    with open(LADDER_OUT, "w", encoding="utf-8") as f:
        for _ in range(n_trials):
            A2 = rng.choice(LADDER_A2)
            w1 = rng.choice(LADDER_W1)
            w2, w3 = rng.choice(LADDER_W23)
            init = rng.choice((0.0, 1.0))
            lv = (40, A2, w1, 85, 90, w2, w3, init)
            wi = rng.randint(MIN_WIN, n - WARMUP)
            lo = rng.randint(WARMUP, n - wi)
            hi = min(lo + wi - 1, n - 1)
            r = evaluate_ladder(code, DIV_YIELD[code], lo, hi, lv)
            f.write(json.dumps({
                "A2": A2, "w1": w1, "w2": w2, "w3": w3, "init": init,
                "lo": dates[lo], "e": dates[hi],
                "ex": round(r["excess"], 4), "ann": round(r["ann"], 4),
                "mdd": round(r["mdd"], 4), "expo": round(r["expo"], 3)}, ensure_ascii=False) + "\n")
            done += 1
            if done % 25000 == 0:
                print("  %d/%d %.0fs" % (done, n_trials, time.time() - t0))
    print("阶梯迭代 %d 组 -> %s  %.0fs" % (done, LADDER_OUT, time.time() - t0))


def finish_ladder():
    """按"买几档/买多少"和"卖点减到几成"分组，全部用含分红口径。"""
    rows = [json.loads(l) for l in open(LADDER_OUT, encoding="utf-8") if l.strip()]
    ex = [r["ex"] for r in rows]
    print("载入 %d 组（中证红利，含分红+现金息，扩张分位，随机窗口）\n" % len(rows))
    print("【全体】平均超额 %+.2f%%  中位 %+.2f%%  正超额 %.0f%%\n"
          % (st.mean(ex) * 100, st.median(ex) * 100, 100.0 * sum(1 for x in ex if x > 0) / len(ex)))

    print("【买入分档】跌到 40 分位建到多少仓（w1）／是否设第二档 20 分位")
    print("%-18s %6s %10s %10s %8s %8s" % ("买入方式", "样本", "平均超额", "中位超额", "正超额", "平均仓位"))
    for A2 in LADDER_A2:
        rs = [r for r in rows if r["A2"] == A2]
        print("  %-16s %6d %+9.2f%% %+9.2f%% %6.0f%% %7.0f%%" % (
            "两档(40%/20%)" if A2 else "只做一档(40%)", len(rs),
            st.mean([r["ex"] for r in rs]) * 100, st.median([r["ex"] for r in rs]) * 100,
            100.0 * sum(1 for r in rs if r["ex"] > 0) / len(rs), st.mean([r["expo"] for r in rs]) * 100))
    for w1 in LADDER_W1:
        rs = [r for r in rows if r["w1"] == w1]
        print("  %-16s %6d %+9.2f%% %+9.2f%% %6.0f%% %7.0f%%" % (
            "40分位建到%.0f%%仓" % (w1 * 100), len(rs),
            st.mean([r["ex"] for r in rs]) * 100, st.median([r["ex"] for r in rs]) * 100,
            100.0 * sum(1 for r in rs if r["ex"] > 0) / len(rs), st.mean([r["expo"] for r in rs]) * 100))

    print("\n【卖点减仓】85分位减到 w2、90分位减到 w3（1.0=不减仓，0=清仓）")
    print("%-18s %6s %10s %10s %8s %8s %9s" % ("减仓方案", "样本", "平均超额", "中位超额", "正超额", "平均仓位", "平均回撤"))
    for w2, w3 in LADDER_W23:
        rs = [r for r in rows if r["w2"] == w2 and r["w3"] == w3]
        if not rs:
            continue
        tag = "不减仓" if w2 >= 1.0 else ("清仓" if w2 == 0.0 else "减到%.0f%%→%.0f%%" % (w2 * 100, w3 * 100))
        print("  %-16s %6d %+9.2f%% %+9.2f%% %6.0f%% %7.0f%% %8.1f%%" % (
            tag, len(rs), st.mean([r["ex"] for r in rs]) * 100, st.median([r["ex"] for r in rs]) * 100,
            100.0 * sum(1 for r in rs if r["ex"] > 0) / len(rs),
            st.mean([r["expo"] for r in rs]) * 100, st.mean([r["mdd"] for r in rs]) * 100))

    print("\n【推荐档组合】两档买(40%建半仓→20%买满) × 各减仓方案")
    print("%-18s %6s %10s %10s %8s %8s %9s" % ("减仓方案", "样本", "平均超额", "中位超额", "正超额", "平均仓位", "平均回撤"))
    for w2, w3 in LADDER_W23:
        rs = [r for r in rows if r["w2"] == w2 and r["w3"] == w3 and r["w1"] == 0.5
              and r["A2"] == 20 and r["init"] == 0.0]
        if not rs:
            continue
        tag = "不减仓" if w2 >= 1.0 else ("清仓" if w2 == 0.0 else "减到%.0f%%→%.0f%%" % (w2 * 100, w3 * 100))
        print("  %-16s %6d %+9.2f%% %+9.2f%% %6.0f%% %7.0f%% %8.1f%%" % (
            tag, len(rs), st.mean([r["ex"] for r in rs]) * 100, st.median([r["ex"] for r in rs]) * 100,
            100.0 * sum(1 for r in rs if r["ex"] > 0) / len(rs),
            st.mean([r["expo"] for r in rs]) * 100, st.mean([r["mdd"] for r in rs]) * 100))

    print("\n【初始状态】\n%-18s %6s %10s %10s %8s" % ("初始", "样本", "平均超额", "中位超额", "正超额"))
    for i0, nm in [(0.0, "空仓等信号"), (1.0, "一上来满仓")]:
        rs = [r for r in rows if r["init"] == i0]
        print("  %-16s %6d %+9.2f%% %+9.2f%% %6.0f%%" % (
            nm, len(rs), st.mean([r["ex"] for r in rs]) * 100,
            st.median([r["ex"] for r in rs]) * 100,
            100.0 * sum(1 for r in rs if r["ex"] > 0) / len(rs)))


BUY_SCHEMES = {                      # 买法：[(分位阈值, 建到仓位), ...]
    "一档买满(40→100%)": [(40, 1.0)],
    "两档(40→50%/20→100%)": [(40, 0.5), (20, 1.0)],
    "两档(40→67%/20→100%)": [(40, 0.67), (20, 1.0)],
    "三档(40→33%/30→67%/20→100%)": [(40, 0.33), (30, 0.67), (20, 1.0)],
    "三等分(40/30/20→33/67/100)反向": [(30, 1.0), (20, 1.0)],
}
SELL_SCHEMES = {                     # 卖法：[(分位阈值, 减到仓位), ...]
    "不减仓": [],
    "85→67%/90→33%": [(85, 0.67), (90, 0.33)],
    "85→50%/90→33%": [(85, 0.5), (90, 0.33)],
    "85→50%/90→0%": [(85, 0.5), (90, 0.0)],
    "85→33%/90→0%": [(85, 0.33), (90, 0.0)],
    "85→0%(一次清仓)": [(85, 0.0)],
}


def _walk(rank, ret, c, s, e, buys, sells, init):
    """在给定窗口上按买/卖阶梯走一遍，返回 (年化, 买入持有年化, 最大回撤, 平均仓位)。"""
    w = init
    eq, peak, mdd, expo = 1.0, 1.0, 0.0, 0.0
    for i in range(s, e + 1):
        r = rank[i]
        if r is not None:
            for thr, wt in buys:
                if r < thr:
                    w = max(w, wt)
            for thr, wt in sells:
                if r > thr:
                    w = min(w, wt)
        expo += w
        eq *= 1 + w * ret[i] + (1 - w) * c
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    bh = 1.0
    for i in range(s + 1, e + 1):
        bh *= 1 + ret[i]
    nd = e - s
    return (eq ** (250.0 / nd) - 1, bh ** (250.0 / nd) - 1, mdd, expo / nd)


def run_grid(n_windows=2000, code="sh000922"):
    """同窗口配对：同一批随机窗口里跑完所有买卖方案，去掉窗口差异的干扰。"""
    dates = preload(code, DIV_YIELD[code])["dates"]
    n = len(dates)
    rng = random.Random(20260918)
    wins = []
    for _ in range(n_windows):
        wi = rng.randint(MIN_WIN, n - WARMUP)
        lo = rng.randint(WARMUP, n - wi)
        wins.append((lo, min(lo + wi - 1, n - 1)))
    S = preload(code, DIV_YIELD[code])
    rank, ret = S["exp"], S["rt"]
    c = (CASH_ANN / 100.0) / 250
    for init in (0.0, 1.0):
        res = {}
        for bn, buys in BUY_SCHEMES.items():
            for sn, sells in SELL_SCHEMES.items():
                exs, mdds, expos = [], [], []
                for lo, hi in wins:
                    ann, bhan, mdd, expo = _walk(rank, ret, c, lo, hi, buys, sells, init)
                    exs.append(ann - bhan)
                    mdds.append(mdd)
                    expos.append(expo)
                res[(bn, sn)] = (st.mean(exs) * 100, st.median(exs) * 100,
                                 100.0 * sum(1 for x in exs if x > 0) / len(exs),
                                 st.mean(mdds) * 100, st.mean(expos) * 100, exs)
        print("\n############ 初始仓位 %.0f%%（%d 个随机窗口，含分红口径，扩张分位）############"
              % (init * 100, n_windows))
        print("【买法 × 卖法】平均超额 / 正超额比例（行=买法，列=卖法）")
        print("%-30s %s" % ("", "".join("%16s" % sn for sn in SELL_SCHEMES)))
        for bn in BUY_SCHEMES:
            cells = []
            for sn in SELL_SCHEMES:
                m = res[(bn, sn)]
                cells.append("%8.2f%%/%3.0f%%" % (m[0], m[2]))
            print("%-30s %s" % (bn, "".join("%16s" % c for c in cells)))
        print("\n【同买法下 各卖法的回撤-收益权衡】")
        print("%-22s %-16s %9s %9s %8s %9s" % ("买法", "卖法", "平均超额", "中位超额", "平均回撤", "平均仓位"))
        for bn in BUY_SCHEMES:
            for sn in SELL_SCHEMES:
                m = res[(bn, sn)]
                print("%-22s %-16s %+8.2f%% %+8.2f%% %7.1f%% %8.0f%%" % (
                    bn, sn, m[0], m[1], m[3], m[4]))
            print()
        # 配对显著性：三档买 vs 两档买 / 清仓 vs 减半
        print("【配对检验】同一批窗口里逐窗比较（正数=前者更好）")
        import itertools
        for (b1, s1), (b2, s2) in [(("三档(40→33%/30→67%/20→100%)", "85→50%/90→33%"),
                                    ("两档(40→50%/20→100%)", "85→50%/90→33%")),
                                   (("两档(40→50%/20→100%)", "不减仓"),
                                    ("两档(40→50%/20→100%)", "85→50%/90→33%")),
                                   (("两档(40→50%/20→100%)", "85→0%(一次清仓)"),
                                    ("两档(40→50%/20→100%)", "85→50%/90→33%"))]:
            d = [a - b for a, b in zip(res[(b1, s1)][5], res[(b2, s2)][5])]
            print("  %s[%s]  vs  %s[%s]：平均 %+.2fpp  前者更好的窗口占 %.0f%%" % (
                b1, s1, b2, s2, st.mean(d) * 100, 100.0 * sum(1 for x in d if x > 0) / len(d)))


def run(n_trials=N_TRIALS):
    alld = {}
    for code, dy in DIV_YIELD.items():
        alld[code] = preload(code, dy)["dates"]
    print("指数 %d 条，各自日期跨度: %s" % (
        len(DIVS), " / ".join("%s %s~%s" % (DIVS[c], alld[c][0], alld[c][-1]) for c in DIVS)))
    os.makedirs(SWEEP_CACHE, exist_ok=True)
    rng = random.Random(20260916)
    t0, done, skip = time.time(), 0, 0
    with open(OUT, "w", encoding="utf-8") as f:
        for _ in range(n_trials):
            code = rng.choice(list(DIVS))
            A = rng.choice(A_LEVELS)
            B = rng.choice(B_LEVELS)
            mode = rng.choice(MODES)
            start_pos = rng.choice((0, 1))
            use_tr = rng.choice((False, True))
            flat = rng.choice((0.0, 0.5))
            dates = alld[code]
            n = len(dates)
            hi_i = n - 1
            w = rng.randint(MIN_WIN, n - WARMUP)
            lo_i = rng.randint(WARMUP, n - w)
            e_i = lo_i + w - 1
            if e_i > hi_i:
                lo_i, e_i = hi_i - w + 1, hi_i
            r = evaluate(code, DIV_YIELD[code], lo_i, e_i, A, B, mode, start_pos, use_tr, flat)
            if r["trades"] < MIN_TRADES:
                skip += 1
                continue
            f.write(json.dumps({
                "code": code, "A": A, "B": B, "mode": mode, "sp": start_pos, "tr": use_tr,
                "flat": flat,
                "lo": dates[lo_i], "e": dates[e_i], "w": w,
                "ann": round(r["ann"], 4), "bhan": round(r["bhan"], 4),
                "ex": round(r["excess"], 4), "mdd": round(r["mdd"], 4),
                "n": r["trades"], "py": round(r["per_year"], 2), "hd": round(r["held"], 3),
                "win": round(r["win"], 3),
                "pf": (round(r["pf"], 2) if r["pf"] == r["pf"] else None)}, ensure_ascii=False) + "\n")
            done += 1
            if done % 20000 == 0:
                print("  %d/%d  %.0fs" % (done, n_trials, time.time() - t0))
    print("有效 %d 组（样本内回合不足 %d 笔丢弃 %d）-> %s  %.0fs" % (
        done, MIN_TRADES, skip, OUT, time.time() - t0))


def agg(rows, key):
    g = {}
    for r in rows:
        g.setdefault(key(r), []).append(r)
    return g


def line(tag, rs):
    ex = [r["ex"] for r in rs]
    md = [r["mdd"] - 0 for r in rs]
    return "%-22s %6d  %+7.2f%%  %+7.2f%%  %5.1f%%  %6.2f  %6.2f%%  %6.0f%%" % (
        tag, len(rs), st.mean(ex) * 100, st.median(ex) * 100,
        100.0 * sum(1 for x in ex if x > 0) / len(ex),
        st.mean([r["py"] for r in rs]), st.mean(md) * 100, st.mean([r["hd"] for r in rs]) * 100)


HDR = "%-22s %6s  %7s  %7s  %6s  %6s  %7s  %7s" % (
    "分组", "样本", "平均超额", "中位超额", "正超额", "次/年", "平均回撤", "在场率")


def finish():
    rows = [json.loads(ln) for ln in open(OUT, encoding="utf-8") if ln.strip()]
    print("载入 %d 组\n" % len(rows))
    ex = [r["ex"] for r in rows]
    print("【全体】平均超额 %+.2f%%  中位 %+.2f%%  正超额占比 %.1f%%  四分位 %.2f/%.2f/%.2f" % (
        st.mean(ex) * 100, st.median(ex) * 100, 100.0 * sum(1 for x in ex if x > 0) / len(ex),
        st.quantiles(ex, n=4)[0] * 100, st.quantiles(ex, n=4)[1] * 100, st.quantiles(ex, n=4)[2] * 100))

    print("\n【按指数】\n" + HDR)
    for c in DIVS:
        rs = [r for r in rows if r["code"] == c]
        if rs:
            print(line(DIVS[c], rs))

    print("\n【按分红口径】\n" + HDR)
    for t, nm in [(False, "价格口径"), (True, "含分红+现金息")]:
        rs = [r for r in rows if r["tr"] == t]
        if rs:
            print(line(nm, rs))

    print("\n【按卖点触发后的仓位】满仓↔空仓 vs 减半不清仓\n" + HDR)
    for t, nm in [(0.0, "卖点清仓(0仓)"), (0.5, "卖点减半(半仓)")]:
        rs = [r for r in rows if r["flat"] == t]
        if rs:
            print(line(nm, rs))
    print("\n【含分红口径下 × 仓位处理】← 真实到手的那一半\n" + HDR)
    for t, nm in [(0.0, "清仓"), (0.5, "减半")]:
        rs = [r for r in rows if r["flat"] == t and r["tr"]]
        if rs:
            print(line("含分红·" + nm, rs))
    for t, nm in [(0.0, "清仓"), (0.5, "减半")]:
        rs = [r for r in rows if r["flat"] == t and r["tr"] and r["mode"] == "exp"]
        if rs:
            print(line("含分红·可实盘·" + nm, rs))
            for c in DIVS:
                rs2 = [r for r in rs if r["code"] == c]
                if rs2:
                    print(line("   " + DIVS[c], rs2))

    print("\n【按分位口径】\n" + HDR)
    for t, nm in [("exp", "扩张(可实盘)"), ("full", "全样本(文章口径)")]:
        rs = [r for r in rows if r["mode"] == t]
        if rs:
            print(line(nm, rs))

    print("\n【按初始状态】\n" + HDR)
    for t, nm in [(1, "初始满仓"), (0, "初始空仓")]:
        rs = [r for r in rows if r["sp"] == t]
        if rs:
            print(line(nm, rs))

    print("\n【买点分位 × 卖点分位 平均超额（%%）】")
    print("        " + "".join("%8d%%" % b for b in B_LEVELS))
    for a in A_LEVELS:
        cells = []
        for b in B_LEVELS:
            rs = [r["ex"] for r in rows if r["A"] == a and r["B"] == b]
            cells.append("%+8.2f" % (st.mean(rs) * 100) if rs else "       -")
        print("  买%3d%% " % a + "".join(cells))

    print("\n【卖点单边效应】卖点分位越高（越不容易卖）")
    for b in B_LEVELS:
        rs = [r["ex"] for r in rows if r["B"] == b]
        if rs:
            print("  卖%3d%%  平均%+.2f%%  正超额%.0f%%" % (b, st.mean(rs) * 100,
                                                     100.0 * sum(1 for x in rs if x > 0) / len(rs)))
    print("\n【买点单边效应】买点分位越高（越容易买）")
    for a in A_LEVELS:
        rs = [r["ex"] for r in rows if r["A"] == a]
        if rs:
            print("  买%3d%%  平均%+.2f%%  正超额%.0f%%" % (a, st.mean(rs) * 100,
                                                     100.0 * sum(1 for x in rs if x > 0) / len(rs)))

    print("\n【窗口终点年份（状态依赖）】\n" + HDR)
    for y in range(2016, 2027):
        rs = [r for r in rows if r["e"][:4] == str(y)]
        if len(rs) > 200:
            print(line("终点 %d" % y, rs))

    print("\n【窗口起点年份】\n" + HDR)
    for y in range(2014, 2026):
        rs = [r for r in rows if r["lo"][:4] == str(y)]
        if len(rs) > 200:
            print(line("起点 %d" % y, rs))

    # 剔掉最好的若干窗口，看超额还剩多少（防"靠个别好年份"）
    print("\n【削顶检验】按窗口集合去掉贡献最大的年份后")
    for y in range(2014, 2027):
        rs = [r["ex"] for r in rows if r["lo"][:4] != str(y) and r["e"][:4] != str(y)]
        if rs:
            print("  去掉起/终点 %d 年: 平均超额 %+.2f%%  正超额 %.0f%%  (剩 %d)" % (
                y, st.mean(rs) * 100, 100.0 * sum(1 for x in rs if x > 0) / len(rs), len(rs)))

    # 推荐组合在迭代里的落点
    print("\n【推荐档 (买40%,卖85%, 扩张分位, 初始满仓) 的分布】")
    for c in DIVS:
        rs = [r for r in rows if r["code"] == c and r["A"] == 40 and r["B"] == 85
              and r["mode"] == "exp" and r["sp"] == 1]
        if rs:
            e2 = [r["ex"] for r in rs]
            print("  %-10s n=%3d 平均%+.2f%% 中位%+.2f%% 正超额%.0f%% 最差%+.2f%%" % (
                DIVS[c], len(rs), st.mean(e2) * 100, st.median(e2) * 100,
                100.0 * sum(1 for x in e2 if x > 0) / len(e2), min(e2) * 100))
            for t in (False, True):
                e3 = [r["ex"] for r in rs if r["tr"] == t]
                if e3:
                    print("      %s: 平均%+.2f%% 正超额%.0f%%" % (
                        "价格" if not t else "含分红", st.mean(e3) * 100,
                        100.0 * sum(1 for x in e3 if x > 0) / len(e3)))
    # 与本仓库报告口径一致的固定窗口（全样本）对照
    print("\n【门槛】正超额占比 ≥55% 且 平均超额 ≥+0.5% 的配置：")
    good = []
    for a in A_LEVELS:
        for b in B_LEVELS:
            for mode in MODES:
                for c in DIVS:
                    rs = [r["ex"] for r in rows if r["A"] == a and r["B"] == b
                          and r["mode"] == mode and r["code"] == c]
                    if len(rs) < 50:
                        continue
                    share = 100.0 * sum(1 for x in rs if x > 0) / len(rs)
                    if share >= 55 and st.mean(rs) >= 0.005:
                        good.append((st.mean(rs), DIVS[c], a, b, mode, share, len(rs)))
    for m, nm, a, b, mode, share, n in sorted(good, reverse=True)[:20]:
        print("  %-10s 买%2d%% 卖%2d%% %-5s 平均%+.2f%% 正超额%.0f%% n=%d" % (
            nm, a, b, mode, m * 100, share, n))
    if not good:
        print("  （无 —— 没有任何配置在这套随机样本里稳定跑赢）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--finish", action="store_true")
    ap.add_argument("--ladder", action="store_true", help="买卖阶梯迭代（分几次买/减多少）")
    ap.add_argument("--ladder-finish", action="store_true")
    ap.add_argument("--grid", action="store_true", help="同窗口配对比较买卖方案")
    ap.add_argument("--windows", type=int, default=2000)
    ap.add_argument("--trials", type=int, default=N_TRIALS)
    a = ap.parse_args()
    if a.grid:
        run_grid(a.windows)
    if a.ladder:
        run_ladder(a.trials)
    if a.ladder_finish:
        finish_ladder()
    if a.run:
        run(a.trials)
    if a.finish:
        finish()


if __name__ == "__main__":
    main()
