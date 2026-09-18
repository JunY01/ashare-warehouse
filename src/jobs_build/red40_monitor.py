# -*- coding: utf-8 -*-
"""红利 40 日收益差监控（14:30 报告 E 区）—— 把研究结论变成每天一行尺子

指标口径（ETF大白《再探40日收益差》，本仓库复现见 reports/红利40日收益差择时_20260916.md、
十万次迭代见 reports/红利40日收益差十万次迭代_20260916.md）：

    40日收益差 = 红利指数 40 日涨跌幅 − 大盘 40 日涨跌幅（百分点）

买卖边界用**固定历史分位**（模型2，迭代证实优于年线±kσ的模型1），并按迭代结论取"上宽下窄"：
买点 40% 分位（激进档 20%）、卖点 85% 分位（激进档 90%）。十万次随机取样的结论是：
① 价格口径平均超额 +1.0%，**含分红口径只有 -0.2%**（空仓要放弃 4% 的票息）；
② 卖点放到 85~90% 分位、买点 20~40% 分位最优，"别卖太早、别等太深"；
③ 三只里中证红利可用，**中证红利低波动在这套规则下三种口径全为负**；
④ 因此卖点只减仓不清仓（"减半"把含分红口径从 -0.84% 救到 -0.23%）。

数据与口径的两条硬约束：
  · 基准用**中证全指（官方源）**，即文章里的 Wind全A 口径。换成沪深300 会让今天
    从"持有"变成"卖点"——分母选错，结论直接反。
  · **只用收盘确认数据**，不做盘中估算：40 日收益差单日能跳 2~4 个百分点
    （2026-09-15→09-16 上证红利 −4.3pp），盘中值没有交易价值；中证官网当日数据
    晚上才发布，故红利低波动通常比别的滞后一天，报告里逐条标数据日。

本模块纯读 `index_daily`（readonly），不联网、不落库。取数归 src/jobs_fetch/fetch_dividend_index.py。
"""
import os
import statistics as st
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "src", "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from src.common import history_db  # noqa: E402

BENCH, BENCH_NAME = "sh000985", "中证全指"
BENCH_ALT, BENCH_ALT_NAME = "sz399317", "国证A指"     # 中证全指取不到时的备用基准
DIVS = [("sh000922", "中证红利"), ("sh000015", "上证红利"), ("csH30269", "中证红利低波动")]
FOCUS = "sh000922"                                     # 迭代选出的推荐标的
WINDOW = 40
MIN_HIST = 500                                         # 分位最少需要的观测数
# 阶梯档位（2026-09-16 用 5000 个随机窗口做同窗配对检验定档，见
# reports/红利40日收益差十万次迭代_20260916.md 第 7 节）：
#   买点1 40%分位 → 建半仓；买点2 20%分位 → 买满
#   卖点1 85%分位 → 减到半仓；卖点2 90%分位 → 减到 1/3（**不清仓**）
# 配对结论：两档买比一次买满 +0.28pp/年（61% 窗口更好；分三档无额外好处）；
#           到卖点减仓比不减仓 +0.47~0.74pp/年（70~82% 窗口更好）；
#           卖点2 减到 1/3 比清仓 +0.26pp/年（清仓只在 31% 的窗口更好）。
BUY1, BUY2 = 40, 20
SELL1, SELL2 = 85, 90
BUY1_POS, BUY2_POS = 0.5, 1.0
SELL1_POS, SELL2_POS = 0.5, 1 / 3.0
# 本尺子不适用的标的（十万次迭代：含分红口径下正超额仅 23.6%）
NOT_APPLICABLE = {"csH30269"}


def _series(conn, code):
    return {d: c for d, c in conn.execute(
        "SELECT date, close FROM index_daily WHERE code=? ORDER BY date", (code,)) if c}


def spread(dates, div, bench):
    """按共同交易日对齐后的 40 日收益差序列（前 40 个点为 None）。"""
    out = [None] * len(dates)
    for i in range(WINDOW, len(dates)):
        out[i] = 100 * ((div[dates[i]] / div[dates[i - WINDOW]] - 1)
                        - (bench[dates[i]] / bench[dates[i - WINDOW]] - 1))
    return out


def _quant(base, q):
    if q <= 0:
        return min(base)
    if q >= 100:
        return max(base)
    return st.quantiles(base, n=100)[int(q) - 1]


def read_one(conn, code, bench, name):
    """单只红利指数的读数。返回 None 表示样本不足。"""
    div = _series(conn, code)
    if not div:
        return None
    dates = sorted(set(div) & set(bench))
    if len(dates) < WINDOW + MIN_HIST:
        return None
    diff = spread(dates, div, bench)
    vals = [x for x in diff if x is not None]
    cur = diff[-1]
    q = _quant(vals, BUY1)
    q2 = _quant(vals, BUY2)
    s1 = _quant(vals, SELL1)
    s2 = _quant(vals, SELL2)
    pct = 100.0 * sum(1 for x in vals if x <= cur) / len(vals)
    signal = "买点" if cur < q else ("卖点" if cur > s1 else "持有")
    prev = diff[-2] if len(diff) > 1 else None
    return {"code": code, "name": name, "asof": dates[-1], "diff": cur, "pct": pct,
            "buy1": q, "buy2": q2, "sell1": s1, "sell2": s2, "signal": signal,
            "gap_buy": q - cur, "gap_sell": s1 - cur, "prev": prev,
            "dod": (cur - prev) if prev is not None else None,
            "n": len(vals), "applicable": code not in NOT_APPLICABLE}


def red40_monitor():
    """返回 {asof, bench, fallback, items}；库不可用/取不到基准时 items 为空。"""
    try:
        conn = history_db.connect(readonly=True)
    except Exception:
        return {"items": [], "bench": BENCH_NAME, "fallback": False, "note": "历史库不可读"}
    try:
        bench, bn, fb = _series(conn, BENCH), BENCH_NAME, False
        if len(bench) < MIN_HIST:
            bench, bn, fb = _series(conn, BENCH_ALT), BENCH_ALT_NAME, True
        if len(bench) < MIN_HIST:
            return {"items": [], "bench": bn, "fallback": fb, "note": "基准指数无数据"}
        items = [x for x in (read_one(conn, c, bench, n) for c, n in DIVS) if x]
        return {"items": items, "bench": bn, "fallback": fb,
                "asof": max(x["asof"] for x in items) if items else ""}
    finally:
        conn.close()


def red40_lines(m, with_header=True):
    """渲染成报告行（4~7 行）。m 为 red40_monitor() 的返回。"""
    if not m.get("items"):
        return ["- 红利 40 日收益差不可用（%s）。" % m.get("note", "无数据")]
    L = []
    if with_header:
        L.append("## E区·红利 40 日收益差（红利择时尺·不直接下单）\n")
    divs = {x["code"]: x["name"] for x in m["items"]}
    L.append("- 基准 %s%s｜样本 %d 个交易日｜阶梯：买点≤%d%%分位建半仓、≤%d%%分位买满；"
             "卖点≥%d%%分位减到半仓、≥%d%%分位减到 1/3（不清仓）"
             % (m["bench"], "（备用基准）" if m["fallback"] else "", m["items"][0]["n"],
                BUY1, BUY2, SELL1, SELL2))
    for x in m["items"]:
        head = "**%s**" % x["name"] if x["code"] == FOCUS else x["name"]
        tail = "" if x["applicable"] else "｜⚠ 本尺子在这只上无效（迭代含分红口径为负），只列示不操作"
        dod = "" if x["dod"] is None else "，单日 %+.2fpp" % x["dod"]
        if x["signal"] == "买点":
            act = "**★买点**：≤%.2f%% 已到——先建到半仓（跌破 %.2f%% 再买满）" % (x["buy1"], x["buy2"])
        elif x["signal"] == "卖点":
            act = "**★卖点**：≥%.2f%% 已到——减到半仓（≥%.2f%% 再减到 1/3，**不清仓**）" % (
                x["sell1"], x["sell2"])
        else:
            act = "**持有不动**：还要再跌 %.2fpp（≤%.2f%%）才到买点、再涨 %.2fpp（≥%.2f%%）才到卖点" % (
                x["diff"] - x["buy1"], x["buy1"], x["sell1"] - x["diff"], x["sell1"])
        L.append("- %s %+.2f%%（%s，%.0f 分位%s）→ %s%s" % (
            head, x["diff"], x["asof"], x["pct"], dod, act, tail))
    L.append("- 用法：底仓常持吃票息，尺子只管仓位档位（半仓↔满仓↔1/3 底仓）；同窗配对检验里"
             "「到卖点就减」比「不减」每年多 0.47~0.74pp，而「减多少」差异很小，所以别纠结幅度。")
    L.append("- ⚠ 单日可跳 2~4 个百分点，约 1/3 的越线两日内会反复：卡线当天别急着动手，看两天。")
    return L


if __name__ == "__main__":
    for line in red40_lines(red40_monitor()):
        print(line)
