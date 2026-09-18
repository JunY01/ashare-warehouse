# -*- coding: utf-8 -*-
"""场外基金交易费用与费后收益模拟（iterate/zhuang_line 系研究共用）

fee: 场外赎回费率按持有天数阶梯（7天内1.5%、30天内0.5%、之后0）
disc_fee: 按止盈/止损/持有日规则模拟一次持有期的费后收益（%）
"""


def fee(hold_days):
    if hold_days < 7:
        return 1.5
    if hold_days < 30:
        return 0.5
    return 0.0


def disc_fee(path, tp, sl, hold):
    n = min(hold, len(path) - 1)
    if n <= 0:
        return 0.0
    for k in range(1, n + 1):
        r = (path[k] - 1) * 100
        if r <= sl:
            return sl - fee(k)
        if r >= tp:
            return tp - fee(k)
    return (path[n] - 1) * 100 - fee(n)