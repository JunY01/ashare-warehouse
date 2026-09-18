# -*- coding: utf-8 -*-
"""美联储加息对 A 股的影响 —— 历史加息日的前瞻收益对照台账。

问题：美联储 2026-09-16 加息 25bp 至 3.75-4.00%（本轮降息周期后的**首次反手加息**），
问 A 股后面怎么看。本脚本只做一件事：把历史上**每一次**美联储加息日之后，A 股主要指数
的 D+5/20/60/120 前瞻收益算出来，并与"所有普通交易日"的基准对照，回答
"加息这个变量本身，对 A 股是偏空还是无关"。

三个口径（都无未来函数）：
  ① 加息日 vs 普通日：同一条指数同一条前瞻窗口，加息日起算的样本 vs 全样本，
     看均值/中位/正收益比例差多少 —— 这才是"加息有没有独立影响"。
  ② 分周期：2004-2006 / 2015-2018 / 2022-2023 三个加息周期分开看。
  ③ 首次 vs 末次：周期首次加息（当下正处这种情形）与末次加息的差别。

数据：data/kline_full/*.csv（指数日收盘，2000 起；无第三方依赖）。
不加息日的样本若该指数数据未覆盖，按"首个交易日与加息日相差 >10 个自然日"剔除，
避免把 2020 年建仓的指数错配到 2015 年的加息日。

用法:
  python research/fed_hike_impact.py            # 上证指数 + 全指数汇总
  python research/fed_hike_impact.py --index 创业板指
"""
import argparse
import datetime
import os
import statistics
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from src.common.paths import DATA_ROOT  # noqa: E402

KLINE_DIR = os.path.join(DATA_ROOT, "kline_full")

HORIZONS = (5, 20, 60, 120)
COVER_GAP_DAYS = 10          # 数据起点晚于加息日超过此天数 → 该样本剔除
BASELINE_WARMUP = 150        # 基准样本跳过前 150 根，避开指数上市初期

# 美联储历次加息（target range 上调）。来源：federalreserve.gov/monetarypolicy/openmarket.htm
# 2026-09-16 为本轮（2024-09 起连续 6 次降息后）的首次反手加息，尚无前瞻数据。
HIKES = [
    # (加息生效日, 所属周期)
    ("2004-06-30", "2004-2006"), ("2004-08-10", "2004-2006"), ("2004-09-21", "2004-2006"),
    ("2004-11-10", "2004-2006"), ("2004-12-14", "2004-2006"), ("2005-02-02", "2004-2006"),
    ("2005-03-22", "2004-2006"), ("2005-05-03", "2004-2006"), ("2005-06-30", "2004-2006"),
    ("2005-08-09", "2004-2006"), ("2005-09-20", "2004-2006"), ("2005-11-01", "2004-2006"),
    ("2005-12-13", "2004-2006"), ("2006-01-31", "2004-2006"), ("2006-03-28", "2004-2006"),
    ("2006-05-10", "2004-2006"), ("2006-06-29", "2004-2006"),
    ("2015-12-17", "2015-2018"), ("2016-12-15", "2015-2018"), ("2017-03-16", "2015-2018"),
    ("2017-06-15", "2015-2018"), ("2017-12-14", "2015-2018"), ("2018-03-22", "2015-2018"),
    ("2018-06-14", "2015-2018"), ("2018-09-27", "2015-2018"), ("2018-12-20", "2015-2018"),
    ("2022-03-17", "2022-2023"), ("2022-05-05", "2022-2023"), ("2022-06-16", "2022-2023"),
    ("2022-07-28", "2022-2023"), ("2022-09-22", "2022-2023"), ("2022-11-03", "2022-2023"),
    ("2022-12-15", "2022-2023"), ("2023-02-02", "2022-2023"), ("2023-03-23", "2022-2023"),
    ("2023-05-04", "2022-2023"), ("2023-07-27", "2022-2023"),
]
CURRENT_HIKE = "2026-09-16"
CYCLE_ORDER = ["2004-2006", "2015-2018", "2022-2023"]


def _date(s):
    return datetime.date(*map(int, s.split("-")))


def load_index(name):
    """读 data/kline_full/<name>.csv → [(date_str, close)]，按日期升序。"""
    path = os.path.join(KLINE_DIR, name + ".csv")
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        header = f.readline()
        if "日期" not in header:
            f.seek(0)
        for line in f:
            p = line.strip().split(",")
            if len(p) < 2 or p[0] == "日期":
                continue
            try:
                rows.append((p[0], float(p[1])))
            except ValueError:
                continue
    rows.sort()
    return rows


def first_trading_day_after(rows, hike):
    """加息后 A 股首个交易日索引；数据未覆盖（晚了 >COVER_GAP_DAYS 天）返回 None。"""
    for i, (d, _) in enumerate(rows):
        if d > hike:
            return i if (_date(d) - _date(hike)).days <= COVER_GAP_DAYS else None
    return None


def fwd(rows, i, n):
    if i is None or i + n >= len(rows):
        return None
    return (rows[i + n][1] / rows[i][1] - 1) * 100


def summarize(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {
        "n": len(vals),
        "mean": round(statistics.mean(vals), 2),
        "median": round(statistics.median(vals), 2),
        "win": round(100 * sum(1 for v in vals if v > 0) / len(vals), 1),
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
    }


def hike_returns(rows, hikes):
    """按前瞻窗口返回 {n: [收益...]}。"""
    out = {n: [] for n in HORIZONS}
    for hike, _ in hikes:
        i = first_trading_day_after(rows, hike)
        for n in HORIZONS:
            out[n].append(fwd(rows, i, n))
    return out


def baseline_returns(rows):
    """全样本前瞻收益（跳过预热段），与加息日样本同期口径对照。"""
    out = {n: [] for n in HORIZONS}
    for i in range(BASELINE_WARMUP, len(rows)):
        for n in HORIZONS:
            if i + n < len(rows):
                out[n].append((rows[i + n][1] / rows[i][1] - 1) * 100)
    return out


def fmt(s):
    return "—" if s is None else f"N={s['n']:<3} 均值{s['mean']:+6.2f}% 中位{s['median']:+6.2f}% 正比{s['win']:>4.1f}%"


def report_index(name, rows):
    print(f"\n{'='*72}\n{name}（{rows[0][0]} ~ {rows[-1][0]}，{len(rows)} 根）\n{'='*72}")
    if name == "上证指数":
        print("\n[口径①] 加息日 vs 普通交易日（同指数同窗口）")
        hk = hike_returns(rows, HIKES)
        bs = baseline_returns(rows)
        for n in HORIZONS:
            print(f"  D+{n:<3} 加息日 {fmt(summarize(hk[n]))}")
            print(f"       普通日 {fmt(summarize(bs[n]))}")
        print("\n[口径②] 分周期（加息日起算）")
        for cyc in CYCLE_ORDER:
            sub = [(h, c) for h, c in HIKES if c == cyc]
            r = hike_returns(rows, sub)
            line = "  ".join(f"D+{n}:{fmt(summarize(r[n]))}" for n in HORIZONS)
            print(f"  {cyc}  {line}")
        print("\n[口径③] 周期首次 vs 末次加息")
        for cyc in CYCLE_ORDER:
            sub = [(h, c) for h, c in HIKES if c == cyc]
            for label, item in (("首次", sub[0]), ("末次", sub[-1])):
                i = first_trading_day_after(rows, item[0])
                vals = ", ".join(f"D+{n} {fwd(rows, i, n):+.2f}%" for n in HORIZONS
                                 if fwd(rows, i, n) is not None)
                print(f"  {cyc} {label} {item[0]}: {vals}")
    else:
        hk = hike_returns(rows, HIKES)
        for n in HORIZONS:
            print(f"  D+{n:<3} 加息日 {fmt(summarize(hk[n]))}")


def scan_all():
    """全指数加息后 20 日汇总（覆盖度不足的自动剔除）。"""
    print(f"\n{'='*72}\n全指数加息后 D+20 汇总（数据须覆盖加息日）\n{'='*72}")
    print(f"{'指数':<10}{'起点':<12}{'样本数':>6}   {'均值':>8}{'中位':>8}{'正比':>7}")
    names = sorted(f[:-4] for f in os.listdir(KLINE_DIR) if f.endswith(".csv"))
    for name in names:
        rows = load_index(name)
        r = hike_returns(rows, HIKES)[20]
        s = summarize(r)
        if s is None or s["n"] < 8:      # 覆盖不足的指数不给结论
            continue
        print(f"{name:<10}{rows[0][0]:<12}{s['n']:>6}   {s['mean']:>+7.2f}%{s['median']:>+7.2f}%{s['win']:>6.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", help="只算单条指数（默认上证指数 + 全指数汇总）")
    args = ap.parse_args()

    print("美联储历次加息日 → A 股前瞻收益台账")
    print(f"加息日样本 {len(HIKES)} 个 | 前瞻窗口 {HORIZONS} | 当前 {CURRENT_HIKE} 为降息周期后首次反手加息（无前瞻数据）")

    if args.index:
        report_index(args.index, load_index(args.index))
        return
    report_index("上证指数", load_index("上证指数"))
    scan_all()


if __name__ == "__main__":
    main()
