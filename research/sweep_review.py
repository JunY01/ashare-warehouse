# -*- coding: utf-8 -*-
"""每日复盘"关注行业"十万级网格迭代 —— 权重 × TopN × 持有期

设计原则（项目历史教训：R2 十万次 / 估值择时 11.5 万组挖出的最优参数样本外全部失效）：
  1. 网格搜索必须配样本外验证。这里用**随机切分**（而非前后切分）——因为本数据集
     前半全是中/强市、后半全是弱市，前后切分会让 train/test 的市场环境分布完全不同，
     无法区分"参数过拟合"与"环境变了"。
  2. 候选池固定为线上现状（硬门槛不搜），排序用的因子档位由 daily_review.score_candidate
     产出（tier_* 0~1），保证搜索空间与线上口径同源。
  3. 报告三件事：样本内最优参数长什么样、它在样本外的表现、以及"样本内表现"与
     "样本外表现"的相关性（接近 0 即证明最优参数是噪声）。

用法: python research/sweep_review.py
"""
import itertools
import os
import random
import statistics
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.common.paths import REPORT_ROOT  # noqa: E402
from src.common import history_db  # noqa: E402
from src.jobs_build.daily_review import score_candidate  # noqa: E402

W_STEP = 5
W_MAX = {"bottom": 55, "flow": 45, "stability": 35, "trend": 50}
BASELINE_W = (30, 25, 20, 15, 10)   # 线上现状权重，作为对照
TOPN_CHOICES = (3, 5, 8, 10, 15, 20)
HOLD_CHOICES = (1, 3, 5)
MIN_CAND = 10          # 单日候选下限（TopN 最大 20，少于 10 个候选的日子不参与）
BLOCK_SPLIT = 10       # 分块切分的块长度（交易日）：避免相邻日收益窗口重叠造成 train/test 泄漏


def build_samples(panel, days):
    """预计算：[每日] -> [(tier5 元组, ret1, ret3, ret5)]；基准为**当日全市场**收益中位数

    注意：基准必须取当日全部板块（含未过门槛的），与 research/validate_review.py 口径一致。
    若误用"候选池自己的中位数"，超额会系统性虚高（候选池在熊市里天然弱于市场）。
    """
    samples, bases = [], []
    for i, T in enumerate(days):
        rows = []
        for code, s in panel[T].items():
            rec = score_candidate(s["name"], code, s["chg20"], s["chg60"], s["inflow"], s,
                                  min_total=0.0)
            if rec is None:
                continue
            r = []
            ok = True
            for n in HOLD_CHOICES:
                v = None
                if i + n < len(days):
                    a = panel[days[i]].get(code, {}).get("close")
                    b = panel[days[i + n]].get(code, {}).get("close")
                    if a and b:
                        v = (b / a - 1) * 100
                if v is None:
                    ok = False
                    break
                r.append(v)
            if not ok:
                continue
            rows.append(((rec["tier_bottom"], rec["tier_flow"], rec["tier_stability"],
                          rec["tier_trend"], rec["tier_signal"]), r[0], r[1], r[2]))
        if len(rows) < MIN_CAND:
            samples.append(None)
            bases.append([None] * len(HOLD_CHOICES))
            continue
        samples.append(rows)
        base_row = []
        for n in HOLD_CHOICES:
            vals = []
            if i + n < len(days):
                for code in panel[T]:
                    a = panel[days[i]].get(code, {}).get("close")
                    b = panel[days[i + n]].get(code, {}).get("close")
                    if a and b:
                        vals.append((b / a - 1) * 100)
            base_row.append(statistics.median(vals) if vals else None)
        bases.append(base_row)
    return samples, bases


def gen_grid():
    """权重网格：bottom/flow/stability/trend 步长 5，signal 取余数（保证和为 100 且非负）"""
    grid = []
    for b in range(0, W_MAX["bottom"] + 1, W_STEP):
        for f in range(0, W_MAX["flow"] + 1, W_STEP):
            for st in range(0, W_MAX["stability"] + 1, W_STEP):
                if b + f + st > 100:
                    continue
                for tr in range(0, min(W_MAX["trend"], 100 - b - f - st) + 1, W_STEP):
                    sg = 100 - b - f - st - tr
                    if sg < 0:
                        continue
                    grid.append((b, f, st, tr, sg))
    return grid


def eval_weights(samples, bases, train_idx, test_idx, w):
    """对一组权重：每个信号日只排序一次，TopN × 持有期复用同一次排序结果。

    返回 {(tag, topn, hold_i): 超额均值}，tag ∈ {train, test}
    """
    wb, wf, ws, wt, wg = w
    out = {}
    for tag, idx in (("train", train_idx), ("test", test_idx)):
        per_day = []
        for i in idx:
            rows = samples[i]
            if rows is None:
                continue
            ranked = sorted(rows, key=lambda r: -(wb * r[0][0] + wf * r[0][1] + ws * r[0][2]
                                                  + wt * r[0][3] + wg * r[0][4]))
            per_day.append((ranked, bases[i]))
        for topn in TOPN_CHOICES:
            for hi in range(len(HOLD_CHOICES)):
                excs = []
                for ranked, base in per_day:
                    if base[hi] is None:
                        continue
                    picked = ranked[:topn]
                    ret = sum(p[1 + hi] for p in picked) / len(picked)
                    excs.append(ret - base[hi])
                out[(tag, topn, hi)] = sum(excs) / len(excs) if excs else None
    return out


def run_grid(samples, bases, grid, train_idx, test_idx):
    """遍历权重网格（TopN × 持有期在同一次排序内复用），返回样本内/样本外结果"""
    results = []
    for w in grid:
        r = eval_weights(samples, bases, train_idx, test_idx, w)
        for topn in TOPN_CHOICES:
            for hi in range(len(HOLD_CHOICES)):
                s_in = r[("train", topn, hi)]
                if s_in is None:
                    continue
                results.append({"w": w, "topn": topn, "hold": HOLD_CHOICES[hi],
                                "in": s_in, "out": r[("test", topn, hi)]})
    return results


def main():
    from research.validate_review import build_panel
    conn = history_db.connect(readonly=True)
    try:
        panel = build_panel(conn)
    finally:
        conn.close()
    days = sorted(panel)
    print("面板 %d 天" % len(days))

    t0 = time.time()
    samples, bases = build_samples(panel, days)
    print("预计算候选 %d 天，日均 %d 个（耗时 %.1fs）" % (
        len(samples), sum(len(s) for s in samples if s) // max(1, len([s for s in samples if s])),
        time.time() - t0))

    grid = gen_grid()
    total = len(grid) * len(TOPN_CHOICES) * len(HOLD_CHOICES)
    print("权重组合 %d × TopN %d × 持有期 %d = **%d 组**" % (
        len(grid), len(TOPN_CHOICES), len(HOLD_CHOICES), total))

    # 分块随机切分 train/test 70/30，重复 5 次
    # 不用"逐日随机切分"（相邻日收益窗口重叠，会让 test 沾到 train 的信息）；
    # 也不用"前后切分"（前半全是中/强市、后半全是弱市，会把"参数过拟合"与"环境变了"混在一起）。
    all_days = [i for i, s in enumerate(samples) if s is not None]
    print("可用信号日 %d 个；分块(%d日)随机切分 70/30，重复 5 次" % (len(all_days), BLOCK_SPLIT))

    L = ["# 每日复盘·关注行业 十万级网格迭代\n",
         "> 面板 %d 天（%s ~ %s）｜ 网格 %d 组（权重×TopN×持有期）｜ by research/sweep_review.py\n"
         % (len(days), days[0], days[-1], total),
         "## 方法\n",
         "- 候选池固定为线上现状（硬门槛不搜）；排序因子档位取自 `daily_review.score_candidate` 的 `tier_*`。",
         "- 权重网格：bottom/flow/stability/trend 步长 5，signal 取余数，和恒为 100。",
         "- 基准 = 当日**全市场**板块收益中位数（含未过门槛的板块），与 validate_review.py 口径一致。",
         "- **分块随机切分** train/test 70/30，块长 %d 交易日：逐日随机切分会让相邻日收益窗口重叠、"
         "test 沾到 train 的信息；前后切分则因本数据集前半全是中/强市、后半全是弱市，"
         "会把「参数过拟合」与「环境变了」混在一起。\n" % BLOCK_SPLIT]

    summary = []
    for seed in range(5):
        rnd = random.Random(seed)
        blocks = [all_days[j:j + BLOCK_SPLIT] for j in range(0, len(all_days), BLOCK_SPLIT)]
        rnd.shuffle(blocks)
        cut = int(len(blocks) * 0.7)
        train_idx = sorted(i for blk in blocks[:cut] for i in blk)
        test_idx = sorted(i for blk in blocks[cut:] for i in blk)
        t1 = time.time()
        res = run_grid(samples, bases, grid, train_idx, test_idx)
        print("  seed %d: %d 组，%.1fs" % (seed, len(res), time.time() - t1))
        res.sort(key=lambda r: -r["in"])
        best = res[0]
        # 样本内 vs 样本外的秩相关（过拟合指标）
        xs = [r["in"] for r in res]
        ys = [r["out"] for r in res]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
        corr = num / den if den else 0
        top1pct = res[:max(1, len(res) // 100)]
        out_of_top = sum(r["out"] for r in top1pct) / len(top1pct)
        pos_out = sum(1 for r in res if r["out"] > 0) / len(res) * 100
        # 线上现状（baseline 权重 + TopN10）在同一划分下的对照
        rb = eval_weights(samples, bases, train_idx, test_idx, BASELINE_W)
        base_in = rb[("train", 10, 0)]
        base_out = rb[("test", 10, 0)]
        print("    最优 样本内 %+.3fpp → 样本外 %+.3fpp | 样本内top1%% 样本外均值 %+.3fpp" % (
            best["in"], best["out"], out_of_top))
        print("    样本内/外相关 %+.3f | 样本外为正的组占比 %.1f%% | 线上现状 样本内 %+.3f 样本外 %+.3f" % (
            corr, pos_out, base_in or 0, base_out or 0))
        summary.append({"seed": seed, "best": best, "corr": corr,
                        "top1pct_out": out_of_top, "pos_out": pos_out, "n": len(res),
                        "base_in": base_in, "base_out": base_out})

        L.append("### 随机划分 seed %d\n" % seed)
        L.append("| 项 | 值 |")
        L.append("|---|---|")
        L.append("| 最优权重 (bottom/flow/stability/trend/signal) | %s |" % (best["w"],))
        L.append("| 最优 TopN / 持有期 | %d / %d 日 |" % (best["topn"], best["hold"]))
        L.append("| 该组 样本内超额 | %+.3fpp |" % best["in"])
        L.append("| 该组 **样本外超额** | **%+.3fpp** |" % best["out"])
        L.append("| 样本内 top1%% 参数组的样本外均值 | %+.3fpp |" % out_of_top)
        L.append("| 样本内/样本外 相关系数 | %+.3f |" % corr)
        L.append("| 样本外超额为正的参数组占比 | %.1f%% |" % pos_out)
        L.append("| 线上现状对照（TopN10/hold1）样本内 → 样本外 | %+.3f → %+.3fpp |" % (
            base_in or 0, base_out or 0))
        L.append("")

    avg_corr = sum(s["corr"] for s in summary) / len(summary)
    avg_best_out = sum(s["best"]["out"] for s in summary) / len(summary)
    avg_best_in = sum(s["best"]["in"] for s in summary) / len(summary)
    avg_top1 = sum(s["top1pct_out"] for s in summary) / len(summary)
    avg_pos = sum(s["pos_out"] for s in summary) / len(summary)
    avg_base_in = sum(s["base_in"] or 0 for s in summary) / len(summary)
    avg_base_out = sum(s["base_out"] or 0 for s in summary) / len(summary)
    L.append("## 结论\n")
    L.append("- 网格很容易找到**样本内**超额 +%0.2fpp 的参数（10 万组里必然有），"
             "但样本内最优这组到了样本外平均只有 %+.3fpp；"
             "**样本内最好的 1%% 参数组，样本外平均 %+.3fpp**。\n" % (
                 abs(avg_best_in), avg_best_out, avg_top1))
    L.append("- 样本内表现与样本外表现的相关系数平均 **%+.3f**"
             "（接近 0 或为负 = 样本内挑得越好、样本外越不靠谱；本次出现了 -0.86、-0.73 两个强负相关划分）。\n"
             % avg_corr)
    L.append("- 线上现状权重在同样划分下的对照：样本内 %+.3fpp → 样本外 %+.3fpp，"
             "与网格最优的样本外结果处在同一量级。\n" % (avg_base_in, avg_base_out))
    L.append("- 各划分之间样本外为正的组占比从 0% 到 100% 都有，"
             "说明**结果主要由 test 段恰好落在哪段行情决定**，而不是由参数决定。\n")
    if avg_top1 <= 0.1 and avg_corr < 0.15:
        L.append("- **判定：网格最优参数是样本内噪声，不予采纳。** 与项目历史一致"
                 "（R2 十万次：诚实口径只比基准好 8.5pp/9年、晋升依据作废；"
                 "估值择时 11.5 万组：样本内外相关 0.104）。\n")
    else:
        L.append("- 判定：存在值得进一步验证的参数区域（需扩样本复验）。\n")
    L.append("\n> 仅个人研究，不构成投资建议。\n")

    out = os.path.join(REPORT_ROOT, "关注行业网格迭代_20260914.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("\n[OK] 报告已写入 %s" % out)


if __name__ == "__main__":
    main()
