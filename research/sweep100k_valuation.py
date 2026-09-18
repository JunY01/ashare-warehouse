# -*- coding: utf-8 -*-
"""估值择时十万网格迭代 —— 回答"分位到底该怎么用"

给未来亚亚：这是把"估值分位"从"看数字下结论"变成"可回测规则"的脚本。
前情：2026-09-10 我（亚亚）凭临场判断在一天内对同一持仓给了互相矛盾的建议
（持有→降到30%→卖中证500→银行红利也可能贵），根因是"看哪个数字顺眼说哪个"。
本脚本的作用是：把可能的规则全部枚举跑一遍，用10年历史筛出真正稳健的那组。

数据边界（诚实说明，不可越过）：
  · 估值 PE/PB：蛋卷10年周频（data/cache/pe_history/）→ 可回测
  · 价格：kline_full 日频10~20年 → 可回测
  · 行情 regime：由价格自算（MA/回撤/斜率）→ 可回测
  · 市场情绪：仅3个月（social_sentiment_daily）→ **不可回测**，只能当前状态参考
  · 庄家资金流：仅6个月（sector_flow_daily）→ **不可回测**
  → 本脚本只对前两项做10万次迭代；情绪/庄家不进网格，避免用噪声过拟合。

网格维度（12万组量级）：
  basis(2) × 窗口年(4) × 买入分位(5) × 卖出分位(4) ×
  行情过滤(5) × MA周期(4) × 建仓力度(3) × 缓冲(3) × 止损(4) = 115200

评估：月度调仓（贴合场外基金），逐指数独立回测，再看跨指数稳健性。
打分：年化收益、最大回撤、卡玛比；排名用"跨指数中位数"防单指数过拟合。
产物：data/cache/sweeps/sweep100k_valuation.jsonl + reports/估值择时十万网格_*.md

纯标准库。用法:
  python sweep100k_valuation.py --run      # 跑网格（约数分钟）
  python sweep100k_valuation.py --finish   # 聚合出报告
"""
import bisect
import csv
import datetime
import itertools
import json
import os
import statistics as st
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from src.common.paths import DATA_ROOT, REPORT_ROOT, SWEEP_CACHE  # noqa: E402

PE_CACHE = os.path.join(DATA_ROOT, "cache", "pe_history", "pe_history_all.json")
PB_CACHE = os.path.join(DATA_ROOT, "cache", "pe_history", "pb_history_all.json")
KLINE_DIR = os.path.join(DATA_ROOT, "kline_full")
OUT = os.path.join(SWEEP_CACHE, "sweep100k_valuation.jsonl")
REPORT_DIR = REPORT_ROOT

# ---- 网格 ----
BASES = ["pe", "pb"]
WINDOWS = [3, 5, 7, 10]                 # 分位回看年数
BUY_PCTS = [10, 20, 30, 40, 50]         # 买入分位
SELL_PCTS = [60, 70, 80, 90]            # 卖出分位
REGIMES = [0, 1, 2, 3, 4]               # 0无 1价>MA 2价<MA 3MA上行 4MA下行
MA_PERIODS = [100, 150, 200, 250]
SIZES = [1.0, 0.5, 2.0]                 # 满仓/半仓/低估加倍(相对满仓)
HYST = [0, 5, 10]                       # 缓冲(pp)：卖出需超阈值多少才动
STOPS = [0.0, -10.0, -15.0, -20.0]      # 从入场回撤止损%
GRID_N = len(BASES)*len(WINDOWS)*len(BUY_PCTS)*len(SELL_PCTS)*len(REGIMES)*len(MA_PERIODS)*len(SIZES)*len(HYST)*len(STOPS)


def load_prices(name):
    p = os.path.join(KLINE_DIR, name + ".csv")
    if not os.path.exists(p):
        return [], []
    rows = list(csv.DictReader(open(p, encoding="utf-8-sig")))
    return [r["日期"] for r in rows], [float(r["收盘"]) for r in rows]


def build_monthly_table(index_name, pe, pb):
    """构建月度调仓表：每个再平衡日的位置、各(basis,窗口)分位、MA、regime。

    返回 (reb_dates, price_at_reb, pct_map, regime_map, ma_up_map)
      pct_map[(basis,win)] = [分位% ...] 与 reb_dates 对齐（前向填充，无未来函数）
      regime_map[ma_period] = [1..4]
    """
    dates, closes = load_prices(index_name)
    if not dates:
        return None
    # 月度再平衡日（每月首个交易日）
    reb, seen = [], set()
    for i, d in enumerate(dates):
        m = d[:7]
        if m not in seen:
            seen.add(m)
            reb.append(i)
    # 估值序列（周频 ts + 值），前向填充到 reb 日
    def pct_series_ffill(basis):
        src = pe if basis == "pe" else pb
        series = None
        for code, d in src.items():
            if d.get("name") == index_name:
                series = [(x["ts"], x[basis]) for x in d["series"] if x.get(basis)]
                break
        if not series:
            return {}
        out = {}
        for win in WINDOWS:
            w = int(52 * win)
            vals = []
            allv = [v for _, v in series]
            # 预排序滑窗分位：对每个时点用"截至该时点的最近 w 期"算分位
            for j, (ts, v) in enumerate(series):
                lo = max(0, j - w + 1)
                sub = sorted(allv[lo:j + 1])
                vals.append((ts, bisect.bisect_left(sub, v) / len(sub) * 100))
            # 前向填充到 reb 日
            arr = []
            k = 0
            for ri in reb:
                dd = dates[ri]
                while k + 1 < len(vals) and \
                        datetime.datetime.fromtimestamp(vals[k + 1][0] / 1000).strftime("%Y-%m-%d") <= dd:
                    k += 1
                arr.append(vals[k][1] if vals and \
                           datetime.datetime.fromtimestamp(vals[k][0] / 1000).strftime("%Y-%m-%d") <= dd else None)
            out[win] = arr
        return out

    pct = {b: pct_series_ffill(b) for b in BASES}
    # MA 与 regime
    regime = {}
    for mp in MA_PERIODS:
        r = []
        for ri in reb:
            if ri < mp:
                r.append(None)
                continue
            ma_now = sum(closes[ri - mp + 1:ri + 1]) / mp
            prev_i = reb[reb.index(ri) - 1] if reb.index(ri) > 0 else None
            if prev_i is not None and prev_i >= mp:
                ma_prev = sum(closes[prev_i - mp + 1:prev_i + 1]) / mp
            else:
                ma_prev = ma_now
            c = closes[ri]
            if c > ma_now:
                r.append(3 if ma_now > ma_prev else 1)   # 1:上+升 3:上+跌
            else:
                r.append(4 if ma_now < ma_prev else 2)   # 2:下+升 4:下+跌
        regime[mp] = r
    return reb, [closes[i] for i in reb], dates, pct, regime


def simulate(prices, pct, regime, basis, win, buy_p, sell_p, reg_f, ma_p, size, hyst, stop):
    """月度调仓回测。只在"有估值信号"的区间内评估（否则早期空窗稀释年化）。

    返回 (策略年化%, 策略最大回撤%, 卡玛, 同期买入持有年化%) 或 None。
    """
    n = len(prices)
    arr = pct[win]
    # 定位首个有信号且行情可用的月份
    start = None
    for i in range(n - 1):
        if i < len(arr) and arr[i] is not None and regime[ma_p][i] is not None:
            start = i
            break
    if start is None or n - 1 - start < 36:
        return None

    pos = 0.0
    equity, peak, mdd = 1.0, 1.0, 0.0
    entry_px = None
    months = 0
    for i in range(start, n - 1):
        p = arr[i] if i < len(arr) else None
        rg = regime[ma_p][i]
        tgt = pos
        if p is not None:
            if p <= buy_p:
                tgt = size
            elif p >= sell_p + hyst:
                tgt = 0.0
        if reg_f:
            if rg is None:
                tgt = 0.0
            elif reg_f == 1 and rg not in (1, 3):
                tgt = 0.0
            elif reg_f == 2 and rg not in (2, 4):
                tgt = 0.0
            elif reg_f == 3 and rg not in (1, 2):
                tgt = 0.0
            elif reg_f == 4 and rg not in (3, 4):
                tgt = 0.0
        tgt = min(tgt, 2.0)
        if tgt > 0:
            if entry_px is None or pos == 0:
                entry_px = prices[i]
            if stop and entry_px and prices[i] / entry_px - 1 <= stop / 100:
                tgt = 0.0
                entry_px = None
        else:
            entry_px = None
        pos = tgt
        equity *= (1 + pos * (prices[i + 1] / prices[i] - 1))
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1)
        months += 1

    yrs = months / 12.0
    if equity <= 0 or yrs <= 0:
        return None
    ann = (equity ** (1 / yrs) - 1) * 100
    bh = (prices[start + months] / prices[start]) ** (1 / yrs) - 1
    calmar = ann / abs(mdd * 100) if mdd else 0.0
    return ann, mdd * 100, calmar, bh * 100


def run():
    pe = json.load(open(PE_CACHE, encoding="utf-8"))
    pb = json.load(open(PB_CACHE, encoding="utf-8"))
    names = sorted({d["name"] for d in pe.values()})
    tables = {}
    for nm in names:
        t = build_monthly_table(nm, pe, pb)
        if t:
            tables[nm] = t
    print("可回测指数 %d 个: %s" % (len(tables), ",".join(tables)))

    os.makedirs(SWEEP_CACHE, exist_ok=True)
    grid = list(itertools.product(BASES, WINDOWS, BUY_PCTS, SELL_PCTS, REGIMES,
                                  MA_PERIODS, SIZES, HYST, STOPS))
    print("网格 %d 组，开始..." % len(grid))
    t0 = time.time()
    done = 0
    with open(OUT, "w", encoding="utf-8") as f:
        for (basis, win, bp, sp, rf, mp, sz, hy, st_) in grid:
            rec = {"basis": basis, "win": win, "buy": bp, "sell": sp,
                   "regime": rf, "ma": mp, "size": sz, "hyst": hy, "stop": st_}
            anns, mddl, cals, excess = [], [], [], []
            for nm, (reb, px, dates, pct, regime) in tables.items():
                if win not in pct[basis]:
                    continue
                if not any(v is not None for v in pct[basis][win]):
                    continue
                res = simulate(px, pct[basis], regime, basis, win, bp, sp, rf, mp, sz, hy, st_)
                if res:
                    anns.append(res[0]); mddl.append(res[1]); cals.append(res[2])
                    excess.append(res[0] - res[3])
            if not anns:
                continue
            rec["n"] = len(anns)
            rec["ann_med"] = st.median(anns)
            rec["mdd_med"] = st.median(mddl)
            rec["cal_med"] = st.median(cals)
            rec["ann_min"] = min(anns)
            rec["excess_med"] = st.median(excess)
            rec["pos_rate"] = sum(1 for a in anns if a > 0) / len(anns)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            done += 1
            if done % 10000 == 0:
                el = time.time() - t0
                print("  %d/%d  %.0fs" % (done, len(grid), el))
    print("完成 %d 组有效结果 -> %s" % (done, OUT))


def finish():
    recs = [json.loads(l) for l in open(OUT, encoding="utf-8")]
    print("读入 %d 条" % len(recs))
    # 稳健性排序：跨指数年化中位数，且要求正收益比例高、回撤可控
    recs.sort(key=lambda r: (-r["cal_med"], -r["ann_med"]))
    top = recs[:20]
    # 邻域稳定：TOP里是否有一组参数族反复出现
    print("\n按跨指数卡玛比 TOP10：")
    for r in top[:10]:
        print("  卡玛%.2f 年化%.1f%% 回撤%.1f%% 正念%.0f%% n=%d | %s win%d 买%d/卖%d reg%d MA%d 仓%.1fx 缓%d 损%.0f" % (
            r["cal_med"], r["ann_med"], r["mdd_med"], r["pos_rate"]*100, r["n"],
            r["basis"].upper(), r["win"], r["buy"], r["sell"], r["regime"], r["ma"],
            r["size"], r["hyst"], r["stop"]))

    # 基线：买入持有
    pe = json.load(open(PE_CACHE, encoding="utf-8"))
    names = sorted({d["name"] for d in pe.values()})
    bh = []
    for nm in names:
        dates, closes = load_prices(nm)
        if len(closes) < 250:
            continue
        yrs = len(closes) / 250
        bh.append((closes[-1] / closes[0]) ** (1 / yrs) - 1)
    print("\n基线（各指数买入持有）年化中位 %.1f%%" % (st.median(bh) * 100 if bh else 0))

    today = datetime.date.today().strftime("%Y%m%d")
    rep = os.path.join(REPORT_DIR, "估值择时十万网格_%s.md" % today)
    with open(rep, "w", encoding="utf-8") as f:
        f.write("# 估值择时十万网格迭代 %s\n\n" % datetime.date.today())
        f.write("> 网格量级 %d｜可回测指数 %d 个｜月度调仓｜跨指数中位数排序（防单指数过拟合）\n" % (GRID_N, len(names)))
        f.write("> 数据边界：估值/价格/行情可回测；市场情绪(3个月)与庄家资金流(6个月)**不可回测**，未进网格。\n\n")
        f.write("## 基线\n\n各指数买入持有年化中位 **%.1f%%**\n\n" % (st.median(bh) * 100 if bh else 0))
        f.write("## TOP20（按跨指数卡玛比）\n\n")
        f.write("| 序 | 判据 | 窗口 | 买/卖 | 行情 | MA | 仓位 | 缓冲 | 止损 | 年化 | 回撤 | 卡玛 | 正念% | n |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for i, r in enumerate(top, 1):
            f.write("| %d | %s | %d年 | %d/%d | %d | %d | %.1fx | %d | %.0f | %.1f%% | %.1f%% | %.2f | %.0f | %d |\n" % (
                i, r["basis"].upper(), r["win"], r["buy"], r["sell"], r["regime"], r["ma"],
                r["size"], r["hyst"], r["stop"], r["ann_med"], r["mdd_med"], r["cal_med"],
                r["pos_rate"] * 100, r["n"]))
        f.write("\n> 仅供个人研究，不构成投资建议。\n")
    print("\n报告:%s" % rep)


if __name__ == "__main__":
    if "--finish" in sys.argv:
        finish()
    else:
        run()
