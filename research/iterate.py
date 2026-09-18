# -*- coding: utf-8 -*-
"""自动迭代机：规则/ML/集成坐标下降，训-验-留三切分，费率+空仓门，达标才停。

达标线（holdout测试集）：费后净超额 ≥ +1.5% 且命中 ≥ 50% 且交易 ≥ 6个信号日。
费率（C类典型，待与券商确认）：持有<7天赎1.5%，7~29天赎0.5%，≥30天0。
空仓门：信号日全市场近5日中位数 < gate_thr → 空仓记0。
切分：训练(≤06-30) / 验证(07-01~07-31) / 留存(≥08-01，调参全程不可见)。
ML用日期充分统计量（XtX/Xty预聚合），每次拟合=求和+解33阶方程，毫秒级。
坐标下降：从v2出发逐维试参，验证集提升>0.05才保留，最多3轮/80次评估，全记日志。

用法: python iterate.py   # 面板构建数分钟，搜索约十几分钟；日志 reports/迭代日志_YYYYMMDD.md
纯标准库。
"""
import datetime
import os
import sys

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DATA_DIR)
for _p in (DATA_DIR, REPO_ROOT,
           os.path.join(REPO_ROOT, "src", "common"),
           os.path.join(REPO_ROOT, "src", "jobs_build")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
REPORT_DIR = os.path.join(REPO_ROOT, "reports")
import train_recommend as TR  # noqa: E402
from train_recommend import BASE_CAPS, WIDE_CAPS, broad_of, mean  # noqa: E402
from src.jobs_build.daily_1430 import EXCLUDE_KW, short_evaluate  # noqa: E402
from src.common.fees import disc_fee, fee  # noqa: E402

TARGET_EXC = 1.5
TARGET_HIT = 0.50
TARGET_N = 6
MIN_COVER = 4       # 验证集最少交易天数，否则视为退化拒收（防空仓门作弊）
BUDGET_EVALS = 80
LOG_PATH = os.path.join(REPORT_DIR, "迭代日志_%s.md" % datetime.date.today().strftime("%Y%m%d"))

BASE = {"kind": "rule", "caps": BASE_CAPS, "mom_peak": 4.0, "w": None, "cutoff": 55.0,
        "topn": 5, "hold": 5, "tp": 8.0, "sl": -6.0, "gate": None,
        "ml_lam": 10.0, "ml_feats": "all", "ml_label": 5}

FEAT_SETS = {
    "all": list(range(len(TR.FEAT_NAMES))),
    "price_flow_reg": [j for j, n in enumerate(TR.FEAT_NAMES) if j < 21],
    "price_flow": [j for j, n in enumerate(TR.FEAT_NAMES) if j < 11 or n == "has_vol"],
    "price": list(range(8)),
    "novol": [j for j, n in enumerate(TR.FEAT_NAMES)
              if n not in ("has_vol", "z_vr", "z_ar", "atr_r", "range_pos",
                           "body", "upshadow", "vpcorr")],
}


class MLEngine:
    """日期充分统计量引擎：给定训练窗，毫秒拟合任意(λ,特征集,标签)。"""

    def __init__(self, rows):
        self.bydate = {}
        for r in rows:
            self.bydate.setdefault(r["date"], []).append(r)
        self.dates = sorted(self.bydate)

    def fit_stats(self, end_date):
        """用≤end_date的行算标准化+按日聚合。返回(mu, sd, perdate, med5, med10)。"""
        use_rows = [r for d in self.dates if d <= end_date for r in self.bydate[d]]
        p = len(TR.FEAT_NAMES)
        mu = [mean([r["f"][j] for r in use_rows]) for j in range(p)]
        sd = [TR.stdev([r["f"][j] for r in use_rows]) or 1.0 for j in range(p)]
        perdate = {}
        for d in self.dates:
            if d > end_date:
                continue
            dr = self.bydate[d]
            y5 = [r["fwd"] for r in dr]
            y10 = [(r["path"][min(10, len(r["path"]) - 1)] - 1) * 100 if len(r["path"]) > 1 else 0.0
                   for r in dr]
            for tag, yv in (("y5", y5), ("y10", y10)):
                vs = sorted(yv)
                med = vs[len(vs) // 2]
                XtX = [[0.0] * p for _ in range(p)]
                Xty = [0.0] * p
                for r, y in zip(dr, yv):
                    yc = y - med
                    xs = [(r["f"][j] - mu[j]) / sd[j] for j in range(p)]
                    for i in range(p):
                        Xty[i] += xs[i] * yc
                        for j in range(i, p):
                            XtX[i][j] += xs[i] * xs[j]
                for i in range(p):
                    for j in range(i):
                        XtX[i][j] = XtX[j][i]
                perdate.setdefault(d, {})[tag] = (XtX, Xty)
        return mu, sd, perdate

    def fit(self, stats, cutoff, lam, feats_idx, label):
        mu, sd, perdate = stats
        tag = "y5" if label == 5 else "y10"
        p = len(TR.FEAT_NAMES)
        S = [[0.0] * p for _ in range(p)]
        v = [0.0] * p
        for d, dd in perdate.items():
            if d > cutoff:
                continue
            XtX, Xty = dd[tag]
            for i in range(p):
                v[i] += Xty[i]
                for j in range(p):
                    S[i][j] += XtX[i][j]
        m = len(feats_idx)
        As = [[S[feats_idx[i]][feats_idx[j]] + (lam if i == j else 0.0) for j in range(m)]
              for i in range(m)]
        vs = [v[feats_idx[i]] for i in range(m)]
        ws = TR.solve(As, vs)
        w = [0.0] * p
        for k, j in enumerate(feats_idx):
            w[j] = ws[k]
        return w, mu, sd

    @staticmethod
    def predict(rows, w, mu, sd):
        p = len(TR.FEAT_NAMES)
        out = []
        for r in rows:
            xs = [(r["f"][j] - mu[j]) / sd[j] for j in range(p)]
            out.append(sum(a * b for a, b in zip(xs, w)))
        return out


def score_rule(rows, cfg):
    out = {}
    for i, r in enumerate(rows):
        if any(k in r["name"] for k in EXCLUDE_KW):
            continue
        ev = short_evaluate(r["name"], r["c5"], r["acc"], r["fl"], r["chg20"],
                            r["chg_today"], 0, 0, None, r["ri"], False,
                            caps=cfg["caps"], mom_peak=cfg["mom_peak"],
                            w=cfg["w"], cutoff=cfg["cutoff"])
        if ev is not None:
            out[i] = ev["score"]
    return out


def pick_top(rows, scores, topn):
    order = sorted(scores, key=lambda i: -scores[i])
    seen, top = set(), []
    for i in order:
        b = rows[i]["broad"]
        if b in seen:
            continue
        seen.add(b)
        top.append(i)
        if len(top) >= topn:
            break
    return top


def eval_cfg(feats, tdates, cfg, eng, stats, fee_on=True):
    """评估一组参数。ML权重按信号日 embargo 现算（毫秒级）。"""
    excs, hits, n = [], 0, 0
    w_cache = {}
    for T in tdates:
        rows = feats[T]["rows"]
        if cfg.get("gate") is not None and feats[T]["tmed"] < cfg["gate"]:
            continue
        if cfg["kind"] == "rule":
            scores = score_rule(rows, cfg)
        else:
            cutoff = feats["__cut"][T] if "__cut" in feats else T
            key = (cutoff, cfg["ml_lam"], cfg["ml_feats"], cfg["ml_label"])
            if key not in w_cache:
                w_cache[key] = eng.fit(stats, cutoff, cfg["ml_lam"],
                                       FEAT_SETS[cfg["ml_feats"]], cfg["ml_label"])
            w, mu, sd = w_cache[key]
            if cfg["kind"] == "ml":
                eli = [i for i, r in enumerate(rows)
                       if not any(k in r["name"] for k in EXCLUDE_KW)]
            else:  # ensemble：规则过滤取前topn*4再排序
                rs = score_rule(rows, cfg)
                eli = sorted(rs, key=lambda i: -rs[i])[:cfg["topn"] * 4]
                if len(eli) < cfg["topn"]:
                    continue
            preds = ML33.predict(rows, w, mu, sd)
            scores = {i: preds[i] for i in eli}
        top = pick_top(rows, scores, cfg["topn"])
        if len(top) < cfg["topn"]:
            continue
        rets = []
        for i in top:
            p = rows[i]["path"]
            if fee_on:
                rets.append(disc_fee(p, cfg["tp"], cfg["sl"], cfg["hold"]))
            else:
                k = min(cfg["hold"], len(p) - 1)
                rets.append((p[k] - 1) * 100 if k > 0 else 0.0)
        uni = sorted(r["fwd"] for r in rows)
        med = uni[len(uni) // 2]
        excs.append(mean(rets) - med)
        hits += sum(1 for x in rets if x > 0)
        n += len(rets)
    if not excs:
        return 0.0, 0.0, 0
    return mean(excs), hits / n, len(excs)


class ML33:
    @staticmethod
    def predict(rows, w, mu, sd):
        return MLEngine.predict(rows, w, mu, sd)


def trail_median(kl, T):
    rs = []
    for pts in kl.values():
        idx = next((i for i, (d, _) in enumerate(pts) if d > T), len(pts)) - 1
        if idx >= 6 and pts[idx - 5][1]:
            rs.append((pts[idx][1] / pts[idx - 5][1] - 1) * 100)
    rs.sort()
    return rs[len(rs) // 2] if rs else 0.0


def trading_dates(dates, step=5):
    return [d for i, d in enumerate(dates) if i % step == 0]


def split_sig(dates):
    trd = [d for d in dates if d <= "2026-06-22"]
    vad = [d for d in dates if "2026-06-23" <= d <= "2026-07-31"]
    ted = [d for d in dates if d >= "2026-08-01"]
    return trd, vad, ted


def describe(cfg):
    if cfg["kind"] == "rule":
        return "rule caps=%s peak=%g cut=%g top=%d hold=%d tp=%g sl=%g gate=%s" % (
            "wide" if cfg["caps"] == WIDE_CAPS else "base", cfg["mom_peak"], cfg["cutoff"],
            cfg["topn"], cfg["hold"], cfg["tp"], cfg["sl"], cfg["gate"])
    return "%s feats=%s label=%d lam=%g top=%d hold=%d tp=%g sl=%g gate=%s" % (
        cfg["kind"], cfg["ml_feats"], cfg["ml_label"], cfg["ml_lam"], cfg["topn"],
        cfg["hold"], cfg["tp"], cfg["sl"], cfg["gate"])


def main():
    logf = open(LOG_PATH, "w", encoding="utf-8")
    logf.write("# 🔁 自动迭代日志 %s\n\n达标线：留存集费后净超额≥+%.1f%%且命中≥%.0f%%且交易≥%d个信号日\n"
               % (datetime.date.today().strftime("%Y-%m-%d"), TARGET_EXC,
                  TARGET_HIT * 100, TARGET_N))
    logf.write("费率：<7天1.5%% / 7~29天0.5%% / ≥30天0。切分：训练≤06-22 / 验证06-23~07-31(步3加密) / 留存≥08-01。\n")
    logf.write("防过拟合：候选验证交易<%d天直接拒收；提升>0.05才保留。\n\n" % MIN_COVER)
    kl, fl, names, turn, marg, hs, oh = TR.load_all()
    print("面板构建中...", flush=True)
    rows = TR.build_panel(kl, fl, names, turn, marg, hs, oh)
    feats = {}
    for r in rows:
        feats.setdefault(r["date"], {"rows": [], "tmed": 0.0})["rows"].append(r)
    for T in feats:
        uni = sorted(r["fwd"] for r in feats[T]["rows"])
        feats[T]["tmed"] = trail_median(kl, T)
    dates = sorted(feats)
    trd, vad, ted = split_sig(dates)
    sig_tr = trading_dates(trd, 5)
    sig_va = trading_dates(vad, 3)  # 验证集加密（有重叠，仅用于选参，终判看留存）
    sig_te = trading_dates(ted, 5)
    logf.write("面板%d行；信号日训练%d/验证%d/留存%d。\n\n" % (len(rows), len(sig_tr), len(sig_va), len(sig_te)))
    print("面板%d行；信号日 %d/%d/%d" % (len(rows), len(sig_tr), len(sig_va), len(sig_te)), flush=True)

    eng = MLEngine([r for d in trd for r in feats[d]["rows"]])
    stats_tr = eng.fit_stats("2026-06-22")
    # embargo：验证信号日T只用≤T-10的训练统计 → feats挂cut表
    cutmap = {}
    for T in sig_va:
        cands = [d for d in trd if d <= dates[max(0, dates.index(T) - 10)]]
        cutmap[T] = cands[-1] if cands else trd[0]
    feats["__cut"] = cutmap

    cur = dict(BASE)
    best = eval_cfg(feats, sig_va, cur, eng, stats_tr)
    nevals = 1
    print("起点 v2验证：超额%+.2f%% 命中%.0f%% %d天" % best, flush=True)
    logf.write("起点 v2：验证集净超额%+.2f%% 命中%.0f%% 交易%d天。\n\n" % best)

    def key_of(cfg):
        return (cfg["kind"], round(cfg.get("mom_peak", 0), 1), cfg.get("cutoff"),
                cfg.get("topn"), cfg.get("hold"), cfg.get("tp"), cfg.get("sl"),
                cfg.get("gate"), cfg.get("ml_feats"), cfg.get("ml_lam"),
                cfg.get("ml_label"), str(cfg.get("caps"))[:8], str(cfg.get("w"))[:20])

    seen = {key_of(cur)}
    DIMS_RULE = [("mom_peak", [3.0, 4.0, 5.0]), ("cutoff", [50.0, 55.0, 60.0, 65.0]),
                 ("topn", [3, 5, 8]), ("caps", [BASE_CAPS, WIDE_CAPS]),
                 ("hold", [5, 8, 10, 20, 30]), ("tp", [5.0, 8.0, 10.0, 12.0]),
                 ("sl", [-4.0, -6.0, -8.0, -10.0]), ("gate", [None, -1.0, -2.0, -3.0])]
    DIMS_ML = [("ml_feats", ["all", "price_flow_reg", "price_flow", "price", "novol"]),
               ("ml_lam", [1.0, 10.0, 100.0, 1000.0]), ("ml_label", [5, 10]),
               ("topn", [3, 5, 8]), ("hold", [5, 10, 20, 30]),
               ("tp", [5.0, 8.0, 12.0]), ("sl", [-6.0, -8.0, -10.0]),
               ("gate", [None, -2.0])]

    for rnd in range(3):
        improved = False
        for kind, dims in (("rule", DIMS_RULE), ("ml", DIMS_ML), ("ensemble", DIMS_ML)):
            for dim, vals in dims:
                for v in vals:
                    if nevals >= BUDGET_EVALS:
                        break
                    cand = dict(cur)
                    cand["kind"] = kind
                    cand[dim] = v
                    if key_of(cand) in seen:
                        continue
                    seen.add(key_of(cand))
                    r = eval_cfg(feats, sig_va, cand, eng, stats_tr)
                    nevals += 1
                    tag = "★" if (r[0] > best[0] + 0.05 and r[2] >= MIN_COVER) else ""
                    logf.write("%s valid %s → 超额%+.2f%% 命中%.0f%% %d天\n" % (
                        tag, describe(cand), r[0], r[1] * 100, r[2]))
                    if r[0] > best[0] + 0.05 and r[2] >= MIN_COVER:
                        cur, best, improved = cand, r, True
                        print("  ↑ %s 超额%+.2f%%" % (describe(cand), r[0]), flush=True)
                if nevals >= BUDGET_EVALS:
                    break
            if nevals >= BUDGET_EVALS:
                break
        logf.write("第%d轮结束：%s 验证超额%+.2f%%。\n" % (rnd + 1, describe(cur), best[0]))
        if not improved:
            break

    # 留存终验：统计窗扩到≤07-31重算
    eng2 = MLEngine([r for d in trd + vad for r in feats[d]["rows"]])
    stats_te = eng2.fit_stats("2026-07-31")
    cutmap2 = {}
    for T in sig_te:
        cands = [d for d in trd + vad if d <= dates[max(0, dates.index(T) - 10)]]
        cutmap2[T] = cands[-1] if cands else trd[0]
    feats["__cut"] = cutmap2
    fin = eval_cfg(feats, sig_te, cur, eng2, stats_te)
    ok = fin[0] >= TARGET_EXC and fin[1] >= TARGET_HIT and fin[2] >= TARGET_N
    logf.write("\n## 留存终验\n%s\n费后净超额%+.2f%% 命中%.0f%% 交易%d天 → **%s**\n" % (
        describe(cur), fin[0], fin[1] * 100, fin[2], "达标🎉" if ok else "未达标"))
    logf.close()
    print("冠军：%s\n留存：超额%+.2f%% 命中%.0f%% %d天 → %s" % (
        describe(cur), fin[0], fin[1] * 100, fin[2], "达标" if ok else "未达标"))
    print("日志:%s 评估%d次" % (LOG_PATH, nevals))
    return cur, ok


if __name__ == "__main__":
    main()
