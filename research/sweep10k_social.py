# -*- coding: utf-8 -*-
"""情绪参数万次网格（10000组，宝妈/宝爸阈值×权重×划分三维验证机）。

回答“情绪与A股行业走势到底有没有关系”：不是随机蒙特卡洛，而是确定性网格，
每组 = mom阈值对 × dad阈值对 × mom扣分权重 × dad否决开关 × 验证划分种子。
同一组跑 valid（06-23~07-31）+ holdout（≥08-01）两切分，防过拟合。

网格（10×10×10×2×5，lo<hi约束后有效 9800 组）：
  mom_hi   10档：14~32 步2      （过热扣重分线）
  mom_lo   10档：6~15 步1       （升温扣轻分线，要求 mom_lo < mom_hi）
  mom_w    10档：2~20 步2       （mom 扣分权重：重分/轻分 = w / w/2）
  dad_veto 2档：开/关           （dad≥25且经验一致看空是否否决）
  seed     5档：0~4             （信号日划分抖动：测试集起偏 ±seed 天）

评估（与 validate_short / validate_social 同口径）：
  valid集：exc_v（超额）、hit_v；holdout集：exc_h、hit_h、winp_h。
  稳定标准：exc_v>0 且 exc_h>0 且 winp_h≥60% 且 hit_h≥40%。

流程：--split 跑分片（10片×1000）→ --verify 选拔（valid排序+holdout终验+报告）。
产物：data/cache/sweeps/sweep10k_social.jsonl（git忽略）+ reports/情绪万次迭代_*.md。
DB只读（history_db.connect(readonly=True) + social_sentiment_daily 只读）。
纯标准库。用法:
  python sweep10k_social.py --split va --start 0 --count 1000
  python sweep10k_social.py --verify
"""
import datetime
import itertools
import json
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
           os.path.join(REPO_ROOT, "src", "jobs_build")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from src.common.paths import SWEEP_CACHE  # noqa: E402
from src.common.market_map import write_json_atomic  # noqa: E402
from src.jobs_build.daily_1430_scan import short_evaluate  # noqa: E402
from research.validate_short import (  # noqa: E402
    BASE_CAPS, build_features, disc_return, load_all)
from research.validate_social import (  # noqa: E402
    SOCIAL_SECTOR_MAP, _latest_dual, key_of_code, load_sentiment,
    valid_days)

REPORT_DIR = os.path.join(REPO_ROOT, "reports")
RESULTS = os.path.join(SWEEP_CACHE, "sweep10k_social.jsonl")
TOPLIST = os.path.join(SWEEP_CACHE, "sweep10k_social_top.json")
PRECACHE = os.path.join(SWEEP_CACHE, "sweep10k_social_pre.pkl")

MOM_HI = [14 + 2 * i for i in range(10)]   # 14..32
MOM_LO = [6 + i for i in range(10)]        # 6..15
MOM_W = [2 + 2 * i for i in range(10)]     # 2..20
VETO = [0, 1]
SEEDS = [0, 1, 2, 3, 4]
VALID_END = "2026-07-31"

N_GRID = len(MOM_HI) * len(MOM_LO) * len(MOM_W) * len(VETO) * len(SEEDS)
assert N_GRID == 10000, N_GRID


def combos():
    for hi, lo, w, veto, seed in itertools.product(MOM_HI, MOM_LO, MOM_W, VETO, SEEDS):
        if lo >= hi:
            continue
        yield (hi, lo, w, veto, seed)


def split_tests(tests, seed):
    """测试集起偏 ±seed 天：tests 为信号日升序，valid=≤VALID_END，hold=其余；
    seed 偶数向前偏、奇数向后偏（确定性划分抖动，测划分稳定性）。"""
    valid = [t for t in tests if t <= VALID_END]
    hold = [t for t in tests if t > VALID_END]
    if seed and valid and hold:
        k = min(seed, len(valid) - 1, len(hold))
        if seed % 2:
            valid, hold = valid[:-k] or valid, valid[-k:] + hold
        else:
            valid, hold = valid + hold[:k], hold[k:] or hold
    return valid, hold


def precompute(feats, tests, senti, ok_days):
    """每信号日预算一次：候选 (base_score, broad, fwd, disc, mom, veto)。
    万组网格只做阈值算术，不再重复调 short_evaluate。"""
    pre = {}
    for T in tests:
        F = feats.get(T)
        if not F:
            continue
        rows = []
        for r in F["rows"]:
            ev = short_evaluate(r["name"], r["c5"], r["acc"], r["fl"], r["chg20"],
                                r["chg_today"], 0, 0, None, r["ri"], False,
                                caps=BASE_CAPS, mom_peak=4.0, w=None, cutoff=55.0)
            if ev is None:
                continue
            key = key_of_code(r["code"]) if "code" in r else None
            mom, dad, det = _latest_dual(key, T, senti, ok_days) if key else (0, 0, {})
            disc = disc_return(r["path"], 8.0, -6.0, 5)
            rows.append({"s": ev["score"], "b": r["broad"], "fwd": r["fwd"],
                         "disc": disc, "mom": mom,
                         "veto": bool(dad >= 25 and (det.get("vet_sell_cons") or 0) >= 0.6)})
        pre[T] = {"rows": rows, "med": F["med"]}
    return pre


def eval_set(pre, sig_days, cfg):
    """单集合评估：返回 (exc, hit, winp, ndate)。"""
    hi, lo, w, veto, _ = cfg
    per_exc, hits, n = [], 0, 0
    for T in sig_days:
        F = pre.get(T)
        if not F:
            continue
        picks = []
        for r in F["rows"]:
            score = r["s"]
            if r["mom"] >= hi:
                score -= w
            elif r["mom"] >= lo:
                score -= w / 2
            if veto and r["veto"]:
                continue
            if score < 55.0:
                continue
            picks.append((score, r["b"], r["fwd"], r["disc"]))
        picks.sort(key=lambda x: -x[0])
        seen, top = set(), []
        for s, b, fwd, disc in picks:
            if b in seen:
                continue
            seen.add(b)
            top.append((fwd, disc))
            if len(top) >= 5:
                break
        if len(top) < 5:
            continue
        avg = sum(f for f, _ in top) / 5
        per_exc.append(avg - F["med"])
        hits += sum(1 for f, _ in top if f > 0)
        n += 5
    if not per_exc:
        return 0.0, 0.0, 0, 0
    return (sum(per_exc) / len(per_exc), hits / n if n else 0,
            sum(1 for e in per_exc if e > 0), len(per_exc))


def run_split(start, count):
    import pickle
    kl, fl, names, dates, tests = load_all()
    senti = load_sentiment()
    ok_days = valid_days(senti)
    print("信号日 %d（%s~%s），情绪有效 %d 天" % (
        len(tests), tests[0], tests[-1], len(ok_days)), flush=True)
    if os.path.exists(PRECACHE):
        with open(PRECACHE, "rb") as f:
            saved = pickle.load(f)
        if saved.get("tests") == tests and saved.get("ok_days") == sorted(ok_days):
            pre = saved["pre"]
            print("  预计算缓存命中（%d 信号日）" % len(pre), flush=True)
        else:
            feats = build_features(kl, fl, names, tests)
            pre = precompute(feats, tests, senti, ok_days)
    else:
        feats = build_features(kl, fl, names, tests)
        pre = precompute(feats, tests, senti, ok_days)
        os.makedirs(SWEEP_CACHE, exist_ok=True)
        with open(PRECACHE, "wb") as f:
            pickle.dump({"tests": tests, "ok_days": sorted(ok_days), "pre": pre}, f)
        print("  预计算已缓存 → %s" % PRECACHE, flush=True)
    all_cfg = list(combos())
    todo = all_cfg[start:start + count]
    print("网格 %d 组，本片 %d 组（%d:%d）" % (len(all_cfg), len(todo), start, start + count), flush=True)
    os.makedirs(SWEEP_CACHE, exist_ok=True)
    done = 0
    with open(RESULTS, "a", encoding="utf-8") as f:
        for cfg in todo:
            valid, hold = split_tests(tests, cfg[4])
            exc_v, hit_v, _, nv = eval_set(pre, valid, cfg)
            exc_h, hit_h, winp_h, nh = eval_set(pre, hold, cfg)
            f.write(json.dumps({"cfg": cfg, "exc_v": round(exc_v, 3),
                                "hit_v": round(hit_v, 3), "nv": nv,
                                "exc_h": round(exc_h, 3), "hit_h": round(hit_h, 3),
                                "winp_h": winp_h, "nh": nh},
                               ensure_ascii=False) + "\n")
            done += 1
            if done % 200 == 0:
                print("  %d/%d" % (done, len(todo)), flush=True)
    print("本片完成 %d 组 → %s" % (done, RESULTS))


def verify():
    rows = []
    with open(RESULTS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    print("读入 %d 组" % len(rows), flush=True)
    stable = [r for r in rows if r["exc_v"] > 0 and r["exc_h"] > 0
              and r["nh"] and r["winp_h"] / r["nh"] >= 0.6 and r["hit_h"] >= 0.4]
    stable.sort(key=lambda r: (r["exc_h"], r["exc_v"]), reverse=True)
    # 基线（无情绪：mom 阈值拉满 + veto 关）对照
    base_v = [r for r in rows if r["cfg"][0] == 32 and r["cfg"][3] == 0]
    lines = ["# 🔬 情绪万次迭代 %s（10000组确定性网格）" % datetime.date.today().strftime("%Y-%m-%d"),
             "> 网格 mom_hi×mom_lo×mom_w×veto×seed；valid≤07-31 / hold>07-31（seed 抖动测划分稳定）",
             "> 稳定标准：exc_v>0 且 exc_h>0 且正超额占比≥60% 且 hit_h≥40%",
             "",
             "## 总览",
             "- 总组数 %d，有效组（nh>0）%d，稳定组 %d（占比%.1f%%）" % (
                 len(rows), sum(1 for r in rows if r["nh"]),
                 len(stable), len(stable) / max(1, len(rows)) * 100),
             "- holdout 超额分布：%s" % _dist([r["exc_h"] for r in rows if r["nh"]]),
             ""]
    if base_v:
        bv = sorted(base_v, key=lambda r: -r["exc_h"])[0]
        lines += ["## 基线对照（mom阈值拉满≈无情绪）",
                  "- cfg=%s：valid超额%+.3f%%/命中%.0f%% holdout超额%+.3f%%/命中%.0f%% 正超额%d/%d" % (
                      bv["cfg"], bv["exc_v"], bv["hit_v"] * 100,
                      bv["exc_h"], bv["hit_h"] * 100, bv["winp_h"], bv["nh"]), ""]
    lines.append("## Top10 稳定组（按 holdout超额）")
    lines.append("| cfg(hi,lo,w,veto,seed) | valid超额/命中 | holdout超额/命中 | 正超额 |")
    lines.append("|---|---|---|---|")
    for r in stable[:10]:
        lines.append("| %s | %+.3f%%/%.0f%% | %+.3f%%/%.0f%% | %d/%d |" % (
            r["cfg"], r["exc_v"], r["hit_v"] * 100,
            r["exc_h"], r["hit_h"] * 100, r["winp_h"], r["nh"]))
    if not stable:
        lines.append("| — | 无稳定组 | | |")
    lines += ["",
              "## 维度边缘分析（holdout平均超额）",
              _edge(rows, 0, ["hi=%s" % v for v in MOM_HI]),
              _edge(rows, 1, ["lo=%s" % v for v in MOM_LO]),
              _edge(rows, 2, ["w=%s" % v for v in MOM_W]),
              _edge(rows, 3, ["veto关", "veto开"]),
              "",
              "## 结论",
              _conclude(rows, stable)]
    write_json_atomic(TOPLIST, stable[:50])
    rp = os.path.join(REPORT_DIR, "情绪万次迭代_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("稳定组 %d，报告:%s" % (len(stable), rp))


def _dist(xs):
    if not xs:
        return "无有效组"
    xs = sorted(xs)
    return "min%+.2f/p25%+.2f/med%+.2f/p75%+.2f/max%+.2f" % (
        xs[0], xs[len(xs) // 4], xs[len(xs) // 2], xs[3 * len(xs) // 4], xs[-1])


def _edge(rows, dim, labels):
    """某维度各档的 holdout 平均超额（只看 nh>0 组）。"""
    vals = sorted({r["cfg"][dim] for r in rows})
    parts = []
    for v in vals:
        xs = [r["exc_h"] for r in rows if r["cfg"][dim] == v and r["nh"]]
        parts.append("%s:%+.3f(n=%d)" % (
            labels[vals.index(v)] if vals.index(v) < len(labels) else v,
            sum(xs) / len(xs) if xs else 0, len(xs)))
    return "- dim%d: %s" % (dim, " ".join(parts))


def _conclude(rows, stable):
    eff = [r for r in rows if r["nh"]]
    if not eff:
        return "holdout 无有效组：情绪窗口与回测信号日重叠不足，结论只能算摸底，不进 config。"
    pos = sum(1 for r in eff if r["exc_h"] > 0)
    return ("holdout 有效组 %d，其中超额为正 %d（%.0f%%）；稳定组 %d。若稳定组占比极低"
            "且 Top 集中在阈值拉满（≈无情绪）附近，则情绪当前无增量；"
            "若稳定组集中在某阈值带，则该带值得每日积累后复验。均不进 config。" % (
                len(eff), pos, pos / len(eff) * 100, len(stable)))


def main():
    args = sys.argv[1:]
    if "--verify" in args:
        verify()
        return
    start, count = 0, 1000
    if "--start" in args:
        start = int(args[args.index("--start") + 1])
    if "--count" in args:
        count = int(args[args.index("--count") + 1])
    split = args[args.index("--split") + 1] if "--split" in args else "va"
    run_split(start, count)


if __name__ == "__main__":
    main()
