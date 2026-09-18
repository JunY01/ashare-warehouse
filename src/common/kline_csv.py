# -*- coding: utf-8 -*-
"""sector_300d.csv 唯一写入者。

历史上 browser_fetch_klines.py 与 update_kline_chunk.py 各自写同一文件且表头不同
（末两列一为"最新收盘/最新日期"、一为"数据截止/领涨股"），下游读到哪套取决于写入者。
统一在此按固定表头产出，两侧只调用不重定义。

纯标准库。closes 统一为 [(date, close), ...] 升序。
"""
import csv
import os

from .paths import DATA_ROOT

SECTOR_300D = os.path.join(DATA_ROOT, "sector_300d.csv")
HEADER = ["代码", "名称", "5日%", "20日%", "60日%", "120日%", "250日%", "300日%",
          "数据截止", "领涨股"]


def calc_chg(closes, days):
    """最后一天相对 days 个交易日前(不含当日)的涨幅%。"""
    if len(closes) <= days:
        return None
    return round((closes[-1][1] / closes[-(days + 1)][1] - 1) * 100, 2)


def write_sector_300d(klines_map, names=None, leads=None, path=None):
    """按统一表头写 sector_300d.csv，按 300 日涨幅降序。

    names 给出板块全集时会为缺 K 线的板块保留空行（板块数稳定）；缺省则只写有数据的板块。
    """
    names = names or {}
    leads = leads or {}
    path = path or SECTOR_300D
    codes = list(names) if names else list(klines_map)
    rows = []
    for code in codes:
        closes = klines_map.get(code, [])
        if not closes:
            rows.append([code, names.get(code, "")] + [""] * 6 + ["", leads.get(code, "")])
            continue
        rows.append([
            code, names.get(code, ""),
            calc_chg(closes, 5), calc_chg(closes, 20), calc_chg(closes, 60),
            calc_chg(closes, 120), calc_chg(closes, 250), calc_chg(closes, 300),
            closes[-1][0], leads.get(code, ""),
        ])
    rows.sort(key=lambda x: -(x[7] if isinstance(x[7], (int, float)) else -999))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    return len(rows)
