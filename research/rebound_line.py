# -*- coding: utf-8 -*-
"""超跌反弹线（右侧抢反弹）—— 超跌状态诊断 + 触发条件化诊断

定位：与 daily_1430 短线B区（要求 20日涨幅 -8%~+5%、不追涨的企稳/动量轴）**反方向**的一条轴：
买"跌狠了的"。核心问题不是"跌得多不多"，而是"跌到哪个位置、配合什么条件，
未来才真的反弹，而不是继续跌"——即把**超跌**与**下跌中继**分开。

两个诊断（都不写报告、不落库，只打印证据）：
  --scan     候选超跌度量分档：未来 5/10/20 日**截面超额**（相对当日全板块中位数）
  --trigger  在超跌状态上叠"止跌触发"，对比有无触发；并按市况（全市场普跌程度）拆开

无未来函数的三条硬约束：
  ① 特征只用 ≤T 的收盘价；
  ② 分档用"当日截面"或"自身历史的扩张窗口"，绝不用全样本分位；
  ③ 超额基准 = 当日全板块中位数（同日截面），不跨期、不含未来。
证据以**独立时段**计数：同一板块连续命中（间隔 ≤EP_GAP）只算一个时段，只取首日为证据，
避免"一个持续三个月的状态被当成 60 条独立样本"。

费率约束（决定持有期下界）：场外赎回 7天内 1.5%、30天内 0.5%、之后 0。
持有 5 天光赎回费就吃 1.5%——故可交易窗口只检验 10/20 日。

数据源：sector_kline（496 板块，2023-01-03 起日收盘，约 818 个可评估交易日）。
纯标准库。

用法:
  python research/rebound_line.py --scan
  python research/rebound_line.py --trigger
"""
import argparse
import bisect
import datetime
import os
import sys

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DATA_DIR)
for _p in (DATA_DIR, REPO_ROOT, os.path.join(REPO_ROOT, "src", "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from src.common import history_db  # noqa: E402

FWD_H = (5, 10, 20)     # 诊断用未来窗口（5 只看不做，落在费率悬崖内）
WARMUP = 60             # 特征预热：至少 60 根K线
EP_GAP = 10             # 同一板块两次命中间隔 ≤10 个交易日 → 同一时段
MIN_EP = 8              # 独立时段数下限（沿用 validate_advice_rule 的判定门槛）
MIN_SPREAD = 3.0        # 时段起点超额中位数门槛（%）
FEE_20 = 0.5            # 持有 20 日的场外赎回费（%）

# 候选超跌度量：名字 → (判定函数, 中文说明)。全部只用 ≤T 数据。
STATES = [
    ("ret5<=-8", "5日跌幅≥8%", lambda r: r["ret5"] <= -8.0),
    ("ret10<=-12", "10日跌幅≥12%", lambda r: r["ret10"] <= -12.0),
    ("ret20<=-15", "20日跌幅≥15%", lambda r: r["ret20"] <= -15.0),
    ("dd20<=-12", "自20日高点回撤≥12%", lambda r: r["dd20"] <= -12.0),
    ("bias20<=-8", "低于20日均线8%以上(负乖离)", lambda r: r["bias20"] <= -8.0),
    ("consec>=4", "连跌≥4日", lambda r: r["consec"] >= 4),
    ("hpct20<=5", "20日涨幅处自身历史最低5%分位", lambda r: r["hpct20"] <= 5.0),
    ("dd60<=-20", "自60日高点回撤≥20%", lambda r: r["dd60"] <= -20.0),
]

# 引入触发的两个超跌底：一个看跌幅、一个看回撤
OVERSOLD = [
    ("20日跌幅≥15%", lambda r: r["ret20"] <= -15.0),
    ("自20日高点回撤≥12%", lambda r: r["dd20"] <= -12.0),
]

# 止跌触发（机制上都对应"卖压衰竭"的一个侧面）
TRIGGERS = [
    ("〔基准〕无触发", lambda r: True),
    ("当日收阳", lambda r: r["up"] > 0),
    ("连两日收阳", lambda r: r["up2"] > 0),
    ("重回5日线上方", lambda r: r["over_ma5"] > 0),
    ("回撤收敛", lambda r: r["dd20_imp"] > 0),
    ("收阳+回撤收敛", lambda r: r["up"] > 0 and r["dd20_imp"] > 0),
    ("逆势走强(涨幅>中位)", lambda r: r["rs"] > 0),
    ("逆势走强+回撤收敛", lambda r: r["rs"] > 0 and r["dd20_imp"] > 0),
]

# 市况档（当日全板块 20 日涨幅中位数）：检验"是不是只有普跌时抢反弹才成立"
MKT_BUCKETS = [
    ("普跌(≤-6%)", lambda m: m <= -6.0),
    ("偏弱(-6~-2%)", lambda m: -6.0 < m <= -2.0),
    ("平稳(-2~+2%)", lambda m: -2.0 < m <= 2.0),
    ("普涨(>+2%)", lambda m: m > 2.0),
]


def rolling_max(vals, w):
    """out[i] = max(vals[max(0,i-w+1)..i])。单调队列 O(n)。"""
    out, dq = [], []
    for i, v in enumerate(vals):
        while dq and vals[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - w:
            dq.pop(0)
        out.append(vals[dq[0]])
    return out


def rolling_mean(vals, w):
    """out[i] = 前 w 个（含 i）均值；不足 w 个时为 None。"""
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= w:
            s -= vals[i - w]
        out.append(s / w if i >= w - 1 else None)
    return out


def expanding_pct(vals):
    """扩张窗口分位：out[i] = vals[i] 在 vals[0..i-1] 中的百分位（0~100）。无前视。"""
    out, seen = [], []
    for v in vals:
        if v is None or not seen:
            out.append(None)
        else:
            out.append(bisect.bisect_left(seen, v) / len(seen) * 100.0)
        if v is not None:
            bisect.insort(seen, v)
    return out


def load_closes():
    """返回 {code: [(date, close), ...]}，按日期升序。"""
    conn = history_db.connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT code, date, close FROM sector_kline ORDER BY code, date").fetchall()
    finally:
        conn.close()
    kl = {}
    for code, d, c in rows:
        kl.setdefault(code, []).append((d, c))
    return kl


def build_features(pts, fwd_h=FWD_H):
    """单板块/指数特征表：返回 {date: row}，row 只含 ≤该日 的信息。"""
    dates = [d for d, _ in pts]
    c = [x for _, x in pts]
    n = len(c)
    if n < WARMUP + max(fwd_h) + 1:
        return {}
    r5, r10, r20, r60, r250 = (rolling_max(c, w) for w in (5, 10, 20, 60, 250))
    m5, m20, m60 = (rolling_mean(c, w) for w in (5, 20, 60))
    ret20 = [None] * n
    for i in range(20, n):
        if c[i - 20]:
            ret20[i] = (c[i] / c[i - 20] - 1) * 100.0
    hp20 = expanding_pct(ret20)
    out = {}
    consec = 0
    for i in range(n):
        if i >= 1 and c[i] < c[i - 1]:
            consec += 1
        elif i >= 1:
            consec = 0
        if i < WARMUP or i + max(fwd_h) >= n:
            continue
        row = {
            "nhist": float(i),
            "ret1": (c[i] / c[i - 1] - 1) * 100.0,
            "ret5": (c[i] / c[i - 5] - 1) * 100.0,
            "ret10": (c[i] / c[i - 10] - 1) * 100.0,
            "ret20": (c[i] / c[i - 20] - 1) * 100.0,
            "ret60": (c[i] / c[i - 60] - 1) * 100.0,
            "dd5": (c[i] / r5[i] - 1) * 100.0,
            "dd10": (c[i] / r10[i] - 1) * 100.0,
            "dd20": (c[i] / r20[i] - 1) * 100.0,
            "dd60": (c[i] / r60[i] - 1) * 100.0,
            "dd250": (c[i] / r250[i] - 1) * 100.0,
            "bias20": (c[i] / m20[i] - 1) * 100.0 if m20[i] else None,
            "bias60": (c[i] / m60[i] - 1) * 100.0 if m60[i] else None,
            "consec": float(consec),
            "hpct20": hp20[i],
            "over_ma5": 1.0 if (m5[i] and c[i] > m5[i]) else 0.0,
            "up": 1.0 if c[i] > c[i - 1] else 0.0,
            "up2": 1.0 if (c[i] > c[i - 1] and c[i - 1] > c[i - 2]) else 0.0,
            # 回撤收敛：今日距20日高点的回撤比昨日浅（跌势减速，不接自由落体的第一问）
            "dd20_imp": 1.0 if c[i] / r20[i] > c[i - 1] / r20[i - 1] else 0.0,
        }
        for h in fwd_h:
            row["fwd%d" % h] = (c[i + h] / c[i] - 1) * 100.0
        out[dates[i]] = row
    return out


def gap_days(d1, d2):
    """两个交易日期的自然日差折算成大致交易日数（EP_GAP=10 交易日）。"""
    a = datetime.date(*map(int, d1.split("-")))
    b = datetime.date(*map(int, d2.split("-")))
    return (b - a).days * 5 // 7


def episodes(hits):
    """hits: [(date, code, exc)]。按板块分组，间隔 ≤EP_GAP 的合并为一个时段，
    每个独立时段只取首日作为证据（避免持续状态被当成多条独立样本）。"""
    by_code = {}
    for d, code, exc in hits:
        by_code.setdefault(code, []).append((d, exc))
    starts = []
    for code, arr in by_code.items():
        arr.sort()
        prev = None
        for d, exc in arr:
            if prev is None or gap_days(prev, d) > EP_GAP:
                starts.append((code, d, exc))
            prev = d
    return starts


def median(vals):
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def context(kl):
    """一次性算出各板块特征表 + 逐日截面基准（中位数）。"""
    feats = {}
    for code, pts in kl.items():
        f = build_features(pts)
        if f:
            feats[code] = f
    alldates = sorted({d for f in feats.values() for d in f})
    by_date = {}
    for code, f in feats.items():
        for d, row in f.items():
            by_date.setdefault(d, []).append((code, row))
    mkt, med1, mret20 = {}, {}, {}
    for d in alldates:
        rows = by_date[d]
        mkt[d] = {h: median([r["fwd%d" % h] for _, r in rows]) for h in FWD_H}
        med1[d] = median([r["ret1"] for _, r in rows])
        mret20[d] = median([r["ret20"] for _, r in rows])
    return feats, alldates, by_date, mkt, med1, mret20


def row_exc(r, med):
    """该行相对当日中位数的超额；任一窗口缺值返回 None。"""
    if any(med[h] is None or r.get("fwd%d" % h) is None for h in FWD_H):
        return None
    return {h: r["fwd%d" % h] - med[h] for h in FWD_H}


def cells_html(vals, fmt="%8.2f"):
    return " ".join(fmt % v if v is not None else "%8s" % "-" for v in vals)


def scan(feats, alldates, by_date, mkt):
    print("\n=== 候选超跌度量诊断（超额 = 相对当日全板块中位数）===")
    print("时段起点 = 每个独立时段只取第一天（可下手的那天）；'全部'= 所有命中日")
    for name, desc, pred in STATES:
        h = []
        for d in alldates:
            for code, r in by_date[d]:
                if not pred(r):
                    continue
                exc = row_exc(r, mkt[d])
                if exc is not None:
                    h.append((d, code, exc))
        if not h:
            print("\n%-12s %s：无命中" % (name, desc))
            continue
        starts = episodes(h)
        print("\n%-12s %s" % (name, desc))
        print("  命中板块日 %6d   独立时段 %4d   覆盖板块 %3d   覆盖交易日 %3d" % (
            len(h), len(starts), len({c for c, _, _ in starts}), len({d for _, d, _ in starts})))
        print("  %-22s %8s %8s %8s" % ("", "fwd5", "fwd10", "fwd20"))
        for label, arr in (("时段起点超额中位", [e for _, _, e in starts]),
                           ("全部命中日超额中位", [e for _, _, e in h])):
            print("  %-22s %s" % (label, cells_html([median([x[hh] for x in arr]) for hh in FWD_H])))
        row = []
        for y in sorted({d[:4] for d in alldates}):
            ys = [(c, d, e) for c, d, e in starts if d[:4] == y]
            row.append("%s:n<3" % y if len(ys) < 3 else
                       "%s:%+.1f(%d)" % (y, median([e[20] for _, _, e in ys]), len(ys)))
        print("  时段起点 fwd20 按年: %s" % "  ".join(row))


def trigger(feats, alldates, by_date, mkt, med1, mret20):
    years = sorted({d[:4] for d in alldates})
    for od_name, opred in OVERSOLD:
        base = []
        for d in alldates:
            for code, r in by_date[d]:
                if not opred(r):
                    continue
                exc = row_exc(r, mkt[d])
                if exc is None:
                    continue
                x = dict(r)
                x["_d"], x["_c"], x["_exc"] = d, code, exc
                x["rs"] = r["ret1"] - (med1[d] or 0.0)
                base.append(x)
        print("\n" + "=" * 78)
        print("=== 超跌(%s) + 止跌触发：条件化诊断 ===" % od_name)
        print("状态命中板块日 %d；净超额 = 时段起点超额 - %.1f%%（20日持有赎回费）"
              % (len(base), FEE_20))
        print("%-20s %7s %6s %9s %9s %9s  %s" % (
            "触发条件", "命中日", "时段", "超额10日", "超额20日", "净20日", "按年超额20日"))
        for tname, tpred in TRIGGERS:
            sel = [x for x in base if tpred(x)]
            if not sel:
                continue
            starts = episodes([(x["_d"], x["_c"], x["_exc"]) for x in sel])
            m10 = median([e[10] for _, _, e in starts])
            m20 = median([e[20] for _, _, e in starts])
            yr = []
            for y in years:
                ys = [(c, d, e) for c, d, e in starts if d[:4] == y]
                yr.append("%s:%+.1f(%d)" % (y, median([e[20] for _, _, e in ys]), len(ys))
                          if ys else "%s:--" % y)
            print("%-20s %7d %6d %9.2f %9.2f %9.2f  %s" % (
                tname, len(sel), len(starts), m10, m20, m20 - FEE_20, " ".join(yr)))

        # 加深程度：超跌越深是否越好
        print("\n  --- 同一状态下按'超跌深度'再分档（时段起点，fwd20 超额）---")
        for lo, hi, lab in ((-15.0, -18.0, "-15%~-18%"), (-18.0, -22.0, "-18%~-22%"),
                            (-22.0, -100.0, "≤-22%")):
            sel = [x for x in base if lo >= x["ret20"] > hi]
            if len(sel) < 20:
                continue
            st = episodes([(x["_d"], x["_c"], x["_exc"]) for x in sel])
            print("  20日跌幅 %-10s 命中%6d 时段%5d 起点超额20日 %+.2f%%" % (
                lab, len(sel), len(st), median([e[20] for _, _, e in st])))

        # 市况档：检验"是不是只有全市场跟着普跌时抢反弹才成立"（恐慌才买）
        print("\n  --- 按市况拆（当日全板块20日涨幅中位数的档位；时段起点，fwd20 超额）---")
        for mname, mpred in MKT_BUCKETS:
            sel = [x for x in base if mpred(mret20[x["_d"]])]
            if len(sel) < 20:
                print("  %-14s 命中%6d 样本不足" % (mname, len(sel)))
                continue
            st = episodes([(x["_d"], x["_c"], x["_exc"]) for x in sel])
            print("  %-14s 命中%6d 时段%5d 起点超额20日 %+.2f%%" % (
                mname, len(sel), len(st), median([e[20] for _, _, e in st])))


def date_clusters(starts, gap=20):
    """把时段起点按日期邻近合并成"市场事件"：相隔 ≤gap 交易日的起点视为同一次普跌行情。
    返回 [[(code, date, exc), ...], ...]——每个元素是一次独立的市场事件。

    必要性：2024-01、2024-09 这类日子有几百个板块同时超跌，"板块×时段"计数会把
    同一次行情当成几百条独立证据。按事件聚合后，样本量才是真实的信息量。"""
    ss = sorted(starts, key=lambda x: x[1])
    clusters, cur, last = [], [], None
    for c, d, e in ss:
        if last is not None and gap_days(last, d) > gap:
            clusters.append(cur)
            cur = []
        cur.append((c, d, e))
        last = d
    if cur:
        clusters.append(cur)
    return clusters


IDX_FWD = (20, 60)      # 指数层持有窗口（宽基反弹节奏比行业慢，且场外基金至少持 10 日）
IDX_MIN_ROWS = 1000     # 至少 4 年历史才纳入（基准需要足够样本）

IDX_STATES = [
    ("20日跌幅≥15%", lambda r: r["ret20"] <= -15.0),
    ("20日跌幅≥20%", lambda r: r["ret20"] <= -20.0),
    ("20日跌幅≥25%", lambda r: r["ret20"] <= -25.0),
    ("自60日高点回撤≥20%", lambda r: r["dd60"] <= -20.0),
    ("自250日高点回撤≥30%", lambda r: r["nhist"] >= 250 and r["dd250"] <= -30.0),
    ("20日跌≥15%+连两日收阳", lambda r: r["ret20"] <= -15.0 and r["up2"] > 0),
    ("20日跌≥20%+连两日收阳", lambda r: r["ret20"] <= -20.0 and r["up2"] > 0),
    ("250日回撤≥30%+连两日收阳",
     lambda r: r["nhist"] >= 250 and r["dd250"] <= -30.0 and r["up2"] > 0),
]


def load_index_series(min_rows=IDX_MIN_ROWS):
    """从 data/kline_full 读长历史指数日收盘（列：日期,收盘）。返回 {名称: [(date, close)]}。"""
    import csv
    d = os.path.join(DATA_DIR, "..", "data", "kline_full")
    out = {}
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".csv"):
            continue
        pts = []
        with open(os.path.join(d, fn), encoding="utf-8-sig") as f:
            rd = csv.reader(f)
            next(rd, None)
            for r in rd:
                if len(r) < 2 or not r[0]:
                    continue
                try:
                    c = float(r[1])
                except ValueError:
                    continue
                if c > 0:
                    pts.append((r[0][:10], c))
        if len(pts) >= min_rows:
            out[fn[:-4]] = pts
    return out


def index_pairs():
    """返回 (per, pairs)。per={名称:[(日期,row)]}（已按日期升序）；
    pairs=[(名称, 日期, row, {h: 超额})]，超额基准 = **该指数自身**的扩张窗口无条件收益中位数
    （逐日把 h 天前那行的已实现未来收益加入样本，故无前视）。"""
    ser = load_index_series()
    per = {}
    for nm, pts in ser.items():
        f = build_features(pts, fwd_h=IDX_FWD)
        if f:
            per[nm] = sorted(f.items())
    pairs = []
    for nm, rows in per.items():
        seen = {h: [] for h in IDX_FWD}
        for j, (d, r) in enumerate(rows):
            for h in IDX_FWD:
                k = j - h
                if k >= 0:                      # 该行的未来收益此刻已实现 → 无前视
                    bisect.insort(seen[h], rows[k][1]["fwd%d" % h])
            if any(len(seen[h]) < 60 for h in IDX_FWD):
                continue
            pairs.append((nm, d, r, {h: r["fwd%d" % h] - median(seen[h]) for h in IDX_FWD}))
    return per, pairs


def index_study():
    """指数层超跌反弹检验。

    与板块层的两个关键差别：
      ① 基准换成**该指数自身的扩张窗口无条件收益中位数**（板块层用当日截面中位数）——
         否则不同指数的长期漂移会被误当成信号；
      ② 历史长得多（上证/深成/恒生到 2000 年），独立事件数量才够做判断。
    独立性：同一天多个指数一起超跌属于同一次行情，按日期聚类只算一条证据。"""
    per, pairs = index_pairs()
    print("\n" + "=" * 78)
    print("=== 指数层超跌反弹检验（超额 = 相对该指数自身扩张窗口基准）===")
    for nm in sorted(per):
        rows = per[nm]
        print("  %-10s %s ~ %s  %d 个交易日  fwd20基准 %+.2f%%"
              % (nm, rows[0][0], rows[-1][0], len(rows),
                 median([r["fwd20"] for _, r in rows])))

    print("\n%-26s %6s %6s %9s %9s %6s" % ("状态", "配对", "事件", "超额20日", "超额60日", "为正"))
    for name, pred in IDX_STATES:
        sel = [(d, nm, exc) for nm, d, r, exc in pairs if pred(r)]
        if not sel:
            print("%-26s 无命中" % name)
            continue
        starts = episodes(sel)
        cl = date_clusters(starts, gap=20)
        v20 = [median([x[2][20] for x in c]) for c in cl]
        v60 = [median([x[2][60] for x in c]) for c in cl]
        pos = sum(1 for v in v20 if v > 0)
        print("%-26s %6d %6d %9.2f %9.2f %4d/%d" % (
            name, len(sel), len(cl), median(v20), median(v60), pos, len(v20)))
        # 聚类间隔敏感性 + 按指数一致性
        sens = []
        for gap in (5, 20, 60, 120):
            cc = date_clusters(starts, gap)
            sens.append("gap=%-3d:%2d事件%+6.2f%%" % (
                gap, len(cc), median([median([x[2][20] for x in c]) for c in cc])))
        print("    敏感度 %s" % "  ".join(sens))
        byidx = {}
        for d, nm, exc in sel:
            byidx.setdefault(nm, []).append(exc[20])
        cons = sorted(byidx.items(), key=lambda kv: -median(kv[1]))
        print("    按指数（配对级中位 fwd20 超额）: %s" % "  ".join(
            "%s%+.1f(%d)" % (k, median(v), len(v)) for k, v in cons[:8]))
        if len(cl) <= 15:
            print("    事件明细:")
            for c in cl:
                d0 = min(x[1] for x in c)
                print("      %s~%s  %d个配对 超额20日%+7.2f%% 超额60日%+7.2f%%" % (
                    d0, max(x[1] for x in c), len(c),
                    median([x[2][20] for x in c]), median([x[2][60] for x in c])))


DEPTH_BUCKETS = [(15.0, 20.0), (20.0, 25.0), (25.0, 30.0), (30.0, 999.0)]
RULE_DEPTH = -20.0          # 触发深度（20日跌幅 ≥20%）
RULE_HOLD = (20, 60)        # 持有窗口
RULE_EP_GAP = 20            # 同一轮超跌里只取首个触发日


def rule_study():
    """把"指数 20 日跌 ≥20% → 抄底"当成一条真规则来检验：互斥深度分档（是否单调）、
    费后可交易性（首触发日买入、持有 20/60 日）、以及当前哪些指数正处该状态。"""
    from src.common.fees import fee
    per, pairs = index_pairs()

    print("\n" + "=" * 78)
    print("=== 互斥深度分档：跌得越深是否真的越好 ===")
    print("%-14s %6s %6s %9s %9s %7s" % ("20日跌幅", "配对", "事件", "超额20日", "超额60日", "为正"))
    for lo, hi in DEPTH_BUCKETS:
        sel = [(d, nm, exc) for nm, d, r, exc in pairs if lo <= -r["ret20"] < hi]
        if not sel:
            continue
        cl = date_clusters(episodes(sel), gap=20)
        v20 = [median([x[2][20] for x in c]) for c in cl]
        v60 = [median([x[2][60] for x in c]) for c in cl]
        lab = "≥30%" if hi > 900 else "%d%%~%d%%" % (lo, hi)
        print("%-14s %6d %6d %9.2f %9.2f %5d/%d" % (
            lab, len(sel), len(cl), median(v20), median(v60),
            sum(1 for v in v20 if v > 0), len(v20)))

    print("\n=== 可交易性：每轮超跌只在首个触发日买入（20日跌幅≥20%），持有 20/60 日 ===")
    trades = {h: [] for h in RULE_HOLD}
    for nm, rows in per.items():
        last_i = None
        for i, (d, r) in enumerate(rows):
            if r["ret20"] > RULE_DEPTH:
                continue
            seen = last_i is not None and i - last_i <= RULE_EP_GAP
            last_i = i
            if seen:
                continue
            for h in RULE_HOLD:
                trades[h].append((nm, d, r["fwd%d" % h] - fee(h), r["fwd%d" % h]))
    for h in RULE_HOLD:
        tr = trades[h]
        if not tr:
            continue
        net = sorted(x[2] for x in tr)
        win = sum(1 for v in net if v > 0)
        print("\n持有 %d 日（赎回费 %.1f%%）：%d 笔" % (h, fee(h), len(tr)))
        print("  费后中位 %+.2f%%  均值 %+.2f%%  为正 %d/%d（%.0f%%）  最差 %+.1f%%  最好 %+.1f%%"
              % (median(net), sum(net) / len(net), win, len(net), win / len(net) * 100,
                 net[0], net[-1]))
        for dec in ("200x", "201x", "202x"):
            ds = sorted(x[2] for x in tr if x[1][:3] == dec[:3])
            if ds:
                print("    %s  %3d 笔  费后中位 %+.2f%%  为正 %d/%d"
                      % (dec, len(ds), median(ds), sum(1 for v in ds if v > 0), len(ds)))
        print("  亏损最大的 5 笔: %s" % "  ".join(
            "%s %s %+.1f%%" % (nm, d, v)
            for nm, d, v, _g in sorted(tr, key=lambda x: x[2])[:5]))
        # 笔数会高估独立性：同一天多个指数同时触发其实是同一轮行情
        ent = [(d, nm, {h: net}) for nm, d, net, _g in tr]
        cl = date_clusters(episodes(ent), gap=RULE_EP_GAP)
        ev = [median([x[2][h] for x in c]) for c in cl]
        print("  但聚成独立行情只有 %d 轮：轮级中位 %+.2f%%  为正 %d/%d"
              % (len(cl), median(ev), sum(1 for v in ev if v > 0), len(ev)))

    print("\n=== 当前状态与推荐（截至最新收盘）===")
    print("推荐依据来自上两节：20%~30% 深度有正超额；<20% 不构成信号；≥30% 的 60 日反而为负。")
    ser = load_index_series()
    cur = []
    for nm, pts in ser.items():
        c = [x for _, x in pts]
        if len(c) < 251:
            continue
        cur.append((nm, pts[-1][0], (c[-1] / c[-21] - 1) * 100,
                    (c[-1] / max(c[-60:]) - 1) * 100, (c[-1] / max(c[-250:]) - 1) * 100))
    cur.sort(key=lambda x: x[2])
    print("%-10s %-12s %9s %9s %9s  %s" % ("指数", "日期", "20日涨幅", "距60日高", "距250日高", "判定"))
    for nm, d, r20, dd60, dd250 in cur:
        if r20 <= -30.0:
            verdict = "极度超跌·60日历史为负，不推荐"
        elif r20 <= -20.0:
            verdict = "★推荐抄底（20~30%深度区间）"
        elif r20 <= -15.0:
            verdict = "观察（超额仅+0.84%，不构成信号）"
        else:
            verdict = "—"
        print("%-10s %-12s %8.2f%% %8.2f%% %8.2f%%  %s" % (nm, d, r20, dd60, dd250, verdict))


def enrich(alldates, by_date, mkt, mret20):
    """把每行附上当日截面基准与超额（相对超跌 rs20 = 20日涨幅 - 当日中位数）。"""
    out = []
    for d in alldates:
        rows = by_date[d]
        mdd20 = median([r["dd20"] for _, r in rows])
        for code, r in rows:
            exc = row_exc(r, mkt[d])
            if exc is None:
                continue
            x = dict(r)
            x["_d"], x["_c"], x["_exc"] = d, code, exc
            x["rs20"] = r["ret20"] - mret20[d]      # 相对全市场：跑输/跑赢多少 pp
            x["rsdd"] = r["dd20"] - mdd20
            out.append(x)
    return out


def sens(alldates, by_date, mkt, mret20):
    """事件聚类间隔敏感性：结论必须在任何间隔下都不成立，才敢判"不成立"。
    间隔太松 → 把独立证据合并掉；太紧 → 把同一次行情算成多条。"""
    enriched = enrich(alldates, by_date, mkt, mret20)
    cand = [
        ("绝对超跌 20日≤-15%", lambda r: r["ret20"] <= -15.0),
        ("绝对超跌 20日≤-18%", lambda r: r["ret20"] <= -18.0),
        ("绝对超跌 20日≤-22%", lambda r: r["ret20"] <= -22.0),
        ("绝对≤-18% + 连两日收阳", lambda r: r["ret20"] <= -18.0 and r["up2"] > 0),
        ("相对超跌 跑输中位≥10pp", lambda r: r["rs20"] <= -10.0),
        ("自20日高点回撤≥12% + 连两日收阳", lambda r: r["dd20"] <= -12.0 and r["up2"] > 0),
    ]
    print("\n" + "=" * 78)
    print("=== 事件聚类间隔敏感性（fwd20 超额，事件级中位）===")
    for name, pred in cand:
        sel = [x for x in enriched if pred(x)]
        if not sel:
            continue
        starts = episodes([(x["_d"], x["_c"], x["_exc"]) for x in sel])
        line = []
        for gap in (3, 5, 10, 20, 40):
            cl = date_clusters(starts, gap)
            vals = [median([x[2][20] for x in c]) for c in cl]
            pos = sum(1 for v in vals if v > 0)
            line.append("gap=%-2d:%3d事件 %+6.2f%%(%d正)"
                        % (gap, len(cl), median(vals), pos))
        print("\n%s" % name)
        print("  " + "  ".join(line))


def judge(feats, alldates, by_date, mkt, mret20):
    """对候选规则做"独立事件级"终审：把同一次普跌里的几百个板块只算一条证据，
    再看收益分布（中位/均值/为正占比），并按市场自身未来方向拆开——区分
    "真有超跌反弹"与"超跌=高贝塔，市场涨就赢"。

    候选含两个方向：① 绝对超跌（跌够了）；② **相对超跌**（跌得比全市场多），
    后者才有截面选择信息——绝对超跌往往全市场一起发生，选不出行业，只剩择时。"""
    enriched = enrich(alldates, by_date, mkt, mret20)

    cand = [
        ("绝对超跌 20日≤-15%", lambda r: r["ret20"] <= -15.0),
        ("绝对超跌 20日≤-18%", lambda r: r["ret20"] <= -18.0),
        ("绝对超跌 20日≤-22%", lambda r: r["ret20"] <= -22.0),
        ("相对超跌 跑输中位≥10pp", lambda r: r["rs20"] <= -10.0),
        ("相对超跌 跑输中位≥15pp", lambda r: r["rs20"] <= -15.0),
        ("相对超跌≥10pp + 连两日收阳", lambda r: r["rs20"] <= -10.0 and r["up2"] > 0),
        ("相对超跌≥10pp + 当日收阳", lambda r: r["rs20"] <= -10.0 and r["up"] > 0),
        ("绝对≤-15% + 连两日收阳", lambda r: r["ret20"] <= -15.0 and r["up2"] > 0),
        ("绝对≤-18% + 连两日收阳", lambda r: r["ret20"] <= -18.0 and r["up2"] > 0),
        ("自身历史最低5%分位 + 连两日收阳",
         lambda r: r["hpct20"] is not None and r["hpct20"] <= 5.0 and r["up2"] > 0),
    ]
    print("\n" + "=" * 78)
    print("=== 独立事件级终审（同一次普跌里的多个板块只算一条证据）===")
    for name, pred in cand:
        sel = [x for x in enriched if pred(x)]
        if not sel:
            continue
        hits = [(x["_d"], x["_c"], x["_exc"]) for x in sel]
        starts = episodes(hits)
        cl = date_clusters(starts, gap=20)
        raw = [e[20] for _, _, e in hits]
        st20 = [e[20] for _, _, e in starts]
        clmed = []
        for c in cl:
            d0 = min(x[1] for x in c)
            clmed.append((d0, max(x[1] for x in c), len(c),
                          median([x[2][20] for x in c]), mkt[d0][20]))
        vals = [x[3] for x in clmed]
        pos = sum(1 for v in vals if v > 0)
        print("\n%s" % name)
        print("  板块日 %6d  → 时段 %5d  → **独立事件 %3d**" % (len(hits), len(starts), len(cl)))
        print("  fwd20 超额：板块日中位 %+.2f%%/均值 %+.2f%% ｜ 时段起点中位 %+.2f%%/均值 %+.2f%%"
              % (median(raw), sum(raw) / len(raw), median(st20), sum(st20) / len(st20)))
        print("  事件级中位 %+.2f%%（%d/%d 个事件为正，%.0f%%）净额 %+.2f%%（扣0.5%%赎回费）"
              % (median(vals), pos, len(vals), pos / len(vals) * 100, median(vals) - FEE_20))
        if len(cl) <= 20:
            print("  事件明细（起点~终点 板块数 该事件超额 市场自身fwd20）:")
            for d0, d1, n, v, mk in clmed:
                print("    %s~%s  n=%-4d 超额%+7.2f%%  市场%+6.2f%%" % (d0, d1, n, v, mk))
        up = [x[3] for x in clmed if x[4] > 0]
        dn = [x[3] for x in clmed if x[4] <= 0]
        fmt = lambda a: "n=%d 中位%+.2f%%" % (len(a), median(a)) if a else "n=0"
        print("  拆市场方向：市场未来20日为正 %s ｜ 为负 %s" % (fmt(up), fmt(dn)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="候选超跌度量诊断")
    ap.add_argument("--trigger", action="store_true", help="超跌+止跌触发条件化诊断")
    ap.add_argument("--judge", action="store_true", help="独立事件级终审（防独立性假账/贝塔误判）")
    ap.add_argument("--sens", action="store_true", help="事件聚类间隔敏感性")
    ap.add_argument("--index", action="store_true", help="指数层超跌反弹检验（长历史）")
    ap.add_argument("--rule", action="store_true", help="指数超跌抄底规则：深度分档/可交易性/当前状态")
    a = ap.parse_args()

    # 指数层不依赖板块库，单独走一条；其余模式共用板块面板
    if a.index:
        index_study()
        return
    if a.rule:
        rule_study()
        return

    kl = load_closes()
    print("板块数 %d，日期区间 %s ~ %s" % (
        len(kl),
        min(d for pts in kl.values() for d, _ in pts),
        max(d for pts in kl.values() for d, _ in pts)))
    feats, alldates, by_date, mkt, med1, mret20 = context(kl)
    print("有效板块 %d，可评估交易日 %d" % (len(feats), len(alldates)))
    print("\n=== 市场背景（当日全板块中位数，%）===")
    print("%-6s %8s %8s %8s" % ("年", "fwd5", "fwd10", "fwd20"))
    for y in sorted({d[:4] for d in alldates}):
        ds = [d for d in alldates if d[:4] == y]
        print("%-6s %s" % (y, cells_html([median([mkt[d][h] for d in ds]) for h in FWD_H])))
    if a.scan or not (a.trigger or a.judge or a.sens):
        scan(feats, alldates, by_date, mkt)
    if a.trigger:
        trigger(feats, alldates, by_date, mkt, med1, mret20)
    if a.judge:
        judge(feats, alldates, by_date, mkt, mret20)
    if a.sens:
        sens(alldates, by_date, mkt, mret20)


if __name__ == "__main__":
    main()
