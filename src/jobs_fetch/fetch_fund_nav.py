# -*- coding: utf-8 -*-
"""抓取场外基金历史净值（天天基金 f10/lsjz 接口），归档到 fund_nav_daily 表，供持仓复盘与看板使用。
注：eastmoney 对 Python urllib 做 TLS 指纹拦截（RemoteDisconnected），故用系统 curl 请求；lsjz 必须带 Referer。"""
import os
import sys
import time

# 自引导 REPO_ROOT，保证 subprocess 直接跑脚本也能 import src.common
try:
    from src.common.paths import REPO_ROOT  # noqa: F401
except ImportError:
    _REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _REPO not in sys.path:
        sys.path.insert(0, _REPO)

from src.common.fetch_util import curl_json  # noqa: E402

REFERER = "http://fundf10.eastmoney.com/"

# 示例：改成你要跟踪的场外基金 (代码, 名称)，净值落库到 fund_nav_daily
FUNDS = [
    ("000000", "某某宽基联接C"),
    ("000001", "某某红利联接C"),
]


def fetch_json(url):
    return curl_json(url, timeout=12, referer=REFERER)


def main():
    from src.common import history_db
    con = history_db.connect()
    total = 0
    for code, name in FUNDS:
        d = fetch_json("https://api.fund.eastmoney.com/f10/lsjz?fundCode=%s&pageIndex=1&pageSize=15" % code)
        items = (d or {}).get("Data", {}).get("LSJZList") or []
        rows = []
        for it in items:
            try:
                # 净值未公布时字段为空串，跳过该日
                rows.append((it["FSRQ"], code, name,
                             float(it["DWJZ"]), float(it["LJJZ"]), float(it["JZZZL"])))
            except (KeyError, ValueError):
                continue
        if rows:
            con.executemany("INSERT OR REPLACE INTO fund_nav_daily VALUES(?,?,?,?,?,?)", rows)
            latest = rows[0]  # 接口按日期倒序返回
            print(f"{name:<16} 最新 {latest[0]}  净值 {latest[3]:<8.4f} {latest[5]:+.2f}%")
            total += len(rows)
        else:
            print(f"{name:<16} 无数据返回")
        time.sleep(1)
    con.commit()
    con.close()
    print(f"\n共 {total} 条 -> fund_nav_daily")


if __name__ == "__main__":
    main()
