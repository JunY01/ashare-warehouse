# -*- coding: utf-8 -*-
"""研究层共用回测引擎原语（消除 sweep_*/backtest_*/validate_* 间的复制实现）。

原状：加权回归（wlinreg_score/score/wstats/score_one）在 4 个脚本各写一份，
MACD 金叉死叉（gold/dead）在 2 个脚本各写一份，常量 FEE/MAXN/REST_DAYS 重复。
本模块给出唯一实现，各脚本按各自返回口径适配。

纯标准库。
"""
import math

# MACD 回测共用参数
FEE = 0.0003
MAXN = 5
REST_DAYS = 20


def wlinreg_stats(closes, window):
    """加权对数线性回归分量，返回 (ann, r2, vol, dd_win)；数据不足/非法返回 None。

    y=收盘对数，权重越近越大(w=i+1)，年化=日log斜率×250，r2=加权R²，
    vol=日收益年化波动，dd_win=窗口内最大回撤（负值）。
    """
    y_all = closes[-window:]
    if len(y_all) < window or any(c is None or c <= 0 for c in y_all):
        return None
    ys = [math.log(c) for c in y_all]
    n = window
    ws = [i + 1 for i in range(n)]
    sw = sum(ws)
    mx = sum(ws[i] * i for i in range(n)) / sw
    my = sum(ws[i] * ys[i] for i in range(n)) / sw
    sxx = sum(ws[i] * (i - mx) ** 2 for i in range(n))
    if sxx <= 0:
        return None
    slope = sum(ws[i] * (i - mx) * (ys[i] - my) for i in range(n)) / sxx
    tot = sum(ws[i] * (ys[i] - my) ** 2 for i in range(n))
    if tot <= 0:
        return None
    res = sum(ws[i] * (ys[i] - (my + slope * (i - mx))) ** 2 for i in range(n))
    r2 = max(0.0, 1 - res / tot)
    ann = slope * 250
    rets = [ys[i] - ys[i - 1] for i in range(1, n)]
    m = sum(rets) / len(rets)
    vol = (sum((x - m) ** 2 for x in rets) / len(rets)) ** 0.5 * (250 ** 0.5)
    pk, dd = y_all[0], 0.0
    for c in y_all:
        pk = max(pk, c)
        dd = min(dd, c / pk - 1)
    return ann, r2, vol, dd


def gold(ma_f, ma_s, i):
    """第 i 根金叉（快线上穿慢线）。"""
    return (ma_f[i] is not None and ma_s[i] is not None and ma_f[i - 1] is not None
            and ma_s[i - 1] is not None and ma_f[i] > ma_s[i] and ma_f[i - 1] <= ma_s[i - 1])


def dead(dif, dea, i):
    """第 i 根死叉（DIF 下穿 DEA）。"""
    return (dif[i] is not None and dea[i] is not None and dif[i - 1] is not None
            and dea[i - 1] is not None and dif[i] < dea[i] and dif[i - 1] >= dea[i - 1])
