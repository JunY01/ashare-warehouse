# -*- coding: utf-8 -*-
"""短线十万网格（100000组，牛长熊短验证机）

给未来亚亚：回答“熊市是不是该短打、牛市是不是能长持”。
网格8维 = mom峰5 × 截断5 × topn4 × 持有5 × 止盈5 × 止损5 × 空仓门4 × 口径2 = 100000。
牛熊按信号日全市场近5日中位数(tmed)划分：>=+1牛 / <=-1熊 / 其余震荡。
流程：valid集(06-23~07-31)分片跑全网格 → 分regime限hold范围选拔 →
TOP留存集(>=08-01)终验 + 邻域稳定 → 写 model_short.json（三挡）。
纯标准库。用法:
  python sweep100k_short.py --split va --start 0 --count 10000  # 10片：0/1w/…/9w
  python sweep100k_short.py --verify    # 选拔+终验+写模型+日志
"""
import datetime
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
           os.path.join(REPO_ROOT, "src", "jobs_build"),
           os.path.join(REPO_ROOT, "src", "jobs_fetch")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import train_recommend as TR
import iterate as IT
from train_recommend import BASE_CAPS, WIDE_CAPS
from src.common.paths import SWEEP_CACHE
from src.common.market_map import write_json_atomic

MODEL_PATH = os.path.join(REPO_ROOT, "config", "model_short.json")  # 晋升写 config/（线上唯一模型目录）
REPORT_DIR = os.path.join(REPO_ROOT, "reports")
RESULTS = os.path.join(SWEEP_CACHE, "sweep100k_short.jsonl")
TOPLIST = os.path.join(SWEEP_CACHE, "sweep100k_short_top.json")

MOMS = [3.0, 4.0, 5.0, 6.0, 7.0]  # 5
CUTS = [50.0, 55.0, 60.0, 65.0, 70.0]  # 5
TOPNS = [3, 5, 8, 10]  # 4
HOLDS = [3, 5, 10, 20, 30]  # 5
TPS = [5.0, 8.0, 10.0, 12.0, 15.0]  # 5
SLS = [-3.0, -4.0, -6.0, -8.0, -10.0]  # 5
GATES = [None, -1.0, -2.0, -3.0]  # 4
CAPSS = [BASE_CAPS, WIDE_CAPS]  # 2 → 5*5*4*5*5*5*4*2=100000
assert len(MOMS) * len(CUTS) * len(TOPNS) * len(HOLDS) * len(TPS) * len(SLS) * len(GATES) * len(CAPSS) == 100000

# 分regime允许的持有期（用户需求：牛长熊短，直接写进选拔约束）
REG_HOLD = {"bear": (3, 5), "chop": (3, 5, 10), "bull": (10, 20, 30)}
REG_CN = {"bear": "熊短打", "chop": "震荡中持", "bull": "牛长持"}
MIN_DAYS = 4  # regime内最少交易天数，否则拒收（防小样本幻觉）


def regime_of(tmed):
    if tmed >= 1.0:
        return "bull"
    if tmed <= -1.0:
        return "bear"
    return "chop"


def combos():
    for mom in MOMS:
        for cut in CUTS:
            for topn in TOPNS:
                for hold in HOLDS:
                    for tp in TPS:
                        for sl in SLS:
                            for gate in GATES:
                                for caps in CAPSS:
                                    yield (mom, cut, topn, hold, tp, sl, gate, caps)


def eval_regimes(feats, sig_days, cfg):
    """单组评估，一次遍历同时出三分regime成绩（含费后净超额/命中/回撤）。"""
    acc = {r: {"excs": [], "hits": 0, "n": 0} for r in ("bear", "chop", "bull")}
    for T in sig_days:
        rows = feats[T]["rows"]
        if cfg.get("gate") is not None and feats[T]["tmed"] < cfg["gate"]:
            continue  # 空仓门：全市场太弱则空仓记0
        scores = IT.score_rule(rows, cfg)
        top = IT.pick_top(rows, scores, cfg["topn"])
        if len(top) < cfg["topn"]:
            continue
        rets = [IT.disc_fee(rows[i]["path"], cfg["tp"], cfg["sl"], cfg["hold"]) for i in top]
        uni = sorted(r["fwd"] for r in rows)
        exc = TR.mean(rets) - uni[len(uni) // 2]
        rg = regime_of(feats[T]["tmed"])
        a = acc[rg]
        a["excs"].append(exc)
        a["hits"] += sum(1 for x in rets if x > 0)
        a["n"] += len(rets)
    out = {}
    for rg, a in acc.items():
        if not a["excs"]:
            out[rg] = {"exc": 0.0, "hit": 0.0, "days": 0, "dd": 0.0}
            continue
        cum, peak, dd = 0.0, 0.0, 0.0
        for e in a["excs"]:  # 按时间顺序累超额算回撤
            cum += e
            peak = max(peak, cum)
            dd = min(dd, cum - peak)
        out[rg] = {"exc": round(TR.mean(a["excs"]), 3), "hit": round(a["hits"] / a["n"], 3),
                   "days": len(a["excs"]), "dd": round(dd, 2)}
    return out


def load_feats():
    """面板构建一次（约十几秒），返回(feats, dates)。"""
    kl, fl, names, turn, marg, hs, oh = TR.load_all()
    rows = TR.build_panel(kl, fl, names, turn, marg, hs, oh)
    feats = {}
    for r in rows:
        feats.setdefault(r["date"], {"rows": [], "tmed": 0.0})["rows"].append(r)
    for T in feats:
        feats[T]["tmed"] = IT.trail_median(kl, T)
    return feats, sorted(feats)


def run_slice(start, count):
    feats, dates = load_feats()
    trd = [d for d in dates if d <= "2026-06-22"]
    vad = [d for d in dates if "2026-06-23" <= d <= "2026-07-31"]
    sig_va = [d for i, d in enumerate(sorted(vad)) if i % 2 == 0]  # 步2加密valid
    print("valid信号日%d个" % len(sig_va), flush=True)
    allc = list(combos())
    fh = open(RESULTS, "a", encoding="utf-8")
    n = 0
    for ci in range(start, min(start + count, len(allc))):
        mom, cut, topn, hold, tp, sl, gate, caps = allc[ci]
        cfg = {"kind": "rule", "caps": caps, "mom_peak": mom, "w": None,
               "cutoff": cut, "topn": topn, "hold": hold, "tp": tp,
               "sl": sl, "gate": gate}
        r = eval_regimes(feats, sig_va, cfg)
        fh.write(json.dumps({"i": ci, "p": [mom, cut, topn, hold, tp, sl, gate,
                                            "wide" if caps == WIDE_CAPS else "base"],
                             "r": r}, ensure_ascii=False) + "\n")
        n += 1
        if n % 2000 == 0:
            print("  片内 %d/%d" % (n, min(count, len(allc) - start)), flush=True)
    fh.close()
    print("本片写入%d组" % n)


def verify():
    feats, dates = load_feats()
    ted = [d for d in dates if d >= "2026-08-01"]
    sig_te = [d for i, d in enumerate(sorted(ted)) if i % 2 == 0]
    rows = [json.loads(l) for l in open(RESULTS, encoding="utf-8") if l.strip()]
    print("共%d组，留存信号日%d个" % (len(rows), len(sig_te)), flush=True)
    bykey = {(x["p"][0], x["p"][1], x["p"][2], x["p"][3], x["p"][4],
              x["p"][5], str(x["p"][6]), x["p"][7]): x for x in rows}
    model = {"_note": "短线三挡模型。牛长熊短：挡位由10万网格分regime选拔+留存终验+邻域稳定晋升。",
             "version": "s1.%s" % datetime.date.today().strftime("%m%d"),
             "date": datetime.date.today().isoformat(), "gears": {}}
    summary = {}
    for rg in ("bear", "chop", "bull"):
        holds = REG_HOLD[rg]
        cands = [x for x in rows if x["p"][3] in holds and x["r"][rg]["days"] >= MIN_DAYS
                 and x["r"][rg]["hit"] >= 0.4 and x["r"][rg]["dd"] >= -10.0]
        cands.sort(key=lambda x: (-x["r"][rg]["exc"], x["r"][rg]["dd"]))
        champ = cands[0] if cands else None
        tele, stable = None, False
        if champ:  # 留存终验：同一参数在留存集重跑
            mom, cut, topn, hold, tp, sl, gate, caps = champ["p"]
            cfg = {"kind": "rule", "caps": WIDE_CAPS if caps == "wide" else BASE_CAPS,
                   "mom_peak": mom, "w": None, "cutoff": cut, "topn": topn,
                   "hold": hold, "tp": tp, "sl": sl, "gate": gate}
            tele = eval_regimes(feats, sig_te, cfg)[rg]
            # 邻域稳定：同参仅hold±1档的邻居在valid同regime也要为正，否则单点幻觉否决
            hi = HOLDS.index(hold)
            nb = []
            for hj in ([HOLDS[hi - 1]] if hi > 0 else []) + ([HOLDS[hi + 1]] if hi < 4 else []):
                hit = bykey.get((mom, cut, topn, hj, tp, sl, str(gate), caps))
                if hit and hit["r"][rg]["days"] >= MIN_DAYS:
                    nb.append(hit["r"][rg]["exc"])
            stable = len(nb) > 0 and min(nb) > 0
        ok = bool(champ and tele["days"] >= 2 and tele["exc"] > 0 and stable)
        if ok:
            model["gears"][rg] = {"hold": champ["p"][3], "tp": champ["p"][4],
                                  "sl": champ["p"][5], "mom_peak": champ["p"][0],
                                  "cutoff": champ["p"][1], "topn": champ["p"][2],
                                  "gate": champ["p"][6], "caps": champ["p"][7],
                                  "valid_exc": champ["r"][rg]["exc"],
                                  "holdout_exc": tele["exc"]}
        summary[rg] = (champ, tele, ok, stable if champ else False)
        print("%s%s 候选%d valid优%s 留存%s 邻域%s → %s" % (
            rg, REG_CN[rg], len(cands),
            ("hold%d超额%+.2f" % (champ["p"][3], champ["r"][rg]["exc"])) if champ else "无",
            ("超额%+.2f/%d天" % (tele["exc"], tele["days"])) if tele else "无",
            ("稳" if stable else "否决") if champ else "无",
            "晋升" if ok else "不晋升"), flush=True)
    if model["gears"]:
        write_json_atomic(MODEL_PATH, model, indent=1)
    os.makedirs(REPORT_DIR, exist_ok=True)
    lp = os.path.join(REPORT_DIR, "R2短线十万次_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    with open(lp, "w", encoding="utf-8") as f:
        f.write("# 短线十万次 %s\n\n" % datetime.date.today())
        f.write("评估%d组，牛长熊短约束：熊hold∈{3,5}、震荡∈{3,5,10}、牛∈{10,20,30}。\n\n" % len(rows))
        for rg in ("bear", "chop", "bull"):
            champ, tele, ok, _st = summary[rg]
            f.write("## %s%s → %s\n" % (rg, REG_CN[rg], "晋升" if ok else "不晋升"))
            if champ:
                f.write("valid最优：hold%d 超额%+.2f%% 命中%.0f%% 回撤%.1f%% %d天\n" % (
                    champ["p"][3], champ["r"][rg]["exc"], champ["r"][rg]["hit"] * 100,
                    champ["r"][rg]["dd"], champ["r"][rg]["days"]))
            if tele:
                f.write("留存复核：超额%+.2f%% %d天\n\n" % (tele["exc"], tele["days"]))
        f.write("结论：%s\n" % (("模型%s" % model["version"]) if model["gears"] else "无挡位晋升"))
    print("日志:%s 模型:%s" % (lp, MODEL_PATH if model["gears"] else "无"), flush=True)


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify()
    else:
        start = int(sys.argv[sys.argv.index("--start") + 1])
        count = int(sys.argv[sys.argv.index("--count") + 1])
        split = sys.argv[sys.argv.index("--split") + 1] if "--split" in sys.argv else "va"
        assert split == "va"
        run_slice(start, count)
