# -*- coding: utf-8 -*-
"""东财估值接口历史 PE 拉取与落盘验证（RPT_VALUEMARKET）

给未来亚亚：国证站（cnindex）与深交所官网都没有指数级历史 PE 可补——
国证 /queryIndustryPEs 是行业口径（要 plateCode+category+industry），
详情页 /indexAnalysis/getIndexAnalysis 是债券指标，深交所 1747_zs 只是成分名单，
中证官网 index-perf 不收深市代码（399001/399006 返回空）。
2026-09-10 改从东财数据中心挖出可用接口：

  GET https://datacenter-web.eastmoney.com/api/data/v1/get
      ?reportName=RPT_VALUEMARKET&columns=TRADE_DATE,CLOSE_PRICE,PE_TTM_AVG
      &sortColumns=TRADE_DATE&sortTypes=1&filter=(TRADE_MARKET_CODE="399006")
  （Referer 必须带 https://data.eastmoney.com/gzfx/；分页 pageNumber/pageSize）

覆盖（日频）：
  399006 创业板指 / 399001 深成指 / 000300 沪深300 / 000001 上证指数：2017-01-03 起
  000688 科创50：2019-07-22 起（开板日）
  000905/000016/000852/000015/399986 等查无此码，不可补。

口径说明（已验证）：东财 PE_TTM_AVG 与蛋卷重叠 497 期——
创业板 +6%、深成 -19%、科创约 -10%，趋势一致可做交叉验证；
但 000300（沪深300）系统性偏高约 +68%（疑似算术平均口径），该码结论弃用。
分位一律按"各自窗口内位置"算，不跨源拼数值。

产物：data/cache/pe_history/em_valuation.json
用法：
  python fetch_em_valuation.py          # 全量抓取（联网，约2分钟）+ 落盘验证
  python fetch_em_valuation.py --check  # 只验证本地缓存（不联网）

纯标准库。
"""
import datetime
import json
import os
import statistics
import sys
import time
import urllib.parse

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

CACHE = os.path.join(DATA_ROOT, "cache", "pe_history", "em_valuation.json")
DJ_CACHE = os.path.join(DATA_ROOT, "cache", "pe_history", "pe_history_all.json")
INDEX_VAL = os.path.join(DATA_ROOT, "index_valuation.json")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0",
           "Referer": "https://data.eastmoney.com/gzfx/"}
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"

CODES = {
    "399006": "创业板指", "399001": "深成指", "000300": "沪深300",
    "000001": "上证指数", "000688": "科创50",
}
# 蛋卷对照码（上证指数蛋卷未收录，无对照）
DJ_MAP = {"399006": "SZ399006", "399001": "SZ399001",
          "000300": "SH000300", "000688": "SH000688"}
BJ = datetime.timezone(datetime.timedelta(hours=8))
# 与本地快照允许的最大偏离（隔 1 交易日，5% 内算对上）
CLOSE_TOL = 0.05


def fetch_all(code):
    """拉取单码全历史（TRADE_DATE 升序分页），返回 [(date, close, pe)]。"""
    rows = []
    page = 1
    pages = 1
    while page <= pages:
        params = {"reportName": "RPT_VALUEMARKET",
                  "columns": "TRADE_DATE,CLOSE_PRICE,PE_TTM_AVG",
                  "quoteColumns": "", "pageNumber": str(page), "pageSize": "500",
                  "sortColumns": "TRADE_DATE", "sortTypes": "1",
                  "filter": '(TRADE_MARKET_CODE="%s")' % code}
        d = urllib_json(BASE + "?" + urllib.parse.urlencode(params),
                        headers=HEADERS, timeout=30, tries=2)
        r = (d.get("result") if d else None) or {}
        pages = r.get("pages") or 1
        for x in r.get("data") or []:
            pe = x.get("PE_TTM_AVG") or 0
            if x.get("TRADE_DATE") and pe > 0:  # PE<=0 为源头脏行，直接丢弃
                rows.append((x["TRADE_DATE"][:10], x["CLOSE_PRICE"], pe))
        page += 1
        time.sleep(0.6)
    seen = {}
    for dt, c, p in rows:
        seen[dt] = (c, p)
    return sorted(seen.items())


def percentile(series, cur):
    return sum(1 for v in series if v <= cur) / len(series) * 100 if series else None


def validate(data):
    """落盘验证：完整性 + 收盘交叉 + 口径对照 + 分位。返回 (ok, 报告行)。"""
    lines = []
    ok = True
    # 1. 完整性：起止、缺省、日期单调
    for code, name in CODES.items():
        rows = (data.get(code) or [])
        dates = [x["date"] for x in rows]
        mono = all(a < b for a, b in zip(dates, dates[1:]))
        miss = sum(1 for x in rows if not x.get("pe"))
        flag = "OK" if rows and mono and miss == 0 else "FAIL"
        if flag == "FAIL":
            ok = False
        lines.append("[完整性] %s %s n=%d %s~%s 缺省PE=%d 单调=%s %s" % (
            code, name, len(rows),
            dates[0] if dates else "?", dates[-1] if dates else "?",
            miss, mono, flag))
    # 2. 收盘交叉：EM 末日收盘 vs 本地快照（隔1交易日，容差5%）
    try:
        with open(INDEX_VAL, encoding="utf-8") as f:
            val = json.load(f)
        today = {r["name"]: r.get("price") for r in val.get("rows", [])}
        for code, name in CODES.items():
            rows = data.get(code) or []
            lp = today.get(name)
            if not rows or not lp:
                continue
            dev = abs(rows[-1]["close"] - lp) / lp
            flag = "OK" if dev <= CLOSE_TOL else "FAIL"
            if flag == "FAIL":
                ok = False
            lines.append("[交叉] %s EM收盘%.2f 本地%.2f 偏离%.2f%% %s" % (
                name, rows[-1]["close"], lp, dev * 100, flag))
    except FileNotFoundError:
        lines.append("[交叉] index_valuation.json 缺席，跳过")
    # 3. 口径对照 + 各自窗口分位
    try:
        with open(DJ_CACHE, encoding="utf-8") as f:
            dj = json.load(f)
        for code, dk in DJ_MAP.items():
            if code == "000300":
                lines.append("[口径] 沪深300 东财口径系统性偏高约+68%%，结论弃用该码")
                continue
            es = {x["date"]: x["pe"] for x in data.get(code, [])}
            ds = {datetime.datetime.fromtimestamp(x["ts"] / 1000, BJ).strftime("%Y-%m-%d"): x["pe"]
                  for x in dj[dk]["series"]}
            common = sorted(set(es) & set(ds))
            if not common:
                continue
            rel = statistics.median([(es[d] - ds[d]) / ds[d] * 100 for d in common])
            epe = [es[d] for d in sorted(es)]
            lines.append("[口径] %s 重叠%d期 偏差中位%+.1f%% EM分位%.0f%%(n=%d,当前%.1f)" % (
                CODES[code], len(common), rel,
                percentile(epe, epe[-1]), len(epe), epe[-1]))
    except FileNotFoundError:
        lines.append("[口径] pe_history_all.json 缺席，跳过")
    return ok, lines


def main():
    check = "--check" in sys.argv
    if check:
        with open(CACHE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
        for code, name in CODES.items():
            pts = fetch_all(code)
            data[code] = [{"date": dt, "close": c, "pe": p} for dt, (c, p) in pts]
            print("%-8s n=%-4d %s~%s PE[%.2f~%.2f]" % (
                name, len(pts), pts[0][0], pts[-1][0],
                min(p for _, (_, p) in pts), max(p for _, (_, p) in pts)) if pts
                else "%-8s 无数据" % name)
            time.sleep(0.8)
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print("\n已落库 %s" % CACHE)
    print("\n—— 落盘验证 ——")
    ok, lines = validate(data)
    for ln in lines:
        print(ln)
    print("验证结论：%s" % ("通过" if ok else "未通过"))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
