# -*- coding: utf-8 -*-
"""庄家抄底线网格选拔（资金流短窗 14万组 / 无资金流长历史 18.6万组）

给未来亚亚：回答“抄底能不能赚超额、参数怎么定”。
双轨：
  默认（资金流轨，窗口 2026-05~08，半年）：吸筹=主力资金净流入，网格 139968 组。
  --nf  （长历史轨，窗口 2024-02~2026-08，三年）：吸筹=相对强度 z_mom20 代理
        （东财资金流历史硬上限约120天，无法补更早，故用价格相对强度替代做长历史稳健性检验），
        网格 186624 组（zthr 四档）。
流程：valid选拔 → 留存终验 + 邻域稳定 → 写 model_zhuang.json / model_zhuang_nf.json。
纯标准库。用法:
  python sweep_zhuang.py --start 0 --count 10000
  python sweep_zhuang.py --nf --start 0 --count 10000
  python sweep_zhuang.py [--nf] --verify
"""
import datetime
import json
import os
import sys

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(os.path.dirname(DATA_DIR), "reports")
for _p in (DATA_DIR, os.path.dirname(DATA_DIR),
           os.path.join(os.path.dirname(DATA_DIR), "src", "common"),
           os.path.join(os.path.dirname(DATA_DIR), "src", "jobs_build"),
           os.path.join(os.path.dirname(DATA_DIR), "src", "jobs_fetch")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import zhuang_line as Z  # noqa: E402
from train_recommend import mean  # noqa: E402
from src.common.paths import SWEEP_CACHE  # noqa: E402
from src.common.market_map import write_json_atomic  # noqa: E402

NF = "--nf" in sys.argv
# 晋升写 config/（daily_1430 C区只认 config/model_zhuang.json）
MODEL_PATH = os.path.join(os.path.dirname(DATA_DIR), "config",
                          "model_zhuang_nf.json" if NF else "model_zhuang.json")
RESULTS = os.path.join(SWEEP_CACHE, "sweep_zhuang_nf.jsonl" if NF else "sweep_zhuang.jsonl")

DD250S = [-15.0, -20.0, -25.0, -30.0]           # 4
BSCORES = [55.0, 60.0, 65.0, 70.0]              # 4
CDS = [(None, None), (-2.0, 2.0), (-1.5, 1.5)]  # 3
R20S = [(None, None), (-8.0, 3.0), (-10.0, 5.0)]  # 3
TOPNS = [3, 5, 8]                               # 3
HOLDS = [5, 10, 20, 30]                         # 4
TPS = [5.0, 8.0, 12.0]                          # 3
SLS = [-4.0, -6.0, -8.0]                        # 3
GATES = [None, -1.0, -2.0]                      # 3
if NF:
    FLOWS = ["nf"]                              # 长历史：相对强度代理吸筹
    ZTHRS = [-0.5, 0.0, 0.5, 1.0]               # 4
else:
    FLOWS = ["abs", "z", "both"]                # 3
    ZTHRS = [0.0]                               # 1
# 网格总数：4*4*3*len(FLOWS)*len(ZTHRS)*3*3*4*3*3*3
GRID_N = len(DD250S) * len(BSCORES) * len(CDS) * len(FLOWS) * len(ZTHRS) \
    * len(R20S) * len(TOPNS) * len(HOLDS) * len(TPS) * len(SLS) * len(GATES)
print("模式:%s 网格%d组" % ("nf长历史" if NF else "资金流短窗", GRID_N), flush=True)

MIN_DAYS = 8 if NF else 4      # 选拔最少交易天数（nf 窗口大取8，防小样本幻觉）
R5 = (-4.0, 6.0)  # 5日区间固定


def combos():
    for dd in DD250S:
        for bs in BSCORES:
            for cd in CDS:
                for fl in FLOWS:
                    for zt in ZTHRS:
                        for r20 in R20S:
                            for topn in TOPNS:
                                for hold in HOLDS:
                                    for tp in TPS:
                                        for sl in SLS:
                                            for gate in GATES:
                                                yield (dd, bs, cd, fl, zt, r20, topn, hold, tp, sl, gate)


def cfg_of(p):
    dd, bs, cd, fl, zt, r20, topn, hold, tp, sl, gate = p
    return {"dd250": dd, "bscore": bs, "cd_lo": cd[0], "cd_hi": cd[1],
            "flow_mode": fl, "zthr": zt, "r20_lo": r20[0], "r20_hi": r20[1],
            "r5_lo": R5[0], "r5_hi": R5[1], "cutoff": 45.0,
            "topn": topn, "hold": hold, "tp": tp, "sl": sl, "gate": gate}


def regime_of(tmed):
    return "bull" if tmed >= 1.0 else ("bear" if tmed <= -1.0 else "chop")


def eval_regimes(feats, sig_days, cfg):
    """单组评估：一次遍历出 overall + 三regime（费后净超额/命中/天数/回撤）。"""
    from src.common.fees import disc_fee
    acc = {"all": {"excs": [], "hits": 0, "n": 0},
           "bull": {"excs": [], "hits": 0, "n": 0},
           "chop": {"excs": [], "hits": 0, "n": 0},
           "bear": {"excs": [], "hits": 0, "n": 0}}
    for T in sig_days:
        F = feats[T]
        if cfg.get("gate") is not None and F["tmed"] < cfg["gate"]:
            continue
        scores = Z.score_rows(F["rows"], cfg)
        top = Z.pick_top(F["rows"], scores, cfg["topn"])
        if len(top) < cfg["topn"]:
            continue
        rets = [disc_fee(F["rows"][i]["path"], cfg["tp"], cfg["sl"], cfg["hold"])
                for i in top]
        uni = sorted(r["fwd"] for r in F["rows"])
        med = uni[len(uni) // 2]
        exc = mean(rets) - med
        rg = regime_of(F["tmed"])
        for key in ("all", rg):
            a = acc[key]
            a["excs"].append(exc)
            a["hits"] += sum(1 for x in rets if x > 0)
            a["n"] += len(rets)
    out = {}
    for key, a in acc.items():
        if not a["excs"]:
            out[key] = {"exc": 0.0, "hit": 0.0, "days": 0, "dd": 0.0}
            continue
        cum, peak, dd = 0.0, 0.0, 0.0
        for e in a["excs"]:
            cum += e
            peak = max(peak, cum)
            dd = min(dd, cum - peak)
        out[key] = {"exc": round(mean(a["excs"]), 3), "hit": round(a["hits"] / a["n"], 3),
                    "days": len(a["excs"]), "dd": round(dd, 2)}
    return out


def load_sig():
    feats, dates = Z.get_panel(need_flow=not NF)
    if NF:
        vad = [d for d in dates if "2024-07-01" <= d <= "2025-12-31"]
        ted = [d for d in dates if d >= "2026-01-01"]
        vstep, tstep = 5, 5
    else:
        vad = [d for d in dates if "2026-06-23" <= d <= "2026-07-31"]
        ted = [d for d in dates if d >= "2026-08-01"]
        vstep, tstep = 3, 5
    sig_va = [d for i, d in enumerate(vad) if i % vstep == 0]
    sig_te = [d for i, d in enumerate(ted) if i % tstep == 0]
    return feats, sig_va, sig_te


def run_slice(start, count, out=None):
    feats, sig_va, _ = load_sig()
    print("valid信号日%d个" % len(sig_va), flush=True)
    allc = list(combos())
    fh = open(out or RESULTS, "a", encoding="utf-8")
    n = 0
    for ci in range(start, min(start + count, len(allc))):
        cfg = cfg_of(allc[ci])
        r = eval_regimes(feats, sig_va, cfg)
        fh.write(json.dumps({"i": ci, "p": list(allc[ci]), "r": r},
                            ensure_ascii=False) + "\n")
        n += 1
        if n % 2000 == 0:
            print("  片内 %d/%d" % (n, min(count, len(allc) - start)), flush=True)
    fh.close()
    print("本片写入%d组" % n)


def verify():
    feats, _, sig_te = load_sig()
    rows = [json.loads(l) for l in open(RESULTS, encoding="utf-8") if l.strip()]
    print("共%d组，留存信号日%d个" % (len(rows), len(sig_te)), flush=True)
    bykey = {}
    for x in rows:
        p = x["p"]
        key = (p[0], p[1], p[2][0], p[2][1], p[3], p[4], p[5][0], p[5][1], p[6], p[7], p[8], p[9], str(p[10]))
        bykey[key] = x
    cands = [x for x in rows if x["r"]["all"]["days"] >= MIN_DAYS
             and x["r"]["all"]["hit"] >= 0.4 and x["r"]["all"]["dd"] >= -10.0
             and x["r"]["all"]["exc"] > 0]
    cands.sort(key=lambda x: (-x["r"]["all"]["exc"], x["r"]["all"]["dd"]))
    # 多候选择优：对 valid 最优的 K 个候选逐一留存终验，晋升留存为正且邻域稳定的最佳者
    champ = tele = stable = None
    for x in cands[:30]:
        cfg = cfg_of(x["p"])
        t = eval_regimes(feats, sig_te, cfg)["all"]
        if t["days"] < 2 or t["exc"] <= 0:
            continue
        # 邻域稳定：dd250 / hold 邻居在 valid 也要为正
        nbs = []
        dd_i = DD250S.index(x["p"][0])
        for d in ([DD250S[dd_i - 1]] if dd_i > 0 else []) + ([DD250S[dd_i + 1]] if dd_i < 3 else []):
            p = x["p"]
            k = (d, p[1], p[2][0], p[2][1], p[3], p[4], p[5][0], p[5][1], p[6], p[7], p[8], p[9], str(p[10]))
            if k in bykey and bykey[k]["r"]["all"]["days"] >= MIN_DAYS:
                nbs.append(bykey[k]["r"]["all"]["exc"])
        h_i = HOLDS.index(x["p"][7])
        for h in ([HOLDS[h_i - 1]] if h_i > 0 else []) + ([HOLDS[h_i + 1]] if h_i < 3 else []):
            p = x["p"]
            k = (p[0], p[1], p[2][0], p[2][1], p[3], p[4], p[5][0], p[5][1], p[6], h, p[8], p[9], str(p[10]))
            if k in bykey and bykey[k]["r"]["all"]["days"] >= MIN_DAYS:
                nbs.append(bykey[k]["r"]["all"]["exc"])
        st = len(nbs) > 0 and min(nbs) > 0
        if st:
            champ, tele, stable = x, t, True
            break
    ok = bool(champ)
    if ok:
        p = champ["p"]
        model = {"_note": "庄家抄底线（%s）。网格选拔+多候选留存终验+邻域稳定晋升。" % ("长历史无资金流" if NF else "资金流短窗"),
                 "version": ("z_nf.%s" if NF else "z1.%s") % datetime.date.today().strftime("%m%d"),
                 "date": datetime.date.today().isoformat(),
                 "params": {"dd250": p[0], "bscore": p[1], "cd_lo": p[2][0], "cd_hi": p[2][1],
                            "flow_mode": p[3], "zthr": p[4], "r20_lo": p[5][0], "r20_hi": p[5][1],
                            "r5_lo": R5[0], "r5_hi": R5[1], "cutoff": 45.0,
                            "topn": p[6], "hold": p[7], "tp": p[8], "sl": p[9], "gate": p[10]},
                 "valid": champ["r"]["all"], "holdout": tele}
        write_json_atomic(MODEL_PATH, model, indent=1)
    lines = ["# 🕵️ 庄家抄底线%s网格 %s" % ("（长历史无资金流）" if NF else "", datetime.date.today()),
             "评估%d组；valid选拔(前30候选逐一留存终验) + dd250/hold邻域稳定。%s" % (
                 len(rows), ("窗口2024-07~2025-12 / 留存2026+" if NF else "窗口2026-06~07 / 留存08+")),
             "选拔门槛：valid超额>0、命中≥%.0f%%、回撤≥-10%%、天数≥%d；留存≥2天且超额>0。" % (0.4 * 100, MIN_DAYS),
             "",
             "## 选拔冠军"]
    if champ:
        a = champ["r"]["all"]
        lines.append("- valid：超额%+.2f%% 命中%.0f%% 回撤%.1f%% %d天（参数 %s）" % (
            a["exc"], a["hit"] * 100, a["dd"], a["days"], list(champ["p"])))
        if tele:
            lines.append("- 留存复核：超额%+.2f%% %d天" % (tele["exc"], tele["days"]))
        lines.append("- 邻域稳定：%s" % ("✅稳" if stable else "❌否决"))
        for rg in ("bull", "chop", "bear"):
            r = champ["r"][rg]
            lines.append("  %s：超额%+.2f%% 命中%.0f%% %d天 回撤%.1f%%" % (
                rg, r["exc"], r["hit"] * 100, r["days"], r["dd"]))
    else:
        lines.append("- 前30候选留存均为负或不足2天 → 不晋升（信号稀有，留存期触发不足）。")
    lines.append("")
    lines.append("## 结论")
    if ok:
        lines.append("- **晋升** → %s（抄底线接入每日推荐，中线左侧）" % MODEL_PATH)
    else:
        lines.append("- **不晋升**：庄家线暂不入交易体系，保留观察名单机制。")
    out = "\n".join(lines)
    print(out)
    os.makedirs(REPORT_DIR, exist_ok=True)
    lp = os.path.join(REPORT_DIR, "庄家抄底线%s十万网格_%s.md" % ("_nf" if NF else "", datetime.date.today().strftime("%Y%m%d")))
    with open(lp, "w", encoding="utf-8") as f:
        f.write(out)
    print("\n日志:%s" % lp)


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify()
    else:
        start = int(sys.argv[sys.argv.index("--start") + 1])
        count = int(sys.argv[sys.argv.index("--count") + 1])
        out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
        run_slice(start, count, out)