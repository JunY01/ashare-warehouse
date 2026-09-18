# -*- coding: utf-8 -*-
"""场外基金净值缓存（含**分红再投资**的真实总收益口径）。

为什么需要它（2026-09-17 用户提问引出）：
用户问「这个债券还会分红，你算进收益了吗」。核查结果：
  · 我此前用的**累计净值(LJJZ)** 确实包含分红，但它是「分红当现金拿走」的口径
    （LJJZ = 单位净值 + 累计分红，**简单相加、不复利**）；
  · 分红**再投资**的真实总收益更高：007172 累计净值口径年化 3.36%，
    而官方日增长率复利后是 **3.71%**（差 0.35pp/年）；161603 差 1.21pp/年。
  · 且这些债基**分红很频繁**：007172 成立 6.8 年分了 **24 次**（约每季一次），
    除息日单位净值会掉 1% 左右——若只看单位净值会以为是亏损。

三种口径（务必分清）：
  DWJZ 单位净值      —— 不含分红，除息日会假跌（**最不能用来算收益**）
  LJJZ 累计净值      —— 含分红但简单相加（= 分红拿现金的持有体验）
  TR   总收益指数    —— 官方日增长率(JZZZL)复利（= 分红再投资，**最准**）

官方日增长率 JZZZL 已验证为**已做分红调整**：除息日 2020-01-09
  单位净值 1.0178→1.0062（-1.14%），JZZZL 却是 +0.04%。

产物：data/cache/otc_nav/{code}.json = {"code","name","nav":{date:LJJZ},"tr":{date:指数},"div":{date:每份分红}}
用法：
  python src/jobs_fetch/fetch_otc_nav_cache.py            # 全部
  python src/jobs_fetch/fetch_otc_nav_cache.py --code 007172
纯标准库。
"""
import argparse
import json
import re
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common.fetch_util import curl_json  # noqa: E402
from src.common.paths import DATA_ROOT  # noqa: E402

REFERER = "http://fundf10.eastmoney.com/"
OUT = os.path.join(DATA_ROOT, "cache", "otc_nav")

# 研究用到的场外基金（债券腿 / 黄金腿 / 长历史代理）
FUNDS = {
    "007171": "易方达中债3-5年国开行债A",
    "007172": "易方达中债3-5年国开行债C",
    "007079": "工银3-5年国开债指数C",
    "006452": "华富中证5年恒定久期国开债C",
    "003377": "广发中债7-10年国开债C",
    "161603": "融通债券A/B",
    "001001": "华夏债券A/B",
    "240003": "华宝宝康债券A",
    "000216": "华安黄金ETF联接A",
    "000217": "华安黄金ETF联接C",
    "002963": "易方达黄金ETF联接C",
}


def _f(v):
    try:
        x = float(v)
        return x
    except (TypeError, ValueError):
        return None


def fetch(code):
    """返回 (days, nav, tr, div)：按日期升序"""
    recs = {}
    page = 1
    while page <= 400:
        url = ("https://api.fund.eastmoney.com/f10/lsjz?fundCode=%s&pageIndex=%d&pageSize=20"
               % (code, page))
        d = curl_json(url, timeout=15, referer=REFERER) or {}
        items = ((d.get("Data") or {}).get("LSJZList")) or []
        if not items:
            break
        for x in items:
            recs[x["FSRQ"]] = x
        page += 1
        time.sleep(0.28)
    days = sorted(recs)
    nav, tr, div, unit = {}, {}, {}, {}
    idx = 1.0
    prev_lj = None
    for k in days:
        x = recs[k]
        lj = _f(x.get("LJJZ"))
        dw = _f(x.get("DWJZ"))
        jz = _f(x.get("JZZZL"))
        # 总收益指数：优先官方日增长率（已含分红调整），退化到累计净值比
        if jz is not None:
            idx *= (1 + jz / 100.0)
        elif lj and prev_lj:
            idx *= lj / prev_lj
        tr[k] = idx
        if lj:
            nav[k] = lj
        elif dw:
            nav[k] = dw
        if prev_lj is None and lj:
            tr[k] = 1.0                     # 首日归一到 1
        if lj:
            prev_lj = lj
        if dw is not None:
            unit[k] = dw                    # 单位净值（不含分红，除息日会下台阶）
        fs = (x.get("FHSP") or "").strip()
        if fs:
            div[k] = _per_share(fs)
    return days, nav, tr, div, unit


def _per_share(text):
    """从「每10份派现金0.1200元」解析出**每份**分红（元）；未知格式返回 0"""
    m = re.search(r"派现金\s*([0-9.]+)\s*元", text)
    if not m:
        return 0.0
    return float(m.group(1)) / 10.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", help="只跑某只")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    for code, name in FUNDS.items():
        if args.code and code != args.code:
            continue
        days, nav, tr, div, unit = fetch(code)
        if not days:
            print("%-8s %-24s 无数据" % (code, name))
            continue
        json.dump({"code": code, "name": name, "nav": nav, "tr": tr,
                   "unit": unit, "div": div},
                  open(os.path.join(OUT, code + ".json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        print("%-8s %-24s %s~%s n=%d 分红%2d次" % (
            code, name, days[0], days[-1], len(days), len(div)))


if __name__ == "__main__":
    main()
