# -*- coding: utf-8 -*-
"""推荐模型训练（纯标准库手搓 Ridge + 滚动回测）。

面板：sector_kline(25-06起) + sector_flow(03-30起) + regime序列重算
      + market_turnover/margin_balance + kline_full沪深300。
特征约25个，目标=未来5日收益。评估：Spearman IC + TopN组合 vs 中位数，
同场对比规则版 short_evaluate（v2线上参数）。滚动回测：训练只用T-10之前，
标签 embargo，无未来函数。

用法: python train_recommend.py   # 约5~8分钟，报告写 reports/推荐模型训练报告_YYYYMMDD.md
"""
import csv
import datetime
import math
import os
import sqlite3
import sys

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DATA_DIR)
for _p in (DATA_DIR, REPO_ROOT,
           os.path.join(REPO_ROOT, "src", "common"),
           os.path.join(REPO_ROOT, "src", "jobs_build"),
           os.path.join(REPO_ROOT, "src", "jobs_fetch")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from src.common.paths import DATA_ROOT
from src.common import history_db  # noqa: E402
from src.jobs_build import scan_regime as SR  # noqa: E402
from src.jobs_build.daily_1430 import EXCLUDE_KW, broad_of, short_evaluate  # noqa: E402
REPORT_DIR = os.path.join(REPO_ROOT, "reports")

PANEL_START = "2026-05-04"  # 资金sum20需20个flow日(03-30起)
FWD = 5
BASE_CAPS = (-10, 12, -3, 8, -4, 6)
WIDE_CAPS = (-12, 20, -4, 12, -5, 8)
XS_NEUTRAL = True  # 截面中性化：y减当日前 median，剔除市场特征（只学截面排序）
FEAT_NAMES = ["ret5", "ret10", "ret20", "accel", "vol20", "dist_ma20", "dist_ma60",
              "dd60", "z_sum5", "z_sum20", "consec", "bscore", "cdist", "cwidth",
              "xage", "bull", "a_gold", "a_bot", "a_fade", "a_fall", "a_trend",
              "turn_r", "margin_chg20", "hs_ret20", "hs_dist60",
              "z_vratio", "z_aratio", "atr_r", "range_pos", "body", "upshadow",
              "vpcorr", "has_vol"]


# ---------- 标准库线性代数 ----------
def solve(A, b):
    """高斯-约旦解 Ax=b。"""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        pv = M[c][c] or 1e-12
        M[c] = [v / pv for v in M[c]]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c]
                M[r] = [a - f * x for a, x in zip(M[r], M[c])]
    return [row[n] for row in M]


def ridge_fit(X, y, lam):
    p = len(X[0])
    XtX = [[0.0] * p for _ in range(p)]
    Xty = [0.0] * p
    for xi, yi in zip(X, y):
        for i in range(p):
            Xty[i] += xi[i] * yi
            for j in range(i, p):
                XtX[i][j] += xi[i] * xi[j]
    for i in range(p):
        for j in range(i):
            XtX[i][j] = XtX[j][i]
        XtX[i][i] += lam
    return solve(XtX, Xty)


def ranks(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(a, b):
    n = len(a)
    if n < 3:
        return 0.0
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    sa = sum((x - ma) ** 2 for x in ra)
    sb = sum((x - mb) ** 2 for x in rb)
    if not sa or not sb:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / math.sqrt(sa * sb)


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


# ---------- 数据 ----------
def load_all():
    conn = history_db.connect(readonly=True)
    try:
        krows = conn.execute(
            "SELECT code, date, close FROM sector_kline ORDER BY code, date").fetchall()
        frows = conn.execute(
            "SELECT code, date, main_net_wan FROM sector_flow_daily ORDER BY code, date").fetchall()
        names = dict(conn.execute(
            "SELECT code, name FROM sector_daily WHERE date=(SELECT MAX(date) FROM sector_daily)"))
        turn = {d: t for d, _, _, t in
                conn.execute("SELECT date, sh_amount, sz_amount, total FROM market_turnover")}
        marg = {d: m for d, m in conn.execute("SELECT date, rzrq_ye FROM margin_balance")}
        oh = {}
        try:
            for code, d, o, h, l, c, v, a in conn.execute(
                    "SELECT code, date, open, high, low, close, vol, amt FROM sector_ohlcv"):
                oh.setdefault(code, {})[d] = (o, h, l, c, v, a)
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    kl, fl = {}, {}
    for code, d, c in krows:
        kl.setdefault(code, []).append((d, c))
    for code, d, v in frows:
        fl.setdefault(code, []).append((d, v or 0))
    hs = {}
    with open(os.path.join(DATA_ROOT, "kline_full", "沪深300.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ks = list(r.values())
            try:
                hs[ks[0]] = float(ks[1])
            except (ValueError, IndexError):
                pass
    return kl, fl, names, turn, marg, hs, oh


def ma(vals, i, n):
    w = vals[max(0, i - n + 1):i + 1]
    return sum(w) / len(w)


def build_panel(kl, fl, names, turn, marg, hs, oh):
    """逐板块算序列特征。返回 rows: list of dict(含date,code,name,f[],fwd)。"""
    dates = sorted({d for pts in kl.values() for d, _ in pts})
    tdates = sorted({d for d in turn})
    mdates = sorted({d for d in marg})
    hsdates = sorted(hs)
    # 嘉实：指数序列
    series = {}
    for ci, code in enumerate(sorted(kl)):
        closes = [c for _, c in kl[code]]
        series[code] = SR.analyze_series(closes)
        if (ci + 1) % 100 == 0:
            print("  趋势序列 %d/%d" % (ci + 1, len(kl)), flush=True)
    dates = sorted({d for pts in kl.values() for d, _ in pts})
    rows = []
    for di, T in enumerate(dates):
        if T < PANEL_START:
            continue
        if dates.index(T) + FWD >= len(dates):
            break
        # 当日 cross z（资金 + 量比）
        s5_all, s20_all, vr_all, ar_all = [], [], [], []
        fcache = {}
        for code, pts in kl.items():
            idx = next((i for i, (d, _) in enumerate(pts) if d > T), len(pts)) - 1
            if idx < 0:
                continue
            fpts = [(d, v) for d, v in fl.get(code, []) if d <= T][-20:]
            fcache[code] = (idx, fpts)
            if len(fpts) >= 5:
                s5_all.append(sum(v for _, v in fpts[-5:]))
            if len(fpts) >= 20:
                s20_all.append(sum(v for _, v in fpts[-20:]))
            od = oh.get(code, {})
            op = [(d, od[d]) for d, _ in pts[:idx + 1] if d in od][-21:]
            if len(op) >= 20:
                vols = [v[1][4] for v in op]
                amts = [v[1][5] for v in op]
                mv20, ma20 = mean(vols[-20:]), mean(amts[-20:])
                if mv20:
                    vr_all.append(vols[-1] / mv20)
                if ma20:
                    ar_all.append(amts[-1] / ma20)
        m5, sd5 = mean(s5_all), stdev(s5_all) or 1.0
        m20, sd20 = mean(s20_all), stdev(s20_all) or 1.0
        mvr, sdvr = mean(vr_all), stdev(vr_all) or 1.0
        mar, sdar = mean(ar_all), stdev(ar_all) or 1.0
        # 市场特征
        ti = max([i for i, d in enumerate(tdates) if d <= T], default=None)
        turn_r = (turn[tdates[ti]] / mean([turn[d] for d in tdates[max(0, ti - 59):ti + 1]])) \
            if ti is not None and ti >= 5 else 1.0
        mi = max([i for i, d in enumerate(mdates) if d <= T], default=None)
        margin_chg = ((marg[mdates[mi]] / marg[mdates[max(0, mi - 20)]] - 1) * 100) \
            if mi is not None and mi >= 20 else 0.0
        hi = max([i for i, d in enumerate(hsdates) if d <= T], default=None)
        if hi is not None and hi >= 60:
            hc = [hs[d] for d in hsdates[hi - 60:hi + 1]]
            hs_ret20 = (hc[-1] / hc[-21] - 1) * 100
            hs_dist60 = (hc[-1] / (sum(hc) / len(hc)) - 1) * 100
        else:
            hs_ret20, hs_dist60 = 0.0, 0.0
        for code, pts in kl.items():
            if code not in fcache:
                continue
            idx, fpts = fcache[code]
            if len(fpts) < 20 or idx < 65 or idx + FWD >= len(pts):
                continue
            closes = [c for _, c in pts[:idx + 1]]
            c0 = closes[-1]
            r1 = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
            ret5 = (c0 / closes[-6] - 1) * 100
            ret10 = (c0 / closes[-11] - 1) * 100
            ret20 = (c0 / closes[-21] - 1) * 100
            prev5 = (closes[-6] / closes[-11] - 1) * 100
            vol20 = stdev(r1[-20:]) * math.sqrt(20)
            dist_ma20 = (c0 / ma(closes, len(closes) - 1, 20) - 1) * 100
            dist_ma60 = (c0 / ma(closes, len(closes) - 1, 60) - 1) * 100
            dd60 = (c0 / max(closes[-60:]) - 1) * 100
            s5 = sum(v for _, v in fpts[-5:])
            s20 = sum(v for _, v in fpts[-20:])
            consec = 0
            for _, v in reversed(fpts[-6:]):
                if v > 0:
                    consec += 1
                else:
                    break
            sg = series[code][idx] or {}
            alert = sg.get("alert", "—")
            # 量价特征（无OHLCV时中性0 + has_vol=0）
            od = oh.get(code, {})
            op = [(d, od[d]) for d, _ in pts[:idx + 1] if d in od][-21:]
            if len(op) >= 20:
                vols = [v[1][4] for v in op]
                amts = [v[1][5] for v in op]
                mv20, ma20 = mean(vols[-20:]), mean(amts[-20:])
                z_vr = (vols[-1] / mv20 - mvr) / sdvr if mv20 else 0.0
                z_ar = (amts[-1] / ma20 - mar) / sdar if ma20 else 0.0
                trs = []
                for k in range(max(1, len(op) - 14), len(op)):
                    o, h, l, c = op[k][1][:4]
                    pc = op[k - 1][1][3]
                    trs.append(max(h - l, abs(h - pc), abs(l - pc)))
                atr_r = (mean(trs) / op[-1][1][3] * 100) if op[-1][1][3] else 0.0
                o, h, l, c = op[-1][1][:4]
                rng = (h - l) or 1e-9
                range_pos = (c - l) / rng
                body = (c - o) / rng
                upshadow = (h - max(o, c)) / rng
                vr = [(vols[i] / vols[i - 1] - 1) for i in range(1, len(vols))]
                rr = [((op[i][1][3] / op[i - 1][1][3]) - 1) for i in range(1, len(op))]
                n2 = min(len(vr), len(rr), 20)
                mv_, mr_ = mean(vr[-n2:]), mean(rr[-n2:])
                cov = sum((a - mv_) * (b - mr_) for a, b in zip(vr[-n2:], rr[-n2:]))
                den = math.sqrt(sum((a - mv_) ** 2 for a in vr[-n2:]) *
                                sum((b - mr_) ** 2 for b in rr[-n2:])) or 1e-9
                vpcorr = cov / den
                has_vol = 1.0
            else:
                z_vr = z_ar = atr_r = range_pos = body = upshadow = vpcorr = 0.0
                has_vol = 0.0
            f = [ret5, ret10, ret20, ret5 - prev5, vol20, dist_ma20, dist_ma60, dd60,
                 (s5 - m5) / sd5, (s20 - m20) / sd20, min(consec, 6),
                 (sg.get("bottom_score", 0) or 0) / 100,
                 max(-5.0, min(5.0, sg.get("cloud_dist", 0) or 0)),
                 sg.get("cloud_width", 0) or 0,
                 min(sg.get("cross_age", 60) or 60, 60) / 60,
                 1.0 if sg.get("regime") == "BULL" else 0.0,
                 1.0 if alert == "GOLDEN_X" else 0.0,
                 1.0 if alert == "BOTTOM_WATCH" else 0.0,
                 1.0 if alert == "RALLY_FADE" else 0.0,
                 1.0 if alert == "FREE_FALL" else 0.0,
                 1.0 if alert == "TREND_LONG" else 0.0,
                 turn_r, margin_chg, hs_ret20, hs_dist60,
                 z_vr, z_ar, atr_r, range_pos, body, upshadow, vpcorr, has_vol]
            fwd = (pts[idx + FWD][1] / c0 - 1) * 100
            nm = names.get(code, code)
            rows.append({"date": T, "code": code, "name": nm, "broad": broad_of(nm),
                         "f": f, "fwd": fwd, "ri": sg,
                         "c5": ret5, "acc": ret5 - prev5, "chg20": ret20,
                         "chg_today": (c0 / closes[-2] - 1) * 100,
                         "fl": {"latest": fpts[-1][1], "consec": min(consec, 6),
                                "sum5": sum(v for _, v in fpts[-5:])},
                         "path": [p[1] / c0 for p in pts[idx + 1:idx + 31]]})
        if (di + 1) % 30 == 0:
            print("  面板 %s 行累计%d" % (T, len(rows)), flush=True)
    return rows


# ---------- 训练评估 ----------
def fit_predict(train_rows, valid_rows, lam):
    use = [i for i, n in enumerate(FEAT_NAMES)
           if not (XS_NEUTRAL and n in ("turn_r", "margin_chg20", "hs_ret20", "hs_dist60"))]
    dmed = {}
    if XS_NEUTRAL:
        by = {}
        for r in train_rows:
            by.setdefault(r["date"], []).append(r["fwd"])
        for d, vs in by.items():
            vs = sorted(vs)
            dmed[d] = vs[len(vs) // 2]
    mu = [mean([r["f"][j] for r in train_rows]) for j in use]
    sd = [stdev([r["f"][j] for r in train_rows]) or 1.0 for j in use]
    y = [(r["fwd"] - dmed.get(r["date"], 0.0)) for r in train_rows]
    ym = 0.0 if XS_NEUTRAL else mean(y)
    y = [v - ym for v in y]
    X = [[(r["f"][j] - mu[k]) / sd[k] for k, j in enumerate(use)] for r in train_rows]
    w = ridge_fit(X, y, lam)
    wfull = [0.0] * len(FEAT_NAMES)
    for k, j in enumerate(use):
        wfull[j] = w[k]
    pv = []
    for r in valid_rows:
        xs = [(r["f"][j] - mu[k]) / sd[k] for k, j in enumerate(use)]
        pv.append(sum(a * b for a, b in zip(xs, w)) + ym)
    return pv, wfull


def topn_stats(rows, preds, topn):
    order = sorted(range(len(rows)), key=lambda i: -preds[i])
    seen, top = set(), []
    for i in order:
        b = rows[i]["broad"]
        if b in seen or any(k in rows[i]["name"] for k in EXCLUDE_KW):
            continue
        seen.add(b)
        top.append(rows[i]["fwd"])
        if len(top) >= topn:
            break
    if len(top) < topn:
        return None
    return {"avg": mean(top), "hit": sum(1 for x in top if x > 0) / topn}


def main():
    kl, fl, names, turn, marg, hs, oh = load_all()
    print("面板构建中...", flush=True)
    rows = build_panel(kl, fl, names, turn, marg, hs, oh)
    bydate = {}
    for r in rows:
        bydate.setdefault(r["date"], []).append(r)
    dates = sorted(bydate)
    print("面板: %d行, %d个交易日 %s~%s" % (len(rows), len(dates), dates[0], dates[-1]), flush=True)

    # 规则版 flow 还原：build_panel 没存，补一列（供同场对比）
    # 注：short_evaluate 需要 fl latest/consec/sum5 —— 用上面相同的 fcache 逻辑重算太贵，
    # 这里给规则版用“中性资金”（consec=2/sum5>0/latest>0），偏向规则版，不亏待它。
    tests = [d for i, d in enumerate(dates) if i % 4 == 0 and dates.index(d) + 1 < len(dates)]
    tests = [d for d in tests if d >= "2026-06-22"]
    print("滚动测试日 %d个" % len(tests), flush=True)

    rep = ["# 🤖 推荐模型训练报告 %s" % datetime.date.today().strftime("%Y-%m-%d"),
           "> Ridge(25特征，纯手搓)+滚动回测；训练只用T-10之前；规则版同场同口径（v2参数，资金中性偏向它）",
           "", "| 测试日 | 模型Top5 | 命中 | 规则Top5 | 命中 | 中位数 | 模型IC(池内) |",
           "|---|---|---|---|---|---|---|"]
    m_hits = r_hits = 0
    m_avgs, r_avgs, m_exc, r_exc, ics = [], [], [], [], []
    feat_imp = {}
    for T in tests:
        train = [r for r in rows if r["date"] < T and r["date"] <= dates[dates.index(T) - 10]]
        if len(train) < 3000:
            continue
        # λ内层选择：训练尾部20%日期做验证
        tds = sorted({r["date"] for r in train})
        cut = tds[int(len(tds) * 0.8)]
        fit_r = [r for r in train if r["date"] < cut]
        val_r = [r for r in train if r["date"] >= cut]
        best_lam, best_ic = 10.0, -1e9
        for lam in (1.0, 10.0, 100.0, 1000.0):
            pv, _ = fit_predict(fit_r, val_r, lam)
            ic = spearman(pv, [r["fwd"] for r in val_r])
            if ic > best_ic:
                best_ic, best_lam = ic, lam
        day_rows = bydate[T]
        pv, w = fit_predict(train, day_rows, best_lam)
        ics.append(spearman(pv, [r["fwd"] for r in day_rows]))
        for j, wn in enumerate(FEAT_NAMES):
            feat_imp.setdefault(wn, []).append(abs(w[j]))
        ms = topn_stats(day_rows, pv, 5)
        # 规则版：资金中性（consec=2恒15分），偏向它
        rscored = []
        for i, r in enumerate(day_rows):
            if any(k in r["name"] for k in EXCLUDE_KW):
                continue
            ev = short_evaluate(r["name"], r["c5"], None,
                                {"latest": 1, "consec": 2, "sum5": 1},
                                r["chg20"], r["chg_today"], 0, 0, None, r["ri"], False,
                                mom_peak=4.0)
            if ev is None:
                continue
            rscored.append((ev["score"], r["fwd"], r["broad"]))
        rscored.sort(key=lambda x: -x[0])
        seen, rt = set(), []
        for s, fwd, b in rscored:
            if b in seen:
                continue
            seen.add(b)
            rt.append(fwd)
            if len(rt) >= 5:
                break
        uni = sorted(r["fwd"] for r in day_rows)
        med = uni[len(uni) // 2]
        if ms is None or len(rt) < 5:
            continue
        m_avgs.append(ms["avg"])
        r_avgs.append(mean(rt))
        m_hits += ms["hit"] * 5
        r_hits += sum(1 for x in rt if x > 0)
        m_exc.append(ms["avg"] - med)
        r_exc.append(mean(rt) - med)
        mark = "✅" if ms["avg"] > mean(rt) else "❌"
        rep.append("| %s | %+.2f%% | %d/5 | %+.2f%% | %d/5 | %+.2f%% | %+.3f |" % (
            T, ms["avg"], round(ms["hit"] * 5), mean(rt), sum(1 for x in rt if x > 0),
            med, ics[-1]))
        print("  %s λ=%.1f 模型%+.2f%% 规则%+.2f%% %s" % (
            T, best_lam, ms["avg"], mean(rt), mark), flush=True)

    n = len(m_avgs)
    imp = sorted(((wn, mean(v)) for wn, v in feat_imp.items()), key=lambda x: -x[1])
    rep += ["",
            "## 汇总（%d个测试日）" % n,
            "- 模型Top5平均 **%+.2f%%**，命中 **%.0f%%**，超额 **%+.2f%%**" % (
                mean(m_avgs), m_hits / (5 * n) * 100 if n else 0, mean(m_exc)),
            "- 规则Top5平均 **%+.2f%%**，命中 **%.0f%%**，超额 **%+.2f%%**" % (
                mean(r_avgs), r_hits / (5 * n) * 100 if n else 0, mean(r_exc)),
            "- 模型池内IC均值 **%+.3f**（>0.03才算有信号）" % mean(ics),
            "",
            "## 特征重要性（|w|均值）",
            " ".join("%s%.2f" % (k, v) for k, v in imp[:10]),
            "",
            "## 结论"]
    verdict = ""
    if n and mean(m_exc) > mean(r_exc) + 1.0 and mean(ics) > 0.03:
        verdict = "模型显著优于规则版且IC达标 → 建议上线（权重见下方，可蒸馏进daily_1430）。"
    else:
        verdict = "模型未显著优于规则版（或IC未达标）→ 保持规则v2，模型继续攒样本再战。"
    rep.append("- " + verdict)
    out = "\n".join(rep)
    print(out)
    os.makedirs(REPORT_DIR, exist_ok=True)
    rp = os.path.join(REPORT_DIR, "推荐模型训练报告_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    with open(rp, "w", encoding="utf-8") as f:
        f.write(out)
    print("报告:%s" % rp)


if __name__ == "__main__":
    main()
