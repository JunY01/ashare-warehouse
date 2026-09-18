# -*- coding: utf-8 -*-
"""ETF MACD趋势跟踪回测（复刻知乎策略生死录①八步法，离线无未来函数版）。

标的: config/etf_macd_universe.json（A股7 + 跨境4基线 + 513180对照），数据 data/kline_etf/*.csv（前复权OHLC）。
规则:
  买入（每周最后一个交易日收盘算信号，下个交易日开盘成交）:
    A股 MA10金叉MA20且收>MA50；跨境 MA5金叉MA10且收>MA20
  卖出（同频率）: DIF下穿DEA（死叉）且DIF从20日高点回落≥RETRACE%
  三层防线（每日收盘检查，下个交易日开盘执行）:
    1) 510300收盘<MA200 → 清仓，收复前不买
    2) 组合从峰值回撤≥12% → 清仓，休息20个交易日
    3) 单只收盘/入场开盘-1 ≤ -8% → 下日开盘卖这只
仓位: 等权最多5只，候选超5只按20日动量优先。费率: 单边0.03%（佣金+滑点包干，无最低5元，见报告假设）。
坐标系: 全程前复权（入场开盘与收盘同坐标，避免原文坑2的混坐标假止损；除权缺口已由前复权抹平，对应原文坑1）。
基准: 510300首日收盘买入持有。
用法:
  python research/backtest_etf_macd.py              # 基线(159920)+对照(513180 swap)双跑
  python research/backtest_etf_macd.py --sensitivity # 网格: 回落{20,30,40}×固定止损{6,8,10}
  python research/backtest_etf_macd.py --atr         # 网格: 回落{20,30,40}×ATR倍数{2.0,2.5,3.0}
报告: reports/ETF_MACD回测_YYYYMMDD.md。纯标准库。
"""
import csv
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.common.paths import DATA_ROOT, REPORT_ROOT, CONFIG_ROOT
from src.common.market_map import cfg_path, load_json
from src.common import technical_indicators as TI
from research._engine import gold, dead, FEE, MAXN, REST_DAYS

KDIR = os.path.join(DATA_ROOT, "kline_etf")
MDD_FUSE = 0.12

UNI = load_json(cfg_path("etf_macd_universe.json"))
A_CODES = [t["code"] for t in UNI["A股宽基"]]
C_CODES = [t["code"] for t in UNI["跨境"]]
X_CODE = UNI["弹性对照"][0]["code"]
NAMES = {t["code"]: t["name"] for g in ("A股宽基", "跨境", "弹性对照") for t in UNI[g]}


def load(code):
    with open(os.path.join(KDIR, code + ".csv"), encoding="utf-8-sig") as f:
        return [(r["日期"], float(r["开"]), float(r["高"]), float(r["低"]), float(r["收盘"]))
                for r in csv.DictReader(f)]


def ind(highs, lows, closes):
    ma5 = TI.sma(closes, 5)
    ma10 = TI.sma(closes, 10)
    ma20 = TI.sma(closes, 20)
    ma50 = TI.sma(closes, 50)
    ma200 = TI.sma(closes, 200)
    dif, dea, _ = TI.macd(closes)
    atr14 = TI.atr(highs, lows, closes)
    return {"ma5": ma5, "ma10": ma10, "ma20": ma20, "ma50": ma50, "ma200": ma200,
            "dif": dif, "dea": dea, "atr14": atr14}


def retrace_ok(dif, i, window=20, need=0.30):
    seg = [v for v in dif[max(0, i - window + 1):i + 1] if v is not None]
    if not seg or dif[i] is None:
        return False
    hi = max(seg)
    if hi <= 0:
        return True  # 从未走强、死叉即确认终结
    return (hi - dif[i]) / hi >= need


def gold_since(ma_f, ma_s, i, n=5):
    """周内窗口：过去n根（含当日）任一根金叉即算（周五调仓不会漏掉周二的交叉）"""
    for k in range(max(1, i - n + 1), i + 1):
        if gold(ma_f, ma_s, k):
            return True
    return False


def dead_since(dif, dea, i, n=5):
    for k in range(max(1, i - n + 1), i + 1):
        if dead(dif, dea, k):
            return k
    return None


def run(codes_a, codes_c, retrace=0.30, stop=-8.0, dif_win=20, stop_mode="pct", atr_k=2.5):
    data = {c: load(c) for c in set(codes_a + codes_c + ["510300"])}
    px = {c: {d: (o, h, l, cl) for d, o, h, l, cl in rows} for c, rows in data.items()}
    idx = {c: [i for i, _ in enumerate(rows)] for c, rows in data.items()}
    bydate = {c: {d: i for i, (d, _, _, _, _) in enumerate(rows)} for c, rows in data.items()}
    closes = {c: [cl for _, _, _, _, cl in rows] for c, rows in data.items()}
    I = {c: ind([h for _, _, h, _, _ in rows], [l for _, _, _, l, _ in rows], closes[c])
         for c, rows in data.items()}
    master = [d for d, _, _, _, _ in data["510300"]]
    mpos = {d: i for i, d in enumerate(master)}
    # 周分组：每周最后一个510300交易日为调仓日
    reb = set()
    weeks = {}
    for d in master:
        weeks.setdefault(datetime.date.fromisoformat(d).isocalendar()[:2], []).append(d)
    for w in weeks.values():
        reb.add(w[-1])
    start = master[100]  # 预热MA50+MACD+DIF窗口
    mi0 = mpos[start]
    cash, pos, entry = 1.0, {}, {}
    pend_buy, pend_sell = [], []  # (code) 下日开盘执行
    peak, rest, eq = 1.0, 0, [1.0]
    edates = []
    bench0 = closes["510300"][bydate["510300"][start]]
    beq = []
    trades, empty_days = [], 0
    hs_ma200 = I["510300"]["ma200"]
    hs_by = bydate["510300"]

    def open_of(code, date):
        return px[code][date][0] if date in px[code] else None

    def close_of(code, date):
        return px[code][date][3] if date in px[code] else None

    for mi in range(mi0, len(master)):
        d = master[mi]
        # 1) 执行昨日pending（开盘价）
        for c in list(pend_sell):
            o = open_of(c, d)
            if o is None or c not in pos:
                continue
            cash += pos.pop(c) * o * (1 - FEE)
            entry.pop(c, None)
            trades.append((d, c, "卖", o))
            pend_sell.remove(c)
        for c in list(pend_buy):
            if c in pos or rest > 0:
                pend_buy.remove(c)
                continue
            o = open_of(c, d)
            if o is None or cash <= 0:
                continue
            alloc = cash / max(1, len(pend_buy))
            shr = alloc * (1 - FEE) / o
            # ATR锚定：取执行日前一根已知的ATR(14)，开盘时已确定，无未来函数
            ie = bydate[c][d]
            ae = I[c]["atr14"][ie - 1] if ie >= 1 else None
            pos[c], entry[c] = shr, (o, ae)
            cash -= alloc
            trades.append((d, c, "买", o))
            pend_buy.remove(c)
        # 2) EOD估值
        val = cash + sum(sh * (close_of(c, d) or entry[c][0]) for c, sh in pos.items())
        eq.append(val)
        edates.append(d)
        bcl = closes["510300"][hs_by[d]]
        beq.append(bcl / bench0)
        peak = max(peak, val)
        dd = val / peak - 1
        if not pos:
            empty_days += 1
        # 3) 每日防线信号（明日执行）
        hi = hs_by[d]
        trend_bad = hs_ma200[hi] is not None and bcl < hs_ma200[hi]
        if trend_bad and pos:
            pend_sell.extend([c for c in pos if c not in pend_sell])
        if dd <= -MDD_FUSE and (pos or pend_buy):
            pend_sell.extend([c for c in pos if c not in pend_sell])
            pend_buy.clear()
            rest = REST_DAYS
        for c, sh in list(pos.items()):
            cl = close_of(c, d)
            if cl is None or c not in entry or c in pend_sell:
                continue
            ep, ea = entry[c]
            if stop_mode == "atr" and ea:
                trig = cl < ep - atr_k * ea  # 波动大自动放宽、波动小收紧
            else:
                trig = cl / ep - 1 <= stop / 100  # 回退：固定百分比
            if trig:
                pend_sell.append(c)
        if rest > 0:
            rest -= 1
        # 4) 周调仓日：卖出MACD确认 + 买入补位
        if d in reb and rest <= 0 and not trend_bad:
            for c in list(pos):
                if c not in bydate or d not in bydate[c]:
                    continue
                i = bydate[c][d]
                if i < 5:
                    continue
                dk = dead_since(I[c]["dif"], I[c]["dea"], i)
                if dk is not None and retrace_ok(I[c]["dif"], dk, dif_win, retrace) \
                        and c not in pend_sell:
                    pend_sell.append(c)
            if len(pos) + len(pend_buy) < MAXN:
                cand = []
                for c in (codes_a + codes_c):
                    if c in pos or c in pend_buy or d not in bydate.get(c, {}):
                        continue
                    i = bydate[c][d]
                    if i < 50:
                        continue
                    cl = closes[c][i]
                    if c in codes_a:
                        ok = gold_since(I[c]["ma10"], I[c]["ma20"], i) and I[c]["ma50"][i] and cl > I[c]["ma50"][i]
                    else:
                        ok = gold_since(I[c]["ma5"], I[c]["ma10"], i) and I[c]["ma20"][i] and cl > I[c]["ma20"][i]
                    if ok:
                        m20 = closes[c][i - 20]
                        cand.append(((cl / m20 - 1) if m20 else -9, c))
                cand.sort(reverse=True)
                for _, c in cand[:MAXN - len(pos) - len(pend_buy)]:
                    pend_buy.append(c)
    # 尾盘按末收平仓估值（不计交易，仅算浮动权益）
    tot = eq[-1] - 1
    yrs = (datetime.date.fromisoformat(edates[-1]) - datetime.date.fromisoformat(edates[0])).days / 365.25
    cagr = eq[-1] ** (1 / yrs) - 1
    pk, md = 1.0, 0.0
    for v in eq[1:]:
        pk = max(pk, v)
        md = min(md, v / pk - 1)
    bt, bc = beq[-1] - 1, (beq[-1]) ** (1 / yrs) - 1
    pk,mdb = 1.0, 0.0
    for v in beq:
        pk = max(pk, v)
        mdb = min(mdb, v / pk - 1)
    yret = {}
    for d, v, b in zip(edates, eq[1:], beq):
        yret.setdefault(d[:4], [None, None, None, None])
    # 年收益：年末/年初-1
    ann = {}
    for y in sorted({d[:4] for d in edates}):
        ys = [(d, v) for d, v in zip(edates, eq[1:]) if d[:4] == y]
        if len(ys) >= 2:
            ann[y] = ys[-1][1] / ys[0][1] - 1
    seg = {}
    for k, (s0, s1) in {"2022熊": ("2022-01-01", "2022-12-31"), "2024牛": ("2024-01-01", "2024-12-31"),
                        "2025": ("2025-01-01", "2025-12-31")}.items():
        ys = [v for d, v in zip(edates, eq[1:]) if s0 <= d <= s1]
        seg[k] = (ys[-1] / ys[0] - 1) if len(ys) >= 2 else None
    return {"eq": tot, "cagr": cagr, "mdd": md, "bench": bt, "bench_cagr": bc, "bench_mdd": mdb,
            "span": (edates[0], edates[-1]), "ntrade": len(trades), "empty": empty_days / len(edates),
            "ann": ann, "seg": seg, "trades": trades}


def fmt(m):
    return ("累计%+.0f%% 年化%+.1f%% 回撤%.0f%%｜基准%+.0f%%/年化%+.1f%%/回撤%.0f%%｜交易%d 空仓%.0f%%" % (
        m["eq"] * 100, m["cagr"] * 100, m["mdd"] * 100, m["bench"] * 100,
        m["bench_cagr"] * 100, m["bench_mdd"] * 100, m["ntrade"], m["empty"] * 100))


def main():
    sens = "--sensitivity" in sys.argv
    atr = "--atr" in sys.argv
    base = run(A_CODES, C_CODES)
    alt = run(A_CODES, [c for c in C_CODES if c != "159920"] + [X_CODE])
    print("基线(11只+159920) %s~%s" % base["span"])
    print(" 策略" + fmt(base))
    for y in sorted(base["ann"]):
        print("  %s %+.1f%%" % (y, base["ann"][y] * 100))
    for k, v in base["seg"].items():
        print("  %s %s" % (k, ("%+.1f%%" % (v * 100)) if v is not None else "—"))
    print("对照(159920→513180)" + fmt(alt))
    res = {"基线": base, "对照513180": alt}
    if sens:
        print("--- 敏感性(基线，累计/回撤/交易数) ---")
        grid = {}
        for rt in (0.20, 0.30, 0.40):
            for st in (-6.0, -8.0, -10.0):
                m = run(A_CODES, C_CODES, retrace=rt, stop=st)
                grid[(rt, st)] = m
                print("  回落%d%%/止损%d%%: 累计%+.0f%% 回撤%.0f%% n%d" % (
                    rt * 100, -st, m["eq"] * 100, m["mdd"] * 100, m["ntrade"]))
        res["sensitivity"] = grid
    if atr:
        print("--- ATR止损(基线，累计/回撤/交易数) ---")
        agrid = {}
        for rt in (0.20, 0.30, 0.40):
            for k in (2.0, 2.5, 3.0):
                m = run(A_CODES, C_CODES, retrace=rt, stop_mode="atr", atr_k=k)
                agrid[(rt, k)] = m
                print("  回落%d%%/ATR×%.1f: 累计%+.0f%% 回撤%.0f%% n%d" % (
                    rt * 100, k, m["eq"] * 100, m["mdd"] * 100, m["ntrade"]))
        res["atr"] = agrid
    rp = os.path.join(REPORT_ROOT, "ETF_MACD回测_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    os.makedirs(REPORT_ROOT, exist_ok=True)
    with open(rp, "w", encoding="utf-8") as f:
        f.write("# ETF MACD趋势跟踪回测 %s\n\n" % datetime.date.today())
        f.write("口径：A股MA10金叉MA20且收>MA50，跨境MA5金叉MA10且收>MA20；卖出DIF下穿DEA且20日高点回落≥30%%；"
                "每周最后一个交易日收盘算信号、下日开盘成交；三层防线510300<MA200清仓/组合回撤12%%休20天/单只-8%%；"
                "等权≤5只、超额按20日动量排序；单边费0.03%%包干；全程前复权同坐标（止损不用混坐标，见坑2）；"
                "区间%s~%s；基准510300买持。\n\n" % base["span"])
        for k in ("基线", "对照513180"):
            m = res[k]
            f.write("## %s\n策略累计%+.1f%% 年化%+.1f%% 回撤%.1f%%；基准累计%+.1f%% 年化%+.1f%% 回撤%.1f%%；交易%d 空仓%.0f%%。\n" % (
                k, m["eq"] * 100, m["cagr"] * 100, m["mdd"] * 100, m["bench"] * 100,
                m["bench_cagr"] * 100, m["bench_mdd"] * 100, m["ntrade"], m["empty"] * 100))
            f.write("年份：" + " ".join("%s%+.1f%%" % (y, v * 100) for y, v in sorted(m["ann"].items())) + "\n\n")
            f.write("分段：" + " ".join("%s%s" % (k2, ("%+.1f%%" % (v * 100)) if v is not None else "—") for k2, v in m["seg"].items()) + "\n\n")
        if "sensitivity" in res:
            f.write("## 敏感性\n\n")
            for (rt, st), m in sorted(res["sensitivity"].items()):
                f.write("- 回落%d%%/止损%d%%：累计%+.1f%% 回撤%.1f%% 交易%d\n" % (
                    rt * 100, -st, m["eq"] * 100, m["mdd"] * 100, m["ntrade"]))
            f.write("\n结论：回落20%%/止损6%%档(+54%%)与邻档(+6%%)翻脸，参数敏感、过拟合嫌疑成立；"
                    "默认档(30%%/-8%%)不作为最优推荐，仅作复刻口径。策略价值在降回撤(~-19%% vs 基准-45%%)，"
                    "不在超额。\n")
        if "atr" in res:
            f.write("\n## ATR止损（止损线=入场价-k×入场ATR14）\n\n")
            for (rt, k), m in sorted(res["atr"].items()):
                f.write("- 回落%d%%/ATR×%.1f：累计%+.1f%% 回撤%.1f%% 交易%d\n" % (
                    rt * 100, k, m["eq"] * 100, m["mdd"] * 100, m["ntrade"]))
            f.write("\n")
            lo = min(m["eq"] for m in res["atr"].values())
            hi = max(m["eq"] for m in res["atr"].values())
            med = sorted(m["eq"] for m in res["atr"].values())[4]
            f.write("ATR网格极差%.1f个百分点（固定止损版极差约50个百分点），中位数%+.1f%%（固定默认档%+.1f%%）。\n" % (
                (hi - lo) * 100, med * 100, base["eq"] * 100))
            if (hi - lo) < 0.15 and med >= base["eq"]:
                f.write("判定：ATR治住了参数敏感，可进入生产信号候选。\n")
            else:
                f.write("判定：ATR没治住参数敏感（最优格散落在对角线、无稳定高原），维持原结论，不转生产信号。\n")
        f.write("\n数据：data/kline_etf 12只（588000自2020-11-16、513180自2021-05-25），etf_kline落库幂等。\n")
    print("报告:%s" % rp)


if __name__ == "__main__":
    main()
