# -*- coding: utf-8 -*-
"""宽基深跌区监控（14:30 报告 F 区）—— 把"离半年线多远"变成每天一行的位置尺

来源：2026-09-17 验证知乎"三重背离"择时法时发现的**副产品**——真正有用的不是背离，
而是**位置**（离半年线 MA120 多远）。见 reports/三重背离验证_20260917.md。

口径（纯位置，**不叠任何背离**；背离经十万次迭代证明是负增量）：
    偏离度 = 收盘 / MA120(120日简单均线) − 1
    ≤ −15%  → 深跌观察区（可分批吸筹）
    ≤ −18%  → 极深区（历史更优）

证据（12 条境内宽基长历史 26.3 年，2026-09-17 独立行情级验算；独立行情 = 单指数 20 交易日
去重后，再按日期聚类成"市场行情轮"，避免同一次普跌被当成多条证据）：

    半年线≤−15%：独立行情 **36 轮**（约 1.4 次/年），真实收益中位
        5日 +1.41%（费后仅 −0.09）｜10日 +1.03%｜20日 +0.73%｜30日 **+1.84%**｜60日 +6.07%
        事件级胜率：5日 75%｜10日 72%｜20日 69%｜30日 69%｜60日 64%
    半年线≤−18%：独立行情 19 轮（约 0.7 次/年），30日 **+6.09%**、事件胜率 79%、
        60日 +9.55% —— **越深越好，30/60 日明显更优**
    对照：随便哪天买入（无条件）20 日中位仅 +0.29%

（注：这是**宽基自己**的口径；比 26 条指数混合口径弱——混合口径 30 日 +2.47%，
但那里面含行业指数，不适用宽基建议。引用务必用宽基口径。）

四条边界（同 reports 结论，务必遵守）：
  ① **只提示位置，不预测底**：深跌是"便宜了可以分批买"，不是"今天就是底"。
  ② **持有 30 日最好，5 日以内光赎回费（1.5%）就吃掉大半**（费后 −0.09%）。
  ③ **状态依赖**：结构熊市（如 2010~2015）失效，"一直买一直跌"。
  ④ 本区只提示机会，**不给金额、不定买卖**——仓位归 holding_advice 与用户计划。

本模块纯读 data/kline_full/*.csv，不联网、不落库。指标算法与 research/divergence_line.py 同源。
"""
import csv
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "src", "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from src.common.paths import DATA_ROOT  # noqa: E402
from src.common import technical_indicators as TI  # noqa: E402

MA_WIN = 120            # 半年线
DEEP = -15.0            # 深跌观察区门槛
EXTREME = -18.0         # 极深区
NEAR = -12.0            # 接近提示（该档历史只值 +1.57%，弱于 −15%）
MIN_BARS = 130          # MA120 预热 + 余量

# 境内宽基（境外/行业不列；恒生等按用户「只看境内」口径排除）
BROAD = [
    ("上证指数", "上证指数"), ("深成指", "深成指"), ("沪深300", "沪深300"),
    ("上证50", "上证50"), ("中证A500", "中证A500"), ("中证500", "中证500"),
    ("中证1000", "中证1000"), ("中证2000", "中证2000"), ("创业板指", "创业板指"),
    ("科创50", "科创50"), ("北证50", "北证50"), ("红利指数", "红利指数"),
]
KLINE_DIR = os.path.join(DATA_ROOT, "kline_full")


def _closes(name, need=MIN_BARS + 10):
    path = os.path.join(KLINE_DIR, name + ".csv")
    if not os.path.exists(path):
        return [], []
    ds, cs = [], []
    with open(path, encoding="utf-8-sig") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) < 2 or not row[0]:
                continue
            try:
                v = float(row[1])
            except ValueError:
                continue
            if v > 0:
                ds.append(row[0][:10])
                cs.append(v)
    return ds[-need:], cs[-need:]


def deepzone_monitor():
    """返回 {asof, level, items}。level ∈ 触发 / 接近 / 平静。
    items 按偏离度升序，每条含 name/date/close/ma120/dev/zone。"""
    items, asof = [], ""
    for name, label in BROAD:
        ds, cs = _closes(name)
        if len(cs) < MIN_BARS:
            continue
        ma = TI.sma(cs, MA_WIN)
        if ma[-1] is None:
            continue
        dev = (cs[-1] / ma[-1] - 1) * 100.0
        items.append({"name": label, "date": ds[-1], "close": cs[-1],
                      "ma120": ma[-1], "dev": dev,
                      "zone": ("极深" if dev <= EXTREME else
                               ("深跌" if dev <= DEEP else
                                ("接近" if dev <= NEAR else "正常")))})
        if ds[-1] > asof:
            asof = ds[-1]
    items.sort(key=lambda x: x["dev"])
    hit = [x for x in items if x["dev"] <= DEEP]
    near = [x for x in items if DEEP < x["dev"] <= NEAR]
    return {"asof": asof, "items": items, "hit": hit, "near": near,
            "level": "触发" if hit else ("接近" if near else "平静")}


def deepzone_lines(m, with_header=True):
    """渲染成报告行（平时 2~3 行，触发时展开表格）。"""
    L = []
    if with_header:
        L.append("\n## F区·宽基深跌区（半年线位置尺·只在到区间时才展开）\n")
    if not m.get("items"):
        L.append("- 宽基深跌监控不可用（kline_full 缺失）。")
        return L
    w = m["items"][0]
    if m["level"] == "触发":
        L.append("- **🟢 已到深跌观察区：%d 条宽基在半年线下方 %.0f%% 以上**" % (len(m["hit"]), -DEEP))
        L.append("")
        L.append("| 宽基 | 数据日 | 偏离半年线 | 档位 |")
        L.append("|---|---|---|---|")
        for x in m["hit"]:
            L.append("| %s | %s | %+.1f%% | %s |" % (
                x["name"], x["date"], x["dev"], "★极深" if x["zone"] == "极深" else "深跌"))
        L.append("")
        L.append("> **历史依据**（12 条境内宽基、独立行情级）：≤−15% 约 **1.4 次/年**（36 轮），"
                 "30 日真实收益中位 **+1.84%**、事件胜率 69%；≤−18% 更深更优（约 0.7 次/年、"
                 "30 日 +6.09%、胜率 79%）。")
        L.append("> **必须持有 30 日**：5 日以内光赎回费 1.5% 就吃掉大半（费后 −0.09%）。")
        L.append("> ⚠ 结构熊市（2010~2015 型）失效，可能一直买一直跌；本区只提示位置，"
                 "**不给金额、不定买卖**。")
    else:
        L.append("- **未到深跌区**。最接近 **%s %+.1f%%**（门槛 %+.0f%%，还差 %.1f 个百分点）。"
                 % (w["name"], w["dev"], DEEP, DEEP - w["dev"]))
        if m["near"]:
            L.append("- ⚠ 接近门槛：%s。该档历史只值 +1.57%%（30 日），弱于 −15%%，先挂着看。"
                     % "、".join("%s %+.1f%%" % (x["name"], x["dev"]) for x in m["near"]))
        L.append("- 尺子：偏离半年线 ≤%.0f%% 才进观察区（越深越好，30 日持有最佳）；"
                 "行业指数不在此列（本区只做宽基）。" % DEEP)
    return L


if __name__ == "__main__":
    m = deepzone_monitor()
    print("数据截至 %s  水平 %s  （%d 条宽基）" % (m["asof"], m["level"], len(m["items"])))
    for x in m["items"]:
        print("  %-9s %-11s %9.0f  MA120 %9.0f  %+7.1f%%  %s"
              % (x["name"], x["date"], x["close"], x["ma120"], x["dev"], x["zone"]))
    print()
    for line in deepzone_lines(m, with_header=False):
        print(line)
