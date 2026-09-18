# -*- coding: utf-8 -*-
"""观察池个股行情/基本面抓取（股票池配置见 watchlist.json，源自产业链种子数据）

数据源:
  - 腾讯 qt.gtimg.cn 批量实时行情（全池，80只/批，GBK 编码，免费无 key）
  - 东财 api/qt/stock/get 个股基本面（fundamental_codes 龙头子集；
    push2delay 直连优先，被 TLS 指纹拦截时回退系统 curl + em_cookie.txt）

落库 market_history.db（同日重跑幂等覆盖）：
  stock_quote_daily(date, code, name, price, chg_pct, amount_wan, turnover,
                    pe, pb, float_mv_yi, total_mv_yi, volume_ratio)
  stock_fundamental_daily(date, code, name, pe, pb, industry,
                          total_mv_yi, float_mv_yi)
另写覆盖式快照 stock_quote.csv。

用法:
  python fetch_stock_watch.py               # 行情 + 基本面
  python fetch_stock_watch.py --quote-only  # 仅腾讯行情
"""
import csv
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.request

# 统一寻址：无论从哪个目录运行都能定位仓库根（消除回退写错目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common import history_db
from src.common.paths import DATA_ROOT, CONFIG_ROOT
from src.common.fetch_util import num as _num

DATA_DIR = DATA_ROOT
WATCHLIST = os.path.join(CONFIG_ROOT, "watchlist.json")
QUOTE_CSV = os.path.join(DATA_ROOT, "stock_quote.csv")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"
QQ_HEADERS = {"User-Agent": UA, "Referer": "https://stockapp.finance.qq.com/"}
EM_HEADERS = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}

# 腾讯代码前缀：按代码首位映射市场（6/5/9沪、0/2/3/1深、4/8北）
_PREFIX = {"6": "sh", "5": "sh", "9": "sh", "0": "sz", "2": "sz", "3": "sz",
           "1": "sz", "4": "bj", "8": "bj"}


def _load_cookie():
    ck = os.environ.get("EM_COOKIE", "")
    if not ck:
        path = os.path.join(CONFIG_ROOT, "em_cookie.txt")
        if not os.path.exists(path):
            path = os.path.join(DATA_DIR, "em_cookie.txt")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                ck = f.read().strip()
    return ck


# ---------- 腾讯批量行情 ----------
def fetch_quotes(codes):
    """腾讯 v_xx 接口，字段下标为 split('~') 后的 0 基位置：
    [1]名称 [3]现价 [32]涨跌幅% [37]成交额(万) [38]换手% [39]PE
    [44]流通市值(亿) [45]总市值(亿) [46]PB [49]量比"""
    out = {}
    for i in range(0, len(codes), 80):
        batch = codes[i:i + 80]
        url = "https://qt.gtimg.cn/q=" + ",".join(_PREFIX.get(c[0], "sz") + c for c in batch)
        try:
            req = urllib.request.Request(url, headers=QQ_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as r:
                text = r.read().decode("gbk", "replace")
        except Exception as e:
            print("  腾讯批次请求失败: %s" % e)
            continue
        for line in text.strip().split(";"):
            line = line.strip()
            if '="' not in line or not line.startswith("v_"):
                continue
            key, content = line.split('="', 1)
            code = key[4:]  # v_sh600519 -> 600519
            f = content.split('"')[0].split("~")
            if len(f) < 50:
                continue
            out[code] = {"name": f[1], "price": _num(f[3]), "chg_pct": _num(f[32]),
                         "amount_wan": _num(f[37]), "turnover": _num(f[38]),
                         "pe": _num(f[39]), "float_mv_yi": _num(f[44]),
                         "total_mv_yi": _num(f[45]), "pb": _num(f[46]),
                         "volume_ratio": _num(f[49])}
        time.sleep(1)
    return out


def save_quotes(date, quotes):
    conn = history_db.connect()
    try:
        rows = [(date, c, q["name"], q["price"], q["chg_pct"], q["amount_wan"],
                 q["turnover"], q["pe"], q["pb"], q["float_mv_yi"],
                 q["total_mv_yi"], q["volume_ratio"])
                for c, q in quotes.items() if q["name"]]
        conn.executemany("INSERT OR REPLACE INTO stock_quote_daily VALUES(%s)"
                         % ",".join("?" * 12), rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def write_csv(quotes):
    rows = sorted(quotes.values(), key=lambda q: -(q["chg_pct"] or -999))
    with open(QUOTE_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["代码", "名称", "现价", "当日%", "成交额(万)", "换手%", "PE", "PB",
                    "流通市值(亿)", "总市值(亿)", "量比"])
        for q in rows:
            if not q["name"]:
                continue
            w.writerow([q["code"], q["name"], q["price"], q["chg_pct"], q["amount_wan"],
                        q["turnover"], q["pe"], q["pb"], q["float_mv_yi"],
                        q["total_mv_yi"], q["volume_ratio"]])


# ---------- 东财个股基本面 ----------
def _fetch_em_json(url):
    """urllib 直连失败（TLS 指纹拦截）时回退 curl 子进程 + cookie"""
    headers = dict(EM_HEADERS)
    ck = _load_cookie()
    if ck:
        headers["Cookie"] = ck
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        pass
    cmd = ["curl", "-s", "-m", "12", "-A", UA,
           "-H", "Referer: https://quote.eastmoney.com/"]
    if ck:
        cmd += ["-H", "Cookie: " + ck]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def fetch_fundamental(code):
    """东财 stock/get 现役字段（2026-08 实测）：f58名称 f127行业
    f162市盈率(动)与clist f9同值、f167市净率与clist f23同值；ROE/毛利率等已不再下发"""
    secid = ("1." if code.startswith("6") else "0.") + code
    url = ("https://push2delay.eastmoney.com/api/qt/stock/get?secid=%s"
           "&fields=f57,f58,f116,f117,f127,f162,f167&fltt=2&invt=2") % secid
    data = (_fetch_em_json(url) or {}).get("data") or {}
    if not data:
        return None

    def g(k):
        return _num(data.get(k))

    tot, flt = g("f116"), g("f117")
    return {"name": data.get("f58") or "", "pe": g("f162"), "pb": g("f167"),
            "industry": data.get("f127") if isinstance(data.get("f127"), str) else "",
            "total_mv_yi": round(tot / 1e8, 2) if tot else None,
            "float_mv_yi": round(flt / 1e8, 2) if flt else None}


def update_fundamentals(date, codes):
    ok = 0
    conn = history_db.connect()
    try:
        for code in codes:
            if code[0] in "48":
                print("  跳过北交所 %s（暂不支持基本面接口）" % code)
                continue
            d = fetch_fundamental(code)
            time.sleep(2)  # 防限流
            if not d or not d["name"]:
                print("  %s 基本面无返回" % code)
                continue
            conn.execute("INSERT OR REPLACE INTO stock_fundamental_daily "
                         "VALUES(?,?,?,?,?,?,?,?)",
                         (date, code, d["name"], d["pe"], d["pb"], d["industry"],
                          d["total_mv_yi"], d["float_mv_yi"]))
            conn.commit()
            ok += 1
    finally:
        conn.close()
    print("  基本面落库 %d/%d 只" % (ok, len(codes)))


def main():
    with open(WATCHLIST, encoding="utf-8") as f:
        wl = json.load(f)
    codes = [s["code"] for s in wl.get("stocks", [])]
    date = datetime.date.today().isoformat()
    print("== 更新个股观察池（%d只） ==" % len(codes))

    quotes = fetch_quotes(codes)
    for c, q in quotes.items():
        q["code"] = c
    if quotes:
        n = save_quotes(date, quotes)
        write_csv(quotes)
        ups = sum(1 for q in quotes.values() if (q["chg_pct"] or 0) > 0)
        downs = sum(1 for q in quotes.values() if (q["chg_pct"] or 0) < 0)
        top5 = sorted(quotes.values(), key=lambda q: -(q["chg_pct"] or -999))[:5]
        print("  行情落库 %d/%d 只（涨%d/跌%d），涨幅前列: %s"
              % (n, len(codes), ups, downs,
                 " ".join("%s%+.2f%%" % (q["name"], q["chg_pct"]) for q in top5)))
    else:
        print("  腾讯行情无返回")

    if "--quote-only" not in sys.argv:
        fund_codes = wl.get("fundamental_codes", [])
        print("== 更新龙头基本面（%d只） ==" % len(fund_codes))
        update_fundamentals(date, fund_codes)


if __name__ == "__main__":
    main()
