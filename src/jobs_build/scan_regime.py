# -*- coding: utf-8 -*-
"""scan_regime.py — 板块/大盘趋势状态每日扫描（EMA云带 + 压制状态机 + 底部评分）

算法移植自 mommy-chaogu quant/ma-suppression-monitor（MIT），纯标准库重写：
  - EMA 55/89 云带定趋势：fast>slow 为 BULL，否则 BEAR；cross_age 距最近交叉bar数
  - 一切阈值 ATR 归一化（本版用 14 日收盘均绝变动近似真 ATR——库里只存收盘价，
    若日后 sector_kline 升级为 OHLCV 可无缝换回 Wilder 口径）
  - 压制三阶段状态机 IDLE→ARMED→ENGAGED→CONFIRMED(COOLDOWN)：熊市反弹被云带压回
    的次数与失败(站上云带)次数；压制失败是趋势反转早期信号
  - 底部评分 0~100：均线走平30% + 底底高30% + 贴线度25% + 云带收敛15%；
    云下 >2ATR 自由落体时封顶49（崩盘过滤器）
  - 警报单一口径优先级：金叉/死叉(≤3bar) > 反弹衰竭 > 底部观察(≥65) > 多头趋势 > 自由落体
  - 核心纪律：信号在收盘 bar 确认，无未来函数（所有指标只用当日及以前数据）

数据源: market_history.db 的 sector_kline（479+板块日收盘）+ 现抓上证指数日K（curl 兜底）
落库: regime_daily(date, code, ...) 同日重跑幂等覆盖

用法:
  python scan_regime.py              # 全量扫描并落库
  python scan_regime.py --no-index   # 跳过上证指数（不联网）
"""
import json
import os
import sqlite3
import subprocess
import sys
import time

# 统一寻址：cookie 与数据统一走 config/ 与 data/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common import history_db
from src.common.paths import DATA_ROOT as DATA_DIR, CONFIG_ROOT

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"
MIN_BARS = 120          # 少于此 bar 数的板块跳过（EMA89/通道窗需要预热）
CHAN_WINDOW = 60        # 回归通道窗口
WATCH_SCORE = 65.0      # BOTTOM_WATCH 阈值
CAUTION_SCORE = 50.0    # 做空引擎降仓阈值


# ---------- 指标层 ----------
def ema(vals, span):
    """递推式指数均线 alpha=2/(span+1)，与 TradingView ta.ema 一致（实现见 common/technical_indicators）"""
    from src.common.technical_indicators import ema as _ema
    return _ema(vals, span)


def atr_proxy(closes, n=14):
    """ATR 近似：|Δclose| 的 n 日简单均值（缺 High/Low 的替代口径）。
    返回与 closes 对齐的列表，前 n 位为 None"""
    tr = [None] + [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    out = [None] * len(closes)
    for i in range(n, len(closes)):
        window = tr[i - n + 1:i + 1]
        out[i] = sum(window) / n
    return out


def linreg_channel_pos(closes, window=CHAN_WINDOW):
    """滚动回归通道位置 0~100（中线=最小二乘，轨=±2σ）。返回逐bar列表，前 window-1 位 None"""
    n = len(closes)
    x = list(range(window))
    mx = sum(x) / window
    sxx = sum((xi - mx) ** 2 for xi in x)
    pos = [None] * n
    for i in range(window - 1, n):
        y = closes[i - window + 1:i + 1]
        my = sum(y) / window
        sxy = sum((x[j] - mx) * (y[j] - my) for j in range(window))
        m = sxy / sxx
        b0 = my - m * mx
        resid = [y[j] - (m * x[j] + b0) for j in range(window)]
        sd = (sum(r * r for r in resid) / window) ** 0.5
        if sd <= 0:
            continue
        mid = m * (window - 1) + b0
        p = (closes[i] - (mid - 2 * sd)) / (4 * sd) * 100
        pos[i] = min(100.0, max(0.0, p))
    return pos


def pivots_low(closes, order=6):
    """摆动低点位置：左右各 order 根内的最小值（含平台阶平台重复，与原实现一致）"""
    idx = []
    for i in range(order, len(closes) - order):
        w = closes[i - order:i + order + 1]
        if closes[i] == min(w):
            idx.append(i)
    return idx


# ---------- 单标的完整计算 ----------
def _bottom_score_at(closes, piv_all, geo_i, i):
    """某根bar的底部评分。piv_all=全序列摆动低点；只用 k<=i-6 的因果确认点（无未来函数）。"""
    W_SLOPE, W_HL, W_DIST, W_GAP = 0.30, 0.30, 0.25, 0.15

    def _lin(x, x0, x1):
        return min(100.0, max(0.0, (x - x0) / (x1 - x0) * 100))

    _, _, dist, width, slope_slow = geo_i
    elig = [k for k in piv_all if k <= i - 6]
    hl = 0.0
    if len(elig) >= 3 and i - elig[-3] <= 120:
        v = [closes[elig[-3]], closes[elig[-2]], closes[elig[-1]]]
        hl = (50.0 if v[2] > v[1] else 0.0) + (50.0 if v[1] > v[0] else 0.0)
    s_slope = _lin(slope_slow, -4.0, 0.0) if slope_slow is not None else 0.0
    s_dist = _lin(dist, -2.0, 1.0)
    s_gap = 100.0 - _lin(width, 0.5, 3.0)
    raw = W_SLOPE * s_slope + W_HL * hl + W_DIST * s_dist + W_GAP * s_gap
    crash_blocked = dist < -2.0
    return round(min(100.0, max(0.0, min(49.0, raw) if crash_blocked else raw)), 1)


def _engine_alert(bull_i, score, cp, cross_age_i, dist, tests_26):
    regime = "BULL" if bull_i else "BEAR"
    if regime == "BULL":
        engine = "LONG_SIDE"
    elif score >= WATCH_SCORE:
        engine = "BOTTOM_WATCH"
    elif score >= CAUTION_SCORE:
        engine = "SHORT_REDUCED"
    else:
        engine = "SHORT_ACTIVE"
    if cross_age_i <= 3:
        alert = "GOLDEN_X" if regime == "BULL" else "DEATH_X"
    elif regime == "BEAR" and cp >= 75 and tests_26 > 0:
        alert = "RALLY_FADE"
    elif regime == "BEAR" and score >= WATCH_SCORE:
        alert = "BOTTOM_WATCH"
    elif regime == "BULL" and dist > 0:
        alert = "TREND_LONG"
    elif dist < -1.5:
        alert = "FREE_FALL"
    else:
        alert = "—"
    return regime, engine, alert


def analyze_series(closes):
    """输入收盘价序列，返回逐bar监控读数列表（不足MIN_BARS/无云几何的bar为None）。
    摆动低点只用因果确认点，全程无未来函数，可直接用于训练特征。"""
    n = len(closes)
    out = [None] * n
    if n < MIN_BARS:
        return out
    f = ema(closes, 55)
    s = ema(closes, 89)
    atr_ = atr_proxy(closes, 14)

    bull = [f[i] > s[i] for i in range(n)]
    cross_age, last_cross = [0] * n, -(10 ** 9)
    for i in range(n):
        prev = bull[i - 1] if i else not bull[0]  # 第0根视作交叉起点
        if bull[i] != prev:
            last_cross = i
        cross_age[i] = i - last_cross if last_cross >= 0 else i + 1

    def cloud_geo(i):
        lo, hi = min(f[i], s[i]), max(f[i], s[i])
        a = atr_[i]
        px = closes[i]
        if not a:
            return None
        dist = (px - hi) / a if px > hi else ((px - lo) / a if px < lo else 0.0)
        width = (hi - lo) / a
        slope_slow = (s[i] - s[i - 26]) / a if i >= 26 else None
        return lo, hi, dist, width, slope_slow

    geo = [cloud_geo(i) for i in range(n)]
    ch_pos = linreg_channel_pos(closes)
    piv_all = pivots_low(closes, 6)

    # ---- 压制三阶段状态机（熊市侧；探入深度用收盘价近似 High）----
    ARM_DIST, BUF, MIN_DEPTH, COOL_DIST, POS_MIN = 1.5, 0.3, 0.2, 1.0, 50
    state, count, failures = "IDLE", 0, 0
    events = []
    prev_bull = bull[0]
    sup_hist = [None] * n
    for i in range(n):
        g = geo[i]
        a = atr_[i]
        if not g or not a:
            continue
        if bull[i] != prev_bull:
            count, state = 0, "IDLE"
            prev_bull = bull[i]
        if bull[i]:
            state = "IDLE"  # 做空引擎只在空头排列工作
            sup_hist[i] = (state, count, failures)
            continue
        lo, hi, _, _, _ = g
        zone_lo, zone_hi = lo - BUF * a, hi
        near = closes[i] >= lo - ARM_DIST * a
        h = closes[i]  # 无 High 序列，以收盘价近似
        if state == "IDLE":
            ok = True if ch_pos[i] is None else ch_pos[i] >= POS_MIN
            if near and closes[i] < lo and ok:
                state = "ARMED"
        elif state == "ARMED":
            if closes[i] > zone_hi + BUF * a:
                state = "IDLE"
            elif h >= zone_lo:
                depth = (min(h, zone_hi) - zone_lo) / a
                if depth >= MIN_DEPTH:
                    state = "ENGAGED"
        elif state == "ENGAGED":
            if closes[i] > zone_hi:
                failures += 1
                state = "IDLE"
            elif closes[i] < lo:
                count += 1
                events.append(i)
                state = "COOLDOWN"
        elif state == "COOLDOWN":
            if abs(closes[i] - lo) > COOL_DIST * a:
                state = "ARMED" if closes[i] < lo else "IDLE"
        sup_hist[i] = (state, count, failures)

    ev_ptr = 0
    for i in range(MIN_BARS, n):
        g = geo[i]
        if not g:
            continue
        _, _, dist, width, _ = g
        score = _bottom_score_at(closes, piv_all, g, i)
        while ev_ptr < len(events) and events[ev_ptr] <= i - 26:
            ev_ptr += 1
        tests_26 = len(events) - ev_ptr
        cp = ch_pos[i] if ch_pos[i] is not None else 0.0
        regime, engine, alert = _engine_alert(bull[i], score, cp, cross_age[i], dist, tests_26)
        _, cnt, fail = sup_hist[i] or ("IDLE", 0, 0)
        out[i] = {"regime": regime, "cross_age": cross_age[i],
                  "cloud_dist": round(dist, 2), "cloud_width": round(width, 2),
                  "chan_pos": round(cp, 1), "bottom_score": score,
                  "engine": engine, "alert": alert,
                  "sup_events": cnt, "sup_failures": fail}
    return out


def analyze(closes):
    """输入收盘价序列，返回最后一根 bar 的监控读数 dict；数据不足返回 None"""
    if len(closes) < MIN_BARS:
        return None
    series = analyze_series(closes)
    return series[-1]


# ---------- 数据获取 ----------
def load_board_closes():
    """sector_kline 全部板块收盘序列 {code: [(date, close)...]}"""
    conn = history_db.connect()
    try:
        rows = conn.execute("SELECT code, date, close FROM sector_kline ORDER BY code, date").fetchall()
    finally:
        conn.close()
    out = {}
    for code, d, c in rows:
        out.setdefault(code, []).append((d, c))
    return out


def load_names():
    conn = history_db.connect()
    try:
        rows = conn.execute(
            "SELECT code, name FROM sector_daily WHERE date=(SELECT MAX(date) FROM sector_daily)").fetchall()
    finally:
        conn.close()
    return dict(rows)


def _cookie():
    ck = os.environ.get("EM_COOKIE", "")
    if not ck:
        path = os.path.join(CONFIG_ROOT, "em_cookie.txt")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                ck = f.read().strip()
    return ck


def fetch_index_closes(secid="1.000001", lmt=320):
    """上证指数日K收盘（push2his 被 TLS 指纹拦截，走 curl + cookie，同 fetch_sentiment）"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=1&lmt=%d&end=20500101") % (secid, lmt)
    cmd = ["curl", "-s", "-m", "15", "-A", UA, "-H", "Referer: https://quote.eastmoney.com/"]
    ck = _cookie()
    if ck:
        cmd += ["-H", "Cookie: " + ck]
    cmd.append(url)
    for attempt in range(3):
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
        if r.returncode == 0 and r.stdout.strip():
            try:
                kl = ((json.loads(r.stdout) or {}).get("data") or {}).get("klines") or []
                return [(k.split(",")[0], float(k.split(",")[1])) for k in kl]
            except (json.JSONDecodeError, IndexError):
                pass
        time.sleep(attempt + 1)
    return tencent_index_closes(secid, lmt)


def tencent_index_closes(secid, lmt):
    """腾讯日K兜底（push2his 为 IP 级限流，限流窗口内 curl/浏览器一并失败）。
    两源在重叠日逐位一致，仅在东财取不到时启用。"""
    code = ("sh" if secid.startswith("1.") else "sz") + secid.split(".", 1)[1]
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (code, lmt)
    try:
        r = subprocess.run(["curl", "-s", "-m", "20", url],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        data = (json.loads(r.stdout) or {}).get("data", {}).get(code, {})
        # 每行 = [日期, 开, 收, 高, 低, 量]
        return [(row[0], float(row[2])) for row in (data.get("qfqday") or data.get("day") or [])]
    except (json.JSONDecodeError, IndexError, TypeError, ValueError):
        return []


# ---------- 主流程 ----------
def main():
    date = None
    names = load_names()
    targets = []  # (code, name, [(date,close)...])

    idx = fetch_index_closes()
    if idx:
        targets.append(("1.000001", "上证指数", idx))
    else:
        print("  上证指数K线获取失败，跳过（不影响板块扫描）")

    boards = load_board_closes()
    for code, pts in sorted(boards.items()):
        targets.append((code, names.get(code, code), pts))

    results = []
    skipped = 0
    for k, (code, name, pts) in enumerate(targets, 1):
        closes = [c for _, c in pts]
        r = analyze(closes)
        if r is None:
            skipped += 1
            continue
        r["date"], r["code"], r["close"] = pts[-1][0], code, closes[-1]
        results.append(r)
        if k % 100 == 0:
            print("  已扫描 %d/%d" % (k, len(targets)), flush=True)

    # 落库（幂等）
    conn = history_db.connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO regime_daily VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["date"], r["code"], r["close"], r["regime"], r["cross_age"],
              r["cloud_dist"], r["cloud_width"], r["chan_pos"], r["bottom_score"],
              r["engine"], r["alert"], r["sup_events"])
             for r in results])
        conn.commit()
    finally:
        conn.close()

    # 控制台简报
    idx_r = next((r for r in results if r["code"] == "1.000001"), None)
    boards_r = [r for r in results if r["code"] != "1.000001"]
    print("\n== 趋势扫描简报 (%s) ==" % (idx_r["date"] if idx_r else results[0]["date"]))
    if idx_r:
        print("上证指数: %s 交叉后%dd 云带距离%+.1fATR 底部评分%.0f 压制%d次/失败%d次 [%s|%s]"
              % (idx_r["regime"], idx_r["cross_age"], idx_r["cloud_dist"], idx_r["bottom_score"],
                 idx_r["sup_events"], idx_r["sup_failures"], idx_r["engine"], idx_r["alert"]))
    bull_n = sum(1 for r in boards_r if r["regime"] == "BULL")
    watch = sorted((r for r in boards_r if r["alert"] == "BOTTOM_WATCH"),
                   key=lambda r: -r["bottom_score"])
    fades = [r for r in boards_r if r["alert"] == "RALLY_FADE"]
    falls = [r for r in boards_r if r["alert"] == "FREE_FALL"]
    golden = [r for r in boards_r if r["alert"] == "GOLDEN_X"]
    print("板块多空比: BULL %d / BEAR %d (共%d, 跳过%d)" % (bull_n, len(boards_r) - bull_n, len(boards_r), skipped))
    print("底部观察(%d):" % len(watch), " ".join("%s:%.0f分" % (names.get(r["code"], r["code"]), r["bottom_score"]) for r in watch[:8]) or "—")
    print("反弹衰竭(%d):" % len(fades), " ".join(names.get(r["code"], r["code"]) for r in fades[:8]) or "—")
    print("自由落体(%d):" % len(falls), " ".join(names.get(r["code"], r["code"]) for r in falls[:8]) or "—")
    if golden:
        print("新金叉:", " ".join(names.get(r["code"], r["code"]) for r in golden[:8]))
    print("regime_daily 落库 %d 行" % len(results))


if __name__ == "__main__":
    main()
