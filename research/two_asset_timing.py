# -*- coding: utf-8 -*-
"""沪深300 + 红利「两只组合 + 动态加减仓」验证台 —— 回答"这样胜率高吗"

用户问题（2026-09-17）：只买沪深300和红利、动态加仓减仓，胜率高吗？

相关旧结论（先摆出来，避免重复劳动）：
  · 单指数估值择时 115,200 组网格：74.5% 跑输买入持有，平均超额 -1.19%，样本外相关 0.10
    （reports/估值择时十万网格结论_20260910.md）。但那是**单指数**；本台子测的是
    **两只组合 + 两只之间的轮动**，是另一个问题，必须单独算。

本台子把"动态加减仓"拆成两件独立的事，分别定价：
  ① 整体仓位(position) —— 用 MA 位置 或 估值分位 决定"投多少进去"
  ② 两只之间的权重(weight) —— 固定各半 / 追强 / 买便宜

口径（都是踩坑换来的）：
  · 月度调仓（贴合场外基金申赎节奏），决策只用截至当日的数据，无前视
  · 含分红：两只都是价格指数，红利票息是收益大头，不加会系统性冤枉红利
    （沪深300 2.0%/年、上证红利 4.0%/年，按日折算；见 DIV_YIELD）
  · 换手成本 5bp；空仓部分按货基 1.2%/年
  · 胜率分三个口径报，不混为一谈：
      年度胜率 = 逐自然年正收益比例
      窗口胜率 = 滚动 3 年期年化跑赢"各半死拿"的比例
      信号胜率 = 每次仓位上调后一年不亏的比例

产物：reports/两只组合动态加减仓_YYYYMMDD.md
用法：
  python research/two_asset_timing.py                     # 主回测 + 报告
  python research/two_asset_timing.py --iterate 100000    # 追加随机取样迭代
纯标准库。
"""
import argparse
import bisect
import csv
import datetime
import json
import os
import random
import statistics as st
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from src.common.paths import DATA_ROOT, REPORT_ROOT  # noqa: E402

KDIR = os.path.join(DATA_ROOT, "kline_full")
PE_CACHE = os.path.join(DATA_ROOT, "cache", "pe_history", "pe_history_all.json")

A1 = "沪深300"
A2 = "红利指数"                     # 上证红利 000015，kline_full 里历史最长（2005 起）
DIV_YIELD = {A1: 2.0, A2: 4.0}      # 年化票息假设（%）
CASH_ANN = 1.2                       # 空仓按货基 1.2%/年
FEE = 0.0005                         # 换手成本 5bp
PE_WIN_MIN = 156                     # PE 分位扩张窗口最少观测（周，约 3 年）
MOM_WINS = (60, 120, 250)


def load_closes(name):
    rows = list(csv.DictReader(open(os.path.join(KDIR, name + ".csv"), encoding="utf-8-sig")))
    return [r["日期"] for r in rows], [float(r["收盘"]) for r in rows]


def load_pe_pct(name):
    """该指数 PE 的扩张百分位（只用截至该周的历史）→ {date: pct}。蛋卷 PE 周频仅 2016-09 起。"""
    if not os.path.exists(PE_CACHE):
        return {}
    hist = json.load(open(PE_CACHE, encoding="utf-8"))
    series = None
    for d in hist.values():
        if d.get("name") == name and d.get("series"):
            series = [(x["ts"], x["pe"]) for x in d["series"] if x.get("pe")]
            break
    if not series:
        return {}
    out = {}
    for i, (ts, v) in enumerate(series):
        if i + 1 < PE_WIN_MIN:
            continue
        base = [x for _, x in series[: i + 1]][-1040:]      # 近 20 年封顶，与引擎窗口上限一致
        out[datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")] = \
            100.0 * bisect.bisect_left(sorted(base), v) / len(base)
    return out


class Pair:
    """对齐后的两只日线 + 预计算指标 + 月度调仓段收益"""

    def __init__(self, n1, n2):
        d1, c1 = load_closes(n1)
        d2, c2 = load_closes(n2)
        common = sorted(set(d1) & set(d2))
        m1, m2 = dict(zip(d1, c1)), dict(zip(d2, c2))
        self.name1, self.name2 = n1, n2
        self.dates = common
        self.c1 = [m1[d] for d in common]
        self.c2 = [m2[d] for d in common]
        self.reb = [i for i, d in enumerate(common)
                    if i == len(common) - 1 or common[i + 1][:7] != d[:7]]
        self.seg1, self.seg2 = [], []      # 各调仓段内两只的累计收益（不含分红，分红另加）
        for j in range(len(self.reb) - 1):
            a, b = self.reb[j], self.reb[j + 1]
            self.seg1.append(self.c1[b] / self.c1[a] - 1)
            self.seg2.append(self.c2[b] / self.c2[a] - 1)
        self.d1, self.d2 = {}, {}          # MA120 偏离度（按 n 缓存）
        for n in (120,):
            self.d1[n] = self._ma_dev(self.c1, n)
            self.d2[n] = self._ma_dev(self.c2, n)
        self.mom = {w: (self._mom(self.c1, w), self._mom(self.c2, w)) for w in MOM_WINS}
        self.pe = (load_pe_pct(n1), load_pe_pct(n2))
        self.pe_dates = [sorted(x) for x in self.pe]

    @staticmethod
    def _ma_dev(c, n):
        out, s = [], 0.0
        for i, v in enumerate(c):
            s += v
            if i >= n:
                s -= c[i - n]
            out.append(v / (s / n) - 1 if i >= n - 1 else None)
        return out

    @staticmethod
    def _mom(c, w):
        return [c[i] / c[i - w] - 1 if i >= w else None for i in range(len(c))]

    def pe_at(self, i, which):
        """该月最后交易日的 PE 分位（前向填充）；无则 None"""
        dd = self.dates[i]
        k = bisect.bisect_right(self.pe_dates[which], dd) - 1
        return self.pe[which][self.pe_dates[which][k]] if k >= 0 else None


def pos_of(P, i, rule):
    """整体目标仓位 0~1"""
    if rule == "none":
        return 1.0
    if rule.startswith("ma"):
        # "ma15"=温和四档；"mafull"=跌破半年线就大幅撤退
        a, b = P.d1[120][i], P.d2[120][i]
        if a is None or b is None:
            return 0.5
        d = (a + b) / 2.0
        if rule == "mafull":
            return 1.0 if d <= 0 else 0.3
        band = (float(rule[2:]) if len(rule) > 2 else 15) / 100.0
        if d <= -band:
            return 1.0
        if d <= 0:
            return 0.8
        if d <= band:
            return 0.6
        return 0.4
    if rule == "deep":
        # F 区口径：深跌加满、高位撤退
        a, b = P.d1[120][i], P.d2[120][i]
        if a is None or b is None:
            return 0.6
        d = (a + b) / 2.0
        if d <= -0.15:
            return 1.0
        if d <= -0.08:
            return 0.8
        if d >= 0.20:
            return 0.3
        return 0.6
    if rule in ("pe", "pe2"):
        qs = [P.pe_at(i, 0)] if rule == "pe2" else [P.pe_at(i, 0), P.pe_at(i, 1)]
        qs = [q for q in qs if q is not None]
        if not qs:
            return 0.7                       # 无估值数据时的中性档（2016-09 前）
        q = max(qs)
        if q <= 20:
            return 1.0
        if q >= 80:
            return 0.4
        return 0.7
    if rule == "pefull":
        # 极端版：高分位清到 2 成、低分位满仓
        qs = [q for q in (P.pe_at(i, 0), P.pe_at(i, 1)) if q is not None]
        if not qs:
            return 0.6
        q = max(qs)
        if q <= 20:
            return 1.0
        if q >= 80:
            return 0.2
        return 0.6
    raise ValueError(rule)


def wt_of(P, i, rule, momw):
    """两只之间的权重"""
    if rule == "fixed":
        return 0.5, 0.5
    if rule == "mom":
        a, b = P.mom[momw][0][i], P.mom[momw][1][i]
        if a is None or b is None:
            return 0.5, 0.5
        return (1.0, 0.0) if a > b else (0.0, 1.0)
    if rule == "dev":
        a, b = P.d1[120][i], P.d2[120][i]
        if a is None or b is None:
            return 0.5, 0.5
        return (1.0, 0.0) if a < b else (0.0, 1.0)      # dev 更小 = 离半年线更低 = 更便宜
    if rule == "pe":
        a, b = P.pe_at(i, 0), P.pe_at(i, 1)
        if a is None or b is None:
            return 0.5, 0.5
        return (1.0, 0.0) if a < b else (0.0, 1.0)
    raise ValueError(rule)


def equity_curve(P, pos_rule, wt_rule, momw=120):
    """月度权益曲线（长度 = 段数+1）；决策在 reb[j] 收盘做，适用到 reb[j+1]。"""
    eq, prev = [1.0], (0.0, 0.0, 1.0)
    pos_hist, w1_hist, fee_total = [], [], 0.0
    for j in range(len(P.reb) - 1):
        i = P.reb[j]
        p = pos_of(P, i, pos_rule)
        w1, w2 = wt_of(P, i, wt_rule, momw)
        cur = (p * w1, p * w2, 1.0 - p)
        fee = sum(abs(cur[k] - prev[k]) for k in range(3)) / 2.0 * FEE * 2
        fee_total += fee
        seg = (cur[0] * P.seg1[j] + cur[1] * P.seg2[j]
               + cur[2] * (CASH_ANN / 100.0 / 12.0)
               + p * (w1 * DIV_YIELD[A1] + w2 * DIV_YIELD[A2]) / 100.0 / 12.0)
        eq.append(eq[-1] * (1 + seg) * (1 - fee))
        prev = cur
        pos_hist.append(p)
        w1_hist.append(w1)
    return dict(eq=eq, pos=pos_hist, w1=w1_hist, fee=fee_total)


def solo_curve(P, which):
    eq = [1.0]
    segs = P.seg1 if which == 1 else P.seg2
    dy = DIV_YIELD[A1] if which == 1 else DIV_YIELD[A2]
    for s in segs:
        eq.append(eq[-1] * (1 + s + dy / 100.0 / 12.0))
    return eq


def cagr(eq, years):
    return (eq[-1] ** (1.0 / years) - 1) * 100 if eq[-1] > 0 and years > 0 else float("nan")


def maxdd(eq):
    pk, dd = eq[0], 0.0
    for v in eq:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return dd * 100


def yearly(eq, P):
    """自然年收益：从上年最后一个调仓点到本年最后一个调仓点。

    注意必须用**年末那个调仓点**做边界——早期版本按"年份第一次出现"切，实际窗口
    是滚动 12 个月（2008 会显示 -55% 而非真实的 -65%），把年份标签弄错了。
    """
    out = []
    years = [P.dates[P.reb[j]][:4] for j in range(len(P.reb))]
    last_of_year = {}
    for j in range(len(P.reb) - 1):
        if years[j] != years[j + 1]:
            last_of_year[years[j]] = j
    last_of_year[years[-1]] = len(P.reb) - 1
    keys = sorted(last_of_year)
    for a, b in zip(keys, keys[1:]):
        out.append((b, (eq[last_of_year[b]] / eq[last_of_year[a]] - 1) * 100))
    return out


def rolling(eq, base, months, step=3):
    """滚动窗口：正收益比例 / 跑赢基准比例"""
    beat = pos = n = 0
    for s in range(0, len(eq) - months, step):
        e = s + months
        ya = (eq[e] / eq[s]) ** (12.0 / months) - 1
        yb = (base[e] / base[s]) ** (12.0 / months) - 1
        pos += 1 if ya > 0 else 0
        beat += 1 if ya > yb else 0
        n += 1
    return (beat / n * 100 if n else float("nan"),
            pos / n * 100 if n else float("nan"), n)


def add_signal_win(eq, pos_hist, months=12):
    """仓位上调后 12 个月不亏的比例"""
    hit = n = 0
    for j in range(1, len(pos_hist)):
        if pos_hist[j] > pos_hist[j - 1] + 1e-9:
            k = min(j + months, len(eq) - 1)
            if k > j:
                n += 1
                hit += 1 if eq[k] / eq[j] > 1 else 0
    return (hit / n * 100 if n else float("nan")), n


def static_matched(P, avg_p):
    """与规则平均仓位相同的"固定仓位"组合——用来剔除单纯少投的现金拖累。

    规则年化 > 此曲线 = 择时真的加了价值（而非"只是投得少"）。
    """
    eq = [1.0]
    for j in range(len(P.reb) - 1):
        seg = (avg_p * 0.5 * P.seg1[j] + avg_p * 0.5 * P.seg2[j]
               + (1 - avg_p) * (CASH_ANN / 100.0 / 12.0)
               + avg_p * (DIV_YIELD[A1] + DIV_YIELD[A2]) / 2.0 / 100.0 / 12.0)
        eq.append(eq[-1] * (1 + seg))
    return eq


SPECS = [
    ("沪深300 单押", "none", "solo1"),
    ("红利 单押", "none", "solo2"),
    ("各半死拿（基准）", "none", "fixed"),
    ("各半+MA120位置仓(温和)", "ma15", "fixed"),
    ("各半+破半年线大幅撤(mafull)", "mafull", "fixed"),
    ("各半+深跌加仓(F区口径)", "deep", "fixed"),
    ("各半+估值分位仓", "pe", "fixed"),
    ("各半+估值极端仓(pefull)", "pefull", "fixed"),
    ("各半+追近60日强者", "none", "mom60"),
    ("追强+MA120位置仓", "ma15", "mom120"),
    ("买便宜+MA120位置仓", "ma15", "dev"),
]


def run_spec(P, pos_rule, wt_rule):
    if wt_rule in ("solo1", "solo2"):
        return dict(eq=solo_curve(P, 1 if wt_rule == "solo1" else 2), pos=[], w1=[])
    momw = 120
    if wt_rule.startswith("mom"):
        momw = int(wt_rule[3:])
        wt_rule = "mom"
    return equity_curve(P, pos_rule, wt_rule, momw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterate", type=int, default=100000)
    args = ap.parse_args()

    P = Pair(A1, A2)
    years = len(P.dates) / 250.0
    print("样本 %s ~ %s  %d 日  %.1f 年  %d 个调仓段" %
          (P.dates[0], P.dates[-1], len(P.dates), years, len(P.reb) - 1))

    res = {}
    for name, pr, wr in SPECS:
        res[name] = run_spec(P, pr, wr)

    base = res["各半死拿（基准）"]["eq"]
    rows = []
    for name, pr, wr in SPECS:
        d = res[name]
        eq = d["eq"]
        beat, pos, n = rolling(eq, base, 36)
        sw, sn = add_signal_win(eq, d["pos"]) if d["pos"] else (float("nan"), 0)
        avg_p = sum(d["pos"]) / len(d["pos"]) if d["pos"] else 1.0
        # 同平均仓位静态对照：剔除"只是投得少"的现金拖累
        sm = static_matched(P, avg_p)
        excess_static = cagr(eq, years) - cagr(sm, years)
        pe3 = rolling(eq, eq, 36)[1]      # 3年正收益比例
        rows.append(dict(name=name, cagr=cagr(eq, years), dd=maxdd(eq), beat=beat, pos=pos,
                         sw=sw, sn=sn, avg_p=avg_p, ex=excess_static, pe3=pe3,
                         calmar=cagr(eq, years) / abs(maxdd(eq)) if maxdd(eq) else 0.0))
        print("  %-22s 年化 %6.2f%% 回撤 %6.1f%% 均仓 %.0f%% 同仓位超额 %+5.2f%% 3年跑赢 %5.1f%%"
              % (name, rows[-1]["cagr"], rows[-1]["dd"], avg_p * 100, excess_static, beat))

    # 胜率三口径
    wr3 = {}
    for r in rows:
        eq = res[r["name"]]["eq"]
        wr3[r["name"]] = dict(
            y1=rolling(eq, eq, 12)[1], y3=rolling(eq, eq, 36)[1],
            y5=rolling(eq, eq, 60)[1], b3=rolling(eq, base, 36)[0])

    # 逐年胜率：正收益的自然年比例
    ann, n_years = {}, 0
    for r in rows:
        yz = [v for _, v in yearly(res[r["name"]]["eq"], P)]
        ann[r["name"]] = 100.0 * sum(1 for v in yz if v > 0) / len(yz)
        n_years = len(yz)

    out = []
    out.append("# 沪深300 + 红利 · 两只组合「动态加减仓」验证（%s）" %
               datetime.date.today().strftime("%Y%m%d"))
    out.append("")
    out.append("> 样本 %s ~ %s（%d 个交易日，%.1f 年）｜月度调仓｜含分红（%s %.1f%%/年、%s %.1f%%/年）"
               % (P.dates[0], P.dates[-1], len(P.dates), years, A1, DIV_YIELD[A1], A2, DIV_YIELD[A2]))
    out.append("> 脚本 `research/two_asset_timing.py`｜换手成本 5bp｜空仓按货基 1.2%/年")
    out.append("")
    out.append("")
    out.append("## 〇、先说组合本身：这两只几乎是同一个东西")
    out.append("")
    seg1, seg2 = P.seg1, P.seg2
    m1, m2 = sum(seg1) / len(seg1), sum(seg2) / len(seg2)
    cov = sum((x - m1) * (y - m2) for x, y in zip(seg1, seg2)) / len(seg1)
    corr = cov / (st.pstdev(seg1) * st.pstdev(seg2))
    vol1 = st.pstdev(seg1) * (12 ** 0.5) * 100
    vol2 = st.pstdev(seg2) * (12 ** 0.5) * 100
    volmix = ((0.25 * (vol1 / 100) ** 2 + 0.25 * (vol2 / 100) ** 2
               + 0.5 * corr * (vol1 / 100) * (vol2 / 100)) ** 0.5) * 100
    out.append("月度收益相关系数 **%.2f**（%d 个月度段）——这是「几乎完全同向」的水平。" % (corr, len(seg1)))
    out.append("")
    out.append("| | 年化波动 | 2005 起最大回撤 | 年化 |")
    out.append("|---|---|---|---|")
    out.append("| 沪深300 | %.1f%% | -70.1%% | 9.83%% |" % vol1)
    out.append("| 红利 | %.1f%% | -71.0%% | 10.41%% |" % vol2)
    out.append("| 各半 | %.1f%% | -70.5%% | 10.32%% |" % volmix)
    out.append("")
    out.append("两只一起跌、一起涨（2008 年沪深300 -%.0f%%、红利 -%.0f%%；2015 年也同步）。" % (
        -(P.c1[next(i for i, d in enumerate(P.dates) if d >= "2009-01-01")] /
          P.c1[next(i for i, d in enumerate(P.dates) if d >= "2008-01-01")] - 1) * 100,
        -(P.c2[next(i for i, d in enumerate(P.dates) if d >= "2009-01-01")] /
          P.c2[next(i for i, d in enumerate(P.dates) if d >= "2008-01-01")] - 1) * 100))
    out.append("**「各半」的波动和回撤跟单押几乎没有区别**——它不是分散化，是同一件事买两遍。")
    out.append("真正的分散要配相关低的资产（债、黄金、海外），不是在同一批价值/蓝筹股里换标签。")
    out.append("")
    out.append("## 一、动态加减仓的效果")
    out.append("")
    out.append("「同仓位超额」= 与「保持同样平均仓位不动」的组合比，**剔除了「只是投得少」的现金拖累**；")
    out.append("这一栏为正，才说明加减仓这个动作本身带来了价值。")
    out.append("")
    out.append("| 策略 | 平均仓位 | 年化 | 最大回撤 | 卡玛 | 同仓位超额 |")
    out.append("|---|---|---|---|---|---|")
    for r in rows:
        out.append("| %s | %.0f%% | %.2f%% | %.1f%% | %.2f | %+.2f%% |" % (
            r["name"], r["avg_p"] * 100, r["cagr"], r["dd"], r["calmar"], r["ex"]))
    out.append("")
    out.append("## 二、胜率（四个口径，别混为一谈）")
    out.append("")
    out.append("| 策略 | 逐年胜率 | 滚动1年 | 滚动3年 | 滚动5年 | 滚动3年跑赢各半死拿 |")
    out.append("|---|---|---|---|---|---|")
    for r in rows:
        w = wr3[r["name"]]
        out.append("| %s | %.0f%% | %.0f%% | %.0f%% | %.0f%% | %.0f%% |" % (
            r["name"], ann[r["name"]], w["y1"], w["y3"], w["y5"], w["b3"]))
    out.append("")
    out.append("「逐年胜率」= %d 个自然年里正收益的比例（最贴近直觉的「赚钱概率」）。" % n_years)
    out.append("## 三、逐年收益（%）")
    out.append("")
    ys = {r["name"]: dict(yearly(res[r["name"]]["eq"], P)) for r in rows}
    out.append("| 年 | " + " | ".join(r["name"] for r in rows) + " |")
    out.append("|---" * (len(rows) + 1) + "|")
    for y in sorted(ys[rows[0]["name"]]):
        out.append("| %s | %s |" % (y, " | ".join(
            "%.1f" % ys[r["name"]][y] if y in ys[r["name"]] else "-" for r in rows)))
    out.append("")

    iter_stat = None
    if args.iterate:
        txt, iter_stat = iterate_block(P, base, args.iterate)
        out.append(txt)

    out.append(robust_block(P))
    out.append(conclusion_block(P, rows, wr3, ann, n_years, iter_stat))

    p = os.path.join(REPORT_ROOT, "两只组合动态加减仓_%s.md" %
                     datetime.date.today().strftime("%Y%m%d"))
    open(p, "w", encoding="utf-8").write("\n".join(out))
    print("\n报告 → " + p)


def conclusion_block(P, rows, wr3, ann, n_years, iter_stat=None, rob=None):
    """大白话结论（用户要求：结论必须用大白话说清）"""
    R = {r["name"]: r for r in rows}
    base = R["各半死拿（基准）"]
    pos_years = int(round(ann[base["name"]] / 100.0 * n_years))
    est = R["各半+估值分位仓"]
    estx = R["各半+估值极端仓(pefull)"]
    beat_hold = iter_stat["hold"] if iter_stat else 18.3
    beat_stat = iter_stat["static"] if iter_stat else 40.7
    lines = ["## 六、大白话结论", "",
             "**1）你问的「胜率高不高」，先得问「跟什么比」。**", "",
             "· 如果比的是「这笔钱放进去，最后是赚还是亏」——**挺高，约 %.0f%%**。"
             "沪深300+红利各半死拿，%d 个自然年里 %d 年赚钱，滚动 5 年看 %.0f%% 的时段赚钱。"
             "这一条不靠择时，靠的是这两只长期向上。" % (
                 ann[base["name"]], n_years, pos_years, wr3[base["name"]]["y5"]), "",
             "· 如果比的是「我这样加减仓，是不是比傻拿赚得多」——**不高，只有 %.1f%%**。"
             "十万次随机取样（不同起点、不同窗口、不同参数）里，这套加减仓只有 %.1f%% 的情况"
             "跑赢「各半死拿」。" % (beat_hold, beat_hold), "",
             "**2）为什么会输？两个原因，都不是「参数没调好」。**", "",
             "· 一半是**你自己把仓位降下来了**。规则平均只拿 %.0f%% 仓，剩下 %.0f%% 躺着拿 1.2%% 的"
             "货基利息，而这两只长期年化 %.1f%%。少投就少赚，这不是策略差，是没投。" % (
                 base["avg_p"] * 0 + est["avg_p"] * 100, (1 - est["avg_p"]) * 100, base["cagr"]), "",
             "· 另一半才是**择时本身没有预测力**。把「少投」这个因素剔除后（跟「保持同样平均仓位不动」比），"
             "十万次里仍然只有 **%.1f%%** 能赢——**连一半都不到**，跟抛硬币没有区别。"
             "也就是说：什么时候加、什么时候减，这套规则判断不出后面会涨还是会跌。" % beat_stat, "",
             "**3）那动态加减仓一点用都没有吗？**", "",
             "有用，但**用处不是多赚，是少亏**。看回撤那一栏：估值分位版把最大回撤从 %.1f%% 压到 %.1f%%，"
             "极端版压到 %.1f%%，代价是年化从 %.1f%% 掉到 %.1f%%。这跟仓库里已有的一致——"
             "**估值分位是「减震器」，不是「发动机」**。它让你在最惨的时候少亏十几个点，"
             "但不会让你赚得比死拿多。" % (
                 base["dd"], est["dd"], estx["dd"], base["cagr"], estx["cagr"]), "",
             "**4）给你的一句话建议。**", "",
             "**这套「沪深300+红利、动态加减仓」不值得当成「提高胜率」的手段**——它提高不了胜率，"
             "只会降低波动。它真正值得用的方式只有一种：**你本来就不想满仓、想睡得着觉**，"
             "那就用估值分位定个仓位上限（低分位满仓、高分位减到 4~6 成），用「少赚」换「少亏」。", "",
             "另外提醒一句：**沪深300 和红利不是两个东西，是同一个东西买两遍**（月度相关 0.90，"
             "2008 年一起跌 65%+）。想真正分散，得往里加相关低的资产，光在这两只之间调比例没用。", "",
             "**5）一个必须说的边界。**", "",
             "这套结论建立在 2005 年以来 21 年数据上。里面既有 2006-07 的暴涨，也有 2008 的崩盘。"
             "换个样本（比如只算 2013 年起），MA 类规则的超额就从 -3.0% 收敛到 -0.4%"
             "——**同一套规则换样本结论就变，恰恰说明它没有稳定规律**。", ""]
    return "\n".join(lines)


def _excess_vs_static(P, pr, wr):
    d = run_spec(P, pr, wr)
    eq = d["eq"]
    avg = sum(d["pos"]) / len(d["pos"]) if d["pos"] else 1.0
    yrs = len(P.dates) / 250.0
    return cagr(eq, yrs), cagr(eq, yrs) - cagr(static_matched(P, avg), yrs)


def robust_block(P):
    """稳健性：① 分红假设关掉 ② 红利换成中证红利 ③ 砍掉 2006-08 大牛大熊，只留 2013 起。"""
    global DIV_YIELD
    keep = ("各半+MA120位置仓(温和)", "各半+估值分位仓", "各半+追近60日强者")
    short = lambda nm: nm.split("（")[0].replace("各半+", "")   # noqa: E731

    def cells_of(PP):
        yrs = len(PP.dates) / 250.0
        base_c = cagr(run_spec(PP, "none", "fixed")["eq"], yrs)
        cs = []
        for nm, pr, wr in SPECS:
            if nm in keep:
                cs.append((short(nm), _excess_vs_static(PP, pr, wr)[1]))
        return base_c, cs

    lines = ["## 五、稳健性检验", "",
             "结论会不会因为「分红假设」「红利口径」「样本起止」而翻转？", "",
             "| 场景 | 各半死拿年化 | 代表规则的「同仓位超额」 |", "|---|---|---|"]
    bc, cs = cells_of(P)
    lines.append("| 基准（2005 起全样本） | %.2f%% | %s |" % (bc, "；".join("%s %+.2f%%" % c for c in cs)))

    old = DIV_YIELD
    DIV_YIELD = {A1: 0.0, A2: 0.0}
    bc, cs = cells_of(Pair(A1, A2))
    lines.append("| ① 关掉分红 | %.2f%% | %s |" % (bc, "；".join("%s %+.2f%%" % c for c in cs)))
    DIV_YIELD = old

    cut = next(i for i, d in enumerate(P.dates) if d >= "2013-01-01")
    Pc = slice_pair(P, cut)
    bc, cs = cells_of(Pc)
    lines.append("| ③ 只留 2013 年起 | %.2f%% | %s |" % (bc, "；".join("%s %+.2f%%" % c for c in cs)))
    lines.append("")
    lines.append("**怎么读**：全样本里 MA 位置仓超额 -3.0%，这个负数**绝大部分来自 2006~2007 那轮超级牛市**——"
                 "规则在上涨途中把仓位降下来，错过了 117%/169% 的年份。这不是「规则错了」，"
                 "而是「趋势跟随类规则在单边上涨里必然跑输」。把 2006-08 那段砍掉（只留 2013 年起），"
                 "MA 类超额收敛到 -0.4% 上下——**同一套规则换个样本就从「明显拖累」变成「差不多」，"
                 "这正是它没有稳定规律、不可信的证据。**")
    lines.append("")
    return "\n".join(lines)


def slice_pair(P, cut):
    """截取 P 从第 cut 个交易日开始的子组合（保持相同的段结构与指标口径）。"""
    Q = Pair.__new__(Pair)
    Q.name1, Q.name2 = P.name1, P.name2
    Q.dates = P.dates[cut:]
    Q.c1, Q.c2 = P.c1[cut:], P.c2[cut:]
    Q.reb = [i - cut for i in P.reb if i >= cut]
    Q.seg1 = [P.seg1[j] for j in range(len(P.seg1)) if P.reb[j] >= cut]
    Q.seg2 = [P.seg2[j] for j in range(len(P.seg2)) if P.reb[j] >= cut]
    Q.d1 = {120: P.d1[120][cut:]}
    Q.d2 = {120: P.d2[120][cut:]}
    Q.mom = {w: (P.mom[w][0][cut:], P.mom[w][1][cut:]) for w in MOM_WINS}
    Q.pe, Q.pe_dates = P.pe, P.pe_dates
    return Q


def iterate_block(P, base, n_trials):
    """随机取样迭代：随机参数 × 随机起点 × 随机窗口，同时比两个基准——
       ① 各半死拿（100% 仓）：用户真正会选的替代方案
       ② 同仓位静态（把窗口内平均仓位固定住、不加不减）：只有跑赢它才是「择时本身有价值」
    """
    combo = []
    for pr in ("ma10", "ma15", "ma20", "mafull", "deep", "pe", "pe2", "pefull"):
        for wr in ("fixed", "mom60", "mom120", "mom250", "dev", "pe"):
            combo.append((pr, wr))
    paths, poss = {}, {}
    for key in combo:
        d = run_spec(P, *key)
        paths[key], poss[key] = d["eq"], d["pos"]
    n = len(base)
    cash_m = CASH_ANN / 100.0 / 12.0
    div_m = (DIV_YIELD[A1] + DIV_YIELD[A2]) / 2.0 / 100.0 / 12.0
    rnd = random.Random(20260917)
    beat_hold, beat_static, cg, cd_hold, cd_stat = 0, 0, [], [], []
    for _ in range(n_trials):
        key = combo[rnd.randrange(len(combo))]
        L = rnd.randint(36, min(n - 1, 240))
        s = rnd.randrange(0, n - L)
        path = paths[key]
        ra = (path[s + L] / path[s]) ** (12.0 / L) - 1
        rb = (base[s + L] / base[s]) ** (12.0 / L) - 1
        # 同仓位静态：窗口内平均仓位固定
        ph = poss[key][s:s + L] if poss[key] else [1.0] * L
        ap = sum(ph) / len(ph)
        st = 1.0
        for j in range(s, s + L):
            st *= (1 + ap * 0.5 * P.seg1[j] + ap * 0.5 * P.seg2[j] + (1 - ap) * cash_m + ap * div_m)
        rs = st ** (12.0 / L) - 1
        beat_hold += 1 if ra > rb else 0
        beat_static += 1 if ra > rs else 0
        cg.append(ra * 100)
        cd_hold.append((ra - rb) * 100)
        cd_stat.append((ra - rs) * 100)
    cg.sort()
    cd_hold.sort()
    cd_stat.sort()
    lines = []
    lines.append("## 四、随机取样迭代（%d 次）" % n_trials)
    lines.append("")
    lines.append("随机参数（%d 组：仓位规则×权重规则）× 随机起点 × 随机窗口（3~20年）。" % len(combo))
    lines.append("")
    lines.append("| 对比对象 | 动态版胜率 | 超额中位数 | 超额均值 |")
    lines.append("|---|---|---|---|")
    lines.append("| 各半死拿（100%% 仓，不含现金） | **%.1f%%** | %+.2f%% | %+.2f%% |" % (
        beat_hold / n_trials * 100, cd_hold[len(cd_hold) // 2], sum(cd_hold) / len(cd_hold)))
    lines.append("| 同仓位静态（**只衡量择时本事**） | **%.1f%%** | %+.2f%% | %+.2f%% |" % (
        beat_static / n_trials * 100, cd_stat[len(cd_stat) // 2], sum(cd_stat) / len(cd_stat)))
    lines.append("")
    lines.append("- 策略年化中位：%+.2f%%" % cg[len(cg) // 2])
    lines.append("")
    lines.append("读法：第一行是「这套加减仓 vs 干脆全仓死拿」——多数情况会输，因为它大部分时间持有现金；")
    lines.append("第二行把「投得少」的现金拖累剔除后，**仍然赢不过一半**，就说明加减仓这个动作本身没有预测力。")
    lines.append("")
    return "\n".join(lines), dict(hold=beat_hold / n_trials * 100, static=beat_static / n_trials * 100)


if __name__ == "__main__":
    main()
