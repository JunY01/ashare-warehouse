# -*- coding: utf-8 -*-
"""中证官网历史 PE 补全（2011-2016，蛋卷 10 年窗口之前的数据）

给未来亚亚：蛋卷 pe_history 的 day=all 上限就是 10 年周频（516期，2016-09起）。
2026-09-10 用户指出中证官网（csindex.com.cn）无需登录，我挖出它的公开接口：

  GET https://www.csindex.com.cn/csindex-home/perf/index-perf
      ?indexCode=000300&startDate=20110101&endDate=20161231
  返回 data[].peg 即为 PE（已验证：与蛋卷重叠期偏差中位+4%，同一量级；
  peg 随价格逐日波动，符合 PE 特征）。peg 从 2011-06-28 起有值，此前为 null。

覆盖：
  可补（7个）：沪深300/中证500/上证50/中证1000/中证银行/红利指数/上证指数
  不可补：创业板指/深成指（深交所指数，中证官网无；国证站JS渲染挖不动）、
          恒生指数/恒生科技（港股，非中证口径）、科创50（2019才成立）

口径说明：官网 PE（疑似静态TTM）vs 蛋卷（滚动），重叠期偏差中位 +4%，
合并使用时以"各自窗口内分位"为准，不直接拼数值比大小。落库时打来源标签。

产物：data/cache/pe_history/csi_pe_daily.json
用法:
  python fetch_csi_pe.py              # 抓取 2011-2016（联网，约2分钟）
  python fetch_csi_pe.py --check      # 只校验覆盖情况

纯标准库。
"""
import json
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from src.common.paths import DATA_ROOT  # noqa: E402
from src.common.fetch_util import urllib_json  # noqa: E402

CACHE = os.path.join(DATA_ROOT, "cache", "pe_history", "csi_pe_daily.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0",
           "Referer": "https://www.csindex.com.cn/"}
BASE = "https://www.csindex.com.cn/csindex-home/perf/index-perf"

CODES = {
    "000300": "沪深300", "000905": "中证500", "000016": "上证50",
    "000852": "中证1000", "399986": "中证银行", "000015": "红利指数",
    "000001": "上证指数",
}


def fetch_range(code, start, end):
    url = "%s?indexCode=%s&startDate=%s&endDate=%s" % (BASE, code, start, end)
    d = urllib_json(url, headers=HEADERS, timeout=20, tries=2)
    rows = (d.get("data") if d else None) or []
    return [(r["tradeDate"], r["peg"]) for r in rows if r.get("peg")]


def main():
    check = "--check" in sys.argv
    out = {}
    for code, name in CODES.items():
        # 分年抓（单次区间太大可能被截断），只取 2011-2016（蛋卷已有之后的不重复）
        all_pts = []
        for y in range(2011, 2017):
            pts = fetch_range(code, "%d0101" % y, "%d1231" % y)
            all_pts.extend(pts)
            time.sleep(0.8)
        # 去重排序
        seen = {}
        for dt, v in all_pts:
            seen[dt] = v
        pts = sorted(seen.items())
        out[code] = {"name": name, "series": [{"date": dt, "pe": v} for dt, v in pts]}
        if pts:
            print("%-8s n=%-4d %s~%s PE[%.2f~%.2f]" % (
                name, len(pts), pts[0][0], pts[-1][0],
                min(v for _, v in pts), max(v for _, v in pts)))
        else:
            print("%-8s 无数据" % name)
    if not check:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        print("\n已落库 %s" % CACHE)


if __name__ == "__main__":
    main()
