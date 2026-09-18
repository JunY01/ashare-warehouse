# -*- coding: utf-8 -*-
"""社群情绪 × 盘面交叉验证（宝妈/宝爸双指数拆分版）。

概念：mom=散户情绪热度（过热扣分，禁追涨）；dad=经验共识（确认/否决信号）。
男人女人投资概念不一样，分开验证：四组对照 v2基线 / v2+mom / v2+dad否决 / v2+mom+dad，
外加 mom/dad 各自 Spearman IC。

两阶段（按情绪样本量自动切换）：
  阶段1（有效天 <30）：描述性研究 —— 双指数各自 IC + mom 情绪桶未来收益。
    不判晋升。
  阶段2（有效天 ≥30）：增量回测 —— 沿 research/validate_short.py 范式
    （STEP=5/FWD=5/费后纪律/超额exc）。

指数用法（阶段2，只用 ≤T 已发布指数，无未来函数）：
  mom_adj（只扣分）：mom_index ≥20 扣 10 分，≥12 扣 5 分（分布 med≈7.5/p90≈15）。
  dad_veto（否决）：dad_index ≥25 且 vet_sell_cons ≥0.6 时否决该候选（经验型一致看空不接）。
  dad_shadow（影子实验，只记录不参与决策）：dad ≥25 且 vet_buy_cons ≥0.6 的候选
    若被 v2 选中，记影子命中；影子不改变 picks，加分口子不开（禁追涨）。

数据：data/social_sentiment_posts_*.json（双指数由 compute_dual 重算，不重抓）；
  行情用 history_db.connect(readonly=True) 的 sector_kline（与 validate_short 同源）。
  SOCIAL_SECTOR_MAP 首个 sector_code 为聚合主码。

用法:
  python validate_social.py          # 自动选阶段并写报告
纯标准库。
"""
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.common import history_db  # noqa: E402
from src.common.paths import DATA_ROOT, REPORT_ROOT  # noqa: E402
from src.common.market_map import SOCIAL_SECTOR_MAP  # noqa: E402
from src.jobs_build.daily_1430_scan import broad_of, chg5_accel, short_evaluate  # noqa: E402
from research.validate_short import (  # noqa: E402
    BASE_CAPS, FWD, STEP, TEST_START, build_features, disc_return,
    eval_params, load_all, run_exp)

MIN_DAYS_STAGE2 = 30


def load_sentiment():
    """读情绪指数 → {date: {sectors: {key: 双指数}, total, nkeys}}。
    主源 DB（social_sentiment_daily，只读），DB 为空时回退 JSON 重算。"""
    out = {}
    try:
        conn = history_db.connect(readonly=True)
        try:
            rows = conn.execute(
                "SELECT date, skey, mom_index, dad_index, total, valid, spam,"
                " buy, sell, vet_buy, vet_sell FROM social_sentiment_daily"
                " ORDER BY date").fetchall()
        finally:
            conn.close()
        for d, k, mom, dad, total, valid, spam, buy, sell, vb, vs in rows:
            day = out.setdefault(d, {"sectors": {}, "total": 0, "nkeys": 0})
            day["sectors"][k] = {
                "mom_index": mom, "dad_index": dad,
                "details": {"total": total or 0, "valid": valid or 0,
                            "spam": spam or 0, "buy": buy or 0,
                            "sell": sell or 0, "vet_buy": vb or 0,
                            "vet_sell": vs or 0,
                            "vet_buy_cons": (vb / (vb + vs) if (vb + vs) else 0.0),
                            "vet_sell_cons": (vs / (vb + vs) if (vb + vs) else 0.0)}}
            day["total"] += total or 0
            if total:
                day["nkeys"] += 1
        if out:
            return out
    except Exception:
        pass
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src", "jobs_fetch"))
    import fetch_social_sentiment as FSS
    for fp in sorted(glob.glob(os.path.join(
            DATA_ROOT, "cache", "social_posts", "social_sentiment_posts_*.json"))):
        d = os.path.basename(fp)[len("social_sentiment_posts_"):-len(".json")]
        d = "%s-%s-%s" % (d[:4], d[4:6], d[6:8])
        try:
            raw = json.load(open(fp, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        sectors = {k: FSS.compute_dual(posts) for k, posts in raw.items()}
        total = sum(v["details"]["total"] for v in sectors.values())
        out[d] = {"sectors": sectors, "total": total,
                  "nkeys": sum(1 for v in sectors.values() if v["details"]["total"])}
    return out


def valid_days(senti, min_posts=30, min_keys=4):
    """有效天：样本充足（帖数/覆盖key数达标），阶段门控与扣分豁免共用。"""
    return {d for d, v in senti.items()
            if v["total"] >= min_posts and v["nkeys"] >= min_keys}


def key_of_code(code):
    """sector code → 情绪 key（聚合主码映射）。"""
    for k, v in SOCIAL_SECTOR_MAP.items():
        if code in v["sector_codes"]:
            return k
    return None


def _latest_dual(key, date, senti, ok_days):
    """取 ≤date 最近有效天的双指数（无未来函数）。返回 (mom, dad, details)。"""
    avail = [d for d in ok_days if d <= date]
    if not avail:
        return 0.0, 0.0, {}
    s = (senti[max(avail)]["sectors"].get(key) or {})
    det = s.get("details", {})
    return s.get("mom_index", 0) or 0, s.get("dad_index", 0) or 0, det


def mom_adj(key, date, senti, ok_days):
    """宝妈扣分 [-10, 0]：散户过热扣分（禁追涨）。样本不足天豁免。
    阈值按分布定（med≈7.5/p90≈15）：≥20扣10分，≥12扣5分。探索性，待重估。"""
    if not key:
        return 0.0
    mom, _, _ = _latest_dual(key, date, senti, ok_days)
    if mom >= 20:
        return -10.0
    if mom >= 12:
        return -5.0
    return 0.0


def dad_veto(key, date, senti, ok_days):
    """宝爸否决：经验共识强（dad≥25）且经验帖一致看空（vet_sell_cons≥0.6）时否决候选。
    分布 dad med≈18.8/p75≈22.7/p90≈31.4。只否决不加分。"""
    if not key:
        return False
    _, dad, det = _latest_dual(key, date, senti, ok_days)
    return dad >= 25 and (det.get("vet_sell_cons") or 0) >= 0.6


def dad_shadow_hit(key, date, senti, ok_days):
    """宝爸影子：dad≥25 且经验帖一致看多（vet_buy_cons≥0.6）。只记录，不参与决策。"""
    if not key:
        return False
    _, dad, det = _latest_dual(key, date, senti, ok_days)
    return dad >= 25 and (det.get("vet_buy_cons") or 0) >= 0.6


def social_adj(key, date, senti, ok_days):
    """兼容旧名单测：mom 扣分。"""
    return mom_adj(key, date, senti, ok_days)


def spearman(xs, ys):
    """手搓 Spearman（纯标准库，无 scipy）。"""
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0] * len(v)
        for pos, i in enumerate(order):
            r[i] = pos
        return r
    n = len(xs)
    if n < 3:
        return 0.0
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    return round(cov / (vx * vy), 3) if vx and vy else 0.0


def stage1(senti, kl, ok_days):
    """描述性研究：mom/dad 各自 IC + mom 情绪桶未来收益对照（仅有效天）。不判晋升。"""
    mom_pairs, dad_pairs = [], []  # (指数, FWD5收益)
    buckets = {"high": [], "mid": [], "low": []}
    for d, day in senti.items():
        if d not in ok_days:
            continue
        for key, s in day["sectors"].items():
            codes = SOCIAL_SECTOR_MAP[key]["sector_codes"][:1]
            if not codes:
                continue
            pts = dict((x[0], x[1]) for x in kl.get(codes[0], []))
            if d not in pts:
                continue
            closes = [c for t, c in kl[codes[0]] if t <= d]
            fut = [c for t, c in kl[codes[0]] if t > d][:FWD]
            if len(closes) < 2 or not fut:
                continue
            # 冷启动期未来不足5天：按实际可用天数算，不等满窗
            fwd = (fut[-1] / pts[d] - 1) * 100
            mom_pairs.append((s["mom_index"], fwd))
            dad_pairs.append((s["dad_index"], fwd))
            buckets["high" if s["mom_index"] >= 12 else (
                "mid" if s["mom_index"] >= 7.5 else "low")].append(fwd)
    ic_mom = spearman([p[0] for p in mom_pairs], [p[1] for p in mom_pairs])
    ic_dad = spearman([p[0] for p in dad_pairs], [p[1] for p in dad_pairs])
    desc = {}
    for k, v in buckets.items():
        desc[k] = {"n": len(v),
                   "avg": round(sum(v) / len(v), 2) if v else 0.0}
    return {"n": len(mom_pairs), "ic_mom": ic_mom, "ic_dad": ic_dad, "buckets": desc}


def _run_arm(feats, tests, senti, ok_days, mode):
    """跑一个对照臂。mode: base / mom / dad / both。
    选股后 Top5（broad 去重）与 validate_short 同口径。
    trig_mom/trig_veto 记决策相关触发（进入候选的映射码）；
    shadow 记被选中的映射预案中 dad 看多一致的数量与命中（只记录不参与决策）。"""
    per_date, all_rets, all_disc, all_excess = [], [], [], []
    hit = dhit = 0
    trig_mom = trig_veto = 0
    sel_mapped = sel_flag = sel_flag_hit = 0
    for T in tests:
        F = feats[T]
        picks = []
        for r in F["rows"]:
            ev = short_evaluate(r["name"], r["c5"], r["acc"], r["fl"], r["chg20"],
                                r["chg_today"], 0, 0, None, r["ri"], False,
                                caps=BASE_CAPS, mom_peak=4.0, w=None, cutoff=55.0)
            if ev is None:
                continue
            key = key_of_code(r["code"]) if "code" in r else None
            score = ev["score"]
            if mode in ("mom", "both"):
                adj = mom_adj(key, T, senti, ok_days)
                if adj:
                    trig_mom += 1
                score += adj
            vetoed = False
            if mode in ("dad", "both") and dad_veto(key, T, senti, ok_days):
                vetoed = True
                trig_veto += 1
            if vetoed:
                continue
            if score < 55.0:
                continue
            disc = disc_return(r["path"], 8.0, -6.0, 5)
            picks.append((score, r["broad"], r["fwd"], disc, key))
        picks.sort(key=lambda x: -x[0])
        seen, top = set(), []
        for s, b, fwd, disc, key in picks:
            if b in seen:
                continue
            seen.add(b)
            top.append((fwd, disc, key))
            if len(top) >= 5:
                break
        if len(top) < 5:
            continue
        avg = sum(f for f, _, _ in top) / 5
        davg = sum(d for _, d, _ in top) / 5
        per_date.append({"T": T, "avg": avg, "davg": davg,
                         "hit": sum(1 for f, _, _ in top if f > 0),
                         "dhit": sum(1 for _, d, _ in top if d > 0),
                         "exc": avg - F["med"], "med": F["med"]})
        all_rets.extend(f for f, _, _ in top)
        all_disc.extend(d for _, d, _ in top)
        all_excess.append(avg - F["med"])
        hit += per_date[-1]["hit"]
        dhit += per_date[-1]["dhit"]
        for fwd, _, key in top:
            if key and T in ok_days:
                sel_mapped += 1
                if dad_shadow_hit(key, T, senti, ok_days):
                    sel_flag += 1
                    if fwd > 0:
                        sel_flag_hit += 1
    n = len(all_rets)
    return {"ndate": len(per_date), "n": n,
            "avg": sum(all_rets) / n if n else 0,
            "davg": sum(all_disc) / n if n else 0,
            "hit": hit / n if n else 0,
            "dhit": dhit / n if n else 0,
            "exc": sum(all_excess) / len(all_excess) if all_excess else 0,
            "winp": sum(1 for e in all_excess if e > 0),
            "per_date": per_date,
            "trig_mom": trig_mom, "trig_veto": trig_veto,
            "shadow": (sel_flag_hit, sel_flag, sel_mapped)}


def overlap_triggers(kl, tests, senti, ok_days):
    """全映射触发诊断（信号日×12映射码，不过 short_evaluate 门槛）：
    说明情绪信号是否存在，即使没进 v2 候选。"""
    overlap = sorted(set(tests) & ok_days)
    mapped = [c for c in kl if key_of_code(c)]
    tm = tv = 0
    for T in overlap:
        for c in mapped:
            k = key_of_code(c)
            if mom_adj(k, T, senti, ok_days):
                tm += 1
            if dad_veto(k, T, senti, ok_days):
                tv += 1
    return overlap, len(mapped), tm, tv


def stage2(feats, tests, senti, ok_days):
    """四组对照：v2基线 / v2+mom / v2+dad否决 / v2+mom+dad（同 eval_params 口径）。"""
    base = _run_arm(feats, tests, senti, ok_days, "base")
    print("[v2] 基线：信号日%d 裸%+.2f%% 纪律%+.2f%% 超额%+.2f%% 正超额%d/%d" % (
        base["ndate"], base["avg"], base["davg"], base["exc"],
        base["winp"], base["ndate"]), flush=True)
    arms = {"v2+mom": _run_arm(feats, tests, senti, ok_days, "mom"),
            "v2+dad否决": _run_arm(feats, tests, senti, ok_days, "dad"),
            "v2+mom+dad": _run_arm(feats, tests, senti, ok_days, "both")}
    return base, arms


def main():
    senti = load_sentiment()
    ok_days = valid_days(senti)
    ndays = len(ok_days)
    print("情绪样本 %d 天（有效 %d 天）: %s ~ %s" % (
        len(senti), ndays, min(senti) if senti else "-",
        max(senti) if senti else "-"), flush=True)
    kl, fl, names, dates, tests = load_all()
    lines = ["# 🔬 社群情绪×盘面交叉验证（宝妈/宝爸双指数） %s" % datetime.date.today().strftime("%Y-%m-%d"),
             "> 股吧标题信号（guba-title-only），SOCIAL_SECTOR_MAP 聚合主码 join sector_kline；"
             "mom=散户过热扣分（禁追涨），dad=经验共识否决+影子记录",
             "> 情绪样本 %d 天（有效 %d 天，帖≥30且覆盖≥4key）" % (len(senti), ndays), ""]
    if ndays < MIN_DAYS_STAGE2:
        s1 = stage1(senti, kl, ok_days)
        lines += ["## 阶段1描述性研究（样本不足30天，不判晋升）",
                  "- mom-未来5日收益 Spearman IC = **%s**（n=%d）" % (s1["ic_mom"], s1["n"]),
                  "- dad-未来5日收益 Spearman IC = **%s**（n=%d）" % (s1["ic_dad"], s1["n"]),
                  "- mom 情绪桶未来5日平均：high≥12位 %s%%(n=%d) / mid %s%%(n=%d) / low %s%%(n=%d)" % (
                      s1["buckets"]["high"]["avg"], s1["buckets"]["high"]["n"],
                      s1["buckets"]["mid"]["avg"], s1["buckets"]["mid"]["n"],
                      s1["buckets"]["low"]["avg"], s1["buckets"]["low"]["n"]),
                  "- 口径说明：high≥12 / mid 7.5~12 / low<7.5（按 mom 分布 med/p90 定）；"
                  "日常积累满30有效天后自动转阶段2四组对照。"]
        print("阶段1: IC_mom=%s IC_dad=%s n=%d" % (s1["ic_mom"], s1["ic_dad"], s1["n"]), flush=True)
    else:
        feats = build_features(kl, fl, names, tests)
        base, arms = stage2(feats, tests, senti, ok_days)
        # 诊断：情绪窗口与回测窗口的重叠度（重叠不足时结论只能算摸底）
        overlap, nmapped, tm_all, tv_all = overlap_triggers(kl, tests, senti, ok_days)
        s1 = stage1(senti, kl, ok_days)
        lines += ["## 阶段2四组对照（v2基线 / v2+mom / v2+dad否决 / v2+mom+dad）",
                  "- 双IC（全有效天）：mom IC=%s / dad IC=%s（n=%d）" % (
                      s1["ic_mom"], s1["ic_dad"], s1["n"]),
                  "- v2 基线：裸%+.2f%% 纪律%+.2f%% 超额%+.2f%% 正超额%d/%d" % (
                      base["avg"], base["davg"], base["exc"], base["winp"], base["ndate"])]
        best = ("v2基线", base["exc"], base["winp"])
        for name, a in arms.items():
            sh, sf, sm = a["shadow"]
            lines.append("- %s：裸%+.2f%% 纪律%+.2f%% 超额%+.2f%% 正超额%d/%d"
                         "（mom触发%d/veto否决%d/选中映射%d个·dad看多%d个命中%d个）" % (
                             name, a["avg"], a["davg"], a["exc"], a["winp"], a["ndate"],
                             a["trig_mom"], a["trig_veto"], sm, sf, sh))
            if a["exc"] > best[1] and a["winp"] >= best[2]:
                best = (name, a["exc"], a["winp"])
            print("阶段2 %s: exc=%+.2f%% winp=%d/%d" % (name, a["exc"], a["winp"], a["ndate"]), flush=True)
        lines += ["- 诊断：信号日 %d 个（%s~%s），与有效情绪天重叠 %d 个；"
                  "全映射触发（信号日×%d映射码）：mom扣分%d次/veto否决%d次" % (
                      len(tests), tests[0], tests[-1], len(overlap), nmapped, tm_all, tv_all),
                  "- 结论：%s（影子只记录不决策，加分口子不开）" % (
                      "%s有增量" % best[0] if best[0] != "v2基线"
                      else "暂无增量，不进 config（仅观察）")]
    os.makedirs(REPORT_ROOT, exist_ok=True)
    rp = os.path.join(REPORT_ROOT, "情绪交叉验证_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("报告:%s" % rp)


if __name__ == "__main__":
    main()
