# -*- coding: utf-8 -*-
"""三重背离线（布林 + MACD + RSI 共振的顶/底背离）—— 把"看着像大顶"写成确定性判据

来源：一篇知乎复盘（葭南）提出的"三重背离"择时法。原文是主观描述
（"布林上轨没创新高""RSI从高点回落"），本脚本先把它**去主观化**成一套
只用 ≤T 收盘价、可复现、无未来函数的判据，再检验它是否真有信息量。

判据定义（顶背离 side=top，底背离 side=bottom 完全对称）：
  参照峰 ref = argmax(收盘[T-L .. T-G])   —— "至少 G 根K线之前的那个前高"，
               即回看 L 根、且排除最近 G-1 根（避免拿昨天当"前高"）
  创新高     = 收盘[T] > max(收盘[T-L .. T-1])  —— 今天真的创了 L 日新高
  三重背离   = 逐项比对 T 与 ref 的指标：
       ① 布林上轨[T]  < 布林上轨[ref]         （价格新高，上轨没新高）
       ② DIF[T] < DIF[ref] 且 柱[T] < 柱[ref] （动能没跟上，反而更低）
       ③ RSI[T] < RSI[ref]                    （情绪没跟上）
  得分 score = ①②③ 命中个数（0~3）。原文"三个全中→大顶"对应 score=3。

无未来函数：ref 必在 T-G 之前、创新高只比 T 之前的收盘、指标值全部 ≤T；
信号日就是决策日（不再等"峰确认"，因为参照峰本身早已是历史）。

评估口径：超额 = 该指数未来 h 日收益 − **该指数自身扩张窗口**的无条件收益中位数
（逐日把 h 天前那行的已实现未来收益加进样本，故无前视——与 rebound_line 同源）。
顶背离要有用，超额应显著为负（信号后确实涨不动）；底背离则相反。

独立性：同一天多个指数一起出信号属于同一次行情，按日期聚类只算一条证据。

用法:
  python research/divergence_line.py --scan      # 信号统计（分侧/分档/分回看窗）
  python research/divergence_line.py --judge     # 独立事件级终审 + 分年代 + 留一
  python research/divergence_line.py --current   # 当前各指数是否正处背离状态
纯标准库。
"""
import argparse
import bisect
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
for _p in (DATA_DIR, REPO_ROOT, os.path.join(REPO_ROOT, "src", "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from src.common import technical_indicators as TI  # noqa: E402
from src.common.fees import fee  # noqa: E402
from rebound_line import load_index_series, median  # noqa: E402

FWD = (5, 10, 20, 30, 60)      # 信号后未来窗口
FEE_SAFE_HOLD = 20             # 场外可交易持有下界（赎回费 0.5% → 0 的天数边界）
BASE_MIN = 120                 # 扩张窗口基准至少攒够多少个已实现样本
DEFAULT_L = 120                # 回看窗（"创 L 日新高"）
DEFAULT_G = 10                 # 排除最近 G-1 根，避免拿昨天当前高

# 指标参数族：名字 -> (布林周期, 布林倍数, RSI周期)；MACD 固定 12/26/9（原文未给参数）
IND_FAMILIES = {
    "std": (20, 2.0, 14),
    "narrow": (20, 1.5, 14),
    "slow": (30, 2.0, 24),
    "fast": (10, 2.0, 6),
}


def build_panel(pts, boll_p=20, boll_s=2.0, rsi_p=14, fwd=FWD):
    """单指数面板：返回 [(date, row)]，row 只含 ≤该日 的指标 + 未来收益。"""
    dates = [d for d, _ in pts]
    c = [x for _, x in pts]
    n = len(c)
    warm = max(boll_p, rsi_p + 1, 35) + 1
    if n < warm + max(fwd) + 2:
        return []
    ub, _mid, lb = TI.bollinger_bands(c, boll_p, boll_s)
    dif, _dea, hist = TI.macd(c)
    r = TI.rsi(c, rsi_p)
    ma120 = TI.sma(c, 120)
    out = []
    for i in range(n):
        if i < warm or i + max(fwd) >= n:
            continue
        if None in (ub[i], lb[i], dif[i], hist[i], r[i]):
            continue
        row = {"close": c[i], "ub": ub[i], "lb": lb[i], "dif": dif[i],
               "hist": hist[i], "rsi": r[i],
               # 距半年线的偏离度（%）；MA120 预热不足为 None，该行不参与 MA120 条件
               "dma120": ((c[i] / ma120[i] - 1) * 100.0) if ma120[i] else None}
        for h in fwd:
            row["fwd%d" % h] = (c[i + h] / c[i] - 1) * 100.0
        out.append((dates[i], row))
    return out


def _win_arg(vals, L, G, want_max=True):
    """out[i] = argmax/argmin(vals[i-L .. i-G])（两端含）；窗口无效返回 None。
    单调队列 O(n)。G=1 时窗口为 [i-L, i-1]，即"不含今天的 L 日极值"。"""
    n = len(vals)
    out = [None] * n
    dq = []
    for i in range(n):
        j = i - G
        if j >= 0:
            v = vals[j]
            if want_max:
                while dq and vals[dq[-1]] <= v:
                    dq.pop()
            else:
                while dq and vals[dq[-1]] >= v:
                    dq.pop()
            dq.append(j)
        lo = i - L
        while dq and dq[0] < lo:
            dq.pop(0)
        if dq and lo >= 0:
            out[i] = dq[0]
    return out


def detect(rows, side, L=DEFAULT_L, G=DEFAULT_G, min_score=3):
    """在面板上找背离信号。返回 [{date, i, score, ref_date, boll, macd, rsi,
    fwd{h}, ref_close}]，按日期升序。"""
    closes = [r["close"] for _, r in rows]
    is_top = (side == "top")
    refmax = _win_arg(closes, L, 1, want_max=is_top)      # 不含今天的 L 日极值
    ref = _win_arg(closes, L, G, want_max=is_top)         # 至少 G 根之前的极值
    sigs = []
    for i in range(len(rows)):
        if refmax[i] is None or ref[i] is None:
            continue
        if is_top:
            if closes[i] <= closes[refmax[i]]:
                continue
        else:
            if closes[i] >= closes[refmax[i]]:
                continue
        ri = ref[i]
        a, b = rows[i][1], rows[ri][1]
        if is_top:
            c_boll = b["ub"] < a["ub"]
            c_macd = a["dif"] < b["dif"] and a["hist"] < b["hist"]
            c_rsi = a["rsi"] < b["rsi"]
        else:
            c_boll = b["lb"] > a["lb"]
            c_macd = a["dif"] > b["dif"] and a["hist"] > b["hist"]
            c_rsi = a["rsi"] > b["rsi"]
        score = int(c_boll) + int(c_macd) + int(c_rsi)
        if score < min_score:
            continue
        sg = {"i": i, "date": rows[i][0], "ref_date": rows[ri][0], "score": score,
              "boll": int(c_boll), "macd": int(c_macd), "rsi": int(c_rsi),
              "dma": a.get("dma120")}
        for h in FWD:
            if "fwd%d" % h in a:
                sg["fwd%d" % h] = a["fwd%d" % h]
        sigs.append(sg)
    return sigs


def panels_with_baseline(fam="std"):
    """返回 {名称: (rows, base)}，base[i][h] = 该指数扩张窗口无条件收益中位数。
    base 只用于"超额"对比，不含未来信息。"""
    boll_p, boll_s, rsi_p = IND_FAMILIES[fam]
    ser = load_index_series()
    out = {}
    for nm, pts in ser.items():
        rows = build_panel(pts, boll_p, boll_s, rsi_p)
        if len(rows) < BASE_MIN + max(FWD) + 5:
            continue
        seen = {h: [] for h in FWD}
        base = []
        for j, (_d, r) in enumerate(rows):
            for h in FWD:
                k = j - h
                if k >= 0:
                    bisect.insort(seen[h], rows[k][1]["fwd%d" % h])
            base.append({h: (median(seen[h]) if len(seen[h]) >= BASE_MIN else None)
                         for h in FWD})
        out[nm] = (rows, base)
    return out


def date_gap(d1, d2):
    a = datetime.date(*map(int, d1.split("-")))
    b = datetime.date(*map(int, d2.split("-")))
    return abs((b - a).days) * 5 // 7


def date_clusters(starts, gap=20):
    """把信号日按日期邻近合并成"市场事件"：相隔 ≤gap 交易日算同一次行情。"""
    ss = sorted(starts, key=lambda x: x[1])
    clusters, cur, last = [], [], None
    for c, d, e in ss:
        if last is not None and date_gap(last, d) > gap:
            clusters.append(cur)
            cur = []
        cur.append((c, d, e))
        last = d
    if cur:
        clusters.append(cur)
    return clusters


def episodes(hits, ep_gap=20):
    """单指数时段去重：同一指数相邻信号间隔 ≤ep_gap 交易日算同一时段，
    只取首日（可下手的那天）。不做这一步，震荡市里一个指数能刷几十次信号，
    把同一段行情重复计账——这是 rebound_line 已经踩过的坑。"""
    by_code = {}
    for nm, d, sg, e in hits:
        by_code.setdefault(nm, []).append((d, sg, e))
    out = []
    for nm, arr in by_code.items():
        arr.sort(key=lambda x: x[0])
        prev = None
        for d, sg, e in arr:
            if prev is not None and date_gap(prev, d) <= ep_gap:
                prev = d
                continue
            prev = d
            out.append((nm, d, sg, e))
    out.sort(key=lambda x: x[1])
    return out


def to_events(hits, ep_gap=20, cl_gap=20):
    """完整两级收敛：单指数时段去重 → 跨指数按日期聚类成市场事件。
    返回 (ep_starts, clusters)。证据以 **ep_starts** 计（每时段一条），
    判定看 clusters（同一次行情只算一条）。"""
    ep = episodes(hits, ep_gap)
    return ep, date_clusters([(nm, d, e) for nm, d, _sg, e in ep], gap=cl_gap)


def collect(panels, side, L, G, min_score):
    """返回 [(name, date, sig, exc{h})]，exc = fwd - 自身扩张基准。"""
    hits = []
    for nm, (rows, base) in panels.items():
        for sg in detect(rows, side, L, G, min_score):
            i = sg["i"]
            b = base[i]
            if any(b[h] is None for h in FWD):
                continue
            exc = {h: sg["fwd%d" % h] - b[h] for h in FWD}
            hits.append((nm, sg["date"], sg, exc))
    return hits


def side_table(side_label, hits):
    """打印一列：命中日 / 单指数时段 / 市场事件 + 各窗口超额中位（时段起点级）。"""
    if not hits:
        print("  %-8s 无命中" % side_label)
        return None
    ep, cl = to_events(hits)
    ev = [median([x[2][20] for x in c]) for c in cl]
    cols = []
    for h in FWD:
        cols.append("%+.2f" % median([e[h] for _n, _d, _s, e in ep]))
    pos = sum(1 for v in ev if v > 0)
    print("  %-8s 日%5d 时段%4d 事件%4d 超额5/10/20/30/60日 %s  事件级20日%+.2f(%d/%d正)"
          % (side_label, len(hits), len(ep), len(cl), " ".join(cols), median(ev), pos, len(ev)))
    return {"n": len(hits), "nep": len(ep), "nev": len(cl), "ev20": median(ev), "pos": pos}


def scan():
    panels = panels_with_baseline("std")
    print("指数 %d 条（长历史）" % len(panels))
    for nm in sorted(panels):
        rows, _ = panels[nm]
        print("  %-10s %s ~ %s  %d 日" % (nm, rows[0][0], rows[-1][0], len(rows)))
    print("\n=== 三重背离：按侧 × 回看窗 × 得分门槛（超额 = 相对该指数自身扩张基准）===")
    print("顶背离要有用 → 超额应为负；底背离要有用 → 超额应为正。")
    for L in (60, 120, 250):
        print("\n-- 回看窗 L=%d（创 %d 日新高/新低；G=%d）--" % (L, L, DEFAULT_G))
        for ms in (3, 2, 1):
            top = collect(panels, "top", L, DEFAULT_G, ms)
            bot = collect(panels, "bottom", L, DEFAULT_G, ms)
            print(" score≥%d:" % ms)
            side_table("顶背离", top)
            side_table("底背离", bot)


def ev_med(cl, h):
    """事件级中位：每个事件内先取该事件的中位，再对事件取中位。"""
    return median([median([x[2][h] for x in c]) for c in cl])


def judge():
    """独立事件级终审：分侧、分得分、分年代、留一检验 + 可交易性净额。"""
    panels = panels_with_baseline("std")
    L, G = DEFAULT_L, DEFAULT_G
    print("=== 独立事件级终审（L=%d, G=%d, 参数族 std）===" % (L, G))
    for side, lab in (("top", "顶背离"), ("bottom", "底背离")):
        for ms in (3, 2, 1):
            hits = collect(panels, side, L, G, ms)
            if not hits:
                continue
            ep, cl = to_events(hits)
            print("\n%s score≥%d：命中日 %d → 单指数时段 %d → 市场事件 %d"
                  % (lab, ms, len(hits), len(ep), len(cl)))
            print("  时段起点超额中位:  %s"
                  % "  ".join("h%d %+.2f" % (h, median([e[h] for _n, _d, _s, e in ep]))
                              for h in FWD))
            print("  事件级超额中位:    %s"
                  % "  ".join("h%d %+.2f" % (h, ev_med(cl, h)) for h in FWD))
            if ms == 3:
                # 三重背离的得分构成：到底是哪一重起了作用
                for bits, name in (((1, 1, 1), "布林+MACD+RSI"),
                                   ((1, 1, 0), "布林+MACD"),
                                   ((1, 0, 1), "布林+RSI"),
                                   ((0, 1, 1), "MACD+RSI")):
                    sel = [(nm, d, sg, e) for nm, d, sg, e in hits
                           if (sg["boll"], sg["macd"], sg["rsi"]) == bits]
                    if len(sel) < 30:
                        continue
                    sep, scl = to_events(sel)
                    print("    组合%-12s 时段%5d 事件%4d  20日%+.2f  30日%+.2f  60日%+.2f"
                          % (name, len(sep), len(scl), ev_med(scl, 20),
                             ev_med(scl, 30), ev_med(scl, 60)))
                # 分年代
                print("    --- 按事件起点年代（事件级 20 日超额）---")
                for dec in ("200x", "201x", "202x"):
                    cc = [c for c in cl if min(x[1] for x in c)[:3] == dec[:3]]
                    if not cc:
                        continue
                    v = [median([x[2][20] for x in c]) for c in cc]
                    print("    %s  事件%3d  20日超额%+.2f  为正 %d/%d"
                          % (dec, len(cc), median(v), sum(1 for x in v if x > 0), len(v)))
                # 留一：剔掉最有利（顶背离=超额最负）的若干事件
                sign = 1 if side == "bottom" else -1
                v = sorted([median([x[2][20] for x in c]) for c in cl], key=lambda x: sign * x)
                if len(v) >= 12:
                    print("    留一检验（20日超额，原中位 %+.2f）：" % median(v))
                    for drop in (1, 3, 5):
                        print("      剔掉最有利 %d 个事件后中位 %+.2f（剩 %d 事件）"
                              % (drop, median(v[:-drop]), len(v) - drop))
            # 可交易净额（每指数时段首日买入，持有 20/30 日，扣赎回费）
            for h in (20, 30):
                nets = sorted(e[h] - fee(h) for _n, _d, _s, e in ep)
                win = sum(1 for x in nets if x > 0)
                print("    可交易(时段首日买·持有%d日扣费%.1f%%): 费后中位 %+.2f%%  为正 %d/%d"
                      % (h, fee(h), median(nets), win, len(nets)))


def control():
    """对照组：把"创 L 日新高/新低"本身的效应与"背离"分开。

    这是判定的关键：若 score=0（创新高/新低但三重指标全都跟上）的超额与
    score≥1 差不多，则"背离"这个说法是多余的，真正起作用的是"创新高/新低"。
    """
    panels = panels_with_baseline("std")
    print("=== 对照检验：'创新高/新低'本身 vs '背离' ===")
    print("读法：score=0 是只创新高/新低而三重指标全都跟上（无背离）的对照组。")
    print("若 score≥1 与 score=0 的 20 日超额差不多大，则'背离'是多余的。")
    for L in (60, 120, 250):
        for side, lab, want in (("top", "创新高→顶背离", "负"),
                                ("bottom", "创新低→底背离", "正")):
            hits = collect(panels, side, L, DEFAULT_G, 0)   # 含 score=0
            print("\n-- L=%d %s（要有用则 20 日超额应为%s）--" % (L, lab, want))
            print("   %-7s %6s %6s %9s %9s %9s" % (
                "score", "时段", "事件", "时段20日", "事件20日", "事件30日"))
            for sc in (0, 1, 2, 3):
                sel = [(nm, d, sg, e) for nm, d, sg, e in hits if sg["score"] == sc]
                if len(sel) < 20:
                    print("   %-7s 样本不足(%d)" % (sc, len(sel)))
                    continue
                sep, scl = to_events(sel)
                print("   %-7d %6d %6d %+9.2f %+9.2f %+9.2f"
                      % (sc, len(sep), len(scl),
                         median([e[20] for _n, _d, _s, e in sep]),
                         ev_med(scl, 20), ev_med(scl, 30)))


def diag():
    """诊断"时段内第几根信号"：把同一指数时段的信号按先后拆开看超额。

    动机：--judge 里底背离"时段起点"与"事件级"符号相反，必须查清是
    "信号无用"还是"第一根信号太早、后面几根才有用"（后者对应文章的'等共振'）。
    """
    panels = panels_with_baseline("std")
    L, G = DEFAULT_L, DEFAULT_G
    for side, lab in (("top", "顶背离"), ("bottom", "底背离")):
        print("\n=== %s：时段内第 n 根信号（L=%d, score≥3）===" % (lab, L))
        grouped = {}
        for nm, (rows, base) in panels.items():
            last_i = None
            epk = None
            for sg in detect(rows, side, L, G, 3):
                i, b = sg["i"], base[sg["i"]]
                if any(b[h] is None for h in FWD):
                    continue
                if last_i is None or i - last_i > 20:
                    epk = (nm, i)
                    grouped.setdefault(epk, [])
                last_i = i
                grouped[epk].append({h: sg["fwd%d" % h] - b[h] for h in FWD})
        for rk, rname in ((1, "第1根"), (2, "第2根"), (3, "第3根及以后")):
            v20, v60 = [], []
            for arr in grouped.values():
                if len(arr) < rk:
                    continue
                v20.append(arr[rk - 1][20])
                v60.append(arr[rk - 1][60])
            if len(v20) < 20:
                print("  %-12s 样本不足(%d)" % (rname, len(v20)))
                continue
            print("  %-12s 时段%4d  20日超额中位 %+.2f%%  60日 %+.2f%%  20日为正 %d/%d"
                  % (rname, len(v20), median(v20), median(v60),
                     sum(1 for v in v20 if v > 0), len(v20)))
        sizes = sorted(len(a) for a in grouped.values())
        print("  （时段规模：中位 %d 根信号，最大 %d 根，共 %d 个时段）"
              % (sizes[len(sizes) // 2], sizes[-1], len(sizes)))


def system():
    """测原文真正的系统：背离叠加 MA120 半年线区间。

    原文逻辑：跌破半年线 15% 才进吸筹区（此时才认底背离）；站上 15% 才认减仓区
    （此时才认顶背离）。若"背离"只有挂在位置上才有用，这里应能看出来。
    """
    panels = panels_with_baseline("std")
    L, G = DEFAULT_L, DEFAULT_G
    print("=== 背离 × MA120 区间（L=%d, G=%d, score≥3）===" % (L, G))
    zons = (("半年线下15%以上(深跌区)", lambda d: d is not None and d <= -15.0),
            ("半年线-15%~0%(偏低区)", lambda d: d is not None and -15.0 < d <= 0.0),
            ("半年线0%~+15%(偏高区)", lambda d: d is not None and 0.0 < d <= 15.0),
            ("半年线上15%以上(高位区)", lambda d: d is not None and d > 15.0))
    for side, lab in (("bottom", "底背离"), ("top", "顶背离")):
        hits = collect(panels, side, L, G, 3)
        print("\n-- %s（%d 个时段）--" % (lab, len(to_events(hits)[0])))
        print("   %-24s %6s %6s %9s %9s" % ("MA120 区间", "时段", "事件", "20日超额", "60日超额"))
        for zname, zpred in zons:
            sel = [(nm, d, sg, e) for nm, d, sg, e in hits if zpred(sg["dma"])]
            if len(sel) < 20:
                print("   %-24s 样本不足(%d)" % (zname, len(sel)))
                continue
            sep, scl = to_events(sel)
            print("   %-24s %6d %6d %+9.2f %+9.2f"
                  % (zname, len(sep), len(scl), ev_med(scl, 20), ev_med(scl, 60)))

    # 关键对照：区间内"背离 vs 无背离"——若两者差不多，说明起作用的只是区间(位置)
    print("\n=== 区间内对照：同一 MA120 区间下 score=0（无背离）vs score≥3（三重背离）===")
    zkey = {"bottom": ("半年线下15%以上(深跌区)", lambda d: d is not None and d <= -15.0),
            "top": ("半年线上15%以上(高位区)", lambda d: d is not None and d > 15.0)}
    for side, lab in (("bottom", "底背离·深跌区"), ("top", "顶背离·高位区")):
        zname, zpred = zkey[side]
        allh = collect(panels, side, L, G, 0)
        print("\n-- %s（%s）--" % (lab, zname))
        print("   %-10s %6s %6s %9s %9s" % ("score", "时段", "事件", "20日超额", "60日超额"))
        for sc in (0, 1, 2, 3):
            sel = [(nm, d, sg, e) for nm, d, sg, e in allh
                   if sg["score"] == sc and zpred(sg["dma"])]
            if len(sel) < 20:
                print("   %-10s 样本不足(%d)" % (sc, len(sel)))
                continue
            sep, scl = to_events(sel)
            print("   %-10d %6d %6d %+9.2f %+9.2f"
                  % (sc, len(sep), len(scl), ev_med(scl, 20), ev_med(scl, 60)))


def current():
    """当前各指数是否正处背离状态（用最新收盘，不砍尾部）。

    注意：面板里的行被砍掉了末尾 max(FWD) 根（那几根算不出未来收益），
    所以"当前状态"必须另走一条不依赖未来收益的路径，否则日期会停在一个月前。
    """
    ser = load_index_series()
    boll_p, boll_s, rsi_p = IND_FAMILIES["std"]
    print("=== 当前背离状态（最新收盘, L=%d, G=%d, score≥2）===" % (DEFAULT_L, DEFAULT_G))
    found = []
    for nm, pts in ser.items():
        c = [x for _, x in pts]
        if len(c) < 130:
            continue
        ub, _m, lb = TI.bollinger_bands(c, boll_p, boll_s)
        dif, _dea, hist = TI.macd(c)
        r = TI.rsi(c, rsi_p)
        ma120 = TI.sma(c, 120)
        rows = []
        for i in range(len(c)):
            if None in (ub[i], lb[i], dif[i], hist[i], r[i]):
                continue
            rows.append((pts[i][0], {"close": c[i], "ub": ub[i], "lb": lb[i],
                                     "dif": dif[i], "hist": hist[i], "rsi": r[i],
                                     "dma120": (c[i] / ma120[i] - 1) * 100.0 if ma120[i] else None}))
        if not rows:
            continue
        for side, lab in (("top", "顶背离"), ("bottom", "底背离")):
            for sg in detect(rows, side, DEFAULT_L, DEFAULT_G, 2):
                if sg["date"] == rows[-1][0]:
                    found.append((nm, lab, sg["score"], sg["date"], sg["ref_date"], sg["dma"]))
    if not found:
        print("  最新交易日无 score≥2 的背离信号")
    for nm, lab, sc, d, rd, dma in sorted(found, key=lambda x: (-x[2], x[0])):
        z = "半年线%+.1f%%" % dma if dma is not None else "半年线预热不足"
        print("  %-10s %s score=%d（参照峰 %s，%s）" % (nm, lab, sc, rd, z))
    print("\n读法：顶背离=注意兑现（近年才较稳）；底背离=需配合'半年线下15%以上'才算数，"
          "单看背离无效（十万次迭代结论）。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--control", action="store_true")
    ap.add_argument("--diag", action="store_true")
    ap.add_argument("--system", action="store_true")
    ap.add_argument("--current", action="store_true")
    a = ap.parse_args()
    if a.current:
        current()
        return
    if a.system:
        system()
        return
    if a.diag:
        diag()
        return
    if a.control:
        control()
        return
    if a.judge:
        judge()
        return
    scan()


if __name__ == "__main__":
    main()
