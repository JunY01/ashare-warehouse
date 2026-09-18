# -*- coding: utf-8 -*-
"""做T规则量化验证（底仓1/3做T vs 死拿）

给未来亚亚：复刻 t_trade_signal.py 的买卖口径到日K回测，回答“T到底增不增厚”。
与原规则一处差异：原规则T份额超10天未达标→休眠变死拿（单次，需手动重拨额度，
log里几乎没真实用过）；回测用轮动sleeve（超10天未达标按市价退出释放资金），
否则10年只交易0-1次、测不出期望——报告里如实记这笔差异，休眠率单独统计。
规则：卖出腿=较锚+2%且满7天且未回落（收盘≥近5日最高×0.992）→ 卖；
买入腿=空仓时较昨收≤-2%且超卖（连跌≥3天或单日≤-3%），每周≤2次；
超10天未达标→市价退出（原规则叫休眠）。锚=本轮买入价。
费率（场外老份额）：买入0，卖出<7天1.5%/7-29天0.5%/≥30天0。
组合=2/3死拿+1/3做T，对比100%死拿。纯标准库。
用法: python backtest_t_trade.py
"""
import csv
import datetime
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
from src.common.paths import DATA_ROOT
KDIR = os.path.join(DATA_ROOT, "kline_10y")
REPORT_DIR = os.path.join(REPO_ROOT, "reports")

SELL_GAIN = 2.0  # 卖出腿：较锚反弹%
BUY_DROP = -2.0  # 买入腿：较昨收跌幅%
FADE_TOL = 0.992  # 未冲高回落：收盘≥近5日最高×此值
MIN_HOLD = 7  # 满7天才赎（过1.5%惩罚线）
MAX_HOLD = 10  # 超10天未达标休眠
WEEK_BUY_MAX = 2


def fee_pct(hold_days):
    if hold_days < 7:
        return 1.5
    if hold_days < 30:
        return 0.5
    return 0.0


def load(name):
    with open(os.path.join(KDIR, name + ".csv"), encoding="utf-8-sig") as f:
        return [(r["日期"], float(r["收盘"])) for r in csv.DictReader(f)]


def run(series, sell_gain=SELL_GAIN, buy_drop=BUY_DROP):
    """轮动sleeve回测，返回指标dict。全程只用当日及以前数据。
    T资金独立轮动：空仓等买点→持有→达标卖出/超10天市价退出，不永久休眠
    （原规则休眠需手动重拨额度，log里无实盘，测不出期望，差异已记档）。"""
    dates = [d for d, _ in series]
    c0 = series[0][1]
    eq_t, eq_b = 1.0, 1.0
    t_cash, holding = 1 / 3, False
    t_value, anchor, buy_date = 0.0, 0.0, ""
    just_bought = False
    week, week_n = None, 0
    downs, n, wins, stale_n, fees = 0, 0, 0, 0, 0.0
    peak_t, peak_b, mdd_t, mdd_b = 1.0, 1.0, 0.0, 0.0
    just_bought = False
    for i in range(1, len(series)):
        d, c = series[i]
        prev = series[i - 1][1]
        wk = d[:7] + "W" + str(datetime.date.fromisoformat(d).isocalendar()[1])
        if wk != week:
            week, week_n = wk, 0
        chg = (c / prev - 1) * 100
        downs = downs + 1 if chg < 0 else 0
        if holding and not just_bought:
            t_value = t_value * (c / prev)
        just_bought = False
        if holding:
            hi5 = max(p for _, p in series[max(0, i - 4):i + 1])
            age = i - dates.index(buy_date)
            gain = (c / anchor - 1) * 100
            if gain >= sell_gain and age >= MIN_HOLD and c >= hi5 * FADE_TOL:
                f = fee_pct(age)  # 达标卖出
                t_cash = t_value * (1 - f / 100)
                fees += t_value * f / 100  # 换算到组合口径
                wins += 1 if gain - f > 0 else 0
                n += 1
                holding, t_value = False, 0.0
            elif age > MAX_HOLD and gain < sell_gain:
                f = fee_pct(age)  # 超期未达标市价退出（原规则叫休眠，此处释放资金）
                t_cash = t_value * (1 - f / 100)
                fees += t_value * f / 100
                wins += 1 if gain - f > 0 else 0
                n += 1
                stale_n += 1
                holding, t_value = False, 0.0
        else:
            oversold = downs >= 3 or chg <= -3.0
            if chg <= buy_drop and oversold and week_n < WEEK_BUY_MAX and i + 1 < len(series):
                # 场外按次日净值买回：现金全额转份额，锚=次日收盘
                nxt, nxt_d = series[i + 1][1], series[i + 1][0]
                t_value = t_cash
                anchor, buy_date, holding = nxt, nxt_d, True
                just_bought = True
                week_n += 1
        eq_t = 2 / 3 * (c / c0) + (t_value if holding else t_cash)
        eq_b = c / c0
        peak_t, peak_b = max(peak_t, eq_t), max(peak_b, eq_b)
        mdd_t, mdd_b = min(mdd_t, eq_t / peak_t - 1), min(mdd_b, eq_b / peak_b - 1)
    return {"t": eq_t - 1, "b": eq_b - 1, "n": n, "win": wins / n if n else 0,
            "stale": stale_n / n if n else 0, "fee": fees,
            "mdd_t": mdd_t, "mdd_b": mdd_b, "span": (dates[0], dates[-1])}


def main():
    res = {}
    series = {}
    for name in ("恒生科技", "沪深300"):
        try:
            series[name] = load(name)
            res[name] = run(series[name])
        except FileNotFoundError:
            print("%s无K线，跳过" % name)
    for name, m in res.items():
        print("%s %s~%s 做T%+.1f%% 死拿%+.1f%% 超额%+.1f%% %d轮胜率%.0f%% 超期退出%.0f%% 费%.2f%% 回撤%.1f/%.1f" % (
            name, m["span"][0], m["span"][1], m["t"] * 100, m["b"] * 100,
            (m["t"] - m["b"]) * 100, m["n"], m["win"] * 100, m["stale"] * 100,
            m["fee"] * 100, m["mdd_t"] * 100, m["mdd_b"] * 100))
    if "--grid" in sys.argv:  # 迷你敏感性：卖出×买入 3×3=9组，验证默认+2/-2
        print("--- 敏感性(恒生科技，超额/胜率/轮数) ---")
        for sg in (1.0, 2.0, 3.0):
            row = []
            for bd in (-1.0, -2.0, -3.0):
                g = run(series["恒生科技"], sg, bd)
                row.append("卖+%.0f/买%.0f:超额%+.1f%%胜%.0f%%n%d" % (
                    sg, bd, (g["t"] - g["b"]) * 100, g["win"] * 100, g["n"]))
            print("  " + " ｜ ".join(row))
    rp = os.path.join(REPORT_DIR, "做T回测_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(rp, "w", encoding="utf-8") as f:
        f.write("# 做T规则验证 %s\n\n口径：底仓1/3轮动做T（+2%%卖/-2%%买/满7天/周≤2次/超10天市价退出），费率<7天1.5%%/7-29天0.5%%。\n\n" % datetime.date.today())
        for name, m in res.items():
            f.write("## %s %s~%s\n做T组合%+.1f%%，死拿%+.1f%%，超额%+.1f%%，%d轮胜率%.0f%%（超期退出%.0f%%），回撤%.1f%%/%.1f%%。\n\n" % (
                name, m["span"][0], m["span"][1], m["t"] * 100, m["b"] * 100,
                (m["t"] - m["b"]) * 100, m["n"], m["win"] * 100, m["stale"] * 100,
                m["mdd_t"] * 100, m["mdd_b"] * 100))
        f.write("结论：超额为正且胜率≥50%%才算T有价值；与原规则差异：原休眠永久停T（需手动重拨额度），此处超期退出释放资金，否则10年仅0-1轮测不出期望。\n")
    print("报告:%s" % rp)


if __name__ == "__main__":
    main()
