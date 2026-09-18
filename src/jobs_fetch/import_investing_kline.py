# -*- coding: utf-8 -*-
"""从英为财情(investing.com)抓取的JSON导入全球指数日K历史到 global_index_kline。

数据获取：浏览器自动化抓取 endpoints.investing.com 接口，存为 JSON。
本脚本只负责清洗 + 落库，不直接联网。

JSON 文件格式（每个指数一个文件，放在 data/cache/investing/ 下）：
  {
    "code": "SOX",
    "name": "费城半导体指数",
    "candles": [
      {"t": "2026-09-08T00:00:00Z", "o": 11987.288, "h": 12023.863, "l": 11843.265, "c": 11887.869, "v": 0},
      ...
    ]
  }

清洗规则：
  - 剔除 o=h=l=c 的脏数据（周末/节假日重复行）
  - 剔除 OHLC 任一为 None/0 的无效行
  - 剔除未来日期
  - 日期统一为 YYYY-MM-DD

用法：
  python src/jobs_fetch/import_investing_kline.py            # 导入 data/cache/investing/ 下全部 JSON
  python src/jobs_fetch/import_investing_kline.py SOX        # 只导入指定 code
  python src/jobs_fetch/import_investing_kline.py --verify   # 只验证不落库
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common.paths import DATA_ROOT, DB_PATH
from src.common import history_db

CACHE_DIR = os.path.join(DATA_ROOT, "cache", "investing")
TODAY = datetime.date.today().isoformat()


def clean_candles(code, name, candles):
    """清洗 candles，返回 (date, o, h, l, c) 列表。"""
    rows = []
    seen = set()
    for c in candles:
        try:
            # 解析日期（取 YYYY-MM-DD）
            dt = c["t"][:10]
            o, h, l, cl = c.get("o"), c.get("h"), c.get("l"), c.get("c")
            # 无效值
            if None in (o, h, l, cl) or 0 in (o, h, l, cl):
                continue
            # 脏数据：o=h=l=c（周末/节假日重复行）
            if o == h == l == cl:
                continue
            # 未来日期
            if dt > TODAY:
                continue
            # 去重
            if dt in seen:
                continue
            seen.add(dt)
            rows.append((code, dt, round(o, 4), round(h, 4), round(l, 4), round(cl, 4)))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r[1])
    return rows


def import_file(filepath, dry_run=False):
    """导入单个 JSON 文件，返回 (code, name, n_raw, n_clean)。"""
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)
    code = data["code"]
    name = data.get("name", code)
    candles = data.get("candles", [])
    rows = clean_candles(code, name, candles)
    if not rows:
        return code, name, len(candles), 0
    if dry_run:
        return code, name, len(candles), len(rows)
    con = history_db.connect()
    try:
        con.executemany(
            "INSERT OR REPLACE INTO global_index_kline(code,date,open,high,low,close) VALUES(?,?,?,?,?,?)",
            rows)
        con.commit()
    finally:
        con.close()
    return code, name, len(candles), len(rows)


def main():
    dry_run = "--verify" in sys.argv
    target_code = None
    for arg in sys.argv[1:]:
        if arg != "--verify":
            target_code = arg.upper()
            break

    if not os.path.isdir(CACHE_DIR):
        print(f"目录不存在: {CACHE_DIR}")
        return

    files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".json")]
    if target_code:
        files = [f for f in files if f.upper().startswith(target_code)]
    if not files:
        print(f"无匹配 JSON 文件 (code={target_code})")
        return

    total_raw = 0
    total_clean = 0
    for fn in sorted(files):
        fp = os.path.join(CACHE_DIR, fn)
        code, name, n_raw, n_clean = import_file(fp, dry_run=dry_run)
        total_raw += n_raw
        total_clean += n_clean
        tag = "[验证] " if dry_run else ""
        print(f"  {tag}{code} {name}: 原始 {n_raw} → 清洗后 {n_clean}")
    mode = "验证" if dry_run else "导入"
    print(f"\n{mode}完成: 原始 {total_raw} → 清洗后 {total_clean}")


if __name__ == "__main__":
    main()
