# -*- coding: utf-8 -*-
"""ETF MACD十万网格（100000组，扫参定生死）。

给未来亚亚：回答“ETF MACD有没有稳健高原”。网格7维 =
回落8 × DIF窗5 × 止损10(固定5+ATR5) × A股均线5 × 跨境均线5 × 熔断5 × 趋势过滤2 = 100000。
面板（K线+全部均线/MACD/ATR）构建一次，多进程并行评估，结果append进jsonl（分片可断点续跑）。
终验：分布统计 + H1(2020-2024)/H2(2025-2026)跨期稳定 + 邻域稳定 → 稳健高原存在才谈生产，不达标不晋升。
纯标准库。用法:
  python research/sweep100k_etf_macd.py --all --jobs 12   # 全量（面板一次，进程池）
  python research/sweep100k_etf_macd.py --start 0 --count 10000  # 分片续跑
  python research/sweep100k_etf_macd.py --verify          # 分布+稳定分析+报告
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.common.paths import DATA_ROOT, REPORT_ROOT, SWEEP_CACHE
from src.common.market_map import cfg_path, load_json
from src.common import technical_indicators as TI
from research._engine import gold, dead, FEE, MAXN, REST_DAYS

UNI = load_json(cfg_path("etf_macd_universe.json"))
A_CODES = [t["code"] for t in UNI["A股宽基"]]
C_CODES = [t["code"] for t in UNI["跨境"]]
KDIR = os.path.join(DATA_ROOT, "kline_etf")
RESULTS = os.path.join(SWEEP_CACHE, "sweep100k_etf_macd.jsonl")

RETRACE = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]  # 8
DIFWIN = [10, 15, 20, 25, 30]  # 5
STOP_PCT = [-4.0, -6.0, -8.0, -10.0, -12.0]  # 5
STOP_ATR = [1.5, 2.0, 2.5, 3.0, 4.0]  # 5 → STOP共10
AMA = [(10, 20, 50), (8, 20, 50), (10, 30, 60), (5, 10, 30), (12, 26, 50)]  # 5
CMA = [(5, 10, 20), (3, 8, 15), (5, 20, 30), (8, 20, 40), (10, 20, 30)]  # 5
FUSE = [0.08, 0.10, 0.12, 0.15, 0.20]  # 5
TREND = [1, 0]  # 2 → 8*5*10*5*5*5*2=100000
assert len(RETRACE) * len(DIFWIN) * 10 * len(AMA) * len(CMA) * len(FUSE) * len(TREND) == 100000
SPANS = sorted({s for t in AMA + CMA for s in t} | {150, 200, 250})
SPLIT = "2025-01-01"


def load(code):
    import csv
    with open(os.path.join(KDIR, code + ".csv"), encoding="utf-8-sig") as f:
        return [(r["日期"], float(r["开"]), float(r["高"]), float(r["低"]), float(r["收盘"]))
                for r in csv.DictReader(f)]


def build_panel():
    raw = {c: load(c) for c in set(A_CODES + C_CODES + ["510300"])}
    bydate = {c: {d: i for i, (d, _, _, _, _) in enumerate(rows)} for c, rows in raw.items()}
    master = [d for d, _, _, _, _ in raw["510300"]]
    M = len(master)
    P = {}
    for c, rows in raw.items():
        # 指标在自身序列上算（与backtest_etf_macd逐笔一致），再对齐到主日历，缺席日留None
        oc = [cl for _, _, _, _, cl in rows]
        oh = [h for _, _, h, _, _ in rows]
        ol = [l for _, _, _, l, _ in rows]
        ma0 = {s: TI.sma(oc, s) for s in SPANS}
        dif0, dea0, _ = TI.macd(oc)
        atr0 = TI.atr(oh, ol, oc)
        o = [None] * M
        cl = [None] * M
        oi = [-1] * M
        ma = {s: [None] * M for s in SPANS}
        dif, dea, atr = [None] * M, [None] * M, [None] * M
        for j, d in enumerate(master):
            if d in bydate[c]:
                i = bydate[c][d]
                oi[j] = i
                o[j], cl[j] = rows[i][1], rows[i][4]
                for s in SPANS:
                    ma[s][j] = ma0[s][i]
                dif[j], dea[j], atr[j] = dif0[i], dea0[i], atr0[i]
        P[c] = {"o": o, "c": cl, "oi": oi, "oc": oc, "ma": ma,
                "dif": dif, "dea": dea, "atr": atr}
    reb = [False] * M
    weeks = {}
    for j, d in enumerate(master):
        weeks.setdefault(datetime.date.fromisoformat(d).isocalendar()[:2], []).append(j)
    for w in weeks.values():
        reb[w[-1]] = True
    b0 = P["510300"]["c"][100]
    beq, cum, peak, mdd = [], 1.0, 1.0, 0.0
    bh1 = None
    for j, d in list(enumerate(master))[100:]:
        cum = P["510300"]["c"][j] / b0
        beq.append(cum)
        peak = max(peak, cum)
        mdd = min(mdd, cum / peak - 1)
        if bh1 is None and d >= SPLIT:
            bh1 = cum
    bench = {"tot": beq[-1] - 1, "mdd": mdd, "h1": bh1 - 1, "h2": beq[-1] / bh1 - 1,
             "span": (master[100], master[-1])}
    return {"P": P, "master": master, "reb": reb, "bench": bench, "M": M}


def eval_cfg(PN, cfg):
    P, master, reb, M = PN["P"], PN["master"], PN["reb"], PN["M"]
    rti, dwi, st, ama, cma, fui, tri = cfg
    rt, dw, fuse, trend = RETRACE[rti], DIFWIN[dwi], FUSE[fui], TREND[tri]
    af, asl, aflt = AMA[ama]
    cf, csl, cflt = CMA[cma]
    stop_pct = st < 5
    sval = STOP_PCT[st] if stop_pct else STOP_ATR[st - 5]
    cash, pos, entry = 1.0, {}, {}
    pend_b, pend_s = [], []
    peak, rest = 1.0, 0
    val, split_v, mdd, empty, ntr = 1.0, None, 0.0, 0, 0
    hs, hsma = P["510300"]["c"], P["510300"]["ma"][200]
    for j in range(100, M):
        d = master[j]
        for c in list(pend_s):
            o = P[c]["o"][j]
            if o is None or c not in pos:
                continue
            cash += pos.pop(c) * o * (1 - FEE)
            entry.pop(c, None)
            ntr += 1
            pend_s.remove(c)
        for c in list(pend_b):
            if c in pos or rest > 0:
                pend_b.remove(c)
                continue
            o = P[c]["o"][j]
            if o is None or cash <= 0:
                continue
            alloc = cash / max(1, len(pend_b))
            pos[c] = alloc * (1 - FEE) / o
            entry[c] = (o, P[c]["atr"][j - 1])
            cash -= alloc
            ntr += 1
            pend_b.remove(c)
        v = cash
        for c, sh in pos.items():
            cc = P[c]["c"][j]
            v += sh * (cc if cc is not None else entry[c][0])
        val = v
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
        if split_v is None and d >= SPLIT:
            split_v = v
        if not pos:
            empty += 1
        dd = v / peak - 1
        tbad = trend and hsma[j] is not None and hs[j] is not None and hs[j] < hsma[j]
        if tbad and pos:
            pend_s.extend([c for c in pos if c not in pend_s])
        if dd <= -fuse and (pos or pend_b):
            pend_s.extend([c for c in pos if c not in pend_s])
            pend_b.clear()
            rest = REST_DAYS
        for c in list(pos):
            cc = P[c]["c"][j]
            if cc is None or c not in entry or c in pend_s:
                continue
            ep, ea = entry[c]
            trig = cc < ep - sval * ea if (not stop_pct and ea) else cc / ep - 1 <= (sval / 100 if stop_pct else -0.08)
            if trig:
                pend_s.append(c)
        if rest > 0:
            rest -= 1
        if reb[j] and rest <= 0 and not tbad:
            for c in list(pos):
                dk = None
                for k in range(max(1, j - 4), j + 1):
                    if dead(P[c]["dif"], P[c]["dea"], k):
                        dk = k
                        break
                if dk is None or c in pend_s:
                    continue
                seg = [x for x in P[c]["dif"][max(0, dk - dw + 1):dk + 1] if x is not None]
                if not seg:
                    continue
                hi = max(seg)
                if (hi <= 0 or (hi - P[c]["dif"][dk]) / hi >= rt):
                    pend_s.append(c)
            if len(pos) + len(pend_b) < MAXN:
                cand = []
                for c in (A_CODES + C_CODES):
                    if c in pos or c in pend_b:
                        continue
                    D = P[c]
                    if D["c"][j] is None or D["oi"][j] < 50:
                        continue
                    if c in A_CODES:
                        f, s, flt = D["ma"][af], D["ma"][asl], D["ma"][aflt]
                    else:
                        f, s, flt = D["ma"][cf], D["ma"][csl], D["ma"][cflt]
                    g = any(gold(f, s, k) for k in range(max(1, j - 4), j + 1))
                    if g and flt[j] is not None and D["c"][j] > flt[j]:
                        m20 = D["oc"][D["oi"][j] - 20]
                        cand.append((D["c"][j] / m20 - 1 if m20 else -9, c))
                cand.sort(reverse=True)
                for _, c in cand[:MAXN - len(pos) - len(pend_b)]:
                    pend_b.append(c)
    split_v = split_v or val
    return {"eq": round(val - 1, 4), "mdd": round(mdd, 4), "n": ntr,
            "empty": round(empty / (M - 100), 3), "h1": round(split_v - 1, 4),
            "h2": round(val / split_v - 1, 4)}


def combos():
    i = 0
    for rt in range(8):
        for dw in range(5):
            for st in range(10):
                for ama in range(5):
                    for cma in range(5):
                        for fu in range(5):
                            for tr in range(2):
                                yield i, (rt, dw, st, ama, cma, fu, tr)
                                i += 1


def cfg_label(cfg):
    rt, dw, st, ama, cma, fu, tr = cfg
    s = ("p%d" % -STOP_PCT[st]) if st < 5 else ("a%.1f" % STOP_ATR[st - 5])
    return "rt%d_dw%d_%s_ama%d_cma%d_fu%d_tr%d" % (rt, dw, s, ama, cma, fu, tr)


_PN = None


def _init(pn):
    global _PN
    _PN = pn


def _work(item):
    i, cfg = item
    try:
        r = eval_cfg(_PN, cfg)
    except Exception as e:
        r = {"err": str(e)[:100]}
    return {"i": i, "p": cfg_label(cfg), "r": r}


def run_all(jobs):
    from multiprocessing import Pool
    PN = build_panel()
    print("面板就绪 %s~%s 基准累计%+.1f%%" % (PN["bench"]["span"][0], PN["bench"]["span"][1],
          PN["bench"]["tot"] * 100), flush=True)
    items = list(combos())
    assert len(items) == 100000
    if os.path.exists(RESULTS):
        done = {json.loads(l)["i"] for l in open(RESULTS, encoding="utf-8") if l.strip()}
        items = [it for it in items if it[0] not in done]
        print("断点续跑，已有%d组，剩余%d组" % (len(done), len(items)), flush=True)
    fh = open(RESULTS, "a", encoding="utf-8")
    with Pool(jobs, initializer=_init, initargs=(PN,)) as pool:
        n = 0
        for row in pool.imap_unordered(_work, items, chunksize=50):
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
            if n % 10000 == 0:
                fh.flush()
                print("  已写 %d/%d" % (n, len(items)), flush=True)
    fh.close()
    print("全量完成")


def run_slice(start, count):
    PN = build_panel()
    items = list(combos())[start:start + count]
    global _PN
    _PN = PN
    fh = open(RESULTS, "a", encoding="utf-8")
    for n, it in enumerate(items, 1):
        fh.write(json.dumps(_work(it), ensure_ascii=False) + "\n")
        if n % 2000 == 0:
            print("  片内 %d/%d" % (n, len(items)), flush=True)
    fh.close()
    print("本片写入%d组" % len(items))


def verify():
    rows = [json.loads(l) for l in open(RESULTS, encoding="utf-8") if l.strip() and "err" not in json.loads(l).get("r", {})]
    print("共%d组" % len(rows), flush=True)
    PN = build_panel()
    b = PN["bench"]
    eqs = sorted(x["r"]["eq"] for x in rows)
    n = len(eqs)
    med = eqs[n // 2]
    beat = sum(1 for x in rows if x["r"]["eq"] > b["tot"])
    pos = sum(1 for x in rows if x["r"]["eq"] > 0)
    # 跨期稳定：H1前10%在H2仍为正的比例
    by_h1 = sorted(rows, key=lambda x: -x["r"]["h1"])[:n // 10]
    h1_hold = sum(1 for x in by_h1 if x["r"]["h2"] > 0) / max(1, len(by_h1))
    # 稳健高原：最优格全维度邻域（每维±1档，共14邻居）是否全为正
    top = max(rows, key=lambda x: (x["r"]["eq"], x["r"]["mdd"]))
    tc = top["p"]
    ti = top["i"]
    # 网格嵌套序（外→内）：rt8/dw5/st10/ama5/cma5/fu5/tr2，坐标编解码取严格邻居
    DIMS = (8, 5, 10, 5, 5, 5, 2)

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

    tc0 = decode(ti)
    nbs = []
    for d, dm in enumerate(DIMS):
        for delta in (-1, 1):
            v = tc0[d] + delta
            if 0 <= v < dm:
                cc = list(tc0)
                cc[d] = v
                nbs.append(encode(cc))
    byi = {x["i"]: x for x in rows}
    nbs = [byi[j] for j in nbs if j in byi]
    nb_ok = len(nbs) >= 10 and all(x["r"]["eq"] > 0 for x in nbs)
    # 最优格本身跨期
    topo = top["r"]["h1"] > 0 and top["r"]["h2"] > 0
    stable = nb_ok and topo
    # 高原体积：跑赢基准且H1/H2双正的占比
    plateau = sum(1 for x in rows if x["r"]["eq"] > b["tot"] and x["r"]["h1"] > 0 and x["r"]["h2"] > 0) / n
    verdict = ("晋升候选" if (stable and plateau >= 0.05) else "不晋升")
    print("中位累计%+.1f%% 正收益%.1f%% 跑赢基准%.1f%%" % (med * 100, pos / n * 100, beat / n * 100), flush=True)
    print("基准：累计%+.1f%% H1%+.1f%% H2%+.1f%% 回撤%.0f%%" % (
        b["tot"] * 100, b["h1"] * 100, b["h2"] * 100, b["mdd"] * 100), flush=True)
    print("最优 %s 累计%+.1f%% 回撤%.0f%% H1%+.1f%% H2%+.1f%%" % (
        tc, top["r"]["eq"] * 100, top["r"]["mdd"] * 100, top["r"]["h1"] * 100, top["r"]["h2"] * 100), flush=True)
    print("H1前10%%跨期H2为正 %.0f%% 邻域全正%s 最优跨期%s 高原体积%.1f%% → %s" % (
        h1_hold * 100, nb_ok, topo, plateau * 100, verdict), flush=True)
    rp = os.path.join(REPORT_ROOT, "ETF_MACD十万次_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    os.makedirs(REPORT_ROOT, exist_ok=True)
    with open(rp, "w", encoding="utf-8") as f:
        f.write("# ETF MACD十万次 %s\n\n评估%d组（回落8×DIF窗5×止损10×A均线5×C均线5×熔断5×趋势2）。"
                "基准510300买持累计%+.1f%%(H1%+.1f%%/H2%+.1f%%/回撤%.0f%%)。\n\n" % (
                    datetime.date.today(), n, b["tot"] * 100, b["h1"] * 100, b["h2"] * 100, b["mdd"] * 100))
        f.write("分布：中位累计%+.1f%%，正收益%.1f%%，跑赢基准%.1f%%。\n\n" % (med * 100, pos / n * 100, beat / n * 100))
        f.write("最优%s：累计%+.1f%% 回撤%.0f%% H1%+.1f%% H2%+.1f%%。\n\n" % (
            tc, top["r"]["eq"] * 100, top["r"]["mdd"] * 100, top["r"]["h1"] * 100, top["r"]["h2"] * 100))
        f.write("稳定：H1前10%%跨期H2为正%.0f%%；最优邻域全正%s；最优跨期双正%s；高原体积（赢基准且双正）%.1f%%。\n\n" % (
            h1_hold * 100, nb_ok, topo, plateau * 100))
        f.write("结论：%s（%s）。%s\n" % (
            verdict,
            "存在稳定高原" if verdict == "晋升候选" else "无稳定高原",
            "最优格+邻域+跨期三稳且高原≥5%，可谈生产；" if verdict == "晋升候选"
            else "十万组扫完仍无稳定超额结构，策略封存为熊市避难组件，不转生产信号。"))
    print("报告:%s" % rp)


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify()
    elif "--all" in sys.argv:
        jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else 8
        run_all(jobs)
    else:
        start = int(sys.argv[sys.argv.index("--start") + 1])
        count = int(sys.argv[sys.argv.index("--count") + 1])
        run_slice(start, count)
