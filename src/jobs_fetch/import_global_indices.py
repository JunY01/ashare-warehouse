# -*- coding: utf-8 -*-
"""Import all global indices from combined JSON file."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common import history_db

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'cache', 'investing')

def main():
    all_data = {}
    for fname in ('global_indices_kline.json', 'a_share_indices_kline.json', 'SOX_kline.json'):
        fpath = os.path.join(CACHE_DIR, fname)
        if not os.path.exists(fpath):
            continue
        with open(fpath, encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict) and 'candles' in d:
            # Single index format: {code, name, candles}
            all_data[d['code']] = d
        else:
            # Multiple indices format: {code: {name, candles}}
            for code, v in d.items():
                all_data[code] = v
    
    con = history_db.connect()
    total = 0
    for code, data in all_data.items():
        name = data['name']
        candles = data['candles']
        rows = []
        for c in candles:
            try:
                dt = c['t'][:10]
                o, h, l, cl = c['o'], c['h'], c['l'], c['c']
                if None in (o, h, l, cl) or 0 in (o, h, l, cl):
                    continue
                # 零振幅=节假日/周末占位行（如 SOX 2026-09-05：h=l=c、开盘价还落在 [l,h] 之外）
                if o == h == l == cl or h == l:
                    continue
                rows.append((code, dt, round(o, 4), round(h, 4), round(l, 4), round(cl, 4)))
            except (KeyError, TypeError, ValueError):
                continue
        con.executemany(
            "INSERT OR REPLACE INTO global_index_kline(code,date,open,high,low,close) VALUES(?,?,?,?,?,?)",
            rows)
        total += len(rows)
        print(f"  {code} {name}: {len(rows)} rows")
    con.commit()
    con.close()
    print(f"\nTotal: {total} rows imported")

if __name__ == "__main__":
    main()
