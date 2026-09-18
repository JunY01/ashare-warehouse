# -*- coding: utf-8 -*-
"""补齐 global_index_kline 的缺口日K（15 个全球指数，东财 push2his 全球指数 secid）。

这张表不在每日管线里（update_data 只跑 fetch_global 的快照表 global_daily），
所以会稳定落后快照一天，本脚本负责把日K追平。

安全闸：新返回的日期必须先把与库内重叠的日期逐位比对，全一致才写入
（防 secid 串号，历史上 kline_10y 曾因串号整段写错指数）；当日未收盘的半截 bar 不写；
零振幅（高=低）的节假日/周末占位行不写（东财 XIN9 序列里成批出现，FTSE50 曾有 38 条）；
有本地同市场日历的代码（A股三只+FTSE50+港股两只）再加一道日期校验——这类占位行的 K 线
形态可能完全正常（如港股台风休市日的 bar），只有日历判得出来。

用法:
  python src/jobs_fetch/fetch_global_kline.py                    # 目标日=今天（15:05 前自动排除当天）
  python src/jobs_fetch/fetch_global_kline.py --target 2026-09-15
  python src/jobs_fetch/fetch_global_kline.py --days 10           # 回溯窗口（自然日，默认 7）
"""
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common import history_db
from src.common.fetch_util import curl_json
from src.common.paths import DATA_ROOT

# secid 前缀：100.* 全球指数、124.* 港股指数、1./0. 沪深指数；顺序即输出顺序
SECIDS = [
    ("DJIA", "100.DJIA"), ("SPX", "100.SPX"), ("IXIC", "100.NDX"), ("SOX", "251.SOX"),
    ("FTSE", "100.FTSE"), ("GDAXI", "100.GDAXI"),
    ("N225", "100.N225"), ("KS11", "100.KS11"), ("TWII", "100.TWII"),
    ("HSI", "100.HSI"), ("HSTECH", "124.HSTECH"),
    ("SSEC", "1.000001"), ("SZI", "0.399001"), ("CSI300", "1.000300"),
    ("FTSE50", "100.XIN9"),
]

CLOSE_TOL = 0.01


def fetch_bars(secid, beg, end):
    """返回 {date: (开,高,低,收)}；接口对 urllib 做 TLS 指纹拦截，走系统 curl。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
           "&klt=101&fqt=1&beg=%s&end=%s&lmt=100") % (secid, beg, end)
    d = curl_json(url, timeout=20)
    if not d or not d.get("data") or not d["data"].get("klines"):
        return None
    out = {}
    for line in d["data"]["klines"]:
        p = line.split(",")
        try:
            out[p[0]] = (round(float(p[1]), 4), round(float(p[3]), 4),
                         round(float(p[4]), 4), round(float(p[2]), 4))
        except (IndexError, ValueError):
            continue
    return out


def is_unfinished(date_iso, now=None):
    """当日 bar 在 15:05 收盘确认前视为未完成。"""
    now = now or datetime.datetime.now()
    return date_iso == now.date().isoformat() and now.time() < datetime.time(15, 5)


def is_degenerate(bar):
    """残缺 bar：零振幅（高=低）——真实交易日不可能没有振幅，是节假日占位行。"""
    return bar[1] == bar[2]


# 同市场交易日历的来源：kline_10y 里同市场指数的 CSV（日期集合即该市场开市日）
CALENDARS = {
    "SSEC": "上证指数.csv", "SZI": "深成指.csv", "CSI300": "沪深300.csv",
    "FTSE50": "上证指数.csv",          # A50 由 A股价格算出，跟 A 股日历一起休市
    "HSI": "恒生指数.csv", "HSTECH": "恒生科技.csv",
}


def load_calendar(code):
    """该指数的交易日历（日期集合）；无同市场 CSV 或读不到时返回 None（不启用校验）。"""
    fn = CALENDARS.get(code)
    if not fn:
        return None
    try:
        with open(os.path.join(DATA_ROOT, "kline_10y", fn), encoding="utf-8-sig") as f:
            lines = f.read().strip().split("\n")[1:]
    except OSError:
        return None
    return {ln.split(",")[0] for ln in lines if ln}


def off_calendar(date_iso, cal):
    """日期在日历覆盖区间内、却不在日历上 → 该市场当天没开市，是占位行。

    区间外一律放行：日历文件可能落后于抓取（末日之后的新日期不该因为它被丢）。
    """
    return bool(cal) and min(cal) <= date_iso <= max(cal) and date_iso not in cal


def main():
    argv = sys.argv
    now = datetime.datetime.now()
    target = argv[argv.index("--target") + 1] if "--target" in argv else now.date().isoformat()
    days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 7
    t_dt = datetime.date.fromisoformat(target)
    beg = (t_dt - datetime.timedelta(days=days)).strftime("%Y%m%d")

    con = history_db.connect()
    rows, skipped = [], []
    try:
        for code, secid in SECIDS:
            bars = fetch_bars(secid, beg, target.replace("-", ""))
            if not bars:
                skipped.append("%s(接口无返回)" % code)
                time.sleep(4)
                continue
            known = dict(con.execute(
                "SELECT date, close FROM global_index_kline WHERE code=? AND date>=? AND date<=?",
                (code, (t_dt - datetime.timedelta(days=days)).isoformat(), target)).fetchall())
            diffs = [(d, known[d], bars[d][3]) for d in known
                     if d in bars and abs(bars[d][3] - known[d]) > CLOSE_TOL]
            if diffs:
                skipped.append("%s(重叠日不符 %s)" % (code, diffs[:2]))
                time.sleep(4)
                continue
            cal = load_calendar(code)
            new = sorted(d for d in bars if d not in known
                         and d <= target and not is_unfinished(d, now)
                         and not is_degenerate(bars[d])
                         and not off_calendar(d, cal))
            off = sorted(d for d in bars if d not in known and off_calendar(d, cal))
            if not new:
                if off:
                    print("  %-7s 新增 0 行（日历外跳过 %s）" % (code, ",".join(off)))
                time.sleep(4)
                continue
            rows.extend([(code, d) + bars[d] for d in new])
            print("  %-7s +%d %s  重叠%d日一致%s" % (
                code, len(new), new[-1], len(known),
                ("  日历外跳过 %s" % ",".join(off)) if off else ""))
            time.sleep(4)
        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO global_index_kline(code,date,open,high,low,close) "
                "VALUES(?,?,?,?,?,?)", rows)
            con.commit()
    finally:
        con.close()
    print("\n写入 %d 行（目标日 %s）" % (len(rows), target))
    if skipped:
        print("跳过: %s" % "、".join(skipped))


if __name__ == "__main__":
    main()
