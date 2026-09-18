# -*- coding: utf-8 -*-
"""R2十万网格迭代（100000组，3年板块历史）

给未来亚亚：千次(sweep1000_r2.py)的放大版，网格从4维扩到7维。
7维 = 窗口20 × TOP5 × 止损5 × 缓冲5 × 市值门4 × 均线对5 × 频率2 = 100000。
切片跑（按窗口分组，断点续跑），结果增量写 sweep100k_results.jsonl，
--finish 时聚合 + TOP20上10年复核 + 邻域稳定否决 + 晋升。
纯标准库。用法:
  python sweep100k_r2.py --ws 0 --wn 4   # 跑第0~3个窗口（共5片：0/4/8/12/16）
  python sweep100k_r2.py --finish         # 聚合选拔+报告+晋升
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
import backtest_r2_momentum as BM
import backtest_r2_index10y as IX
from src.jobs_build import regime_lamp as LAMP
from src.common.paths import SWEEP_CACHE
from src.common.market_map import write_json_atomic

MODEL_PATH = os.path.join(REPO_ROOT, "config", "model_r2.json")  # 晋升写 config/（线上唯一模型目录）
REPORT_DIR = os.path.join(REPO_ROOT, "reports")
RESULTS = os.path.join(SWEEP_CACHE, "sweep100k_results.jsonl")

WINDOWS = [15, 18, 20, 22, 25, 28, 30, 35, 40, 45, 50, 55, 60, 70, 80, 90, 100, 120, 135, 150]  # 20
TOPNS = [1, 2, 3, 4, 5]  # 5
STOPS = [-6.0, -8.0, -10.0, -12.0, -15.0]  # 5
BUFS = [0.0, 0.05, 0.10, 0.20, 0.30]  # 5
CAPS = [0.0, 50.0, 100.0, 200.0]  # 4（0=不过滤）
MAPAIRS = [(10, 30), (20, 60), (20, 120), (30, 90), (40, 120)]  # 5
FREQS = [1, 2]  # 2（1=月度 2=双月）
MIN_MONTHS = 12  # 调仓段<12不排名（3年样本下保证一年以上）
assert len(WINDOWS) * len(TOPNS) * len(STOPS) * len(BUFS) * len(CAPS) * len(MAPAIRS) * len(FREQS) == 100000


def scored_boards(series, vols, caps, cal, px, window, ma_pair, freq):
    """某(窗口,均线对,频率)的每月评分榜。warmup=窗口+慢线+10，无未来函数。"""
    fast, slow = ma_pair
    warmup = window + slow + 10
    months, seen = [], set()
    for d in cal:
        m = d[:7]
        if m not in seen:
            seen.add(m)
            months.append(d)
    months = [m for m in months if cal.index(m) >= warmup][::freq]
    ranked = {}
    for rb in months:
        lst = []
        for code, pts in series.items():
            if caps.get(code, 0) and caps[code] < CAPS[0]:
                pass  # 市值门在组合层动态过滤（榜共用，不过滤）
            idx = next((i for i, (d, _) in enumerate(pts) if d > rb), len(pts)) - 1
            if idx < warmup or pts[idx][0] != rb:
                continue
            cl = [p[1] for p in pts[:idx + 1]]
            mf, ms = BM.ma(cl, fast), BM.ma(cl, slow)
            if mf is None or ms is None or not (cl[-1] > mf > ms):
                continue
            r = BM.wlinreg_score(cl, window)
            if not r:
                continue
            ann, r2, score = r
            if ann <= 0 or score <= 0:
                continue
            vmap = vols.get(code, {})
            if rb in vmap:
                vdates = sorted(d for d in vmap if d <= rb)[-6:]
                if len(vdates) == 6 and all(vmap[d] for d in vdates):
                    avg5 = sum(vmap[d] for d in vdates[:5]) / 5
                    if avg5 and vmap[rb] > BM.VOL_MULT * avg5:
                        continue
            lst.append((score, code))
        lst.sort(reverse=True)
        ranked[rb] = lst
    return months, ranked


def simulate(months, ranked, caps_map, px, cal, topn, stop, buf, min_cap):
    """持仓模拟（含市值门动态过滤/缓冲/止损/空仓切货币）。"""
    equity = [1.0]
    peak, maxdd = 1.0, 0.0
    turns, empty = [], 0
    hold, hs, entry = [], 0.0, {}
    for mi in range(len(months) - 1):
        rb, nxt = months[mi], months[mi + 1]
        # 市值门动态过滤（榜是共用的，不过滤，组合层再筛）
        flt = [(s, c) for s, c in ranked[rb]
               if not (min_cap and caps_map.get(c, 0) and caps_map[c] < min_cap)]
        cand = [c for _, c in flt[:topn]]
        cs = sum(s for s, _ in flt[:topn]) / topn if cand else 0
        if not cand:
            empty += 1
        if hold and cand and cs <= hs * (1 + buf):
            cand, cs = list(hold), hs
        turns.append(len(set(cand) - set(hold)) / topn if topn else 0)
        hold, hs = cand, cs
        entry = {c: px[c][rb] for c in hold if rb in px[c]}
        stopped = set()  # 2026-09-09补：止损后当月剩余按货币，不再复活（复活=免费过滤下跌，印假收益）
        i0, i1 = cal.index(rb), cal.index(nxt)
        for d in cal[i0 + 1:i1 + 1]:
            legs, prev = [], cal[cal.index(d) - 1]
            for c in hold:
                if c in stopped:
                    legs.append(0.0)
                    continue
                if c not in px or d not in px[c] or c not in entry or not entry[c]:
                    continue
                if px[c][d] / entry[c] - 1 <= stop / 100:
                    stopped.add(c)
                    legs.append(0.0)
                    continue
                legs.append(px[c][d] / px[c][prev] - 1 if prev in px[c] else 0.0)
            r = sum(legs) / len(legs) if legs else 0.0
            equity.append(equity[-1] * (1 + r))
        peak = max(peak, equity[-1])
        maxdd = min(maxdd, equity[-1] / peak - 1)
    nseg = len(months) - 1
    seglen = max(1, (len(equity) - 1) // nseg) if nseg else 1
    wins = sum(1 for i in range(nseg)
               if min(len(equity) - 1, 1 + i * seglen + seglen) > 1 + i * seglen
               and equity[min(len(equity) - 1, 1 + i * seglen + seglen)] > equity[i * seglen])
    avgm = (equity[-1]) ** (1 / nseg) - 1 if nseg and equity[-1] > 0 else -1
    return {"total": equity[-1] - 1, "avgm": avgm, "maxdd": maxdd,
            "win": wins / nseg * 100 if nseg else 0,
            "turn": sum(turns) / len(turns) * 100 if turns else 0, "n": nseg, "empty": empty}


def run_windows(ws, wn):
    """跑窗口下标[ws:ws+wn)，结果增量 append 到 jsonl（断点=文件行数）。"""
    series, vols, names, caps = BM.load_data()
    cal = sorted({d for pts in series.values() for d, _ in pts})
    px = {c: dict(pts) for c, pts in series.items()}
    fh = open(RESULTS, "a", encoding="utf-8")
    n = 0
    for w in WINDOWS[ws:ws + wn]:
        for ma_pair in MAPAIRS:
            for freq in FREQS:
                months, ranked = scored_boards(series, vols, caps, cal, px, w, ma_pair, freq)
                if len(months) - 1 < MIN_MONTHS:
                    continue
                for t in TOPNS:
                    for s in STOPS:
                        for b in BUFS:
                            for mc in CAPS:
                                m = simulate(months, ranked, caps, px, cal, t, s, b, mc)
                                fh.write(json.dumps(
                                    {"w": w, "t": t, "s": s, "b": b, "mc": mc,
                                     "ma": list(ma_pair), "f": freq, "m": {
                                         "total": round(m["total"], 4), "avgm": round(m["avgm"], 5),
                                         "maxdd": round(m["maxdd"], 4), "win": round(m["win"], 1),
                                         "turn": round(m["turn"], 1), "n": m["n"]}},
                                    ensure_ascii=False) + "\n")
                                n += 1
        print(" 窗口w%-3d 完成" % w, flush=True)
    fh.close()
    print("本片写入%d组" % n)


def finish():
    rows = [json.loads(l) for l in open(RESULTS, encoding="utf-8") if l.strip()]
    print("共%d组" % len(rows))
    ok = [r for r in rows if r["m"]["maxdd"] >= -0.10 and r["m"]["turn"] <= 50.0]
    ok.sort(key=lambda r: (-r["m"]["avgm"], -r["b"], r["m"]["turn"]))
    print("达标%d组" % len(ok), flush=True)
    checked = []
    for r in ok[:20]:
        ix = IX.run(window=r["w"], topn=min(r["t"], 5), stop=r["s"], buffer=r["b"])
        checked.append((r, ix))
        print(" 复核 w%-3d top%d 年化%+.1f%%" % (r["w"], r["t"], ix["cagr"] * 100), flush=True)
    passed = [c for c in checked if c[1]["cagr"] > 0.08 and c[0]["t"] >= 2]
    # 邻域稳定：冠军各维±1档邻居中位数也达标，否则否决（防单点幻觉）
    champ = passed[0] if passed else None
    stable = False
    if champ:
        r = champ[0]
        grid = {"w": WINDOWS, "t": TOPNS, "s": STOPS, "b": BUFS, "mc": CAPS}
        keys = {"w": r["w"], "t": r["t"], "s": r["s"], "b": r["b"], "mc": r["mc"]}
        bykey = {(x["w"], x["t"], x["s"], x["b"], x["mc"]): x for x in rows}
        nb = []
        for k, vals in grid.items():
            i = vals.index(keys[k])
            for j in (i - 1, i + 1):
                if 0 <= j < len(vals):
                    kk = dict(keys)
                    kk[k] = vals[j]
                    hit = bykey.get((kk["w"], kk["t"], kk["s"], kk["b"], kk["mc"]))
                    # 邻居须同均线对/频率才可比
                    if hit and hit["ma"] == r["ma"] and hit["f"] == r["f"]:
                        nb.append(hit["m"]["avgm"])
        stable = len(nb) >= 4 and sorted(nb)[len(nb) // 2] > 0.05
        print("邻域%d个，中位月均%+.2f%% → %s" % (
            len(nb), (sorted(nb)[len(nb) // 2] * 100 if nb else 0), "稳" if stable else "否决"), flush=True)
    try:
        old = json.load(open(MODEL_PATH, encoding="utf-8"))
    except Exception:
        old = {"version": "v3"}
    BASE_REF = (80, 2, -6.0, 0.20)  # v3基线同口径比较（苹果比苹果）
    new_ver = None
    today = datetime.date.today()
    if champ and stable:
        r, ix = champ
        # 基线月均从达标表里找v3参数行（同均线/频率优先，不过度精确）
        new_ver = "v4.%s" % today.strftime("%m%d")
        write_json_atomic(MODEL_PATH, {
                   "_note": "R2动量中线模型本地参数版本。十万网格+10年复核+邻域稳定晋升。",
                   "version": new_ver, "date": today.isoformat(),
                   "window": r["w"], "topn": r["t"], "stop": r["s"], "buffer": r["b"],
                   "min_cap_yi": r["mc"], "ma_pair": r["ma"], "freq": r["f"],
                   "params": {"window": r["w"], "topn": r["t"], "stop": r["s"],
                              "buffer": r["b"]},
                   "metrics": {"sector_proxy": {
                       "total": r["m"]["total"], "avgm": r["m"]["avgm"],
                       "maxdd": r["m"]["maxdd"], "win": r["m"]["win"] / 100,
                       "turn": r["m"]["turn"] / 100, "n": r["m"]["n"]},
                       "index10y": {"total": round(ix["eq"], 3),
                                    "cagr": round(ix["cagr"], 3),
                                    "maxdd": round(ix["maxdd"], 3)}}},
                  indent=1)
    lamp_s, lamp_adv, lamp_sh, lamp_ratio, lamp_date = LAMP.lamp()
    os.makedirs(REPORT_DIR, exist_ok=True)
    lp = os.path.join(REPORT_DIR, "R2十万次迭代_%s.md" % today.strftime("%Y%m%d"))
    with open(lp, "w", encoding="utf-8") as f:
        f.write("# R2十万次迭代 %s\n\n" % today)
        f.write("评估100000组，有效%d组，达标%d组（回撤>=-10%%换手<=50%%）。\n\n" % (len(rows), len(ok)))
        f.write("## 板块代理TOP20（按月均）\n\n")
        for r in ok[:20]:
            m = r["m"]
            f.write("- w%-3d top%d stop%4.0f buf%3.0f%% cap%g MA%s f%d 月均%+.2f%% 累计%+.1f%% 回撤%.1f%% 换手%.0f%%\n" % (
                r["w"], r["t"], r["s"], r["b"] * 100, r["mc"], r["ma"], r["f"],
                m["avgm"] * 100, m["total"] * 100, m["maxdd"] * 100, m["turn"]))
        f.write("\n## TOP20的10年复核\n\n")
        for r, ix in checked:
            f.write("- w%-3d top%d 10年累计%+.0f%% 年化%+.1f%% 回撤%.0f%% → %s\n" % (
                r["w"], r["t"], ix["eq"] * 100, ix["cagr"] * 100,
                ix["maxdd"] * 100, "过" if ix["cagr"] > 0.08 else "挂"))
        f.write("\n## 仓位灯\n%s 上证%s BULL%.0f%%：%s\n" % (lamp_s, lamp_sh, lamp_ratio, lamp_adv))
        f.write("\n## 结论\n%s → %s（分散门+邻域稳定）\n" % (
            old.get("version"), new_ver if new_ver else "不晋升（沿用%s）" % old.get("version")))
    print("模型%s→%s 日志:%s" % (old.get("version"), new_ver or "不晋升", lp), flush=True)


if __name__ == "__main__":
    if "--finish" in sys.argv:
        finish()
    else:
        ws = int(sys.argv[sys.argv.index("--ws") + 1]) if "--ws" in sys.argv else 0
        wn = int(sys.argv[sys.argv.index("--wn") + 1]) if "--wn" in sys.argv else 4
        run_windows(ws, wn)
