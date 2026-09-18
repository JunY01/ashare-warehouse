# -*- coding: utf-8 -*-
"""回填板块每日主力资金流历史（东财 fflow daykline 接口，约100个交易日）

用法:
  python backfill_flow.py           # 回填最近100个交易日
  python backfill_flow.py 30        # 只回填最近30天
说明:
  - push2his 对 Python urllib 做 TLS 指纹拦截，故走 curl
  - 盘中运行会包含当日未收盘的临时值，收盘后重跑自动覆盖（INSERT OR REPLACE 幂等）
  - 接口每次返回整段历史，无需每日跑；每周跑一次或需要时手动补即可
"""
import json
import os
import subprocess
import sys
import time

# 统一寻址：cookie 与历史库统一走 config/ 与 data/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common import history_db
from src.common.paths import CONFIG_ROOT
from src.common.fetch_util import UA, wan as _wan

URL = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?secid=%s"
       "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57&lmt=%d")
# 浏览器会话cookie（见 config/em_cookie.txt），无它则高频请求会被 rc=102 频控
_COOKIE_FILE = os.path.join(CONFIG_ROOT, "em_cookie.txt")
COOKIE = open(_COOKIE_FILE).read().strip() if os.path.exists(_COOKIE_FILE) else ""


def fetch_flow(code, days, tries=3):
    """单板块抓取；限流时接口返回空klines，故重试并递增等待

    code 为裸板块代码（BKxxxx）；接口 secid 需带市场前缀 90.（缺前缀返回 rc=102）。
    落库行的 code 列仍用裸代码，故此处只给 URL 加前缀。
    """
    secid = code if "." in code else "90." + code
    for attempt in range(tries):
        cmd = ["curl", "-s", "-m", "12", "-A", UA]
        if COOKIE:
            # 带浏览器会话cookie可绕过东财频控（rc=102）
            cmd += ["-H", "Referer: https://data.eastmoney.com/", "-H", "Cookie: " + COOKIE]
        cmd.append(URL % (secid, days))
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            try:
                d = json.loads(r.stdout)
                kl = (d.get("data") or {}).get("klines") or []
                if kl:
                    return _parse(kl, code)
            except json.JSONDecodeError:
                pass
        time.sleep(2 * (attempt + 1))
    return None


def _parse(kl, code):
    out = []
    for k in kl:
        p = k.split(",")
        if len(p) < 7:
            continue
        try:
            main_pct = round(float(p[6]), 2)
        except (TypeError, ValueError):
            main_pct = None
        # 字段序: 日期, 主力, 小单, 中单, 大单, 超大单, 主力占比%
        out.append((p[0], code, _wan(p[1]), _wan(p[2]), _wan(p[3]),
                    _wan(p[4]), _wan(p[5]), main_pct))
    return out


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    conn = history_db.connect()
    # 重要板块优先（按今日主力净流入绝对值排序），已回填满的跳过支持断点续传
    codes = [r[0] for r in conn.execute(
        "SELECT code FROM sector_daily WHERE date=(SELECT MAX(date) FROM sector_daily) "
        "ORDER BY ABS(main_inflow_wan) DESC")]
    filled = {r[0] for r in conn.execute(
        "SELECT code FROM sector_flow_daily GROUP BY code HAVING COUNT(*) >= 90")}
    codes = [c for c in codes if c not in filled]
    print("断点续传: 已完成 %d 个, 待回填 %d 个" % (len(filled), len(codes)), flush=True)
    print("待回填板块: %d, 天数上限: %d" % (len(codes), days), flush=True)
    done = failed = 0
    for i, code in enumerate(codes, 1):
        rows = fetch_flow(code, days)
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO sector_flow_daily VALUES(%s)" % ",".join("?" * 8), rows)
            conn.commit()
            done += 1
        else:
            failed += 1
        if i % 25 == 0:
            print("  进度 %d/%d（失败 %d）" % (i, len(codes), failed), flush=True)
        time.sleep(6.0)  # 频控敏感：cookie 单发可过但额度低，保守间隔
    n = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT code), MIN(date), MAX(date) FROM sector_flow_daily").fetchone()
    print("完成: 成功 %d / 失败 %d; 共 %d 行, %s ~ %s" % (done, failed, n[0], n[2], n[3]))
    conn.close()


if __name__ == "__main__":
    main()
