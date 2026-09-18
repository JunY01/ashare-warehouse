# -*- coding: utf-8 -*-
"""超跌反弹十万次迭代 —— 回答"这条规则到底是真规律，还是我挑的那几段行情"

给未来亚亚：前情是 2026-09-16 的 `rebound_line.py` 验证得出的两条结论
（板块层不成立、指数层"20日跌≥20%"成立，见 reports/超跌反弹验证_20260916.md）。
但那条结论只建立在**约 28 轮独立行情**上，样本太少，任何单点结论都可能是
"挑出来的"。本脚本把结论摊开成分布：用 10 万次**随机取样**（随机指数子集 +
随机时间窗）叠随机参数，看这条规则在各式各样的样本里有多少比例真的赚钱。

网格维度（640 组参数 × 约 156 个随机样本 ≈ 10 万次）：
  触发深度(16) × 入场确认(5) × 持有天数(8) = 640
  深度门槛两族：A族绝对跌幅（-8%~-35%，跨指数同一把尺子）；
                B族该指数**自身历史分位**（3%~20%，自动适配波动率——
                科创50 跌 10% 与上证50 跌 10% 不是一回事）
  随机样本：指数子集（26 条里随机取 8~26 条）× 时间窗（随机起点，跨度取 10%~100%）

评估口径（无前视）：
  · 特征只用 ≤T 收盘价；未来收益用 build_features 预计算的 fwd{10..120}
  · **每轮超跌只算首个触发日**（同一指数间隔 ≤20 交易日合并），避免持续状态刷样本
  · **独立性双算**：笔数 与 按日期聚类后的"独立行情轮数"，判定看后者
  · 费后：场外赎回费按持有天数（<7天1.5%、<30天0.5%、之后0）

产物：data/cache/sweeps/sweep100k_rebound.jsonl + reports/超跌反弹十万次迭代_*.md
纯标准库。用法:
  python research/sweep100k_rebound.py --run      # 跑 10 万次（约数分钟）
  python research/sweep100k_rebound.py --finish   # 聚合报告
"""
import argparse
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
from src.common.paths import DATA_ROOT, REPORT_ROOT, SWEEP_CACHE  # noqa: E402
from src.common.fees import fee  # noqa: E402
from rebound_line import build_features, load_index_series, median  # noqa: E402

HOLD_LEVELS = (10, 15, 20, 30, 40, 60, 90, 120)
# 深度门槛两族：
#   abs = 绝对跌幅（"20日跌够 X%"），跨指数同一个尺子
#   pct = 该指数**自身历史**分位（"20日涨幅落到自己历史最低 X%"），自动适配波动率
#   理由：科创50 跌 10% 与上证50 跌 10% 不是一回事，绝对尺子对高波动指数过松、对低波动过严。
DEPTH_ABS = (-8.0, -10.0, -12.0, -15.0, -18.0, -20.0, -22.0, -25.0, -28.0, -30.0, -35.0)
DEPTH_PCT = (3.0, 5.0, 10.0, 15.0, 20.0)
DEPTH_SPECS = [("abs", d) for d in DEPTH_ABS] + [("pct", p) for p in DEPTH_PCT]
PCT_MIN_HIST = 250          # 分位尺子要求至少 250 根K线，否则"自身历史分位"没有意义
TRIGGERS = (("none", 0), ("up", 1), ("up2", 2), ("over_ma5", 4), ("imp", 8))
EP_GAP = 20                 # 同一指数的超跌间隔 ≤20 交易日算同一轮
N_TRIALS = 100000
OUT = os.path.join(SWEEP_CACHE, "sweep100k_rebound.jsonl")
BASE_OUT = os.path.join(SWEEP_CACHE, "sweep100k_rebound_baseline.json")
REPORT_DIR = REPORT_ROOT

# flags 位：0=当日收阳 1=连两日收阳 2=重回5日线上方 3=回撤收敛
def _flags(r):
    f = 0
    if r["up"] > 0:
        f |= 1
    if r["up2"] > 0:
        f |= 2
    if r["over_ma5"] > 0:
        f |= 4
    if r["dd20_imp"] > 0:
        f |= 8
    return f


def build_entries():
    """预计算"每轮超跌首个触发日"的入场点。

    返回 {(kind, value): {trigger: [(date, name, fwd_tuple)]}}，每个列表按日期升序。
    fwd_tuple 与 HOLD_LEVELS 对齐。热样本表按最浅的两族门槛剪枝
    （ret20 ≤ -8% 或 自身历史分位 ≤ 20%），故每指数的热样本只有几百到上千条，
    10 万次迭代才跑得动。
    """
    ser = load_index_series()
    out = {spec: {t: [] for t, _ in TRIGGERS} for spec in DEPTH_SPECS}
    n_hot = 0
    for nm, pts in ser.items():
        f = build_features(pts, fwd_h=HOLD_LEVELS)
        if not f:
            continue
        rows = sorted(f.items())
        hot = []
        for i, (d, r) in enumerate(rows):
            hp = r["hpct20"]
            deep_abs = r["ret20"] <= DEPTH_ABS[0]
            deep_pct = hp is not None and hp <= DEPTH_PCT[-1] and r["nhist"] >= PCT_MIN_HIST
            if not (deep_abs or deep_pct):
                continue
            hot.append((i, d, r["ret20"], hp, _flags(r),
                        tuple(r["fwd%d" % h] for h in HOLD_LEVELS)))
        n_hot += len(hot)
        for tname, tmask in TRIGGERS:
            sel = [x for x in hot if (x[4] & tmask) == tmask]
            for kind, val in DEPTH_SPECS:
                last, ents = None, []
                for i, d, r20, hp, _fl, fw in sel:
                    ok = r20 <= val if kind == "abs" else (hp is not None and hp <= val)
                    if not ok:
                        continue
                    if last is not None and i - last <= EP_GAP:
                        last = i
                        continue
                    last = i
                    ents.append((d, nm, fw))
                out[(kind, val)][tname].extend(ents)
    for spec in DEPTH_SPECS:
        for tname, _ in TRIGGERS:
            out[spec][tname].sort(key=lambda x: x[0])
    tot = sum(len(out[s][t]) for s in DEPTH_SPECS for t, _ in TRIGGERS)
    print("热样本 %d 条；已预计算 %d 组入场点（共 %d 个入场点）" % (
        n_hot, len(DEPTH_SPECS) * len(TRIGGERS), tot))
    return out


def eval_sample(ents, universe, lo, hi, hold_idx):
    """在给定样本（指数子集 + 时间窗）上评估。返回 (笔数, 笔级中位, 笔级为正率,
    轮数, 轮级中位, 轮级为正率)。ents 已按日期升序 → 过滤后仍保序，可一趟聚类。"""
    nets, cl_dates = [], []
    fd = fee(HOLD_LEVELS[hold_idx])
    for d, nm, fw in ents:
        if nm not in universe or d < lo or d > hi:
            continue
        nets.append(fw[hold_idx] - fd)
        cl_dates.append(d)
    if not nets:
        return 0, 0.0, 0.0, 0, 0.0, 0.0
    # 按日期聚类成独立行情轮（同一天多个指数算一轮）
    ev, cur = [], []
    for v, d in zip(nets, cl_dates):
        if cur and date_gap(cur[-1][1], d) > EP_GAP:
            ev.append(median([x[0] for x in cur]))
            cur = []
        cur.append((v, d))
    if cur:
        ev.append(median([x[0] for x in cur]))
    return (len(nets), median(nets), sum(1 for v in nets if v > 0) / len(nets),
            len(ev), median(ev), sum(1 for v in ev if v > 0) / len(ev))


def date_gap(d1, d2):
    import datetime
    a = datetime.date(*map(int, d1.split("-")))
    b = datetime.date(*map(int, d2.split("-")))
    return (b - a).days * 5 // 7


def run():
    ents = build_entries()
    ser = load_index_series()
    names = sorted(ser)
    alldates = sorted({d for pts in ser.values() for d, _ in pts})
    lo0, hi0 = alldates[0], alldates[-1]
    print("指数 %d 条，日期跨度 %s ~ %s" % (len(names), lo0, hi0))

    os.makedirs(SWEEP_CACHE, exist_ok=True)
    rng = random.Random(20260916)
    t0, done, skipped = time.time(), 0, 0
    with open(OUT, "w", encoding="utf-8") as f:
        for _ in range(N_TRIALS):
            kind, depth = rng.choice(DEPTH_SPECS)
            tname, _m = rng.choice(TRIGGERS)
            hold_idx = rng.randrange(len(HOLD_LEVELS))
            k = rng.randint(8, len(names))
            universe = set(rng.sample(names, k))
            # 随机时间窗：跨度随机取 10%~100%（下探到 2~3 年，才能测"只给近几年数据"的样本）
            span = len(alldates)
            w = max(120, int(span * rng.uniform(0.10, 1.0)))
            s = rng.randrange(0, span - w + 1)
            lo, hi = alldates[s], alldates[s + w - 1]
            n, med, wr, nev, emed, ewr = eval_sample(
                ents[(kind, depth)][tname], universe, lo, hi, hold_idx)
            if n == 0:
                skipped += 1
                continue
            f.write(json.dumps({
                "kind": kind, "depth": depth, "trig": tname,
                "hold": HOLD_LEVELS[hold_idx],
                "k": k, "lo": lo, "hi": hi, "w": w, "n": n, "med": round(med, 2),
                "wr": round(wr, 3), "nev": nev, "emed": round(emed, 2),
                "ewr": round(ewr, 3)}, ensure_ascii=False) + "\n")
            done += 1
            if done % 10000 == 0:
                print("  %d/%d  %.0fs" % (done, N_TRIALS, time.time() - t0))
    print("有效 %d 组（空样本 %d）-> %s  %.0fs" % (done, skipped, OUT, time.time() - t0))

    # 基线配置的逐轮明细：给 --finish 做"剔掉最好的几轮"留一检验
    base = ents[("abs", -20.0)]["none"]
    bl = []
    for d, nm, fw in base:
        bl.append({"d": d, "nm": nm,
                   "r": [round(fw[i] - fee(HOLD_LEVELS[i]), 2) for i in range(len(HOLD_LEVELS))]})
    # 触发频率：每个门槛下"全指数池、无确认"平均每年有几次机会（回答"门槛放宽到哪还能用"）
    import datetime
    yrs = (datetime.date(*map(int, hi0.split("-")))
           - datetime.date(*map(int, lo0.split("-")))).days / 365.25
    freq = {"%s|%.1f" % (k, v): round(len(ents[(k, v)]["none"]) / yrs, 2)
            for k, v in DEPTH_SPECS}
    with open(BASE_OUT, "w", encoding="utf-8") as f:
        json.dump({"hold": list(HOLD_LEVELS), "rows": bl, "years": round(yrs, 1),
                   "freq": freq}, f, ensure_ascii=False)
    print("基线(20日跌≥20%%·无确认)入场点 %d 条 -> %s" % (len(bl), BASE_OUT))
    print("触发频率（次/年，全指数池·无确认）:")
    for k, v in DEPTH_SPECS:
        print("   %s%-7s %5.2f 次/年" % (
            "跌幅≥" if k == "abs" else "自身分位≤",
            "%.0f%%" % (-v if k == "abs" else v), freq["%s|%.1f" % (k, v)]))


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
    print("读入 %d 组" % len(recs))
    freq, base = {}, None
    if os.path.exists(BASE_OUT):
        base = json.load(open(BASE_OUT, encoding="utf-8"))
        freq = base.get("freq", {})

    lines = []
    def out(s=""):
        print(s)
        lines.append(s)

    def block(label, rows):
        if len(rows) < 30:
            out("  %-16s 样本 %d 不足" % (label, len(rows)))
            return
        em = [r["emed"] for r in rows]
        pos = sum(1 for v in em if v > 0) / len(em)
        out("  %-16s %6d %+8.2f %+8.2f %9.0f%% %+9.2f %+9.2f"
            % (label, len(rows), median(em), sum(em) / len(em), pos * 100,
               pct(em, 5), pct(em, 95)))

    def head():
        out("  %-16s %6s %8s %8s %9s %9s %9s" % (
            "门槛/分组", "样本", "轮级中位", "均值", "为正比例", "5%分位", "95%分位"))

    out("=== 全部 %d 次随机取样的整体分布（轮级 = 独立行情级，单位 %%）===" % len(recs))
    em = [r["emed"] for r in recs]
    out("  轮级中位 %+.2f  均值 %+.2f  **为正比例 %.0f%%**  5%%分位 %+.2f  95%%分位 %+.2f"
        % (median(em), sum(em) / len(em), sum(1 for v in em if v > 0) / len(em) * 100,
           pct(em, 5), pct(em, 95)))
    out("  每轮独立行情的轮数中位 %d（四分位 %d~%d）" % (
        median([r["nev"] for r in recs]), pct([r["nev"] for r in recs], 25),
        pct([r["nev"] for r in recs], 75)))

    out("\n=== A族·绝对跌幅门槛（跨指数同一个尺子；'次/年'=全指数池无确认的触发频率）===")
    head()
    for d in DEPTH_ABS:
        rows = [r for r in recs if r["kind"] == "abs" and r["depth"] == d]
        fq = freq.get("abs|%.1f" % d)
        lab = "跌幅≥%.0f%%" % -d + ("" if fq is None else "  %.2f/年" % fq)
        block(lab, rows)

    out("\n=== B族·该指数自身历史分位门槛（自动适配波动率）===")
    head()
    for p in DEPTH_PCT:
        rows = [r for r in recs if r["kind"] == "pct" and r["depth"] == p]
        fq = freq.get("pct|%.1f" % p)
        lab = "自身最低%.0f%%" % p + ("" if fq is None else "  %.2f/年" % fq)
        block(lab, rows)

    out("\n=== 深度 × 持有20日 交叉（同口径下看'门槛放宽到哪还能用'）===")
    head()
    for d in DEPTH_ABS:
        block("跌幅≥%.0f%%" % -d,
              [r for r in recs if r["kind"] == "abs" and r["depth"] == d and r["hold"] == 20])
    for p in DEPTH_PCT:
        block("自身最低%.0f%%" % p,
              [r for r in recs if r["kind"] == "pct" and r["depth"] == p and r["hold"] == 20])

    out("\n=== 深度 × 持有30日 交叉 ===")
    head()
    for d in DEPTH_ABS:
        block("跌幅≥%.0f%%" % -d,
              [r for r in recs if r["kind"] == "abs" and r["depth"] == d and r["hold"] == 30])
    for p in DEPTH_PCT:
        block("自身最低%.0f%%" % p,
              [r for r in recs if r["kind"] == "pct" and r["depth"] == p and r["hold"] == 30])

    out("\n=== 按持有天数（全门槛混合）===")
    head()
    for h in HOLD_LEVELS:
        block("%d 日" % h, [r for r in recs if r["hold"] == h])

    out("\n=== 按入场确认 ===")
    head()
    for t, _m in TRIGGERS:
        block(t, [r for r in recs if r["trig"] == t])

    out("\n=== 按样本大小（随机取到多少条指数）===")
    head()
    for lo_k, hi_k, lab in ((8, 12, "8~12 条"), (13, 19, "13~19 条"), (20, 26, "20~26 条")):
        block(lab, [r for r in recs if lo_k <= r["k"] <= hi_k])

    out("\n=== 按样本早晚（时间窗终点年；'只给你近年数据'有没有用）===")
    head()
    for lab, y0, y1 in (("终点<2010", "2000", "2010"), ("2010~2015", "2010", "2015"),
                        ("2015~2020", "2015", "2020"), ("终点≥2020", "2020", "2027")):
        block(lab, [r for r in recs if y0 <= r["hi"][:4] < y1])

    out("\n=== 按样本长度（时间窗只有几年；短样本最能暴露是否靠某段行情）===")
    head()
    for lab, a_, b_ in (("<3 年", 0, 750), ("3~6 年", 750, 1500),
                        ("6~13 年", 1500, 3250), ("≥13 年", 3250, 99999)):
        block(lab, [r for r in recs if a_ <= r["w"] < b_])

    out("\n=== 放宽门槛后的可用子集：跌幅≥18%% + 持有20~30日，共 %d 组 ==="
        % len([r for r in recs if r["kind"] == "abs" and r["depth"] <= -18.0
               and 20 <= r["hold"] <= 30]))
    head()
    bl = [r for r in recs if r["kind"] == "abs" and r["depth"] <= -18.0 and 20 <= r["hold"] <= 30]
    block("全部", bl)
    for t, _m in TRIGGERS:
        block(t, [r for r in bl if r["trig"] == t])
    for lab, y0, y1 in (("终点<2015", "2000", "2015"), ("终点≥2020", "2020", "2027")):
        block(lab, [r for r in bl if y0 <= r["hi"][:4] < y1])

    # 留一检验：剔掉赚最多的若干轮之后还成立吗
    if base:
        rows, h = base["rows"], base["hold"]
        out("\n=== 留一检验：基线配置（20日跌≥20%·无确认）剔掉最好的 N 轮 ===")
        out("  入场点 %d 个" % len(rows))
        for hi in (20, 60):
            i = h.index(hi)
            vals = sorted(x["r"][i] for x in rows)
            out("  持有 %d 日：全样本中位 %+.2f%%（%d 个入场点）" % (
                hi, median(vals), len(vals)))
            for drop in (1, 2, 3, 5):
                v = vals[:-drop] if drop < len(vals) else []
                out("    剔掉前 %d 名后中位 %+.2f%%  均值 %+.2f%%" % (
                    drop, median(v), sum(v) / len(v)))
            out("    前 5 名: %s" % "  ".join("+%.1f" % x for x in vals[-5:]))

    path = os.path.join(REPORT_DIR, "超跌反弹十万次迭代_20260916.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 超跌反弹十万次随机取样迭代（2026-09-16）\n\n")
        f.write("门槛两族：A族=绝对跌幅（-8%~-35%），B族=该指数自身历史分位（3%~20%）。\n")
        f.write("单位 %%，'轮级'=独立行情级（同日多指数触发算一轮）。\n\n```\n")
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
