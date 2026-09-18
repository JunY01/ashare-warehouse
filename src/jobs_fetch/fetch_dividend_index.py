# -*- coding: utf-8 -*-
"""红利家族 + 全市场基准 日K补齐（红利 40 日收益差指标的取数层）

为什么单独一个脚本：`index_daily` 之前只有零碎片段（红利低波 155 天），而"红利 40 日收益差"
要 ≥500 个观测才算得出分位，且**红利指数与全市场基准必须同源、同交易日对齐**。

两个源，按可用性依次尝试：
  ① 中证指数官网 perf 接口 —— 官方源，2013-12-19 起完整（3100 个交易日），带 全收益指数；
     东财 K 线端点被限流时它是唯一拿得到的源。**返回体含指数中文名，必须逐条校验**，
     否则会像历史上 kline_10y 那样把别的指数写进来（代码串号）。
  ② 腾讯 fqkline —— 秒级、免 cookie，覆盖 000015/000922/000985 等中证系数字代码；
     中证系 H 代码（H30269/H00922）腾讯未收录。

落库：market_history.db 的 index_daily（date 主键幂等覆盖）。纯抓取，不生成报告。
今日 bar 在 15:05 前一律丢弃（与 fetch_index_csv_kline.unfinished 同一把尺子）。

用法:
  python src/jobs_fetch/fetch_dividend_index.py           # 增量（回看 60 天做重叠校验）
  python src/jobs_fetch/fetch_dividend_index.py --full    # 全历史重建
  python src/jobs_fetch/fetch_dividend_index.py --extra   # 附带 沪深300/中证银行/中证500（研究用对照）
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common import history_db  # noqa: E402
from src.common.fetch_util import num  # noqa: E402

CS_START = "20130101"
CS_URL = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
          "?indexCode=%s&startDate=%s&endDate=%s")
QT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,1300,qfq"
TOL = 0.005                      # 重叠日收盘偏差容忍度（与 fetch_global_kline 同）
QT_WINDOWS = 4                   # 腾讯单次窗口年数（>4 年会被截断成最近 1300 根）
CLOSE_HM = (15, 5)               # 收盘确认时刻

# name -> (库内 code, 中证官网(代码, 名称关键字) 或 None, 腾讯代码 或 None)
INDEXES = {
    "上证红利": ("sh000015", ("000015", "上证红利"), "sh000015"),
    "中证红利": ("sh000922", ("000922", "中证红利"), "sh000922"),
    "中证红利低波动": ("csH30269", ("H30269", "红利低波"), None),
    "中证全指": ("sh000985", ("000985", "中证全指"), "sh000985"),
    "中证红利全收益": ("csH00922", ("H00922", "中证红利全收益"), None),
    # 备用基准：中证全指取不到时用国证A指顶上（两者日收益相关性 1.000，仅点位不同）
    "国证A指": ("sz399317", None, "sz399317"),
}
EXTRA = {
    "沪深300": ("sh000300", None, "sh000300"),
    "中证银行": ("sz399986", None, "sz399986"),
    "中证500": ("sh000905", None, "sh000905"),
}


def unfinished(date_iso, now=None):
    """当日 bar 在 15:05 收盘确认前视为未完成。"""
    now = now or datetime.datetime.now()
    return date_iso == now.date().isoformat() and now.time() < datetime.time(*CLOSE_HM)


def drop_today(rows):
    return [(d, v) for d, v in rows if not unfinished(d[:4] + "-" + d[4:6] + "-" + d[6:8])]


def cs_index(code, name_kw, start):
    """中证官网 → (rows, err)。rows 为 [(YYYY-MM-DD, close)]；名称不匹配则报错不出数据。"""
    end = datetime.date.today().strftime("%Y%m%d")
    url = CS_URL % (code, start, end)
    for _ in range(3):
        try:
            r = subprocess.run(["curl", "-s", "-m", "30", "-A", "Mozilla/5.0",
                                "-H", "Referer: https://www.csindex.com.cn/", url],
                               capture_output=True, timeout=45)
            j = json.loads(r.stdout.decode("utf-8", "ignore"))
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, UnicodeDecodeError):
            time.sleep(2)
            continue
        data = j.get("data") or []
        if not data:
            time.sleep(2)
            continue
        got = (data[0].get("indexNameCnAll") or "")
        if name_kw not in got:          # 代码串号护栏：名字对不上宁可不出数
            return None, "名称不符（%s 返回的是「%s」）" % (code, got)
        rows = []
        for it in data:
            td, c = it.get("tradeDate"), num(it.get("close"))
            if td and c:
                rows.append((td[:4] + "-" + td[4:6] + "-" + td[6:8], c))
        return drop_today(rows), None
    return None, "请求失败/空数据"


def qt_block(code, start, end):
    """腾讯单窗口 → [(日期, 收盘)]；失败返回 []"""
    url = QT_URL % (code, start, end)
    for _ in range(3):
        try:
            r = subprocess.run(["curl", "-s", "-m", "25", url], capture_output=True, timeout=35)
            b = ((json.loads(r.stdout.decode("utf-8", "ignore")).get("data") or {}).get(code) or {})
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, UnicodeDecodeError):
            time.sleep(1)
            continue
        out = []
        for row in (b.get("qfqday") or b.get("day") or []):
            v = num(row[2])
            if row and v:
                out.append((row[0], v))
        if out:
            return out
    return []


def qt_index(code, start, want, incremental_days=None):
    """腾讯 → (rows, err)。incremental_days 给定时只取尾部（用它算起点）。"""
    if incremental_days is not None:
        s = datetime.date.today() - datetime.timedelta(days=int(incremental_days * 1.5) + 10)
        rows = qt_block(code, s.isoformat(), datetime.date.today().isoformat())
        return drop_today(rows), (None if rows else "腾讯空数据")
    merged = {}
    y = int(start[:4])
    yend = datetime.date.today().year
    while y <= yend:
        rows = qt_block(code, "%d-01-01" % y, "%d-12-31" % min(y + QT_WINDOWS - 1, yend))
        for d, v in rows:
            merged[d] = v
        y += QT_WINDOWS
        time.sleep(0.5)
    rows = sorted(merged.items())
    return drop_today(rows), (None if rows else "腾讯空数据")


def db_rows(conn, code):
    return {d: c for d, c in conn.execute(
        "SELECT date, close FROM index_daily WHERE code=?", (code,))}


def topup_today(conn, name, code, qt_code, primary_dates):
    """腾讯补当日/补官方缺的日期。

    中证官网当天数据晚上才发布，而腾讯 15:05 后就有收盘价；但腾讯**不能**覆盖官方
    已有的日期（官方优先），只填官方没给的、比它更新的那一天。重叠日仍先校验。
    """
    if not qt_code:
        return None
    rows = drop_today(qt_block(qt_code,
                               (datetime.date.today() - datetime.timedelta(days=20)).isoformat(),
                               datetime.date.today().isoformat()))
    base = set(primary_dates) or set(db_rows(conn, code))
    if not rows or not base:
        return None
    newest = max(base)
    n_common = len([1 for d, _v in rows if d in base])
    dev = 0.0
    old = db_rows(conn, code)
    for d, v in rows:
        if d in base and old.get(d):
            dev = max(dev, abs(v - old[d]) / old[d])
    if n_common and dev > TOL:
        print("    %-12s 腾讯补当日：重叠偏差 %.2f%% 超容忍度，跳过" % (name, dev * 100))
        return None
    add = [(d, v) for d, v in rows if d not in base and d > newest]
    if not add:
        return None
    conn.executemany("INSERT OR REPLACE INTO index_daily(date,code,name,close) VALUES(?,?,?,?)",
                     [(d, code, name, v) for d, v in add])
    print("    %-12s 腾讯补 %s=%.2f（%d 天，重叠偏差 %.3f%%）"
          % (name, add[-1][0], add[-1][1], len(add), dev * 100))
    return add[-1][0]


def check_overlap(old, new):
    """重叠日校验 → (共同日数, 最大相对偏差)。"""
    common = [(d, old[d], v) for d, v in new if d in old]
    if not common:
        return 0, 0.0
    dev = max(abs(v - o) / o for _d, o, v in common if o)
    return len(common), dev


def sync(conn, name, code, cs_spec, qt_code, full, days):
    old = db_rows(conn, code)
    want_full = full or not old
    rows, src, err = None, "", ""
    if cs_spec:
        cs_start = CS_START if want_full else (
            datetime.datetime.strptime(max(old), "%Y-%m-%d") - datetime.timedelta(days=90)
        ).strftime("%Y%m%d")
        rows, err = cs_index(cs_spec[0], cs_spec[1], cs_start)
        if rows:
            src = "中证官网"
    if not rows and qt_code:
        rows, err2 = qt_index(qt_code, CS_START, name,
                             None if want_full else days)
        if rows:
            src = "腾讯"
        err = err or err2
    if not rows:
        print("  %-12s %-6s ✗ %s（本地保留 %d 行）" % (name, code, err or "无数据", len(old)))
        return 0, None, set()
    n_common, dev = check_overlap(old, rows)
    if n_common and dev > TOL:
        print("  %-12s %-6s ✗ 重叠校验不通过：%d 天共同数据最大偏差 %.2f%% > %.2f%%（拒写）"
              % (name, code, n_common, dev * 100, TOL * 100))
        return 0, None, set()
    conn.executemany("INSERT OR REPLACE INTO index_daily(date,code,name,close) VALUES(?,?,?,?)",
                     [(d, code, name, v) for d, v in rows])
    # 清掉官方源还没发布、却被别的途径写进来的"今日行"（历史事故：手工灌的盘中值使
    # 40 日收益差多算一天，把同日对比变成了跨日对比，读数差了近 4 个百分点）
    stale = [d for d in old if d > rows[-1][0] and d == datetime.date.today().isoformat()]
    if stale:
        conn.executemany("DELETE FROM index_daily WHERE code=? AND date=?", [(code, d) for d in stale])
        print("    %-12s 清掉未确认的今日行 %s（等官方源或收盘价）" % (name, "、".join(stale)))
    new = len([1 for d, _v in rows if d not in old])
    last = rows[-1]
    print("  %-12s %-9s %-6s %5d 行（新增 %4d） 收 %s=%.2f  重叠%3d天 偏差%.3f%%%s"
          % (name, code, src, len(rows), new, last[0], last[1], n_common, dev * 100,
             "  ⚠全历史重建" if want_full else ""))
    return len(rows), last[0], {d for d, _v in rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="全历史重建（默认增量）")
    ap.add_argument("--extra", action="store_true", help="附带 沪深300/中证银行/中证500（研究对照）")
    ap.add_argument("--days", type=int, default=300, help="增量回看交易日数")
    a = ap.parse_args()
    items = dict(INDEXES)
    if a.extra:
        items.update(EXTRA)
    conn = history_db.connect()
    print("红利指数日K同步（%s）：" % ("全量" if a.full else "增量"))
    latest = []
    for name, (code, cs_spec, qt_code) in items.items():
        _n, last, p_dates = sync(conn, name, code, cs_spec, qt_code, a.full, a.days)
        t = topup_today(conn, name, code, qt_code, p_dates)
        if t:
            last = t
        if last:
            latest.append(last)
        time.sleep(1.2)          # 串行 + 间隔，防限流
    conn.commit()
    row = conn.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM index_daily").fetchone()
    print("index_daily 共 %d 行（%s ~ %s）；本次数据日 %s"
          % (row[0], row[1], row[2], max(latest) if latest else "无"))
    conn.close()


if __name__ == "__main__":
    main()
