# -*- coding: utf-8 -*-
"""R2动量板块代理回测 v2（保守场外低频版）

复刻知乎动量三轮 + 四防线，改成场外可执行：
  趋势: 收盘>MA20>MA60
  动量: N日对数加权回归（越近权重越高），得分=年化斜率×R2
  排雷: 有量才判，无量跳过（sector_ohlcv仅89个板块有量）
  卖出: 月度换仓TOPN + 换仓缓冲（新TOP超旧均分10%才换）+ 止损 + 空窗切货币

纯标准库。用法:
  python backtest_r2_momentum.py                        # 默认 v2（60日/TOP3/-10%/缓冲10%）
  python backtest_r2_momentum.py --top 1 --window 25 --buffer 0  # 还原原文对照
  python backtest_r2_momentum.py --grid                  # 约96组网格搜索
"""
import os
import sys
import datetime

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
from src.common import history_db
from research._engine import wlinreg_stats
REPORT_DIR = os.path.join(REPO_ROOT, "reports")

WINDOW = 60
TOPN = 3
STOP_PCT = -10.0
BUFFER = 0.10
VOL_MULT = 2.5
MIN_CAP_YI = 50.0  # 流通市值<50亿剔除（用最新快照静态过滤）


def wlinreg_score(closes, window=WINDOW):
    """加权R2动量分：返回 (年化, R2, 得分)，不够或非法返回None。
    给未来亚亚：y取收盘对数（涨跌幅可加），权重越近越大(w=i+1)，
    年化=日log斜率×250，得分=年化×R2（又猛又稳才高分）。"""
    s = wlinreg_stats(closes, window)
    if s is None:
        return None
    ann, r2 = s[0], s[1]
    return ann, r2, ann * r2


def ma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def load_data():
    import csv
    conn = history_db.connect(readonly=True)
    try:
        rows = conn.execute("SELECT code,date,close FROM sector_kline ORDER BY code,date").fetchall()
        vols = {}
        try:
            for code, d, v in conn.execute("SELECT code,date,vol FROM sector_ohlcv"):
                vols.setdefault(code, {})[d] = v
        except Exception:
            pass
        names = {}
        try:
            for code, name in conn.execute(
                    "SELECT code,name FROM sector_daily WHERE date=(SELECT MAX(date) FROM sector_daily)"):
                names[code] = name
        except Exception:
            pass
    finally:
        conn.close()
    caps = {}
    try:
        with open(os.path.join(DATA_ROOT, "sector_snapshot.csv"), encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    caps[r["代码"]] = float(r.get("流通市值(亿)") or 0)
                except Exception:
                    pass
    except Exception:
        pass
    series = {}
    for code, d, c in rows:
        series.setdefault(code, []).append((d, c))
    return series, vols, names, caps


def run_backtest(series, vols, names, caps, window=WINDOW, topn=TOPN,
                 stop=STOP_PCT, buffer=BUFFER, quiet=True):
    """核心回测，返回指标dict。无未来函数，只用<=rb数据。
    给未来亚亚：月度调仓（每月首个交易日）；换仓缓冲=新TOP均分超旧持仓
    (1+buffer)才换（压场外换手）；止损后该腿当月按货币记0；无候选则
    空仓记0（防御模式）；市值<50亿静态剔除（快照最新值代理）。"""
    cal = sorted({d for pts in series.values() for d, _ in pts})
    px = {c: dict(pts) for c, pts in series.items()}
    months, seen = [], set()
    for d in cal:
        m = d[:7]
        if m not in seen:
            seen.add(m)
            months.append(d)
    warmup = window + 70
    months = [m for m in months if cal.index(m) >= warmup]
    if len(months) < 2:
        return None
    equity = [1.0]
    peak, maxdd = 1.0, 0.0
    turnovers, empty = [], 0
    holding, hold_score = [], 0.0
    entry = {}
    for mi in range(len(months) - 1):
        rb, nxt = months[mi], months[mi + 1]
        scored = []
        for code, pts in series.items():
            if caps.get(code, 0) and caps[code] < MIN_CAP_YI:
                continue
            idx = next((i for i, (d, _) in enumerate(pts) if d > rb), len(pts)) - 1
            if idx < warmup or pts[idx][0] != rb:
                continue
            cl = [p[1] for p in pts[:idx + 1]]
            m20, m60 = ma(cl, 20), ma(cl, 60)
            if m20 is None or m60 is None or not (cl[-1] > m20 > m60):
                continue
            r = wlinreg_score(cl, window)
            if not r:
                continue
            ann, r2, score = r
            if ann <= 0 or score <= 0:
                continue
            vmap = vols.get(code, {})
            if rb in vmap:
                vdates = sorted(d for d in vmap if d <= rb)[-6:]
                if len(vdates) == 6 and all(vmap[d] for d in vdates):
                    avg5 = sum(vmap[d] for d in vdates[:5]) / 5
                    if avg5 and vmap[rb] > VOL_MULT * avg5:
                        continue
            scored.append((score, code))
        scored.sort(reverse=True)
        cand = [c for _, c in scored[:topn]]
        cand_score = sum(s for s, _ in scored[:topn]) / topn if cand else 0
        if not cand:
            empty += 1
        # 换仓缓冲：新TOP均分超旧持仓均分(1+buffer)才换，否则续持
        if holding and cand:
            if cand_score <= hold_score * (1 + buffer):
                cand, cand_score = list(holding), hold_score
        chg = len(set(cand) - set(holding)) / topn if topn else 0
        turnovers.append(chg)
        holding, hold_score = cand, cand_score
        entry = {c: px[c][rb] for c in holding if rb in px[c]}
        stopped = set()  # 2026-09-09补：止损后当月剩余按货币，不再复活（复活=免费过滤下跌，印假收益）
        i0, i1 = cal.index(rb), cal.index(nxt)
        for d in cal[i0 + 1:i1 + 1]:
            legs = []
            prev = cal[cal.index(d) - 1]
            for c in holding:
                if c in stopped:
                    legs.append(0.0)
                    continue
                if c not in px or d not in px[c] or c not in entry or not entry[c]:
                    continue
                if px[c][d] / entry[c] - 1 <= stop / 100:
                    stopped.add(c)
                    legs.append(0.0)  # 止损后当月剩余按货币
                    continue
                legs.append(px[c][d] / px[c][prev] - 1 if prev in px[c] else 0.0)
            r = sum(legs) / len(legs) if legs else 0.0
            equity.append(equity[-1] * (1 + r))
        peak = max(peak, equity[-1])
        maxdd = min(maxdd, equity[-1] / peak - 1)
    tot = equity[-1] - 1
    # 月胜率：按调仓段分段算
    nseg = len(months) - 1
    seglen = max(1, (len(equity) - 1) // nseg) if nseg else 1
    wins = 0
    for i in range(nseg):
        a = 1 + i * seglen
        b = min(len(equity) - 1, a + seglen)
        if b > a and equity[b] > equity[a - 1]:
            wins += 1
    return {"total": tot, "maxdd": maxdd, "win": wins / nseg * 100 if nseg else 0,
            "turn": sum(turnovers) / len(turnovers) * 100 if turnovers else 0,
            "n": nseg, "empty": empty, "months": (months[0], months[-1])}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=TOPN)
    ap.add_argument("--window", type=int, default=WINDOW)
    ap.add_argument("--stop", type=float, default=STOP_PCT)
    ap.add_argument("--buffer", type=float, default=BUFFER)
    ap.add_argument("--grid", action="store_true")
    a = ap.parse_args()
    series, vols, names, caps = load_data()
    # 给未来亚亚：网格约96组(w[25,40,60,90]×top[1,2,3]×stop×buf)，约束回撤>=-10%，
    # 按“先回撤达标、再累计、再换手”排序，只记录不自动改默认参数。
    if a.grid:
        best = []
        for w in (25, 40, 60, 90):
            for t in (1, 2, 3):
                for s in (-8.0, -10.0, -12.0, -15.0):
                    for b in (0.0, 0.10):
                        m = run_backtest(series, vols, names, caps, w, t, s, b)
                        if m:
                            best.append((w, t, s, b, m))
        ok = [x for x in best if x[4]["maxdd"] * 100 >= -10.0]
        ok.sort(key=lambda x: (-x[4]["total"], x[4]["turn"]))
        print("网格 %d组，回撤<10%%的有%d组，前8：" % (len(best), len(ok)))
        for w, t, s, b, m in (ok or sorted(best, key=lambda x: x[4]["maxdd"], reverse=True))[:8]:
            print(" w%-3d top%d stop%4.0f%% buf%3.0f%% 累计%+.1f%% 回撤%.1f%% 胜率%.0f%% 换手%.0f%%" % (
                w, t, s, b * 100, m["total"] * 100, m["maxdd"] * 100, m["win"], m["turn"]))
        return
    m = run_backtest(series, vols, names, caps, a.window, a.top, a.stop, a.buffer, quiet=False)
    print("=" * 60)
    print("R2动量板块代理回测 v2 window=%d top=%d stop=%s%% buf=%d%%" % (
        a.window, a.top, a.stop, a.buffer * 100))
    print("区间 %s~%s 调仓%d次 空仓%d次" % (m["months"][0], m["months"][1], m["n"], m["empty"]))
    print("策略累计 %+.1f%% 最大回撤 %.1f%% 月胜率 %.0f%% 平均换手 %.0f%%" % (
        m["total"] * 100, m["maxdd"] * 100, m["win"], m["turn"]))
    os.makedirs(REPORT_DIR, exist_ok=True)
    rp = os.path.join(REPORT_DIR, "R2动量回测_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    with open(rp, "w", encoding="utf-8") as f:
        f.write("# R2动量板块代理回测 v2 %s\n\n" % datetime.date.today())
        f.write("参数 window=%d top=%d stop=%s%% buffer=%d%%，月度换仓，止损后切货币。\n\n" % (
            a.window, a.top, a.stop, a.buffer * 100))
        f.write("区间 %s~%s，调仓%d次，空仓%d月。\n\n" % (m["months"][0], m["months"][1], m["n"], m["empty"]))
        f.write("累计 %+.1f%%，最大回撤 %.1f%%，月胜率 %.0f%%，换手 %.0f%%。\n\n" % (
            m["total"] * 100, m["maxdd"] * 100, m["win"], m["turn"]))
        f.write("注意：样本仅2025-06以来约14个月，未经历真熊市，不可外推年化。\n")
    print("报告:%s" % rp)


if __name__ == "__main__":
    main()
