# -*- coding: utf-8 -*-
"""抓取全球主要股指快照（东财全球指数接口），覆盖美股/欧股/亚太，供复盘对照外围联动。
注：push2 对 Python urllib 做 TLS 指纹拦截（RemoteDisconnected），故用系统 curl 请求。"""
import os
import sys
from datetime import datetime

# 统一寻址：无论从哪个目录运行都能定位仓库根，避免回退写错库
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common import history_db
from src.common.fetch_util import curl_json

# secid 前缀：100.* 全球指数、124.* 港股指数；顺序即输出顺序：美股/欧洲/亚太
SECIDS = [
    ("100.DJIA", "道琼斯"),
    ("100.SPX", "标普500"),
    ("100.NDX", "纳斯达克"),
    ("251.SOX", "费城半导体"),
    ("100.FTSE", "英国富时100"),
    ("100.GDAXI", "德国DAX30"),
    ("124.HSTECH", "恒生科技"),
    ("100.HSI", "恒生指数"),
    ("100.N225", "日经225"),
    ("100.KS11", "韩国KOSPI"),
    ("100.TWII", "台湾加权"),
]


def fetch_json(url):
    return curl_json(url, timeout=12)


def main():
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?ut=fa5fd1943c7b386f172d6893dbfba10b"
           "&fltt=2&invt=2&secids=%s&fields=f2,f3,f12,f14" % ",".join(sec for sec, _ in SECIDS))
    d = fetch_json(url)
    if not d or not d.get("data") or not d["data"].get("diff"):
        print("接口无返回，退出")
        return

    rows = []
    for it in d["data"]["diff"]:
        try:
            # fltt=2 时返回值已是实际浮点数；占位符 "-" 跳过
            rows.append([it["f12"], it["f14"], round(float(it["f2"]), 2), round(float(it["f3"]), 2)])
        except (KeyError, TypeError, ValueError):
            continue

    for _, name, price, pct in rows:
        print(f"{name:<12} {price:>12.2f}  {pct:+6.2f}%")

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 快照归档到历史库（同日重跑幂等覆盖），供外围联动的历史对照分析
    day = stamp.split(" ")[0]
    from src.common import history_db
    con = history_db.connect()
    con.executemany("INSERT OR REPLACE INTO global_daily VALUES(?,?,?,?,?)",
                    [(day,) + tuple(r) for r in rows])
    con.commit()
    con.close()
    print(f"\n共 {len(rows)} 条 -> global_daily({day})")


if __name__ == "__main__":
    main()
