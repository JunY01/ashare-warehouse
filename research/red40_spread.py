# -*- coding: utf-8 -*-
"""红利 40 日收益差验证台 —— "红利相对大盘跑过头了没有"

指标口径（ETF大白《再探40日收益差》）：每日滚动比较**过去 40 个交易日**
红利指数与大盘的涨跌幅差值（单位：百分点）

    收益差_t = (红利_t/红利_{t-40} - 1)*100 - (大盘_t/大盘_{t-40} - 1)*100

"跌过头了会涨、涨过头了会跌"，所以把它当均值回复的摆：低到历史低分位→买，
高到高分位→卖。两种定边界的办法：
  模型1  年线(MA250)为轴 ± k 倍标准差 —— 边界随时间漂移（只看近一年）
  模型2  历史分位固定边界 —— 用全历史排序定 A/B 分位（文章结论：模型2 更好）

本台子两个诊断（只打印证据，不写报告、不落库）：
  --scan       当前收益差落在历史什么位置；各档分位对应的收益差值；距买/卖点还差几个点
  --backtest   复现模型1/模型2，对比买入持有；分"全样本分位(文章口径)"与
               "扩张窗口分位(可实盘：只用截止当日的历史)"两套，后者才是能真跑的口径

数据源：market_history.db 的 index_daily（红利家族 + 大盘基准的日收盘，见 --div/--mkt）。
大盘基准用国证A指(sz399317, 全市场)代理文章里的 Wind全A；中证全指在腾讯源上有缺日，
不可用。纯标准库。

用法:
  python research/red40_spread.py --scan
  python research/red40_spread.py --backtest
  python research/red40_spread.py --scan --div sz399324
"""
import argparse
import os
import statistics as st
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "src", "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from src.common import history_db  # noqa: E402

WIN = 40                       # 收益差窗口（约两个月）
DIVS = {                       # 红利指数 -> index_daily.code
    "sh000922": "中证红利",
    "sh000015": "上证红利",
    "sz399324": "深证红利",
    "csH30269": "中证红利低波动",
}
MKT = "sz399317"               # 国证A指 = 全市场基准（Wind全A 代理）
BANDS = [(20, 80), (30, 85), (32, 83), (40, 85), (30, 80)]   # (买入分位, 卖出分位)
YEARS_FULL = "2013-01-04"      # 模型2 的全样本起点
ROLL = 2400                    # "10年滚动"口径的窗口长度（≈10年交易日）


def load(code, start):
    with history_db.connect(readonly=True) as conn:
        rows = conn.execute("SELECT date, close FROM index_daily WHERE code=? AND date>=? "
                            "ORDER BY date", (code, start)).fetchall()
    return {d: c for d, c in rows if c}


def spread_series(div, mkt, start):
    """按共同交易日对齐后算 40 日收益差"""
    d, m = load(div, start), load(mkt, start)
    dates = sorted(set(d) & set(m))
    diff = [None] * len(dates)
    for i in range(WIN, len(dates)):
        diff[i] = 100 * ((d[dates[i]] / d[dates[i - WIN]] - 1) - (m[dates[i]] / m[dates[i - WIN]] - 1))
    return dates, diff


def quant(base, q):
    if q >= 100:
        return max(base)
    if q <= 0:
        return min(base)
    return st.quantiles(base, n=100)[int(q) - 1]


def pct_rank(v, base):
    return 100.0 * sum(1 for x in base if x <= v) / len(base)


def scan(div_code, div_name, mkt, start):
    dates, diff = spread_series(div_code, mkt, start)
    valid = [x for x in diff if x is not None]
    cur = diff[-1]
    print("\n=== %s (%s) 基准=%s  样本 %s~%s n=%d ===" % (div_name, div_code, mkt, dates[0], dates[-1], len(valid)))
    print("最新收益差 %.2f%%  历史分位 %.1f%%  均值 %.2f  中位 %.2f  标准差 %.2f  (min %.2f / max %.2f)" % (
        cur, pct_rank(cur, valid), st.mean(valid), st.median(valid), st.pstdev(valid), min(valid), max(valid)))
    print("  %-12s %9s %9s %9s %9s %s" % ("边界(买/卖分位)", "买点收益差", "卖点收益差", "距买点", "距卖点", "信号"))
    for A, B in BANDS:
        lo, hi = quant(valid, A), quant(valid, B)
        if cur < lo:
            where = "★买点(收在买点下方)"
        elif cur > hi:
            where = "★卖点(收在卖点上方)"
        else:
            where = "持有"
        print("  %-12s %8.2f%% %8.2f%% %+8.2f%% %+8.2f%% %s" % (
            "(%d%%,%d%%)" % (A, B), lo, hi, lo - cur, hi - cur, where))
    return cur, valid


def run(dates, ret, cash, sig, start_pos=1):
    pos, curve, trades = start_pos, [1.0], []
    ein, eeq = 0, 1.0
    held = start_pos
    for i in range(1, len(dates)):
        if sig[i - 1] == 1 and pos == 0:
            pos, ein, eeq = 1, i, curve[-1]
        elif sig[i - 1] == -1 and pos == 1:
            pos = 0
            trades.append(curve[-1] / eeq - 1)
        held += pos
        curve.append(curve[-1] * (1 + (ret[i] if pos else cash)))
    if pos and ein:
        trades.append(curve[-1] / eeq - 1)
    n = len(dates) - 1
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    wins = [t for t in trades if t > 0]
    los = [t for t in trades if t <= 0]
    return dict(ann=curve[-1] ** (250.0 / n) - 1, mdd=mdd, n=len(trades),
                per_year=len(trades) / (n / 250.0),
                held=held / n,
                win=(len(wins) / len(trades) if trades else float("nan")),
                pf=(st.mean(wins) / abs(st.mean(los)) if wins and los else float("nan")))


def sig_pct(diff, A, B, mode):
    n = len(diff)
    sig = [None] * n
    valid = [x for x in diff if x is not None]
    for i in range(n):
        if diff[i] is None:
            continue
        if mode == "full":
            base = valid
        elif mode == "exp":
            base = [x for x in diff[:i] if x is not None]
        else:
            base = [x for x in diff[max(0, i - ROLL):i] if x is not None]
        if len(base) < 500:
            continue
        if diff[i] < quant(base, A):
            sig[i] = 1
        elif diff[i] > quant(base, B):
            sig[i] = -1
    return sig


def sig_ma(diff, k_lo, k_hi, w=250):
    n = len(diff)
    sig = [None] * n
    for i in range(n):
        if diff[i] is None or i < w:
            continue
        base = [x for x in diff[i - w:i] if x is not None]
        mu, sd = st.mean(base), st.pstdev(base)
        if diff[i] < mu - k_lo * sd:
            sig[i] = 1
        elif diff[i] > mu + k_hi * sd:
            sig[i] = -1
    return sig


def backtest(div_code, div_name, mkt, start, dy):
    dates, diff = spread_series(div_code, mkt, start)
    d = load(div_code, start)
    ret = [0.0] * len(dates)
    for i in range(1, len(dates)):
        ret[i] = d[dates[i]] / d[dates[i - 1]] - 1 + (dy / 100.0) / 250
    cash = 0.0
    h = run(dates, ret, cash, [None] * len(dates))
    print("\n=== %s  %s~%s  买入持有 年化%.2f%% 回撤%.1f%% ===" % (div_name, dates[0], dates[-1], h["ann"] * 100, h["mdd"] * 100))
    print("  %-10s %-14s %8s %8s %8s %6s %6s %8s %7s" % ("模型", "边界", "年化", "超额", "回撤", "胜率", "赔率", "次/年", "在场"))
    for mode, ml in [("full", "全样本"), ("exp", "扩张(实盘)"), ("roll", "10年滚动")]:
        for A, B in BANDS:
            r = run(dates, ret, cash, sig_pct(diff, A, B, mode))
            print("  %-10s (%d%%,%d%%) %7.2f%% %+7.2f%% %7.1f%% %5.0f%% %6.2f %8.2f %6.0f%%" % (
                "M2 " + ml, A, B, r["ann"] * 100, (r["ann"] - h["ann"]) * 100, r["mdd"] * 100,
                r["win"] * 100, r["pf"], r["per_year"], r["held"] * 100))
    for kl, kh in [(2.0, 2.0), (1.5, 1.5), (1.0, 1.0), (0.5, 0.5), (0.5, 1.0), (1.0, 1.5)]:
        r = run(dates, ret, cash, sig_ma(diff, kl, kh))
        print("  %-10s %.1fσ/%.1fσ    %7.2f%% %+7.2f%% %7.1f%% %5.0f%% %6.2f %8.2f %6.0f%%" % (
            "M1 年线", kl, kh, r["ann"] * 100, (r["ann"] - h["ann"]) * 100, r["mdd"] * 100,
            r["win"] * 100, r["pf"], r["per_year"], r["held"] * 100))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="打印当前收益差与各档分位边界")
    ap.add_argument("--backtest", action="store_true", help="复现模型1/模型2 并对比买入持有")
    ap.add_argument("--div", default=None, help="只算某只红利指数（默认全部）")
    ap.add_argument("--mkt", default=MKT, help="大盘基准 code（默认国证A指 sz399317）")
    ap.add_argument("--start", default=YEARS_FULL, help="样本起点")
    ap.add_argument("--div-yield", type=float, default=0.0, help="红利年化分红率，用于含分红口径（如 4.22）")
    a = ap.parse_args()
    if not (a.scan or a.backtest):
        ap.error("至少给一个 --scan / --backtest")
    target = {a.div: DIVS.get(a.div, a.div)} if a.div else DIVS
    for code, name in target.items():
        if a.scan:
            scan(code, name, a.mkt, a.start)
        if a.backtest:
            backtest(code, name, a.mkt, a.start, a.div_yield)


if __name__ == "__main__":
    main()
