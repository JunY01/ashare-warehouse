# -*- coding: utf-8 -*-
"""R2动量大网格（192000组，10年指数验证：得分加权×止损族×评分族）。

给未来亚亚：回答“R2还剩哪颗螺丝能拧”。10年数据只有收盘价，ATR用收盘价代理
（近14日|涨跌幅|均值，同scan_regime口径，如实注明）。
网格9维 = 窗口10 × TOP4 × 配权2(等权/得分) × 止损12(固定6+ATR6) × 缓冲5 × 评分5 × 趋势2 × 频率2 × 动量门2 = 192000。
面板（每月每指数回归分量）构建一次，多进程并行，结果append进jsonl。
终验：分布 + H1(2016-2021)/H2(2022-2026，真熊+反转)跨期 + 邻域稳定；只下结论不写模型文件。
纯标准库。用法:
  python research/sweep192k_r2.py --all --jobs 12
  python research/sweep192k_r2.py --verify
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.common.paths import DATA_ROOT, REPORT_ROOT, SWEEP_CACHE
from research._engine import wlinreg_stats

KDIR = os.path.join(DATA_ROOT, "kline_10y")
RESULTS = os.path.join(SWEEP_CACHE, "sweep192k_r2.jsonl")

# 上市日门：回填历史不可交易，按上市日截断（防回填偷看未来）
LAUNCH = {"科创50": "2020-07-22", "恒生科技": "2020-07-27", "中证2000": "2023-08-11",
          "中证A500": "2024-09-23", "北证50": "2022-11-21"}
WINDOWS = [20, 30, 40, 50, 60, 70, 80, 90, 100, 120]  # 10
TOPNS = [1, 2, 3, 4]  # 4
WEIGHTS = [0, 1]  # 2（0=等权 1=得分加权）
STOP_FIX = [-4.0, -6.0, -8.0, -10.0, -12.0, -15.0]  # 6
STOP_ATR = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0]  # 6 → 止损共12
BUFS = [0.0, 0.05, 0.10, 0.20, 0.30]  # 5
SCORING = [0, 1, 2, 3, 4]  # 5（0=ann×R2 1=纯斜率 2=夏普式 3=R²门槛 4=回撤惩罚）
TRENDS = [0, 1]  # 2（0=严格收>MA20>MA60 1=宽松收>MA20）
FREQS = [1, 2]  # 2（1=月度 2=双月）
ANNGATE = [0, 1]  # 2（0=ann>0 1=ann>20%）
assert len(WINDOWS) * len(TOPNS) * len(WEIGHTS) * 12 * len(BUFS) * len(SCORING) * len(TRENDS) * len(FREQS) * len(ANNGATE) == 192000
SPLIT = "2022-01-01"
DIMS = (10, 4, 2, 12, 5, 5, 2, 2, 2)


def load():
    import csv
    series = {}
    for fn in os.listdir(KDIR):
        if not fn.endswith(".csv"):
            continue
        with open(os.path.join(KDIR, fn), encoding="utf-8-sig") as f:
            rows = []
            for r in csv.DictReader(f):
                try:
                    rows.append((r["日期"].strip().strip("'"), float(r["收盘"].strip().strip("'"))))
                except (ValueError, AttributeError):
                    continue  # 体内重复表头等脏行直接丢
        # 2026-09曾出现带单引号重导出的脏文件，兼容剥引号；顺手按日期正序排
        rows = sorted(rows)
        name = fn[:-4]
        if name in LAUNCH:
            rows = [(d, c) for d, c in rows if d >= LAUNCH[name]]
        series[name] = rows
    return series


def wstats(cl, window):
    """加权回归分量：ann, r2, vol, dd_win。只用<=rb数据。"""
    return wlinreg_stats(cl, window)


def build_panel():
    raw = load()
    px = {k: dict(v) for k, v in raw.items()}
    cal = sorted({d for v in raw.values() for d, _ in v})
    months, seen = [], set()
    for d in cal:
        m = d[:7]
        if m not in seen:
            seen.add(m)
            months.append(d)
    warm = 120 + 70
    months = [m for m in months if cal.index(m) >= warm]
    comp = {}  # (code, rb) -> {w: (ann,r2,vol,dd), ma20, ma60, atr, close}
    for code, pts in raw.items():
        closes = [p[1] for p in pts]
        dates = [p[0] for p in pts]
        pos = {d: i for i, d in enumerate(dates)}
        for rb in months:
            if rb not in pos:
                continue
            i = pos[rb]
            cl = closes[:i + 1]
            if len(cl) < 135:
                continue
            c = {"ma20": sum(cl[-20:]) / 20, "ma60": sum(cl[-60:]) / 60, "close": cl[-1]}
            chg = [abs(cl[k] / cl[k - 1] - 1) for k in range(max(1, len(cl) - 14), len(cl))]
            c["atr"] = sum(chg) / len(chg) if chg else 0.0
            for w in WINDOWS:
                r = wstats(cl, w)
                if r:
                    c[w] = r
            comp[(code, rb)] = c
    braw = raw["沪深300"]
    bpx = dict(braw)
    b0 = bpx[months[0]]
    beq, peak, mdd = [], 1.0, 0.0
    bh1 = None
    for d in cal[cal.index(months[0]):]:
        if d not in bpx:
            continue
        cum = bpx[d] / b0
        beq.append(cum)
        peak = max(peak, cum)
        mdd = min(mdd, cum / peak - 1)
        if bh1 is None and d >= SPLIT:
            bh1 = cum
    bench = {"tot": beq[-1] - 1, "mdd": mdd, "h1": bh1 - 1, "h2": beq[-1] / bh1 - 1}
    return {"raw": raw, "px": px, "cal": cal, "months": months, "comp": comp, "bench": bench}


def score_of(c, w, variant, agate):
    r = c.get(w)
    if not r:
        return None
    ann, r2, vol, dd = r
    if agate and ann <= 0.20:
        return None
    if ann <= 0:
        return None
    if variant == 0:
        return ann * r2
    if variant == 1:
        return ann
    if variant == 2:
        return ann / vol if vol > 0 else None
    if variant == 3:
        return ann * r2 if r2 >= 0.30 else None
    # 4: 回撤惩罚
    return ann * r2 / (1 - dd) if dd < 0 else ann * r2


def eval_cfg(PN, cfg, delay=0):
    wi, ti, wti, sti, bui, sci, tri, fri, agi = cfg
    window, topn = WINDOWS[wi], TOPNS[ti]
    stop_fix = sti < 6
    sval = STOP_FIX[sti] if stop_fix else STOP_ATR[sti - 6]
    buf, scoring = BUFS[bui], SCORING[sci]
    months = PN["months"][::FREQS[fri]]
    raw, px, cal, comp = PN["raw"], PN["px"], PN["cal"], PN["comp"]
    eq = [1.0]
    peak, mdd, empty, ntr = 1.0, 0.0, 0, 0
    hold, hs, hw = [], 0.0, []
    entry, eatr = {}, {}
    split_v, h1v = None, None
    for mi in range(len(months) - 1):
        rb, nxt = months[mi], months[mi + 1]
        scored = []
        for code in raw:
            c = comp.get((code, rb))
            if not c or window not in c:
                continue
            if tri == 0 and not (c["close"] > c["ma20"] > c["ma60"]):
                continue
            if tri == 1 and not (c["close"] > c["ma20"]):
                continue
            s = score_of(c, window, scoring, agi)
            if s is not None and s > 0:
                scored.append((s, code))
        scored.sort(reverse=True)
        cand = [c for _, c in scored[:topn]]
        if not cand:
            empty += 1
        wts = None
        if cand and wti == 1:
            ss = [s for s, _ in scored[:topn]]
            tot = sum(ss)
            wts = [s / tot for s in ss] if tot > 0 else None
        cs = sum(s for s, _ in scored[:topn]) / topn if cand else 0
        if hold and cand and cs <= hs * (1 + buf):
            cand, cs, wts = list(hold), hs, list(hw)
        else:
            if cand and wts is None:
                wts = [1 / len(cand)] * len(cand)
            elif not cand:
                wts = []
        ntr += len(set(cand) - set(hold)) / topn if topn else 0
        hold, hs, hw = cand, cs, wts or []
        for c in hold:
            if rb in px[c]:
                entry[c] = px[c][rb]
                eatr[c] = comp[(c, rb)]["atr"]
        stopped = set()  # 当月已止损的腿：剩余交易日按货币记0，不再复活（否则等于免费过滤下跌）
        i0, i1 = cal.index(rb), cal.index(nxt)
        for d in cal[i0 + 1:i1 + 1]:
            legs = []
            prev = cal[cal.index(d) - 1]
            for k, c in enumerate(hold):
                if c in stopped:
                    continue
                if c not in px or d not in px[c] or c not in entry or not entry[c]:
                    continue
                trig = (px[c][d] < entry[c] * (1 - sval * eatr.get(c, 0)) if (not stop_fix and eatr.get(c))
                        else px[c][d] / entry[c] - 1 <= (sval / 100 if stop_fix else -0.08))
                if trig:
                    stopped.add(c)
                    if delay == 0:
                        continue  # 场内：当日按货币
                    # delay=1（场外）：信号日按实际涨跌算，次日起按货币
                r = px[c][d] / px[c][prev] - 1 if prev in px[c] else 0.0
                legs.append(r * (hw[k] if k < len(hw) else 0))
            # 权重归一：存活腿权重重归一（止损腿收益0但释放权重？保守：不释放，按原权）
            rr = sum(legs)
            eq.append(eq[-1] * (1 + rr))
        peak = max(peak, eq[-1])
        mdd = min(mdd, eq[-1] / peak - 1)
        if split_v is None and nxt >= SPLIT:
            split_v = eq[-1]
    split_v = split_v or eq[-1]
    yrs = (datetime.date.fromisoformat(months[-1]) - datetime.date.fromisoformat(months[0])).days / 365.25
    cagr = eq[-1] ** (1 / yrs) - 1 if eq[-1] > 0 else -1
    return {"eq": round(eq[-1] - 1, 4), "cagr": round(cagr, 4), "mdd": round(mdd, 4),
            "n": len(months) - 1, "empty": empty, "turn": round(ntr / max(1, len(months) - 1), 3),
            "h1": round(split_v - 1, 4), "h2": round(eq[-1] / split_v - 1, 4)}


def combos():
    i = 0
    for wi in range(10):
        for ti in range(4):
            for wti in range(2):
                for sti in range(12):
                    for bui in range(5):
                        for sci in range(5):
                            for tri in range(2):
                                for fri in range(2):
                                    for agi in range(2):
                                        yield i, (wi, ti, wti, sti, bui, sci, tri, fri, agi)
                                        i += 1


def lbl(cfg):
    wi, ti, wti, sti, bui, sci, tri, fri, agi = cfg
    s = ("p%d" % -STOP_FIX[sti]) if sti < 6 else ("a%.1f" % STOP_ATR[sti - 6])
    return "w%d_t%d_%s_%s_b%d_s%d_tr%d_f%d_g%d" % (
        WINDOWS[wi], TOPNS[ti], "eq" if wti == 0 else "sw", s, int(BUFS[bui] * 100),
        sci, tri, fri, agi)


_PN = None


def _init(pn):
    global _PN
    _PN = pn


def _work(item):
    i, cfg = item
    try:
        r = eval_cfg(_PN, cfg)
    except Exception as e:
        r = {"err": str(e)[:120]}
    return {"i": i, "p": lbl(cfg), "r": r}


def run_all(jobs):
    from multiprocessing import Pool
    PN = build_panel()
    print("面板 %s~%s 基准累计%+.1f%%" % (PN["months"][0], PN["months"][-1],
          PN["bench"]["tot"] * 100), flush=True)
    items = list(combos())
    assert len(items) == 192000
    if os.path.exists(RESULTS):
        done = {json.loads(l)["i"] for l in open(RESULTS, encoding="utf-8") if l.strip()}
        items = [it for it in items if it[0] not in done]
        print("断点续跑，已有%d，剩余%d" % (len(done), len(items)), flush=True)
    fh = open(RESULTS, "a", encoding="utf-8")
    with Pool(jobs, initializer=_init, initargs=(PN,)) as pool:
        n = 0
        for row in pool.imap_unordered(_work, items, chunksize=50):
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
            if n % 20000 == 0:
                fh.flush()
                print("  已写 %d/%d" % (n, len(items)), flush=True)
    fh.close()
    print("全量完成")


def decode(i):
    c = []
    for dm in reversed(DIMS):
        c.append(i % dm)
        i //= dm
    return list(reversed(c))


def encode(c):
    i = 0
    for v, dm in zip(c, DIMS):
        i = i * dm + v
    return i


def verify():
    rows = [json.loads(l) for l in open(RESULTS, encoding="utf-8")
            if l.strip() and "err" not in json.loads(l).get("r", {})]
    n = len(rows)
    print("共%d组" % n, flush=True)
    PN = build_panel()
    b = PN["bench"]
    eqs = sorted(x["r"]["eq"] for x in rows)
    med = eqs[n // 2]
    beat = sum(1 for x in rows if x["r"]["eq"] > b["tot"]) / n
    pos = sum(1 for x in rows if x["r"]["eq"] > 0) / n
    by_h1 = sorted(rows, key=lambda x: -x["r"]["h1"])[:n // 10]
    h1_hold = sum(1 for x in by_h1 if x["r"]["h2"] > 0) / max(1, len(by_h1))
    top = max(rows, key=lambda x: (x["r"]["eq"], x["r"]["mdd"]))
    tc0 = decode(top["i"])
    nbs = []
    for d, dm in enumerate(DIMS):
        for dl in (-1, 1):
            v = tc0[d] + dl
            if 0 <= v < dm:
                cc = list(tc0)
                cc[d] = v
                nbs.append(encode(cc))
    byi = {x["i"]: x for x in rows}
    nbs = [byi[j] for j in nbs if j in byi]
    nb_ok = len(nbs) >= 12 and all(x["r"]["eq"] > 0 for x in nbs)
    topo = top["r"]["h1"] > 0 and top["r"]["h2"] > 0
    plateau = sum(1 for x in rows if x["r"]["eq"] > b["tot"] and x["r"]["h1"] > 0 and x["r"]["h2"] > 0) / n
    # 当前线上v3在网格中的位置
    v3 = [x for x in rows if x["p"].startswith("w80_t2_eq_p6_b20_s0_tr0_f0_g0")]
    v3r = v3[0]["r"] if v3 else None
    print("中位%+.1f%% 正%.0f%% 赢基准%.0f%%" % (med * 100, pos * 100, beat * 100), flush=True)
    print("基准：累计%+.1f%% H1%+.1f%% H2%+.1f%% 回撤%.0f%%" % (
        b["tot"] * 100, b["h1"] * 100, b["h2"] * 100, b["mdd"] * 100), flush=True)
    print("最优 %s 累计%+.1f%% 回撤%.0f%% H1%+.1f%% H2%+.1f%%" % (
        top["p"], top["r"]["eq"] * 100, top["r"]["mdd"] * 100, top["r"]["h1"] * 100, top["r"]["h2"] * 100), flush=True)
    print("H1前10%%跨期正%.0f%% 邻域全正%s 跨期双正%s 高原%.1f%%" % (
        h1_hold * 100, nb_ok, topo, plateau * 100), flush=True)
    if v3r:
        print("线上v3(w80等权-6%% buf20) 累计%+.1f%% 回撤%.0f%% H1%+.1f%% H2%+.1f%%" % (
            v3r["eq"] * 100, v3r["mdd"] * 100, v3r["h1"] * 100, v3r["h2"] * 100), flush=True)
    # 边际
    mgn = {}
    for nm, dm in (("window", 0), ("topn", 1), ("weight", 2), ("stop", 3), ("buf", 4),
                   ("scoring", 5), ("trend", 6), ("freq", 7), ("anngate", 8)):
        d = {}
        for x in rows:
            v = decode(x["i"])[dm]
            a, c = d.get(v, (0.0, 0))
            d[v] = (a + x["r"]["eq"], c + 1)
        mgn[nm] = {v: round(a / c, 3) for v, (a, c) in sorted(d.items())}
    rp = os.path.join(REPORT_ROOT, "R2十万次v2_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    os.makedirs(REPORT_ROOT, exist_ok=True)
    with open(rp, "w", encoding="utf-8") as f:
        f.write("# R2动量大网格 %s\n\n评估%d组（窗口10×TOP4×配权2×止损12×缓冲5×评分5×趋势2×频率2×动量门2）。"
                "15指数10年，基准沪深300买持累计%+.1f%%(H1%+.1f%%/H2%+.1f%%/回撤%.0f%%)。"
                "ATR为收盘价代理口径。\n\n" % (
                    datetime.date.today(), n, b["tot"] * 100, b["h1"] * 100, b["h2"] * 100, b["mdd"] * 100))
        f.write("分布：中位%+.1f%%，正收益%.0f%%，赢基准%.0f%%。\n\n" % (med * 100, pos * 100, beat * 100))
        f.write("最优%s：累计%+.1f%% 回撤%.0f%% H1%+.1f%% H2%+.1f%%。\n\n" % (
            top["p"], top["r"]["eq"] * 100, top["r"]["mdd"] * 100, top["r"]["h1"] * 100, top["r"]["h2"] * 100))
        f.write("稳定：H1前10%%跨期正%.0f%%；最优邻域全正%s；跨期双正%s；高原体积%.1f%%。\n\n" % (
            h1_hold * 100, nb_ok, topo, plateau * 100))
        if v3r:
            f.write("线上v3：累计%+.1f%% 回撤%.0f%% H1%+.1f%% H2%+.1f%%。\n\n" % (
                v3r["eq"] * 100, v3r["mdd"] * 100, v3r["h1"] * 100, v3r["h2"] * 100))
        f.write("边际（均值累计）：\n")
        for nm, d in mgn.items():
            f.write("- %s: %s\n" % (nm, ", ".join("%s=%+.3f" % (k, v) for k, v in d.items())))
        f.write("\n结论：待分析后手写。\n")
    print("报告:%s" % rp)


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify()
    else:
        jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else 8
        run_all(jobs)
