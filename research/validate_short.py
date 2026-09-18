# -*- coding: utf-8 -*-
"""短期算法历史回放验证 + 调参（与线上同一套 short_evaluate，无未来函数）。

回放口径（T=信号日，收盘后视角）：
  5日%/加速度/当日% ← sector_kline 收盘（与线上同源）
  20日% ← K线实算（历史 sector_daily 只存了9天，不可用，用K线代替）
  涨跌家数/流通市值 ← 历史无存档，取中性（广度5分/跳过市值门槛）
  资金 ← sector_flow_daily 上≤T近6日（与线上一致）
  趋势信号 ← scan_regime.analyze(截至T的收盘序列) 逐日重算，无未来数据
  窗口：每5个交易日一个信号日，持有5天，天然无重叠

用法:
  python validate_short.py              # v2 vs base 写报告
  python validate_short.py --exp NAME   # 单组实验
  python validate_short.py --exp all    # 全部实验对比
  python validate_short.py --tune       # 网格搜索（训练/测试分组防过拟合）
纯标准库。
"""
import datetime
import itertools
import os
import sys

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DATA_DIR)
for _p in (DATA_DIR, REPO_ROOT,
           os.path.join(REPO_ROOT, "src", "common"),
           os.path.join(REPO_ROOT, "src", "jobs_build"),
           os.path.join(REPO_ROOT, "src", "jobs_fetch")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from src.common import history_db  # noqa: E402
from src.jobs_build import scan_regime as SR  # noqa: E402
from src.jobs_build.daily_1430 import broad_of, chg5_accel, short_evaluate  # noqa: E402
REPORT_DIR = os.path.join(REPO_ROOT, "reports")

TEST_START = "2026-04-10"
STEP = 5                   # 每5个交易日一信号日（持有5天，无重叠）
FWD = 5

BASE_CAPS = (-10, 12, -3, 8, -4, 6)
WIDE_CAPS = (-12, 20, -4, 12, -5, 8)

EXPS = {
    "base":   {"note": "v1线上旧版", "caps": BASE_CAPS, "mom_peak": 3.0, "tp": 5.0, "sl": -8.0, "hold": 8},
    "v2":     {"note": "v2线上版：峰值4+持有5天/止盈8/止损6", "caps": BASE_CAPS, "mom_peak": 4.0,
               "tp": 8.0, "sl": -6.0, "hold": 5},
}

W_GRID = {
    "w_base":   (30, 15, 25, 20, 10),
    "w_mom":    (40, 15, 20, 15, 10),
    "w_flow":   (25, 10, 35, 20, 10),
    "w_sig":    (25, 10, 20, 35, 10),
    "w_acc":    (25, 25, 20, 20, 10),
    "w_bal":    (20, 20, 20, 20, 20),
    "w_noflow": (35, 20, 5, 30, 10),
}


def disc_return(closes, tp, sl, hold):
    """收盘触发近似（无OHLCV，实盘盘中触发只会更早）。"""
    c0 = closes[0]
    for c in closes[1:hold + 1]:
        r = (c / c0 - 1) * 100
        if r <= sl:
            return sl
        if r >= tp:
            return tp
    return (closes[hold] / c0 - 1) * 100


def trail_median(kl, T):
    rs = []
    for pts in kl.values():
        idx = next((i for i, (d, _) in enumerate(pts) if d > T), len(pts)) - 1
        if idx >= 6 and pts[idx - 5][1]:
            rs.append((pts[idx][1] / pts[idx - 5][1] - 1) * 100)
    rs.sort()
    return rs[len(rs) // 2] if rs else 0.0


def load_all():
    conn = history_db.connect()
    try:
        krows = conn.execute(
            "SELECT code, date, close FROM sector_kline ORDER BY code, date").fetchall()
        frows = conn.execute(
            "SELECT code, date, main_net_wan FROM sector_flow_daily ORDER BY code, date").fetchall()
        names = dict(conn.execute(
            "SELECT code, name FROM sector_daily WHERE date=(SELECT MAX(date) FROM sector_daily)"))
    finally:
        conn.close()
    kl, fl = {}, {}
    for code, d, c in krows:
        kl.setdefault(code, []).append((d, c))
    for code, d, v in frows:
        fl.setdefault(code, []).append((d, v or 0))
    dates = sorted({d for _, d, _ in krows})
    tests = [d for i, d in enumerate(dates)
             if d >= TEST_START and i % STEP == 0 and dates.index(d) + 8 < len(dates)]
    return kl, fl, names, dates, tests


def build_features(kl, fl, names, tests):
    """每个(T, code)算一次特征 + 趋势重算 + forward。返回 feats[T] = [dict...]。"""
    feats = {}
    for ti, T in enumerate(tests):
        rows = []
        uni = []
        for code, pts in kl.items():
            idx = next((i for i, (d, _) in enumerate(pts) if d > T), len(pts)) - 1
            if idx < 125 or len(pts) <= idx + 8:
                continue
            past_pairs = pts[:idx + 1]
            past = [c for _, c in past_pairs]
            fwd = (pts[idx + FWD][1] / pts[idx][1] - 1) * 100
            uni.append(fwd)
            c5, acc = chg5_accel(past_pairs[-12:] if len(past_pairs) >= 12 else past_pairs)
            chg_today = (past[-1] / past[-2] - 1) * 100
            chg20 = (past[-1] / past[-21] - 1) * 100 if len(past) >= 21 else 0
            fpts = [(d, v) for d, v in fl.get(code, []) if d <= T][-6:]
            if not fpts:
                continue
            consec = 0
            for _, v in reversed(fpts):
                if v > 0:
                    consec += 1
                else:
                    break
            a = SR.analyze(past)
            ri = ({"regime": a["regime"], "bottom_score": a["bottom_score"],
                   "engine": a["engine"], "alert": a["alert"],
                   "cross_age": a["cross_age"]} if a else {})
            nm = names.get(code, code)
            rows.append({"code": code, "name": nm, "broad": broad_of(nm),
                         "c5": c5, "acc": acc, "chg_today": chg_today, "chg20": chg20,
                         "fl": {"latest": fpts[-1][1], "consec": consec,
                                "sum5": sum(v for _, v in fpts[-5:])},
                         "ri": ri, "fwd": fwd,
                         "path": [p[1] for p in pts[idx:idx + 9]]})
        uni.sort()
        feats[T] = {"rows": rows, "med": uni[len(uni) // 2] if uni else 0,
                    "tmed": trail_median(kl, T)}
        print("  特征 %d/%d %s 候选%d" % (ti + 1, len(tests), T, len(rows)), flush=True)
    return feats


def eval_params(feats, tests, caps, mom_peak, w, cutoff, topn, tp, sl, hold):
    """给定参数在指定信号日上跑分。返回(date_rows, summary)。"""
    per_date, all_rets, all_disc, all_excess = [], [], [], []
    hit = dhit = 0
    for T in tests:
        F = feats[T]
        picks = []
        for r in F["rows"]:
            ev = short_evaluate(r["name"], r["c5"], r["acc"], r["fl"], r["chg20"],
                                r["chg_today"], 0, 0, None, r["ri"], False,
                                caps=caps, mom_peak=mom_peak, w=w, cutoff=cutoff)
            if ev is None:
                continue
            disc = disc_return(r["path"], tp, sl, hold)
            picks.append((ev["score"], r["broad"], r["fwd"], disc))
        picks.sort(key=lambda x: -x[0])
        seen, top = set(), []
        for s, b, fwd, disc in picks:
            if b in seen:
                continue
            seen.add(b)
            top.append((fwd, disc))
            if len(top) >= topn:
                break
        if len(top) < topn:
            continue
        avg = sum(f for f, _ in top) / topn
        davg = sum(d for _, d in top) / topn
        per_date.append({"T": T, "avg": avg, "davg": davg,
                         "hit": sum(1 for f, _ in top if f > 0),
                         "dhit": sum(1 for _, d in top if d > 0),
                         "exc": avg - F["med"], "med": F["med"], "tmed": F["tmed"]})
        all_rets.extend(f for f, _ in top)
        all_disc.extend(d for _, d in top)
        all_excess.append(avg - F["med"])
        hit += per_date[-1]["hit"]
        dhit += per_date[-1]["dhit"]
    n = len(all_rets)
    return per_date, {"ndate": len(per_date), "n": n,
                      "avg": sum(all_rets) / n if n else 0,
                      "davg": sum(all_disc) / n if n else 0,
                      "hit": hit / n if n else 0,
                      "dhit": dhit / n if n else 0,
                      "exc": sum(all_excess) / len(all_excess) if all_excess else 0,
                      "winp": sum(1 for e in all_excess if e > 0)}


def run_exp(feats, tests, exp):
    cfg = EXPS[exp]
    per_date, s = eval_params(feats, tests, cfg["caps"], cfg["mom_peak"], None,
                              55.0, 5, cfg["tp"], cfg["sl"], cfg["hold"])
    s.update({"exp": exp, "note": cfg["note"], "per_date": per_date})
    print("[%s] %s：信号日%d 裸%+.2f%%(命中%.0f%%) 纪律%+.2f%% 超额%+.2f%% 正超额%d/%d" % (
        exp, cfg["note"], s["ndate"], s["avg"], s["hit"] * 100,
        s["davg"], s["exc"], s["winp"], s["ndate"]), flush=True)
    return s


def tune(feats, tests):
    """网格搜索：前半训练选参，后半测试验证。"""
    cut = len(tests) // 2
    train, test = tests[:cut], tests[cut:]
    print("训练 %d (%s~%s) / 测试 %d (%s~%s)" % (
        len(train), train[0], train[-1], len(test), test[0], test[-1]), flush=True)
    grid = list(itertools.product(W_GRID, [3.0, 4.0, 5.0], [50.0, 55.0, 60.0],
                                  [BASE_CAPS, WIDE_CAPS], [3, 5]))
    scored = []
    for wn, peak, cutv, caps, topn in grid:
        _, s = eval_params(feats, train, caps, peak, W_GRID[wn], cutv, topn,
                           8.0, -6.0, 5)
        if s["ndate"] >= len(train) - 2:
            scored.append((s["exc"], wn, peak, cutv,
                           "wide" if caps == WIDE_CAPS else "base", topn, s))
    scored.sort(key=lambda x: -x[0])
    print("\n== 训练Top10 → 测试验证 ==")
    print("%-8s %-4s %-6s %-4s %-3s | 训练超额 | 测试超额 | 测试裸/纪律/命中" % (
        "权重", "峰值", "截断", "上限", "Top"))
    results = []
    for exc_tr, wn, peak, cutv, capn, topn, _ in scored[:10]:
        caps = WIDE_CAPS if capn == "wide" else BASE_CAPS
        _, s_te = eval_params(feats, test, caps, peak, W_GRID[wn], cutv, topn,
                              8.0, -6.0, 5)
        _, s_tr = eval_params(feats, train, caps, peak, W_GRID[wn], cutv, topn,
                              8.0, -6.0, 5)
        print("%-8s %-4g %-6g %-4s %-3d | %+.2f%%(%d) | %+.2f%%(%d) | %+.2f/%+.2f/%.0f%%" % (
            wn, peak, cutv, capn, topn, s_tr["exc"], s_tr["ndate"],
            s_te["exc"], s_te["ndate"], s_te["avg"], s_te["davg"], s_te["hit"] * 100))
        results.append((wn, peak, cutv, capn, topn, s_tr, s_te))
    return results, train, test


def main():
    args = sys.argv[1:]
    kl, fl, names, dates, tests = load_all()
    print("信号日 %d个: %s ~ %s" % (len(tests), tests[0], tests[-1]), flush=True)
    if "--tune" in args:
        feats = build_features(kl, fl, names, tests)
        tune(feats, tests)
        return
    feats = build_features(kl, fl, names, tests)
    if "--exp" in args and args[args.index("--exp") + 1] != "all":
        run_exp(feats, tests, args[args.index("--exp") + 1])
        return
    if "--exp" in args:
        for exp in EXPS:
            run_exp(feats, tests, exp)
        return
    # 默认：v2 vs base 写报告
    b = run_exp(feats, tests, "base")
    s = run_exp(feats, tests, "v2")
    lines = ["# 🔬 短期算法回放验证 %s（v2 线上版，无重叠窗口）"
             % datetime.date.today().strftime("%Y-%m-%d"),
             "> 同一套 short_evaluate，无未来函数；20日%用K线实算、广度中性；每5交易日一信号日+持有5天",
             "> v2=动量峰值4、持有5天、止盈+8%/止损-6%（base=峰值3、持有8天、+5%/-8%）",
             "",
             "| 信号日 | 市况5日 | Top5裸5日 | 命中 | 纪律 | 纪律命中 | 超额 | 中位数 |",
             "|---|---|---|---|---|---|---|---|"]
    for p in s["per_date"]:
        mark = "✅" if p["exc"] > 0 else "❌"
        lines.append("| %s | %+.1f%% | %+.2f%% | %d/5 | %+.2f%% | %d/5 | %s%+.2f%% | %+.2f%% |" % (
            p["T"], p["tmed"], p["avg"], p["hit"], p["davg"], p["dhit"], mark, p["exc"], p["med"]))
    lines += ["",
              "## 汇总（%d个信号日，%d只Top股）" % (s["ndate"], s["n"]),
              "- Top5裸5日平均 **%+.2f%%**，命中率 **%.0f%%**" % (s["avg"], s["hit"] * 100),
              "- 纪律平均 **%+.2f%%**，命中率 **%.0f%%**" % (s["davg"], s["dhit"] * 100),
              "- 平均超额 **%+.2f%%**，正超额 **%d/%d**" % (s["exc"], s["winp"], s["ndate"]),
              "- base 对照：裸%+.2f%%/纪律%+.2f%%/超额%+.2f%%/正超额%d/%d" % (
                  b["avg"], b["davg"], b["exc"], b["winp"], b["ndate"])]
    os.makedirs(REPORT_DIR, exist_ok=True)
    rp = os.path.join(REPORT_DIR, "短期算法回放验证_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("报告:%s" % rp)


if __name__ == "__main__":
    main()
