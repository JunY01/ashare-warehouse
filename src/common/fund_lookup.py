# -*- coding: utf-8 -*-
"""场外基金检索（daily_1430 与 daily_review 共用，消除两份复制实现）。

load_fund_list 原本在两脚本逐字重复；find_c_funds 则有两套口径：
  - 短线口径（daily_1430）：大类+原名多级回退、含生活方式代理，返回 top2；
  - 复盘口径（daily_review）：单键匹配、含 QDII、C类优先按代码降序，返回 cap 条。
两者行为不同、输出会进各自报告，故保留为两个具名函数，不做强行归一。

纯标准库。
"""
import json
import os

from .market_map import SECTOR_FUND_MAP, cfg_path


def load_fund_list():
    """加载全量基金列表 fundcode_all.js，返回 [(code, name, type)]。"""
    path = cfg_path("fundcode_all.js")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        txt = f.read()
    arr = json.loads(txt[txt.index("["):txt.rindex("]") + 1])
    return [(c, n, t) for c, _p, n, t, _f in arr]


def find_c_funds_broad(fund_list, sector_name, broad=""):
    """daily_1430 口径：大类+原名多级回退，场外C类优先，返回 top2。"""
    hay = "%s %s" % (broad, sector_name)
    key_sets = []
    for kw, (ks, bs) in SECTOR_FUND_MAP.items():
        if kw and kw in hay:
            key_sets.append((ks, bs))
    if not key_sets:
        short = sector_name.replace("Ⅲ", "").replace("Ⅱ", "").replace("类", "")
        for n in (short, short[:4], short[:2]):
            if n:
                key_sets.append(([n], []))
    # 无对应指数的生活方式型行业，用消费指数做可买代理（报告里照实显示名称）
    if any(k in hay for k in ("旅游", "景区", "酒店", "服务", "服装", "纺织", "包装")):
        key_sets.append((["消费"], ["增强"]))
    seen, hits = set(), []
    for keys, bans in key_sets:
        for code, name, ftype in fund_list:
            if code in seen:
                continue
            if code[:2] in ("51", "52", "56", "58", "15"):
                continue
            if "指数" not in ftype and "联接" not in name:
                continue
            if not any(k in name for k in keys):
                continue
            if any(b in name for b in bans):
                continue
            seen.add(code)
            hits.append((code, name))
    rank = lambda n: (0 if "联接C" in n or n.endswith("C") else
                      (1 if "联接" in n else 2))
    hits.sort(key=lambda x: (rank(x[1]), -int(x[0]) if x[0].isdigit() else 0))
    return hits[:2]


def find_c_funds_simple(fund_list, sector_name, cap=3):
    """daily_review 口径：单键匹配、含 QDII、C类优先按代码降序，返回 cap 条。"""
    matched_keys = None
    for kw, (keys, bans) in SECTOR_FUND_MAP.items():
        if kw in sector_name:
            matched_keys = (keys, bans)
            break
    if not matched_keys:
        matched_keys = ([sector_name], [])
    keys, bans = matched_keys

    hits = []
    for code, name, ftype in fund_list:
        if code[:2] in ("51", "52", "56", "58", "15"):
            continue
        if "指数" not in ftype and "联接" not in name and "QDII" not in ftype:
            continue
        if not any(k in name for k in keys):
            continue
        if any(b in name for b in bans):
            continue
        hits.append((code, name))

    c_funds = [(c, n) for c, n in hits if "C" in n or "联接C" in n]
    if not c_funds:
        c_funds = hits
    c_funds.sort(key=lambda x: x[0], reverse=True)
    return c_funds[:cap]
