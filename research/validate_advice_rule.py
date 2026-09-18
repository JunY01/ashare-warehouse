# -*- coding: utf-8 -*-
"""估值规则验证台 —— 任何想加进 holding_advice 的规则，先过这里再上线。

为什么需要它（2026-09-16 的教训）：我向用户提了一个"ROE趋势闸门"，全市场汇总数据
很支持它，但中证银行单独看完全相反（PE 高位的独立时段全部下跌，与规则主张相反）。
规则一旦写进引擎是**对所有标的统一生效**的，所以"汇总通过"绝不等于"可以上线" ——
必须同时看个别指数是否变成例外。本脚本把这条检验流程固化下来。

两条硬口径（都是踩坑换来的）：
  1. **无前视**：分位只用"当时能看到的历史"（滚动 PCT_WIN 周）算，不用全样本分位。
     全样本分位会把未来的高低点泄露给过去，让规则看起来比实际准。
  2. **证据看独立时段、幅度看周中位**：周频样本高度重叠，命中集常集中在少数几段行情里
     （如"2018年1-2月"按周能数成 5 个样本）。故——
       · 收益中位数用**周样本**算（两组同口径、可比，与引擎 basis_score 一致）；
       · 但判决要求命中集的**独立时段数** ≥ MIN_EPISODES，否则视为证据集中在个别行情。
     注意：独立时段合并只对**命中集**有意义。对照组（如"非高PE"）在时间上几乎连续，
     硬合并会塌成一段、其"起点收益"等于一个任意日期，故对照组只报周样本数。

规则怎么写：在 RULES 里加一条即可。hit 为命中条件，base 为对照条件（缺省=命中集的补集），
expect 说明规则主张的方向（hit_worse=命中组未来收益更差 / hit_better=更好）。

用法:
  python research/validate_advice_rule.py                  # 跑全部规则
  python research/validate_advice_rule.py --rule high_pe   # 只跑某条（按 id 前缀匹配）

上线是**手动步骤**：本脚本只判定"这份证据够不够"，通过后由人决定是否写进
src/jobs_build/holding_advice.py，并同步检查所有输出点。
纯标准库。
"""
import bisect
import csv
import datetime
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.common.paths import DATA_ROOT
from src.jobs_build import holding_advice as ha

KDIR = os.path.join(DATA_ROOT, "kline_full")
FWD = 250          # 未来一年（交易日）
PCT_WIN = 156      # 分位滚动窗口（周，约 3 年）
EP_GAP_DAYS = 21   # 相邻命中样本间隔超过此天数即算新的独立时段
MIN_EPISODES = 8   # 判决所需的最少命中独立时段数
MIN_SPREAD = 0.05  # 判决所需的最小收益差异（5 个百分点）


def series(hist, name, key):
    for v in hist.values():
        if v.get("name") == name:
            return {x["ts"]: x[key] for x in v["series"] if x.get(key)}
    return {}


def cells(name, pe_h, pb_h):
    """该指数逐周状态（只保留未来一年已知的周）。分位用滚动窗口算，避免前视。"""
    P, B = series(pe_h, name, "pe"), series(pb_h, name, "pb")
    TS = sorted(set(P) & set(B))
    if len(TS) < PCT_WIN + 50:
        return []
    path = os.path.join(KDIR, name + ".csv")
    if not os.path.exists(path):
        return []
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    kd = [r["日期"] for r in rows]
    kc = [float(r["收盘"]) for r in rows]
    pe_a = [P[t] for t in TS]
    pb_a = [B[t] for t in TS]
    roe_a = [b / p for b, p in zip(pb_a, pe_a)]
    out = []
    for i, t in enumerate(TS):
        if i + 1 < PCT_WIN:
            continue
        dt = datetime.datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d")
        k = bisect.bisect_left(kd, dt)
        if k >= len(kd) or k + FWD >= len(kd):
            continue
        w = slice(i + 1 - PCT_WIN, i + 1)

        def pct(arr):
            """分位：只用最近 PCT_WIN 周（滚动）。"""
            sub = arr[w]
            return bisect.bisect_left(sorted(sub), arr[i]) / len(sub) * 100

        def rng(arr):
            """区间位置：当前值落在"至今为止全部历史"的最低~最高之间的百分比。

            必须用全历史而不是滚动窗口——同一个滚动窗口里，"分位≥80%"几乎等价于
            "接近窗口最高点"，区间位置就退化成分位的换算了。用全历史才可能两者背离
            （银行就是这个背离：市盈率分位 95%，但绝对值只在自己区间的 66% 位置）。
            只取 arr[:i+1]，仍然无前视。
            """
            sub = arr[:i + 1]
            lo, hi = min(sub), max(sub)
            return 50.0 if hi <= lo else (arr[i] - lo) / (hi - lo) * 100

        out.append({"ts": t, "pe_pct": pct(pe_a), "pb_pct": pct(pb_a), "roe_pct": pct(roe_a),
                    "pe_range": rng(pe_a), "pb_range": rng(pb_a),
                    "fwd": kc[k + FWD] / kc[k] - 1,
                    # 引擎真正的触发条件：只用截至当周的数据跑它自己的 windows/verdict
                    "pe_v": ha.verdict(ha.windows(pe_a[:i + 1], pe_a[i]))[0],
                    "pb_v": ha.verdict(ha.windows(pb_a[:i + 1], pb_a[i]))[0]})
    return out


def episodes(points):
    """把命中点按时间连续性合并成独立时段（只对稀疏的命中集使用）。"""
    if not points:
        return []
    pts = sorted(points, key=lambda p: p["ts"])
    runs, cur = [], [pts[0]]
    for prev, cur_p in zip(pts, pts[1:]):
        if (cur_p["ts"] - prev["ts"]) / 1000 / 86400 > EP_GAP_DAYS:
            runs.append(cur)
            cur = []
        cur.append(cur_p)
    runs.append(cur)
    return runs


def stat(points):
    """返回 (周样本数, 独立时段数, 周中位收益, 时段起点中位收益)。"""
    if not points:
        return 0, 0, None, None
    eps = episodes(points)
    return (len(points), len(eps),
            st.median(p["fwd"] for p in points),
            st.median(e[0]["fwd"] for e in eps))


def judge(n_ep_hit, d, expect):
    if n_ep_hit < MIN_EPISODES or d is None:
        return "样本不足(命中独立时段 %d <%d)" % (n_ep_hit, MIN_EPISODES)
    ok = d >= MIN_SPREAD if expect == "hit_worse" else d <= -MIN_SPREAD
    return "通过" if ok else "不通过"


RULES = [
    {"id": "engine_reduce", "name": "引擎减仓触发（多窗口一致≥80%）预示下跌", "expect": "hit_worse",
     "hit": lambda c: c["pe_v"] == "REDUCE" or c["pb_v"] == "REDUCE"},
    {"id": "engine_add", "name": "引擎加仓触发（多窗口一致≤20%）预示上涨", "expect": "hit_better",
     "hit": lambda c: c["pe_v"] == "ADD" or c["pb_v"] == "ADD"},
    {"id": "high_pe", "name": "高PE分位(>=80)预示下跌", "expect": "hit_worse",
     "hit": lambda c: c["pe_pct"] >= 80},
    {"id": "low_pe", "name": "低PE分位(<=20)预示上涨", "expect": "hit_better",
     "hit": lambda c: c["pe_pct"] <= 20},
    {"id": "high_pe_high_pb", "name": "高PE且PB同高，比高PE且PB低更差", "expect": "hit_worse",
     "hit": lambda c: c["pe_pct"] >= 80 and c["pb_pct"] >= 50,
     "base": lambda c: c["pe_pct"] >= 80 and c["pb_pct"] < 50},
    {"id": "abs_level", "name": "分位高但绝对水平仍在中低位时，高PE不预示下跌", "expect": "hit_better",
     "hit": lambda c: c["pe_pct"] >= 80 and c["pe_range"] < 50,
     "base": lambda c: c["pe_pct"] >= 80 and c["pe_range"] >= 50},
    {"id": "roe_guard", "name": "盈利(ROE)低位时高PE不预示下跌", "expect": "hit_better",
     "hit": lambda c: c["pe_pct"] >= 80 and c["roe_pct"] < 33,
     "base": lambda c: c["pe_pct"] >= 80 and c["roe_pct"] >= 33},
]


def run_rule(rule, universe):
    hit_pool, base_pool, per_index = [], [], []
    for name, cs in universe:
        h = [c for c in cs if rule["hit"](c)]
        b = [c for c in cs if (rule["base"](c) if rule.get("base") else not rule["hit"](c))]
        hit_pool += h
        base_pool += b
        nh, _, mh, _ = stat(h)
        nb, _, mb, _ = stat(b)
        if nh and nb:
            per_index.append((name, nh, nb, mb - mh))
    nh, neh, mh, _ = stat(hit_pool)
    nb, _, mb, _ = stat(base_pool)
    d = None if (mh is None or mb is None) else mb - mh
    return {"pooled": (nh, neh, nb, mh, mb, d), "per_index": per_index,
            "verdict": judge(neh, d, rule["expect"])}


def main():
    only = sys.argv[sys.argv.index("--rule") + 1] if "--rule" in sys.argv else None
    pe_h, pb_h = ha.load_hist()
    universes = [(n[:-4], cells(n[:-4], pe_h, pb_h))
                 for n in sorted(os.listdir(KDIR)) if n.endswith(".csv")]
    universes = [(n, c) for n, c in universes if c]
    print("样本宇宙: %d 个指数；分位用滚动 %d 周（无前视）；未来一年已知的周参与统计"
          % (len(universes), PCT_WIN))
    print("判定口径: 命中集独立时段数 ≥%d，且周中位收益差异 ≥%.0fpp、方向合乎主张"
          % (MIN_EPISODES, MIN_SPREAD * 100))

    for rule in RULES:
        if only and not rule["id"].startswith(only):
            continue
        r = run_rule(rule, universes)
        nh, neh, nb, mh, mb, d = r["pooled"]
        print("\n" + "=" * 96)
        print("规则 %s —— %s   [主张: %s]" % (rule["id"], rule["name"], rule["expect"]))
        print("  汇总: 命中 %d 周 / %d 独立时段   对照 %d 周" % (nh, neh, nb))
        if mh is not None and mb is not None:
            print("        命中周中位 %+6.1f%%   对照周中位 %+6.1f%%   差异 %+6.1fpp"
                  % (mh * 100, mb * 100, d * 100))
        print("  判定: %s" % r["verdict"])

        if d is not None and r["per_index"]:
            same = [p for p in r["per_index"] if (p[3] > 0) == (d > 0)]
            opp = [p for p in r["per_index"] if (p[3] > 0) != (d > 0)]
            print("  逐指数方向: 同向 %d / 反向 %d（%d 个指数有样本）"
                  % (len(same), len(opp), len(r["per_index"])))
            for name, nh2, nb2, dd in opp:
                print("    反向: %-8s 命中%3d周/对照%3d周  差异 %+6.1fpp" % (name, nh2, nb2, dd * 100))


if __name__ == "__main__":
    main()
