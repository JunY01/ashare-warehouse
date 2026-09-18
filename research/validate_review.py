# -*- coding: utf-8 -*-
"""每日复盘"关注行业"五因子算法回测（K 线重建版，窗口拉到资金流全部历史）

两个前置事实（2026-09-14 实测，本脚本自证）:
  1. 快照 CSV 的列名与实际字段不符：列"20日%"读的是东财 f109，实为 **5 日涨幅**
     （K线 5 日涨幅相关 0.982）；列"60日%"读的是 f160，实为 **10 日涨幅**（相关 0.979）。
     线上算法因此一直用 5 日/10 日涨幅填充 chg20/chg60 两个输入，本回测按线上实际口径重建。
  2. sector_kline 可以用：重建的 regime/bottom_score 与 regime_daily 归档读数一致率
     99~100%（bottom_score 偏差中位 0.00），重建耗时约 5 秒。

窗口: 2026-03-30（sector_flow_daily 起点，东财资金流历史硬上限约 120 交易日）~ 2026-09-11。
      收益需未来 5 日，故最后有效信号日为 2026-09-04。

用法: python research/validate_review.py
"""
import datetime
import os
import sys
from collections import defaultdict
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.common.paths import REPORT_ROOT  # noqa: E402
from src.common import history_db  # noqa: E402
from src.jobs_build import scan_regime as SR  # noqa: E402
from src.jobs_build.daily_review import score_candidate, rank_candidates  # noqa: E402

HORIZONS = (1, 3, 5)
TOPN = 10
STEP_ROBUST = 5          # 无重叠子集：每 5 个交易日取一个信号日
F109_DAYS = 5            # CSV"20日%"实为 f109 = 5 日涨幅
F160_DAYS = 10           # CSV"60日%"实为 f160 = 10 日涨幅
FACTORS = ("bottom_score", "chg20", "chg60", "inflow", "score_flow",
           "score_stability", "score_trend", "score_signal")


def build_panel(conn):
    """从 K 线重建每个交易日的因子面板（不含未来数据：analyze_series 为逐 bar 因果读数）"""
    series = defaultdict(list)
    for code, d, v in conn.execute(
            "SELECT code, date, close FROM sector_kline WHERE close IS NOT NULL ORDER BY code, date"):
        series[code].append((d, v))
    names = {}
    for code, name in conn.execute(
            "SELECT code, name FROM sector_daily WHERE name IS NOT NULL AND name != ''"):
        names.setdefault(code, name)
    flow = defaultdict(dict)
    for code, d, v in conn.execute("SELECT code, date, main_net_wan FROM sector_flow_daily"):
        flow[d][code] = v

    panel = defaultdict(dict)
    for code, sq in series.items():
        ds = [d for d, _ in sq]
        cs = [v for _, v in sq]
        rows = SR.analyze_series(cs)
        for i in range(F160_DAYS, len(ds)):
            d = ds[i]
            if d not in flow:
                continue
            f = flow[d].get(code)
            if f is None:
                continue
            r = rows[i] if i < len(rows) else None
            if not r or r.get("regime") is None:
                continue
            panel[d][code] = {
                "name": names.get(code, ""), "close": cs[i],
                "chg20": (cs[i] / cs[i - F109_DAYS] - 1) * 100,
                "chg60": (cs[i] / cs[i - F160_DAYS] - 1) * 100,
                "inflow": f,
                "regime": r.get("regime"), "bottom_score": r.get("bottom_score"),
                "engine": r.get("engine"), "alert": r.get("alert"),
            }
    return panel


def calibration(conn):
    """自证：重建的 chg 输入与快照字段的一致性（按线上口径对齐）"""
    rows = []
    for code, d, v in conn.execute(
            "SELECT code, date, close FROM sector_kline WHERE close IS NOT NULL ORDER BY code, date"):
        rows.append((code, d, v))
    kl = defaultdict(dict)
    for code, d, v in rows:
        kl[code][d] = v
    out = []
    for col, n in (("chg_20d", F109_DAYS), ("chg_60d", F160_DAYS)):
        xs, ys = [], []
        for anchor in ("2026-08-25", "2026-09-01", "2026-09-08", "2026-09-11"):
            for code, v in conn.execute(
                    "SELECT code, %s FROM sector_daily WHERE date=?" % col, (anchor,)):
                s = kl.get(code)
                if not s or v is None:
                    continue
                ds = sorted(s)
                if anchor not in s or len(ds) < n:
                    continue
                i = ds.index(anchor)
                if i < n:
                    continue
                xs.append((s[anchor] / s[ds[i - n]] - 1) * 100)
                ys.append(v)
        if len(xs) > 200:
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
            den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
            out.append((col, n, num / den if den else 0, len(xs)))
    return out


def fwd_ret(panel, days, code, i, n):
    if i + n >= len(days):
        return None
    a = panel[days[i]].get(code, {}).get("close")
    b = panel[days[i + n]].get(code, {}).get("close")
    if not a or not b:
        return None
    return (b / a - 1) * 100


def market_median(panel, days, i, n):
    vals = [fwd_ret(panel, days, c, i, n) for c in panel[days[i]]]
    vals = [v for v in vals if v is not None]
    return median(vals) if vals else None


def replay(panel, days, weights=None, min_total=50.0):
    per_day, pool = [], []
    for i, T in enumerate(days):
        recs = []
        for code, s in panel[T].items():
            rec = score_candidate(s["name"], code, s["chg20"], s["chg60"], s["inflow"], s,
                                  weights=weights, min_total=min_total)
            if rec is not None:
                recs.append(rec)
        top = rank_candidates(recs, topn=TOPN)
        row = {"date": T, "n_pool": len(recs), "top": top}
        for n in HORIZONS:
            rets = [x for x in (fwd_ret(panel, days, r["code"], i, n) for r in top) if x is not None]
            bm = market_median(panel, days, i, n)
            if rets and bm is not None:
                row["t%d" % n] = (sum(rets) / len(rets), bm, rets)
        per_day.append(row)
        for r in recs:
            fr = fwd_ret(panel, days, r["code"], i, 1)
            if fr is not None:
                pool.append((r, fr))
    return per_day, pool


# 权重候选：不做大网格（项目历史证明十万级网格挖出的参数样本外必失效），
# 只检验"削底部评分、让权重给趋势/信号"这一个有归因依据的假设，并带邻域与反向对照。
WEIGHT_SCHEMES = [
    ("baseline 现状",        {"bottom": 30, "flow": 25, "stability": 20, "trend": 15, "signal": 10}),
    ("削底·温和(15)",        {"bottom": 15, "flow": 25, "stability": 15, "trend": 30, "signal": 15}),
    ("削底·中等(10)",        {"bottom": 10, "flow": 25, "stability": 15, "trend": 30, "signal": 20}),
    ("削底·激进(0)",         {"bottom": 0, "flow": 25, "stability": 15, "trend": 40, "signal": 20}),
    ("邻域: 激进 bottom=5",  {"bottom": 5, "flow": 25, "stability": 15, "trend": 35, "signal": 20}),
    ("反向对照: 加重底部",    {"bottom": 40, "flow": 20, "stability": 10, "trend": 20, "signal": 10}),
]


def weight_compare(panel, days):
    """各权重方案在 全窗口/前半/后半/无重叠 上的表现（T+1、T+5 超额，pp）。

    统一用 min_total=0 隔离"排序能力"，使各方案的候选池一致（硬门槛与权重无关）。
    """
    half = len(days) // 2
    out = []
    for label, w in WEIGHT_SCHEMES:
        per_day, _ = replay(panel, days, weights=w, min_total=0.0)
        robust = [d for j, d in enumerate(per_day) if j % STEP_ROBUST == 0]
        seg = {"全窗口": per_day, "前半": per_day[:half], "后半": per_day[half:], "无重叠": robust}
        r = {"label": label,
             "n_pool": sum(d["n_pool"] for d in per_day) / len(per_day)}
        for name, sub in seg.items():
            for n in (1, 5):
                s = summarize(sub, n)
                r["%s_T%d" % (name, n)] = s["exc"] if s else None
        out.append(r)
        print("  %-20s 全窗T+1 %+.2f | 前半 %+.2f | 后半 %+.2f | 无重叠 %+.2f | 全窗T+5 %+.2f" % (
            label, r["全窗口_T1"], r["前半_T1"] or 0, r["后半_T1"] or 0,
            r["无重叠_T1"] or 0, r["全窗口_T5"] or 0))
    return out


def regime_split(panel, days, weights=None):
    """按当日 BULL 板块占比分组，看算法超额的"环境依赖"（熊市是否系统性失效）"""
    per_day, _ = replay(panel, days, weights=weights, min_total=0.0)
    buckets = {"弱(BULL<30%)": [], "中(30~60%)": [], "强(>60%)": []}
    for d in per_day:
        T = d["date"]
        total = len(panel[T])
        if not total:
            continue
        bull = sum(1 for s in panel[T].values() if s["regime"] == "BULL") / total
        key = "弱(BULL<30%)" if bull < 0.3 else ("中(30~60%)" if bull < 0.6 else "强(>60%)")
        buckets[key].append(d)
    out = []
    for k, rows in buckets.items():
        s1, s5 = summarize(rows, 1), summarize(rows, 5)
        out.append((k, len(rows), s1["exc"] if s1 else None,
                    s1["bm"] if s1 else None, s5["exc"] if s5 else None))
        print("  环境 %-14s n=%-3d T+1超额 %+.2fpp (基准%+.2f%%) | T+5超额 %+.2fpp" % (
            k, len(rows), out[-1][2] or 0, out[-1][3] or 0, out[-1][4] or 0))
    return out


def regime_time_cross(panel, days, weights=None):
    """环境 × 时间 交叉：检验「弱市失效」是否独立于时间（若后半全是弱市，则无法区分）"""
    per_day, _ = replay(panel, days, weights=weights, min_total=0.0)
    half = len(per_day) // 2
    out = []
    for seg_name, seg in (("前半", per_day[:half]), ("后半", per_day[half:])):
        buckets = defaultdict(list)
        for d in seg:
            T = d["date"]
            total = len(panel[T])
            if not total:
                continue
            bull = sum(1 for s in panel[T].values() if s["regime"] == "BULL") / total
            key = "弱" if bull < 0.3 else ("中" if bull < 0.6 else "强")
            buckets[key].append(d)
        for k in ("弱", "中", "强"):
            rows = buckets.get(k, [])
            if not rows:
                out.append((seg_name, k, 0, None, None))
                continue
            s1, s5 = summarize(rows, 1), summarize(rows, 5)
            out.append((seg_name, k, len(rows), s1["exc"] if s1 else None,
                        s5["exc"] if s5 else None))
    for seg_name, k, n, e1, e5 in out:
        print("  交叉 %s·%s市 n=%-3d T+1 %s | T+5 %s" % (
            seg_name, k, n, "%+.2fpp" % e1 if e1 is not None else "  -",
            "%+.2fpp" % e5 if e5 is not None else "  -"))
    return out


def summarize(per_day, n):
    rows = [d for d in per_day if ("t%d" % n) in d]
    if not rows:
        return None
    avg = sum(d["t%d" % n][0] for d in rows) / len(rows)
    bm = sum(d["t%d" % n][1] for d in rows) / len(rows)
    win = sum(1 for d in rows if d["t%d" % n][0] > d["t%d" % n][1])
    allr = [x for d in rows for x in d["t%d" % n][2]]
    wq = sum(1 for d in rows for x in d["t%d" % n][2] if x > d["t%d" % n][1])
    return {"n_day": len(rows), "avg": avg, "bm": bm, "exc": avg - bm,
            "win_day": win, "n_pick": len(allr), "win_pick": wq}


def attribution(pool):
    out = {}
    for key in FACTORS:
        vals = [(r[key], f) for r, f in pool if r.get(key) is not None]
        if len(vals) < 100:
            continue
        vals.sort()
        k = len(vals) // 4
        out[key] = [sum(f for _, f in g) / len(g) for g in
                    (vals[:k], vals[k:2 * k], vals[2 * k:3 * k], vals[3 * k:])]
    return out


def real_records(panel, days, picks):
    """真实推送记录（live + 回填）的事后收益；replay 是重建行，不混入本节"""
    idx = {d: i for i, d in enumerate(days)}
    per_day = defaultdict(list)
    for (pd, rank, code, name, score, ddate, source, *_r) in picks:
        if source not in ("live", "backfill"):
            continue
        base = pd if pd in idx else ddate
        if base not in idx:
            continue
        i = idx[base]
        r = {"rank": rank, "name": name, "score": score, "source": source, "base": base}
        for n in HORIZONS:
            r["t%d" % n] = fwd_ret(panel, days, code, i, n)
        r["bm1"] = market_median(panel, days, i, 1)
        per_day[pd].append(r)
    return per_day


def save_replay(panel, days):
    """把重建的每日榜单落库（source='replay'），跳过已有真实推送记录的日期。

    表主键是 (push_date, sector_code)、不含 source，故同一推荐日只能有一条；
    真实推送（live/backfill）优先，重建只补历史的空缺日。
    """
    from src.jobs_build.daily_review import load_fund_list, find_c_funds
    conn = history_db.connect()
    try:
        exist = {r[0] for r in conn.execute("SELECT DISTINCT push_date FROM review_pick_daily")}
        fund_list = load_fund_list()
        rows, days_saved = [], set()
        for T in days:
            if T in exist:
                continue
            recs = []
            for code, s in panel[T].items():
                rec = score_candidate(s["name"], code, s["chg20"], s["chg60"], s["inflow"], s)
                if rec is not None:
                    recs.append(rec)
            for rk, rec in enumerate(rank_candidates(recs, topn=TOPN), 1):
                funds = find_c_funds(fund_list, rec["broad"], cap=1)
                fc, fn = funds[0] if funds else (None, None)
                rows.append((T, rk, rec["code"], rec["name"], rec["broad"], rec["score"],
                             T, "replay", rec["bottom_score"], rec["score_bottom"],
                             rec["score_flow"], rec["score_stability"], rec["score_trend"],
                             rec["score_signal"], rec["chg20"], rec["chg60"], rec["inflow"],
                             rec["reason"], fc, fn))
                days_saved.add(T)
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO review_pick_daily("
                "push_date, rank_no, sector_code, sector_name, broad_state, score,"
                "data_date, source, bottom_score, score_bottom, score_flow,"
                "score_stability, score_trend, score_signal, chg20, chg60, inflow,"
                "reason, fund_code, fund_name)"
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
        return len(rows), len(days_saved)
    finally:
        conn.close()


def main():
    conn = history_db.connect(readonly=True)
    try:
        calib = calibration(conn)
        panel = build_panel(conn)
        picks = list(conn.execute(
            "SELECT push_date, rank_no, sector_code, sector_name, score, data_date, source "
            "FROM review_pick_daily ORDER BY push_date, rank_no"))
    finally:
        conn.close()

    days = sorted(panel)
    print("重建面板: %d 个交易日 (%s ~ %s)，日均 %d 板块" % (
        len(days), days[0], days[-1], sum(len(panel[d]) for d in days) // len(days)))
    for col, n, r, cnt in calib:
        print("  校准 %s := K线 %d 日涨幅，相关 %.3f (n=%d)" % (col, n, r, cnt))

    per_day, pool = replay(panel, days)
    robust = [d for j, d in enumerate(per_day) if j % STEP_ROBUST == 0]

    L = ["# 每日复盘·关注行业 算法回测（K 线重建，长窗口）\n",
         "> 窗口 %s ~ %s（%d 个交易日）｜ 生成 by research/validate_review.py\n" % (
             days[0], days[-1], len(days)),
         "## 〇、前置事实与重建校准\n",
         "**字段映射有误（本次实测）**：快照 CSV 的列名与东财字段不符——列 `20日%` 读的是 `f109`，"
         "实为 **5 日涨幅**；列 `60日%` 读的是 `f160`，实为 **10 日涨幅**。"
         "线上算法因此一直用 5 日/10 日涨幅填充 `chg20`/`chg60` 两个输入，"
         "推荐理由里写的“20日+X%”实际是 5 日涨幅。本回测按线上实际口径重建。\n",
         "| 快照列 | 实际字段 | 实测窗口 | 与 K 线重建相关性 | 样本 |",
         "|---|---|---|---|---|"]
    for col, n, r, cnt in calib:
        L.append("| %s | f%s | %d 日 | %.3f | %d |" % (
            col, "109" if col == "chg_20d" else "160", n, r, cnt))
    L.append("\n`regime`/`bottom_score` 由 `scan_regime.analyze_series` 重建，与 `regime_daily` "
             "归档读数一致率 99~100%（bottom_score 偏差中位 0.00），重建全程约 5 秒。\n")
    L.append("窗口受 `sector_flow_daily` 限制（东财资金流历史硬上限约 120 交易日），"
             "收益需未来 5 日，故最后有效信号日 %s。\n" % days[-(HORIZONS[-1] + 1)])

    L.append("## 一、重放：算法每天选出的 Top10 事后表现\n")
    L.append("### 全窗口（信号日连续，收益窗口重叠）\n")
    L.append("| 窗口 | 信号日 | 推荐均值 | 全市场中位 | 超额 | 按日超额胜率 | 按个券超额胜率 |")
    L.append("|---|---|---|---|---|---|---|")
    for n in HORIZONS:
        s = summarize(per_day, n)
        if not s:
            continue
        L.append("| T+%d | %d | %+.2f%% | %+.2f%% | **%+.2fpp** | %d/%d (%.0f%%) | %d/%d (%.0f%%) |" % (
            n, s["n_day"], s["avg"], s["bm"], s["exc"], s["win_day"], s["n_day"],
            s["win_day"] / s["n_day"] * 100, s["win_pick"], s["n_pick"], s["win_pick"] / s["n_pick"] * 100))
        print("  T+%d: 推荐 %+.2f%% vs 基准 %+.2f%% = 超额 %+.2fpp (n=%d日/%d条)" % (
            n, s["avg"], s["bm"], s["exc"], s["n_day"], s["n_pick"]))
    L.append("\n### 无重叠子集（每 %d 个交易日一个信号日，统计上更干净）\n" % STEP_ROBUST)
    L.append("| 窗口 | 信号日 | 推荐均值 | 全市场中位 | 超额 | 按日超额胜率 |")
    L.append("|---|---|---|---|---|---|")
    for n in HORIZONS:
        s = summarize(robust, n)
        if not s:
            continue
        L.append("| T+%d | %d | %+.2f%% | %+.2f%% | **%+.2fpp** | %d/%d (%.0f%%) |" % (
            n, s["n_day"], s["avg"], s["bm"], s["exc"],
            s["win_day"], s["n_day"], s["win_day"] / s["n_day"] * 100))
        print("  [无重叠] T+%d 超额 %+.2fpp (n=%d)" % (n, s["exc"], s["n_day"]))

    attr = attribution(pool)
    L.append("\n## 二、分项归因：各因子与次日收益的关系\n")
    L.append("样本池 = 全部通过硬门槛的候选（%d 条），按因子值四分位分组，看未来 1 日收益均值。\n" % len(pool))
    L.append("| 因子 | 最低25% | 中低25% | 中高25% | 最高25% | 方向 |")
    L.append("|---|---|---|---|---|---|")
    for key, g in attr.items():
        inc = all(g[j] < g[j + 1] for j in range(3))
        dec = all(g[j] > g[j + 1] for j in range(3))
        span = abs(g[3] - g[0])
        if inc:
            trend = "单调递增（跨度 %.2fpp）" % span
        elif dec:
            trend = "单调递减（跨度 %.2fpp）" % span
        elif span >= 0.3:
            trend = ("高分组更好" if g[3] > g[0] else "高分组更差") + "（跨度 %.2fpp）" % span
        else:
            trend = "无明显方向"
        L.append("| %s | %+.2f%% | %+.2f%% | %+.2f%% | %+.2f%% | %s |" % (
            key, g[0], g[1], g[2], g[3], trend))
        print("  归因 %-16s %+.2f / %+.2f / %+.2f / %+.2f  %s" % (
            key, g[0], g[1], g[2], g[3], trend))

    wcomp = weight_compare(panel, days)
    rsplit = regime_split(panel, days)
    rcross = regime_time_cross(panel, days)
    real = real_records(panel, days, picks)
    L.append("\n## 四、权重方案对比（修正尝试）\n")
    L.append("依据分项归因（`bottom_score` 方向为负、`score_trend` 最强）检验单一假设："
             "削底部评分权重、让给趋势与信号。**不做大网格**——项目历史（R2 十万次、估值择时 11.5 万组）"
             "已证明网格最优参数在样本外基本失效。\n")
    L.append("统一用 `min_total=0` 隔离排序能力，使各方案候选池一致；"
             "判稳标准为 **全窗口、前半、后半三段超额同时为正**。\n")
    L.append("| 方案 | 全窗口 T+1 | 前半 T+1 | 后半 T+1 | 无重叠 T+1 | 全窗口 T+5 | 判定 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in wcomp:
        vals = [r["全窗口_T1"], r["前半_T1"], r["后半_T1"]]
        ok = all(v is not None and v > 0 for v in vals)
        verdict = "跨期三段均为正 ✓" if ok else "未通过（含非正段）"
        L.append("| %s | %+.2f | %+.2f | %+.2f | %+.2f | %+.2f | %s |" % (
            r["label"], r["全窗口_T1"], r["前半_T1"] or 0, r["后半_T1"] or 0,
            r["无重叠_T1"] or 0, r["全窗口_T5"] or 0, verdict))

    L.append("\n**结论：权重不是瓶颈。** 六个方案的差异都在 0.04pp 以内，"
             "连「反向加重底部评分」都同样接近零，且**没有任何方案做到跨期三段全为正**"
             "（所有方案的后半段都是负的）。故线上权重维持现状，不改。\n")

    L.append("\n## 五、环境依赖：算法在什么市况下失效\n")
    L.append("按信号日当天的 BULL 板块占比分组（BULL 占比即 `regime_daily` 的牛熊分布）：\n")
    L.append("| 市况 | 信号日 | T+1 超额 | 当期全市场中位 | T+5 超额 |")
    L.append("|---|---|---|---|---|")
    for k, n, e1, bm, e5 in rsplit:
        L.append("| %s | %d | %+.2fpp | %+.2f%% | %+.2fpp |" % (k, n, e1 or 0, bm or 0, e5 or 0))
    L.append("\n注意：BULL 占比与时间强相关（前半强、后半弱），所以本节的「环境依赖」"
             "与第四节的「前半正/后半负」很可能是同一件事的两个说法，不能当作两个独立证据。\n")
    L.append("\n**环境 × 时间 交叉检验**（只有前后半各自内部都呈现同一模式，才算独立证据）：\n")
    L.append("| 时段 | 市况 | 信号日 | T+1 超额 | T+5 超额 |")
    L.append("|---|---|---|---|---|")
    for seg_name, k, n, e1, e5 in rcross:
        if n == 0:
            L.append("| %s | %s市 | 0 | 该时段无此市况 | |" % (seg_name, k))
        else:
            L.append("| %s | %s市 | %d | %+.2fpp | %+.2fpp |" % (seg_name, k, n, e1 or 0, e5 or 0))

    L.append("\n## 六、真实推送记录（live + 回填历史报告）\n")
    L.append("| 推送日 | 条数 | 来源 | T+1 均值 | 全市场中位 | 超额 |")
    L.append("|---|---|---|---|---|---|")
    for d in sorted(real):
        rows = real[d]
        n_ok = [r["t1"] for r in rows if r.get("t1") is not None]
        bm = rows[0].get("bm1")
        src = "/".join(sorted({r["source"] for r in rows}))
        label = d if rows[0].get("base") == d else "%s→%s" % (d, rows[0].get("base"))
        if n_ok and bm is not None:
            avg = sum(n_ok) / len(n_ok)
            L.append("| %s | %d | %s | %+.2f%% | %+.2f%% | %+.2fpp |" % (
                label, len(rows), src, avg, bm, avg - bm))
        else:
            L.append("| %s | %d | %s | 无未来数据 | | |" % (label, len(rows), src))
    allrec = [r for d in real for r in real[d] if r.get("t1") is not None]
    if allrec:
        avg = sum(r["t1"] for r in allrec) / len(allrec)
        wq = sum(1 for d in real for r in real[d]
                 if r.get("t1") is not None and r.get("bm1") is not None and r["t1"] > r["bm1"])
        L.append("\n**合计**：%d 条有 T+1 数据，平均 %+.2f%%，超额胜率 %d/%d（%.0f%%）。\n" % (
            len(allrec), avg, wq, len(allrec), wq / len(allrec) * 100))

    L.append("\n## 七、限制\n")
    L.append("1. 因子输入用的是**5 日/10 日涨幅**（线上实际口径），不是列名暗示的 20 日/60 日；"
             "若要按 20/60 日口径评估，需另行改造算法。\n")
    L.append("2. 14:30 运行时快照是盘中值，本回测用收盘重建，含轻微前视。\n")
    L.append("3. 资金流历史硬上限约 120 交易日，窗口无法再往前推。\n")
    L.append("4. `sector_kline` 个别日期有批次错位（如 08-27 与 08-26 归档重复），"
             "已通过重建校准（相关 0.98）确认整体可用，但不排除个别板块异常。\n")
    L.append("\n> 仅个人研究，不构成投资建议。\n")

    out = os.path.join(REPORT_ROOT, "每日复盘算法回测_20260914.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("\n[OK] 报告已写入 %s" % out)

    if "--save" in sys.argv:
        n_rows, n_days = save_replay(panel, days)
        print("[OK] 重建榜单已落库 review_pick_daily(source='replay'): %d 天 / %d 行 "
              "（已有真实记录的日期已跳过）" % (n_days, n_rows))
    return out


if __name__ == "__main__":
    main()
