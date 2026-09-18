# -*- coding: utf-8 -*-
"""庄家抄底线 vs 现有短线(v2) 同场交叉验证 + 重叠分析。

回放口径与 validate_short 一致（无未来函数）：
  信号日 = 每5交易日一取样，持有期用 disc_fee 费后近似（止盈/止损/持有日）。
  基准 = 当日全市场未来5日中位数（超额）。
  庄家线：zhuang_line.zhuang_score（左侧抄底，中线持有 hold=10）
  现算法：daily_1430.short_evaluate（v2线上参数：mom_peak4、caps=BASE、cutoff55、topn5、hold5/tp8/sl-6）

输出：reports/庄家抄底线验证_YYYYMMDD.md（对打表 + 重叠分析 + 结论）
用法: python validate_zhuang.py          # 全信号日对打
纯标准库。
"""
import datetime
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
from src.jobs_build.daily_1430 import short_evaluate  # noqa: E402
from train_recommend import mean  # noqa: E402
from src.common.fees import disc_fee  # noqa: E402
from src.common.paths import SWEEP_CACHE  # noqa: E402

TEST_START = "2026-06-22"
STEP = 5
NF = "--nf" in sys.argv   # 长历史无资金流轨：单独验证庄家线自身（v2 需资金流，无法长历史跑）

V2_CFG = {"caps": (-10, 12, -3, 8, -4, 6), "mom_peak": 4.0, "cutoff": 55.0,
          "topn": 5, "hold": 5, "tp": 8.0, "sl": -6.0}


def load_champion_cfg():
    """优先读 config/model_zhuang[_nf].json；否则从 sweep 结果里取 valid 最优（未晋升也要看看）。"""
    model_path = os.path.join(os.path.dirname(DATA_DIR), "config",
                              "model_zhuang_nf.json" if NF else "model_zhuang.json")
    if os.path.exists(model_path):
        import json as _json
        m = _json.load(open(model_path, encoding="utf-8"))
        p = m["params"]
        return {**p, "topn": p.get("topn", 5), "hold": p.get("hold", 10),
                "tp": p.get("tp", 8.0), "sl": p.get("sl", -6.0), "gate": p.get("gate")}
    res_path = os.path.join(SWEEP_CACHE, "sweep_zhuang_nf.jsonl" if NF else "sweep_zhuang.jsonl")
    if os.path.exists(res_path):
        import json as _json
        import sweep_zhuang as SW
        rows = [_json.loads(l) for l in open(res_path, encoding="utf-8") if l.strip()]
        min_days = 8 if NF else 4
        cands = [x for x in rows if x["r"]["all"]["days"] >= min_days
                 and x["r"]["all"]["hit"] >= 0.3 and x["r"]["all"]["exc"] > 0]
        cands.sort(key=lambda x: (-x["r"]["all"]["exc"], x["r"]["all"]["dd"]))
        if cands:
            cfg = SW.cfg_of(cands[0]["p"])
            return cfg
    return dict(Z.BASE_CFG)


def _disc(rows, picks, cfg):
    rets = [disc_fee(rows[i]["path"], cfg["tp"], cfg["sl"], cfg["hold"]) for i in picks]
    return rets


def _v2_score(rows):
    """short_evaluate v2 全板块评分，返回 {idx: score}。"""
    out = {}
    for i, r in enumerate(rows):
        ev = short_evaluate(r["name"], r["ret5"], r["ret5"] - r["prev5"],
                            {"latest": r["flow"]["latest"], "consec": r["flow"]["consec"],
                             "sum5": r["flow"]["s5"]},
                            r["ret20"], r["chg_today"], 0, 0, None,
                            {"regime": "BULL" if r["bull"] else "BEAR",
                             "alert": r["alert"], "bottom_score": r["bscore"],
                             "engine": r["engine"], "cross_age": r["xage"]},
                            False, caps=V2_CFG["caps"], mom_peak=V2_CFG["mom_peak"],
                            cutoff=V2_CFG["cutoff"])
        if ev is not None:
            out[i] = ev["score"]
    return out


def main():
    ZH_CFG = load_champion_cfg()
    print("庄家线参数:", {k: ZH_CFG[k] for k in ("dd250", "bscore", "flow_mode", "topn", "hold", "tp", "sl")})
    feats, dates = Z.get_panel()
    sig = [d for i, d in enumerate(dates) if i % STEP == 0 and d >= TEST_START]
    print("信号日 %d 个: %s ~ %s" % (len(sig), sig[0], sig[-1]), flush=True)

    rows_report = []
    agg = {"zh": {"excs": [], "hits": 0, "n": 0, "avg": [], "davg": []},
           "v2": {"excs": [], "hits": 0, "n": 0, "avg": [], "davg": []}}
    overlap = {"both": 0, "zh_only_ok": 0, "zh_only_bad": 0, "v2_only_ok": 0, "v2_only_bad": 0,
               "tot": 0}
    for T in sig:
        F = feats[T]
        rows = F["rows"]
        med = sorted(r["fwd"] for r in rows)[len(rows) // 2]
        # 庄家线
        zsc = Z.score_rows(rows, ZH_CFG)
        ztop = Z.pick_top(rows, zsc, ZH_CFG["topn"])
        # v2
        vsc = _v2_score(rows)
        vtop = Z.pick_top(rows, vsc, V2_CFG["topn"])
        if len(ztop) < ZH_CFG["topn"] or len(vtop) < V2_CFG["topn"]:
            continue
        zr = _disc(rows, ztop, ZH_CFG)
        vr = _disc(rows, vtop, V2_CFG)
        zexc = mean(zr) - med
        vexc = mean(vr) - med
        for key, exc, rets in (("zh", zexc, zr), ("v2", vexc, vr)):
            a = agg[key]
            a["excs"].append(exc)
            a["avg"].append(mean(rets))
            a["davg"].append(mean(rets))
            a["hits"] += sum(1 for x in rets if x > 0)
            a["n"] += len(rets)
        # 重叠分析（用板块代码集合）
        zc = {rows[i]["code"] for i in ztop}
        vc = {rows[i]["code"] for i in vtop}
        both = zc & vc
        z_only = zc - vc
        v_only = vc - zc
        overlap["tot"] += 1
        overlap["both"] += len(both)
        ok_z = {rows[i]["code"] for i in ztop if rows[i]["fwd"] > 0}
        ok_v = {rows[i]["code"] for i in vtop if rows[i]["fwd"] > 0}
        overlap["zh_only_ok"] += len(z_only & ok_z)
        overlap["zh_only_bad"] += len(z_only - ok_z)
        overlap["v2_only_ok"] += len(v_only & ok_v)
        overlap["v2_only_bad"] += len(v_only - ok_v)
        mark = "✅庄" if zexc > vexc else "✅现"
        rows_report.append((T, F["tmed"], zexc, vexc, mark, len(zc), len(both), len(z_only)))

    def fnum(v):
        return "无" if v is None else ("%+.0f" % v)
    lines = ["# 🕵️ 庄家抄底线 vs 现有短线(v2) 交叉验证 %s" % datetime.date.today(),
             "> 同场同基准（全市场未来5日中位数），费后纪律收益；庄家线=左侧抄底中线(hold%d)，现算法=v2短线(hold%d)" % (
                 ZH_CFG["hold"], V2_CFG["hold"]),
             "> 庄家线参数：回撤≤%s%%、底部≥%.0f分、贴云带[%s,%s]、吸筹(%s)、20日[%s,%s]" % (
                 fnum(ZH_CFG["dd250"]), ZH_CFG["bscore"],
                 fnum(ZH_CFG["cd_lo"]), fnum(ZH_CFG["cd_hi"]),
                 ZH_CFG["flow_mode"], fnum(ZH_CFG["r20_lo"]), fnum(ZH_CFG["r20_hi"])),
             "",
             "| 信号日 | 市况5日 | 庄家超额 | v2超额 | 赢家 | 庄家数 | 重合 | 庄独有 |",
             "|---|---|---|---|---|---|---|---|"]
    for T, tmed, ze, ve, mark, nz, nb, no in rows_report:
        lines.append("| %s | %+.1f%% | %+.2f%% | %+.2f%% | %s | %d | %d | %d |" % (
            T, tmed, ze, ve, mark, nz, nb, no))

    def summary(key):
        a = agg[key]
        exc = mean(a["excs"])
        cum, peak, dd = 0.0, 0.0, 0.0
        for e in a["excs"]:
            cum += e
            peak = max(peak, cum)
            dd = min(dd, cum - peak)
        return {"ndate": len(a["excs"]), "avg": mean(a["avg"]),
                "hit": a["hits"] / a["n"] if a["n"] else 0,
                "exc": exc, "dd": dd, "winp": sum(1 for e in a["excs"] if e > 0)}

    s_zh, s_v2 = summary("zh"), summary("v2")
    lines += ["",
              "## 汇总（%d 个信号日）" % s_zh["ndate"],
              "- **庄家线**：纪律均%+.2f%% 命中%.0f%% 超额%+.2f%% 回撤%.1f%% 正超额%d/%d" % (
                  s_zh["avg"], s_zh["hit"] * 100, s_zh["exc"], s_zh["dd"], s_zh["winp"], s_zh["ndate"]),
              "- **现v2**：  纪律均%+.2f%% 命中%.0f%% 超额%+.2f%% 回撤%.1f%% 正超额%d/%d" % (
                  s_v2["avg"], s_v2["hit"] * 100, s_v2["exc"], s_v2["dd"], s_v2["winp"], s_v2["ndate"]),
              "",
              "## 重叠分析（%d 个信号日）" % overlap["tot"],
              "- 两线重合板块 **%d** 个（共同确认，最稳）" % overlap["both"],
              "- 庄家独有 %d 个：%d 对了 / %d 错了（占比 %.0f%%）" % (
                  overlap["zh_only_ok"] + overlap["zh_only_bad"],
                  overlap["zh_only_ok"], overlap["zh_only_bad"],
                  (overlap["zh_only_ok"] / max(1, overlap["zh_only_ok"] + overlap["zh_only_bad"])) * 100),
              "- 现v2独有 %d 个：%d 对了 / %d 错了（占比 %.0f%%）" % (
                  overlap["v2_only_ok"] + overlap["v2_only_bad"],
                  overlap["v2_only_ok"], overlap["v2_only_bad"],
                  (overlap["v2_only_ok"] / max(1, overlap["v2_only_ok"] + overlap["v2_only_bad"])) * 100),
              "",
              "## 结论"]
    zh_won = s_zh["exc"] > s_v2["exc"] and s_zh["winp"] > s_v2["winp"]
    abs_ok = s_zh["exc"] > 0
    if zh_won and abs_ok:
        lines.append("- 庄家线超额与稳定性均优于现算法且绝对超额为正 → **可以上线**（接入每日推荐，中线左侧）。")
    elif zh_won:
        lines.append("- 庄家线相对现算法**更优**，但绝对超额仍为负 → 定位为**观察名单**（不直接交易），攒样本/补估值数据再战。")
    else:
        lines.append("- 庄家线未优于现算法 → 保留观察名单机制，与现短线互补，暂不晋升。")
    lines.append("- 风险提示：样本约 3 个月单边行情，需 10 年穿越复核 + 攒样本后再终判。")

    out = "\n".join(lines)
    print(out)
    os.makedirs(REPORT_DIR, exist_ok=True)
    rp = os.path.join(REPORT_DIR, "庄家抄底线验证_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    with open(rp, "w", encoding="utf-8") as f:
        f.write(out)
    print("\n报告:%s" % rp)


def main_nf():
    """长历史无资金流轨：庄家线（相对强度代理吸筹）在 2024~2026 三年窗口单独验证。"""
    import sweep_zhuang as SW
    ZH = load_champion_cfg()
    feats, dates = Z.get_panel(need_flow=False)
    sig = [d for i, d in enumerate(dates) if i % STEP == 0 and d >= "2024-02-01"]
    print("长历史庄家线参数:", {k: ZH[k] for k in ("dd250", "bscore", "flow_mode", "zthr", "topn", "hold", "tp", "sl")})
    print("信号日 %d 个: %s ~ %s" % (len(sig), sig[0], sig[-1]), flush=True)
    per_date, excs, hits, n = [], [], 0, 0
    for T in sig:
        F = feats[T]
        if ZH.get("gate") is not None and F["tmed"] < ZH["gate"]:
            continue
        scores = Z.score_rows(F["rows"], ZH)
        top = Z.pick_top(F["rows"], scores, ZH["topn"])
        if len(top) < ZH["topn"]:
            continue
        rets = [disc_fee(F["rows"][i]["path"], ZH["tp"], ZH["sl"], ZH["hold"]) for i in top]
        uni = sorted(r["fwd"] for r in F["rows"])
        med = uni[len(uni) // 2]
        exc = mean(rets) - med
        per_date.append((T, F["tmed"], exc, mean(rets), sum(1 for x in rets if x > 0)))
        excs.append(exc)
        hits += sum(1 for x in rets if x > 0)
        n += len(rets)
    cum, peak, dd = 0.0, 0.0, 0.0
    for e in excs:
        cum += e
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    # 分 regime
    reg = {"bull": [], "chop": [], "bear": []}
    for T, tmed, exc, avg, h in per_date:
        rg = "bull" if tmed >= 1.0 else ("bear" if tmed <= -1.0 else "chop")
        reg[rg].append(exc)
    lines = ["# 🕵️ 庄家抄底线（长历史无资金流）验证 %s" % datetime.date.today(),
             "> 窗口 2024-02~2026-08（三年，%d 信号日），吸筹=相对强度 z_mom20 代理（东财资金流历史硬上限约120天）" % len(per_date),
             "> 庄家线参数：回撤≤%+.0f%%、底部≥%.0f分、相对强度≥%.1f、持有%d天 tp%+.0f%% sl%+.0f%%" % (
                 ZH["dd250"], ZH["bscore"], ZH.get("zthr", 0.0), ZH["hold"], ZH["tp"], ZH["sl"]),
             "",
             "| 信号日 | 市况5日 | 纪律均 | 超额 | 命中 |",
             "|---|---|---|---|---|"]
    for T, tmed, exc, avg, h in per_date:
        lines.append("| %s | %+.1f%% | %+.2f%% | %s%+.2f%% | %d/%d |" % (
            T, tmed, avg, "✅" if exc > 0 else "❌", exc, h, ZH["topn"]))
    lines += ["",
              "## 汇总（%d 信号日，%d 次交易）" % (len(per_date), n),
              "- 纪律均 **%+.2f%%**，命中 **%.0f%%**" % (mean([p[3] for p in per_date]), hits / n * 100 if n else 0),
              "- 平均超额 **%+.2f%%**，正超额 **%d/%d**，超额回撤 **%.1f%%**" % (
                  mean(excs), sum(1 for e in excs if e > 0), len(excs), dd),
              "",
              "## 分 regime"]
    for rg, arr in reg.items():
        if arr:
            lines.append("- %s：超额%+.2f%%（%d 日）" % (rg, mean(arr), len(arr)))
    lines += ["",
              "## 结论"]
    abs_ok = mean(excs) > 0
    if abs_ok and len(excs) >= 20 and (sum(1 for e in excs if e > 0) / len(excs)) >= 0.55:
        lines.append("- 三年窗口绝对超额为正且正超额占比≥55% → **信号有效**，值得接入每日抄底观察并继续攒数据。")
    else:
        lines.append("- 三年窗口未稳定跑赢全市场中位数 → **不晋升**，保留观察名单机制。")
    lines.append("- 信号稀有（深跌+结构底+企稳共振的日数有限），样本仍需更长周期（多轮牛熊）才能终判。")
    out = "\n".join(lines)
    print(out)
    os.makedirs(REPORT_DIR, exist_ok=True)
    rp = os.path.join(REPORT_DIR, "庄家抄底线验证_nf_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    with open(rp, "w", encoding="utf-8") as f:
        f.write(out)
    print("\n报告:%s" % rp)


if __name__ == "__main__":
    if NF:
        main_nf()
    else:
        main()