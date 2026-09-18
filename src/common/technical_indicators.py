# -*- coding: utf-8 -*-
"""技术指标库 - 纯Python标准库实现

提供常用技术指标的实现，所有函数仅依赖Python标准库。
设计原则：
1. 输入和输出为列表，便于与现有代码集成
2. 不足数据点时返回None填充
3. 实现与主流交易平台一致的算法
4. 充分注释以确保可维护性

已实现指标：
- EMA (Exponential Moving Average)
- RSI (Relative Strength Index)
- MACD (Moving Average Convergence Divergence)
- Bollinger Bands
- Stochastic Oscillator (需High/Low/Close数据)
- Williams %R
- CCI (Commodity Channel Index) 
"""

import math
from typing import List, Optional, Tuple


def ema(values: List[Optional[float]], span: int) -> List[Optional[float]]:
    """指数移动平均线 (Exponential Moving Average)
    
    Args:
        values: 价格序列（可以包含None）
        span: EMA周期
        
    Returns:
        EMA序列，前期不足的位置为None
    """
    if not values or span <= 0:
        return [None] * len(values)
    
    # 找到第一个非None值作为起点
    start_idx = 0
    while start_idx < len(values) and values[start_idx] is None:
        start_idx += 1
    
    if start_idx >= len(values):
        return [None] * len(values)
    
    result = [None] * len(values)
    multiplier = 2.0 / (span + 1)
    
    # 第一次EMA值简单取第一个有效值
    ema_val = values[start_idx]
    result[start_idx] = ema_val
    
    # 计算后续EMA值
    for i in range(start_idx + 1, len(values)):
        if values[i] is not None:
            ema_val = (values[i] * multiplier) + (ema_val * (1 - multiplier))
        else:
            ema_val = ema_val * (1 - multiplier)  # 保持前值的衰减
        result[i] = ema_val
    
    return result


def rsi(values: List[Optional[float]], period: int = 14) -> List[Optional[float]]:
    """相对强弱指数 (Relative Strength Index)
    
    Args:
        values: 价格序列（收盘价）
        period: RSI周期，默认14
        
    Returns:
        RSI序列 (0-100)，前期不足的位置为None
    """
    if not values or len(values) < period + 1:
        return [None] * len(values)
    
    # 计算价格变化
    deltas = []
    for i in range(1, len(values)):
        if values[i] is not None and values[i-1] is not None:
            deltas.append(values[i] - values[i-1])
        else:
            deltas.append(0.0)  # 无法计算变化时视为0
    
    # 前期补None
    deltas = [None] + deltas
    
    result = [None] * len(values)
    
    # 计算首次平均收益和平均损失
    gains = []
    losses = []
    for i in range(1, period + 1):
        if deltas[i] is not None:
            if deltas[i] >= 0:
                gains.append(deltas[i])
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(deltas[i]))
        else:
            gains.append(0.0)
            losses.append(0.0)
    
    if period == 0:
        return [None] * len(values)
        
    avg_gain = sum(gains) / period if period > 0 else 0
    avg_loss = sum(losses) / period if period > 0 else 0
    
    # 计算第一个RSI值
    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100.0 - (100.0 / (1 + rs))
    
    # 使用 Wilder 平滑法计算后续RSI
    for i in range(period + 1, len(values)):
        if deltas[i] is not None:
            if deltas[i] >= 0:
                gain = deltas[i]
                loss = 0.0
            else:
                gain = 0.0
                loss = abs(deltas[i])
        else:
            gain = 0.0
            loss = 0.0
            
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100.0 - (100.0 / (1 + rs))
    
    return result


def macd(values: List[Optional[float]], 
         fast_period: int = 12, 
         slow_period: int = 26, 
         signal_period: int = 9) -> Tuple[List[Optional[float]], 
                                         List[Optional[float]], 
                                         List[Optional[float]]]:
    """MACD指标 (Moving Average Convergence Divergence)
    
    Args:
        values: 价格序列（收盘价）
        fast_period: 快线EMA周期，默认12
        slow_period: 慢线EMA周期，默认26
        signal_period: 信号线EMA周期，默认9
        
    Returns:
        (MACD线, 信号线, 柱状图) 三元组，每个元素为列表
    """
    if not values:
        empty = [None] * len(values)
        return empty, empty, empty
    
    # 计算快线和慢线EMA
    ema_fast = ema(values, fast_period)
    ema_slow = ema(values, slow_period)
    
    # 计算MACD线 = 快线 - 慢线
    macd_line = []
    for f, s in zip(ema_fast, ema_slow):
        if f is not None and s is not None:
            macd_line.append(f - s)
        else:
            macd_line.append(None)
    
    # 计算信号线 = MACD线的EMA
    signal_line = ema(macd_line, signal_period)
    
    # 计算柱状图 = MACD线 - 信号线
    histogram = []
    for m, s in zip(macd_line, signal_line):
        if m is not None and s is not None:
            histogram.append(m - s)
        else:
            histogram.append(None)
    
    return macd_line, signal_line, histogram


def bollinger_bands(values: List[Optional[float]], 
                    period: int = 20, 
                    std_dev: float = 2.0) -> Tuple[List[Optional[float]], 
                                                   List[Optional[float]], 
                                                   List[Optional[float]]]:
    """布林带 (Bollinger Bands)
    
    Args:
        values: 价格序列（收盘价）
        period: 移动平均周期，默认20
        std_dev: 标准差倍数，默认2.0
        
    Returns:
        (上轨, 中轨, 下轨) 三元组，每个元素为列表
    """
    if not values or len(values) < period:
        empty = [None] * len(values)
        return empty, empty, empty
    
    result_upper = [None] * len(values)
    result_middle = [None] * len(values)
    result_lower = [None] * len(values)
    
    for i in range(period - 1, len(values)):
        # 获取窗口数据
        window = values[i - period + 1:i + 1]
        # 过滤掉None值
        valid_values = [v for v in window if v is not None]
        
        if len(valid_values) < period:
            # 数据不足时保持None
            continue
            
        # 计算移动平均
        ma = sum(valid_values) / len(valid_values)
        
        # 计算标准差
        variance = sum((x - ma) ** 2 for x in valid_values) / len(valid_values)
        std = math.sqrt(variance) if variance >= 0 else 0.0
        
        # 计算布林带
        upper = ma + (std_dev * std)
        lower = ma - (std_dev * std)
        
        result_middle[i] = ma
        result_upper[i] = upper
        result_lower[i] = lower
    
    return result_upper, result_middle, result_lower


def stochastic_oscillator(highs: List[Optional[float]], 
                         lows: List[Optional[float]], 
                         closes: List[Optional[float]], 
                         k_period: int = 14, 
                         d_period: int = 3) -> Tuple[List[Optional[float]], 
                                                     List[Optional[float]]]:
    """随机振荡器 (Stochastic Oscillator)
    
    Args:
        highs: 最高价序列
        lows: 最低价序列  
        closes: 收盘价序列
        k_period: %K周期，默认14
        d_period: %D周期（%K的移动平均），默认3
        
    Returns:
        (%K线, %D线) 二元组，每个元素为列表
    """
    if not highs or not lows or not closes:
        empty = [None] * len(closes)
        return empty, empty
    
    # 确保所有列表长度一致
    min_len = min(len(highs), len(lows), len(closes))
    highs = highs[:min_len]
    lows = lows[:min_len]
    closes = closes[:min_len]
    
    if min_len < k_period:
        empty = [None] * min_len
        return empty, empty
    
    k_percent = [None] * min_len
    d_percent = [None] * min_len
    
    # 计算%K
    for i in range(k_period - 1, min_len):
        # 获取窗口数据
        window_highs = highs[i - k_period + 1:i + 1]
        window_lows = lows[i - k_period + 1:i + 1]
        
        # 过滤None值
        valid_highs = [h for h in window_highs if h is not None]
        valid_lows = [l for l in window_lows if l is not None]
        
        if not valid_highs or not valid_lows:
            continue
            
        highest_high = max(valid_highs)
        lowest_low = min(valid_lows)
        close_val = closes[i]
        
        if highest_high == lowest_low:
            # 避免除零错误
            k_percent[i] = 50.0
        elif close_val is not None:
            k_percent[i] = ((close_val - lowest_low) / (highest_high - lowest_low)) * 100
    
    # 计算%D = %K的d_period简单移动平均
    for i in range(d_period - 1, min_len):
        # 获取%K窗口数据
        k_window = k_percent[i - d_period + 1:i + 1]
        # 过滤None值
        valid_k = [k for k in k_window if k is not None]
        
        if len(valid_k) >= d_period:
            d_percent[i] = sum(valid_k) / len(valid_k)
    
    return k_percent, d_percent


def williams_r(highs: List[Optional[float]], 
               lows: List[Optional[float]], 
               closes: List[Optional[float]], 
               period: int = 14) -> List[Optional[float]]:
    """威廉姆斯%R (Williams %R)
    
    Args:
        highs: 最高价序列
        lows: 最低价序列
        closes: 收盘价序列
        period: 周期，默认14
        
    Returns:
        Williams %R序列 (-100到0)，前期不足的位置为None
    """
    if not highs or not lows or not closes:
        return [None] * len(closes)
    
    # 确保所有列表长度一致
    min_len = min(len(highs), len(lows), len(closes))
    highs = highs[:min_len]
    lows = lows[:min_len]
    closes = closes[:min_len]
    
    if min_len < period:
        return [None] * min_len
    
    result = [None] * min_len
    
    for i in range(period - 1, min_len):
        # 获取窗口数据
        window_highs = highs[i - period + 1:i + 1]
        window_lows = lows[i - period + 1:i + 1]
        
        # 过滤None值
        valid_highs = [h for h in window_highs if h is not None]
        valid_lows = [l for l in window_lows if l is not None]
        
        if not valid_highs or not valid_lows:
            continue
            
        highest_high = max(valid_highs)
        lowest_low = min(valid_lows)
        close_val = closes[i]
        
        if highest_high == lowest_low:
            # 避免除零错误
            result[i] = -50.0
        elif close_val is not None:
            result[i] = ((highest_high - close_val) / (highest_high - lowest_low)) * -100
    
    return result


def cci(highs: List[Optional[float]], 
        lows: List[Optional[float]], 
        closes: List[Optional[float]], 
        period: int = 20, 
        constant: float = 0.015) -> List[Optional[float]]:
    """商品通道指数 (Commodity Channel Index)
    
    Args:
        highs: 最高价序列
        lows: 最低价序列
        closes: 收盘价序列
        period: 周期，默认20
        constant: 常数，默认0.015
        
    Returns:
        CCI序列，前期不足的位置为None
    """
    if not highs or not lows or not closes:
        return [None] * len(closes)
    
    # 确保所有列表长度一致
    min_len = min(len(highs), len(lows), len(closes))
    highs = highs[:min_len]
    lows = lows[:min_len]
    closes = closes[:min_len]
    
    if min_len < period:
        return [None] * min_len
    
    result = [None] * min_len
    
    for i in range(period - 1, min_len):
        # 获取窗口数据
        window_highs = highs[i - period + 1:i + 1]
        window_lows = lows[i - period + 1:i + 1]
        window_closes = closes[i - period + 1:i + 1]
        
        # 过滤None值
        valid_highs = [h for h in window_highs if h is not None]
        valid_lows = [l for l in window_lows if l is not None]
        valid_closes = [c for c in window_closes if c is not None]
        
        if len(valid_highs) < period or len(valid_lows) < period or len(valid_closes) < period:
            continue
            
        # 计算典型价格 (Typical Price)
        tp_values = []
        for h, l, c in zip(valid_highs, valid_lows, valid_closes):
            tp = (h + l + c) / 3
            tp_values.append(tp)
        
        # 计算典型价格的简单移动平均
        ma_tp = sum(tp_values) / len(tp_values)
        
        # 计算平均偏差
        mean_deviation = sum(abs(tp - ma_tp) for tp in tp_values) / len(tp_values)
        
        # 计算CCI
        if mean_deviation == 0:
            result[i] = 0.0
        else:
            latest_tp = tp_values[-1]  # 最新典型价格
            result[i] = (latest_tp - ma_tp) / (constant * mean_deviation)
    
    return result


def sma(values: List[Optional[float]], period: int) -> List[Optional[float]]:
    """简单移动平均线 (Simple Moving Average)
    
    Args:
        values: 价格序列
        period: 周期
        
    Returns:
        SMA序列，前期不足的位置为None
    """
    if not values or period <= 0:
        return [None] * len(values)
    
    result = [None] * len(values)
    
    for i in range(period - 1, len(values)):
        # 获取窗口数据
        window = values[i - period + 1:i + 1]
        # 过滤None值
        valid_values = [v for v in window if v is not None]
        
        if len(valid_values) == 0:
            continue
            
        result[i] = sum(valid_values) / len(valid_values)
    
    return result


def atr(highs: List[Optional[float]], 
        lows: List[Optional[float]], 
        closes: List[Optional[float]], 
        period: int = 14) -> List[Optional[float]]:
    """平均真实范围 (Average True Range)
    
    Args:
        highs: 最高价序列
        lows: 最低价序列
        closes: 收盘价序列
        period: 周期，默认14
        
    Returns:
        ATR序列，前期不足的位置为None
    """
    if not highs or not lows or not closes:
        return [None] * len(closes)
    
    # 确保所有列表长度一致
    min_len = min(len(highs), len(lows), len(closes))
    highs = highs[:min_len]
    lows = lows[:min_len]
    closes = closes[:min_len]
    
    if min_len < period:
        return [None] * min_len
    
    # 计算真实范围 (True Range)
    tr = [None] * min_len
    for i in range(min_len):
        high = highs[i]
        low = lows[i]
        close = closes[i]
        prev_close = closes[i-1] if i > 0 else None
        
        if high is None or low is None or close is None:
            tr[i] = None
            continue
            
        # TR = max[(H-L), |H-PC|, |L-PC|]
        tr1 = high - low if high is not None and low is not None else 0
        tr2 = abs(high - prev_close) if high is not None and prev_close is not None else 0
        tr3 = abs(low - prev_close) if low is not None and prev_close is not None else 0
        
        tr[i] = max(tr1, tr2, tr3)
    
    # 计算ATR = TR的简单移动平均
    result = [None] * min_len
    
    for i in range(period - 1, min_len):
        # 获取窗口数据
        window = tr[i - period + 1:i + 1]
        # 过滤None值
        valid_tr = [t for t in window if t is not None]
        
        if len(valid_tr) == 0:
            continue
            
        result[i] = sum(valid_tr) / len(valid_tr)
    
    return result