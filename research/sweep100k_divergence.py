# -*- coding: utf-8 -*-
"""三重背离十万次迭代 —— 回答"这条规则是真规律，还是我挑的那几段行情"

前情：`research/divergence_line.py` 把知乎原文的三重背离写成确定性判据并长历史回测，
两个关键发现（见 reports/三重背离验证_*.md）：
  ① **顶背离侧**：得分（三重共振程度）**毫无增量**——只要"创 L 日新高且价在半年线上方
     15% 以上"，20 日超额就已经是负的（-2.4% 级），加不加背离差不多。也就是说
     "三重背离"这个名字对逃顶是多余的，起作用的其实是**位置**（离半年线太远）。
  ② **底背离侧**：只有叠加"价在半年线下方 15% 以上（深跌区）"才转正，且
     **无背离的对照组最差**（score=0 → -2.6%，score≥1 → 转正）。这一侧背离有真实过滤价值。

但以上都建立在**约 30~70 个市场事件**上，样本太少，任何单点结论都可能是"挑出来的"。
本脚本把结论摊开成分布：十万次**随机取样**（随机指数子集 + 随机时间窗）+ 随机参数
（指标参数族 × 回看窗 L × 排除缝 G × 得分门槛 × 半年线区间），看这条规则在
各式各样的样本里有多少比例真的成立。

网格维度：
  指标参数族(4) × 回看窗L(3) × 排除缝G(3) × 得分门槛(4) × 半年线区间(5) × 侧(2)
随机样本：指数子集（26 条里随机取 8~26 条）× 时间窗（随机起点，跨度取 10%~100%）

评估口径（无前视）：
  · 偏离度用半年线 sma120；特征只用 ≤T 收盘；未来收益用 build_panel 预计算的 fwd{5..60}
  · **每指数时段只取首日**（间隔 ≤20 交易日合并），再按日期聚类成"独立行情轮"
  · 判定看轮级（独立行情级）中位超额
  · 费后：场外赎回费按持有天数（<7天1.5%、<30天0.5%、之后0）

产物：data/cache/sweeps/sweep100k_divergence.jsonl + reports/三重背离十万次迭代_*.md
纯标准库。用法:
  python research/sweep100k_divergence.py --run      # 跑十万次
  python research/sweep100k_divergence.py --finish   # 聚合报告
"""
import argparse
import bisect
import json
import os
import random
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DATA_DIR)
for _p in (DATA_DIR, REPO_ROOT, os.path.join(REPO_ROOT, "src", "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from src.common.paths import SWEEP_CACHE, REPORT_ROOT  # noqa: E402
from src.common.fees import fee  # noqa: E402
from divergence_line import (IND_FAMILIES, FWD, detect, episodes, date_clusters,  # noqa: E402
                             date_gap, median, panels_with_baseline)

FAMS = ("std", "narrow", "slow", "fast")
LS = (60, 120, 250)
GS = (5, 10, 20)
SCORES = (0, 1, 2, 3)
# 半年线区间：None=不限；(lo,hi)=偏离度区间（左开右闭，None 表示无界）
ZONES = (
    ("any", None),
    ("深跌区(≤-15%)", (-999.0, -15.0)),
    ("偏低区(-15~0%)", (-15.0, 0.0)),
    ("偏高区(0~+15%)", (0.0, 15.0)),
    ("高位区(>+15%)", (15.0, 999.0)),
)
SIDES = ("top", "bottom")
EP_GAP = 20
N_TRIALS = 100000
OUT = os.path.join(SWEEP_CACHE, "sweep100k_divergence.jsonl")
REPORT_DIR = REPORT_ROOT

# 缓存：面板（按指数分解）与入场点列表
_PANELS = {}
_ENTRIES = {}


def panels(fam):
    if fam not in _PANELS:
        _PANELS[fam] = panels_with_baseline(fam)
    return _PANELS[fam]


def entries(side, fam, L, G, score, zone):
    """某组合下全指数合并、按日期升序的入场点 [(date, nm, i, exc_tuple)]。带缓存。
    zone 过滤在此一次性完成（区间只看 ≤T 的偏离度，无前视）。"""
    key = (side, fam, L, G, score, zone)
    if key in _ENTRIES:
        return _ENTRIES[key]
    zr = dict(ZONES)[zone]
    out = []
    for nm, (rows, base) in panels(fam).items():
        for sg in detect(rows, side, L, G, score):
            i = sg["i"]
            b = base[i]
            if any(b[h] is None for h in FWD):
                continue
            dma = sg.get("dma")
            if zr is not None:
                if dma is None or not (zr[0] < dma <= zr[1]):
                    continue
            out.append((sg["date"], nm, i,
                        tuple(sg["fwd%d" % h] - b[h] for h in FWD)))
    out.sort(key=lambda x: x[0])
    _ENTRIES[key] = out
    return out


def eval_sample(ents, universe, lo, hi, hold_idx, ep_gap=EP_GAP):
    """在样本（指数子集 + 时间窗）上评估。返回 (时段数, 轮数, 轮级中位, 轮级为正率)。
    ents 已按日期升序 → 窗口切片用 bisect，切片内仍保序，可一趟做"每指数时段去重 + 跨指数聚类"。

    每指数时段去重：同一指数 i 间隔 ≤ep_gap 只取首日。"""
    l, r = bisect.bisect_left(ents, (lo,)), bisect.bisect_right(ents, (hi, "\uffff"))
    # 窗口内先按指数分流（同指数按 i 升序）
    by_idx = {}
    for d, nm, i, exc in ents[l:r]:
        if nm in universe:
            by_idx.setdefault(nm, []).append((i, d, exc[hold_idx]))
    # 去重：每指数时段取首日 → 收成"时段起点"，再跨指数按日期聚类
    starts = []
    for nm, arr in by_idx.items():
        prev = None
        for i, d, v in arr:
            if prev is not None and i - prev <= ep_gap:
                prev = i
                continue
            prev = i
            starts.append((d, nm, v))
    if not starts:
        return 0, 0, 0.0, 0.0
    starts.sort(key=lambda x: x[0])
    ev, cur, last = [], [], None
    for d, nm, v in starts:
        if last is not None and date_gap(last, d) > ep_gap:
            ev.append(median([x[2] for x in cur]))
            cur = []
        cur.append((d, nm, v))
        last = d
    if cur:
        ev.append(median([x[2] for x in cur]))
    return (len(starts), len(ev), median(ev), sum(1 for x in ev if x > 0) / len(ev))


def run():
    ser_names = sorted(panels("std"))
    alldates = sorted({r[0] for nm, (rows, _b) in panels("std").items() for r in rows})
    lo0, hi0 = alldates[0], alldates[-1]
    print("指数 %d 条，日期跨度 %s ~ %s" % (len(ser_names), lo0, hi0))

    os.makedirs(SWEEP_CACHE, exist_ok=True)
    rng = random.Random(20260917)
    t0, done, skipped = time.time(), 0, 0
    with open(OUT, "w", encoding="utf-8") as f:
        for t in range(N_TRIALS):
            side = rng.choice(SIDES)
            fam = rng.choice(FAMS)
            L = rng.choice(LS)
            G = rng.choice(GS)
            score = rng.choice(SCORES)
            zone = rng.choice(ZONES)[0]
            hold_idx = rng.randrange(len(FWD))
            ents = entries(side, fam, L, G, score, zone)
            if not ents:
                skipped += 1
                continue
            k = rng.randint(8, len(ser_names))
            universe = set(rng.sample(ser_names, k))
            span = len(alldates)
            w = max(120, int(span * rng.uniform(0.10, 1.0)))
            s = rng.randrange(0, max(1, span - w + 1))
            lo, hi = alldates[s], alldates[min(span - 1, s + w - 1)]
            nep, nev, emed, ewr = eval_sample(ents, universe, lo, hi, hold_idx)
            if nev == 0:
                skipped += 1
                continue
            f.write(json.dumps({
                "side": side, "fam": fam, "L": L, "G": G, "score": score,
                "zone": zone, "hold": FWD[hold_idx], "k": k,
                "lo": lo, "hi": hi, "w": w, "nep": nep, "nev": nev,
                "emed": round(emed, 2), "ewr": round(ewr, 3)}, ensure_ascii=False) + "\n")
            done += 1
            if done % 10000 == 0:
                print("  %d/%d  %.0fs" % (done, N_TRIALS, time.time() - t0))
    print("有效 %d 组（空样本 %d）-> %s  %.0fs" % (done, skipped, OUT, time.time() - t0))


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(p / 100.0 * (len(s) - 1)))))
    return s[i]


def finish():
    recs = []
    with open(OUT, encoding="utf-8") as f:
        for line in f:
            recs.append(json.loads(line))
    lines = []

    def out(s=""):
        print(s)
        lines.append(s)

    def block(label, rows):
        if len(rows) < 30:
            out("  %-22s 样本 %d 不足" % (label, len(rows)))
            return
        em = [r["emed"] for r in rows]
        pos = sum(1 for v in em if v > 0) / len(em)
        out("  %-22s %6d %+8.2f %+8.2f %8.0f%% %+9.2f %+9.2f"
            % (label, len(rows), median(em), sum(em) / len(em), pos * 100,
               pct(em, 5), pct(em, 95)))

    def head():
        out("  %-22s %6s %8s %8s %9s %9s %9s" % (
            "分组", "样本", "轮级中位", "均值", "为正比例", "5%分位", "95%分位"))

    out("=== 全部 %d 次随机取样的整体分布（轮级 = 独立行情级，单位 %% 超额）===" % len(recs))
    em = [r["emed"] for r in recs]
    out("  轮级中位 %+.2f  均值 %+.2f  为正比例 %.0f%%  5%%分位 %+.2f  95%%分位 %+.2f"
        % (median(em), sum(em) / len(em), sum(1 for v in em if v > 0) / len(em) * 100,
           pct(em, 5), pct(em, 95)))

    # 顶背离：要有用 → 超额为负；底背离 → 为正。分侧看，避免正负相抵。
    for side, slab in (("bottom", "底背离（应为正）"), ("top", "顶背离（应为负）")):
        sub = [r for r in recs if r["side"] == side]
        out("\n=== %s：按得分门槛（分数越高=越'三重共振'）===" % slab)
        head()
        for sc in SCORES:
            block("score=%d" % sc, [r for r in sub if r["score"] == sc])
        out("\n=== %s：按半年线区间 × 得分门槛（轮级中位）===" % slab)
        head()
        for zn, _z in ZONES:
            for sc in SCORES:
                block("%s score=%d" % (zn, sc),
                      [r for r in sub if r["zone"] == zn and r["score"] == sc])
        out("\n=== %s：按持有天数 ===" % slab)
        head()
        for h in FWD:
            block("持有 %d 日" % h, [r for r in sub if r["hold"] == h])
        out("\n=== %s：按指标参数族（换参数还成不成立）===" % slab)
        head()
        for fm in FAMS:
            block(fm, [r for r in sub if r["fam"] == fm])
        out("\n=== %s：按回看窗 L ===" % slab)
        head()
        for L in LS:
            block("L=%d" % L, [r for r in sub if r["L"] == L])
        out("\n=== %s：按排除缝 G ===" % slab)
        head()
        for G in GS:
            block("G=%d" % G, [r for r in sub if r["G"] == G])
        out("\n=== %s：按样本早晚（时间窗终点年）===" % slab)
        head()
        for lab, y0, y1 in (("终点<2010", "2000", "2010"), ("2010~2015", "2010", "2015"),
                            ("2015~2020", "2015", "2020"), ("终点≥2020", "2020", "2027")):
            block(lab, [r for r in sub if y0 <= r["hi"][:4] < y1])
        out("\n=== %s：按样本长度 ===" % slab)
        head()
        for lab, a_, b_ in (("<3 年", 0, 750), ("3~6 年", 750, 1500),
                            ("6~13 年", 1500, 3250), ("≥13 年", 3250, 99999)):
            block(lab, [r for r in sub if a_ <= r["w"] < b_])

    # 关键判定：底背离"深跌区带背离 vs 不带背离"，和顶背离"高位区带 vs 不带"
    out("\n=== 关键判定：同一区间下，'有背离'是否真的比'无背离'好 ===")
    out("（底背离看深跌区：有背离应显著优于 score=0；顶背离看高位区：若两者差不多，则背离多余）")
    for side, zone, slab in (("bottom", "深跌区(≤-15%)", "底背离·深跌区"),
                             ("top", "高位区(>+15%)", "顶背离·高位区")):
        out("\n%s：" % slab)
        head()
        for sc in SCORES:
            block("score=%d" % sc, [r for r in recs if r["side"] == side
                                    and r["zone"] == zone and r["score"] == sc])

    path = os.path.join(REPORT_DIR, "三重背离十万次迭代_20260917.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 三重背离十万次随机取样迭代（2026-09-17）\n\n")
        f.write("网格：参数族(4)×回看窗(3)×排除缝(3)×得分门槛(4)×半年线区间(5)×侧(2)；"
                "随机指数子集(8~26条)×随机时间窗(跨度10%~100%)。\n")
        f.write("单位 %%，'轮级'=独立行情级（同日多指数触发算一轮）。"
                "顶背离为负才有用，底背离为正才有用。\n\n```\n")
        f.write("\n".join(lines))
        f.write("\n```\n")
    print("\n报告 -> %s" % path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--finish", action="store_true")
    a = ap.parse_args()
    if a.run or not a.finish:
        run()
        finish()


if __name__ == "__main__":
    main()
