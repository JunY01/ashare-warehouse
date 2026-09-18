# -*- coding: utf-8 -*-
"""板块级估值（行业安全垫）——蛋卷行业指数 PE/PB 分位 + 东财板块 PE 快照

数据源:
  A. 蛋卷 index_eva：63 个指数含中证行业（煤炭/白酒/证券/军工/电子…），提供 PE/PB 历史分位，
     映射到东财板块大类（broad）作"行业安全垫"。庄家抄底用 pb_pct 低来确认便宜。
  B. 东财板块 clist f9(PE TTM)：496 板块当日 PE（PB 该接口为空），每日累积可自算分位。

输出:
  data/sector_valuation_map.json   - 蛋卷行业 → broad 映射估值（pb_pct/pe_pct/股息率）
  data/sector_valuation.json       - 蛋卷全部指数原始估值（参考）
  market_history.db sector_valuation - 东财板块当日 PE 累积（幂等，date+code 主键）

用法:
  python fetch_sector_valuation.py
纯标准库。
"""
import datetime
import json
import os
import sys
import time

try:
    from src.common.paths import DATA_ROOT, DB_PATH
except ImportError:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    DATA_ROOT = os.path.join(REPO_ROOT, "data")
    DB_PATH = os.path.join(DATA_ROOT, "market_history.db")
    for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "src", "common")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
try:
    from src.common import history_db
    from src.common.fetch_util import urllib_json, num
    from src.common.market_map import INDEX_BROAD
except ImportError:
    import history_db  # noqa: F401
    from fetch_util import urllib_json, num  # noqa: F401
    from market_map import INDEX_BROAD  # noqa: F401

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"}
DANJUAN_URL = "https://danjuanfunds.com/djapi/index_eva/dj"
SECTOR_VAL_MAP = os.path.join(DATA_ROOT, "sector_valuation_map.json")
SECTOR_VAL_RAW = os.path.join(DATA_ROOT, "sector_valuation.json")

# 蛋卷行业指数 → 东财板块大类映射（定义见 src/common/market_map.py，与 daily_1430.broad_of 口径对齐）


def get(url):
    return urllib_json(url, headers=HEADERS, timeout=15, tries=3, sleep_s=1)


def _num(v):
    """估值快照保留 3 位小数。"""
    n = num(v)
    return None if n is None else round(n, 3)


def fetch_danjuan():
    d = get(DANJUAN_URL)
    if not d or not d.get("data") or not d["data"].get("items"):
        return []
    items = []
    for it in d["data"]["items"]:
        items.append({
            "code": it.get("index_code", ""), "name": it.get("name", ""),
            "pe": _num(it.get("pe")), "pb": _num(it.get("pb")),
            "pe_pct": _num(it.get("pe_percentile")), "pb_pct": _num(it.get("pb_percentile")),
            "yeild": _num(it.get("yeild")),
        })
    return items


def danjuan_source_date():
    """数据源自带的估值日期（items 里逐条带 date="MM-DD"，如 "09-17"）。

    别用 today()：源只在收盘后更新，盘中跑会把上一交易日的估值标成今天
    （2026-09-18 实测：源 date=09-17 而 today()=09-18）。取不到唯一日期时返回 None。
    """
    d = get(DANJUAN_URL)
    ds = {it.get("date") for it in ((d or {}).get("data") or {}).get("items") or [] if it.get("date")}
    if len(ds) != 1:
        return None
    mm, dd = list(ds)[0].split("-")
    today = datetime.date.today()
    day = datetime.date(today.year, int(mm), int(dd))
    return (day if day <= today else datetime.date(today.year - 1, int(mm), int(dd))).isoformat()


def fetch_board_pe():
    """东财 496 板块当日 PE(TTM)。返回 {code: {name, pe}}。"""
    all_items = []
    pn = 1
    while True:
        url = ("http://push2delay.eastmoney.com/api/qt/clist/get?pn=%d&pz=200&po=1&np=1&fltt=2&invt=2"
               "&fid=f3&fs=m:90+t:2&fields=f2,f9,f12,f14&_=%d") % (pn, int(time.time() * 1000))
        d = get(url)
        if not d or not d.get("data") or not d["data"].get("diff"):
            break
        items = d["data"]["diff"]
        if isinstance(items, dict):
            items = [items]
        all_items.extend(items)
        if len(all_items) >= d["data"]["total"]:
            break
        pn += 1
        time.sleep(1)
    out = {}
    for it in all_items:
        pe = _num(it.get("f9"))
        if pe is not None:
            out[it.get("f12")] = {"name": it.get("f14", ""), "pe": pe}
    return out


def build_map(items):
    """蛋卷指数 → broad 映射估值（多指数同名取 pb_pct 最低者=最保守）。"""
    m = {}
    for it in items:
        for kw, broad in INDEX_BROAD.items():
            if kw in it["name"]:
                cur = m.get(broad)
                pb = it.get("pb_pct") or 1.0
                if cur is None or pb < (cur.get("pb_pct") or 1.0):
                    m[broad] = {**it, "broad": broad, "pb_pct": it.get("pb_pct")}
                break
    return m


def archive_board_pe(board):
    date = datetime.date.today().isoformat()
    conn = history_db.connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO sector_valuation(date, code, name, pe) VALUES(?,?,?,?)",
            [(date, c, v["name"], v["pe"]) for c, v in board.items()])
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM sector_valuation WHERE date=?", (date,)).fetchone()[0]
        return n
    finally:
        conn.close()


def main():
    print("== 拉取蛋卷行业估值 ==")
    items = fetch_danjuan()
    date = danjuan_source_date() or datetime.date.today().isoformat()
    print("  蛋卷指数: %d 个（估值日期 %s，取自数据源）" % (len(items), date))
    with open(SECTOR_VAL_RAW, "w", encoding="utf-8") as f:
        json.dump({"date": date, "items": items}, f,
                  ensure_ascii=False, indent=1)
    if items:
        m = build_map(items)
        with open(SECTOR_VAL_MAP, "w", encoding="utf-8") as f:
            json.dump({"date": date, "map": m},
                      f, ensure_ascii=False, indent=1)
        print("  行业安全垫映射: %d 个大类" % len(m))
        for b, v in sorted(m.items()):
            print("    %-6s pb分位%.2f pe分位%.2f 股息%.2f%%" % (
                b, v.get("pb_pct") or 0, v.get("pe_pct") or 0, (v.get("yeild") or 0) * 100))

    print("== 东财板块当日 PE 快照 ==")
    board = fetch_board_pe()
    print("  板块 PE 有效: %d/496" % len(board))
    if board:
        n = archive_board_pe(board)
        print("  sector_valuation 落库 %d 行" % n)


if __name__ == "__main__":
    main()