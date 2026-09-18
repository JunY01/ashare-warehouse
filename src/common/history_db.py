# -*- coding: utf-8 -*-
"""板块/行业/ETF 快照历史库（SQLite）

update_data.py 每次更新快照后调用 archive_snapshots() 把三份覆盖式快照按交易日累积进
market_history.db；同一天重复运行自动覆盖（幂等）。另支持从 kline_raw.json 把已有的
300 日板块收盘价一次性导入，使多日资金流/动量分析立即可用。

表结构（英文列名便于 SQL 书写，值仍为中文）:
  sector_daily(date, code, name, close, chg_pct, chg_20d, chg_60d,
               main_inflow_wan, adv_cnt, dec_cnt, leader, float_mv_yi) -- 东财496板块
  industry_daily(date, name, chg_pct, chg_5d, chg_20d, chg_60d,
               chg_52w, chg_ytd, main_inflow_wan)           -- 腾讯31一级行业
  etf_daily(date, code, name, close, chg_pct, chg_20d, chg_60d, main_inflow_wan)
  sector_kline(code, date, close)                           -- 板块日收盘价（名称查询时 join sector_daily）

情绪/行情扩展表（fetch_sentiment/fetch_global/龙虎榜/涨跌停池写入，均走系统 curl）:
  market_turnover / margin_balance        -- 两市成交额 / 两融余额（fetch_sentiment）
  global_daily                            -- 全球股指（fetch_global）
  zt_pool_daily / dt_pool_daily           -- 涨停/跌停池明细（fetch_limit_up_down, push2ex）
  dragon_tiger_daily                      -- 龙虎榜明细（fetch_dragon_tiger, datacenter T-1）

推荐记录表（每日推荐落库，source 区分 live 实时推送 / backfill 历史回填）:
  industry_push_daily / zhuang_pick_daily -- 14:30抢票机 B区短期 / C区抄底名单（daily_1430）
  review_pick_daily                       -- 每日复盘"关注行业"五因子榜单（daily_review）

单位约定:
  - 东财 f62 接口实际返回"元"，旧 CSV 表头误标为"(万)"；入库统一换算为 万元(main_inflow_wan)
  - 腾讯 zljlr 本身就是万元，直接入库
  - fund_amt/amount(涨跌停池)、buy_amt/sell_amt/net_amt(龙虎榜) 均为 万元
用法:
  python history_db.py                  # 归档当天三份快照
  python history_db.py --import-kline   # 从 kline_raw.json 导入板块收盘价历史（幂等）
"""
import csv
import datetime
import json
import os
import sqlite3
import sys

# 统一寻址：无论从哪个目录运行都能定位仓库根（避免回退写错库）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common.paths import DATA_DIR, DATA_ROOT, DB_PATH
SECTOR_SNAP = os.path.join(DATA_ROOT, "sector_snapshot.csv")
TENCENT_SNAP = os.path.join(DATA_ROOT, "tencent_sector.csv")
ETF_SNAP = os.path.join(DATA_ROOT, "etf_snapshot.csv")
KLINE_RAW = os.path.join(DATA_ROOT, "cache", "kline_raw.json")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sector_daily(
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  close REAL, chg_pct REAL, chg_20d REAL, chg_60d REAL,
  main_inflow_wan REAL, adv_cnt INTEGER, dec_cnt INTEGER, leader TEXT,
  float_mv_yi REAL,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS industry_daily(
  date TEXT NOT NULL, name TEXT NOT NULL,
  chg_pct REAL, chg_5d REAL, chg_20d REAL, chg_60d REAL,
  chg_52w REAL, chg_ytd REAL, main_inflow_wan REAL,
  PRIMARY KEY(date, name));
CREATE TABLE IF NOT EXISTS etf_daily(
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  close REAL, chg_pct REAL, chg_20d REAL, chg_60d REAL, main_inflow_wan REAL,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS sector_kline(
  code TEXT NOT NULL, date TEXT NOT NULL, close REAL,
  PRIMARY KEY(code, date));
CREATE TABLE IF NOT EXISTS sector_flow_daily(
  date TEXT NOT NULL, code TEXT NOT NULL,
  main_net_wan REAL, small_net_wan REAL, med_net_wan REAL,
  large_net_wan REAL, super_net_wan REAL, main_pct REAL,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS stock_quote_daily(
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  price REAL, chg_pct REAL, amount_wan REAL, turnover REAL,
  pe REAL, pb REAL, float_mv_yi REAL, total_mv_yi REAL, volume_ratio REAL,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS stock_fundamental_daily(
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  pe REAL, pb REAL, industry TEXT,
  total_mv_yi REAL, float_mv_yi REAL,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS sector_member_daily(
  date TEXT NOT NULL, bk_code TEXT NOT NULL, stock_code TEXT NOT NULL,
  name TEXT, price REAL, chg_pct REAL,
  main_net_wan REAL, float_mv_yi REAL,
  PRIMARY KEY(date, bk_code, stock_code));
CREATE TABLE IF NOT EXISTS regime_daily(
  date TEXT NOT NULL, code TEXT NOT NULL, close REAL,
  regime TEXT, cross_age INTEGER,
  cloud_dist REAL, cloud_width REAL, chan_pos REAL, bottom_score REAL,
  engine TEXT, alert TEXT, sup_events INTEGER,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS sector_valuation(
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  pe REAL, pb REAL, pe_pct REAL, pb_pct REAL,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS global_daily(
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  close REAL, chg_pct REAL, PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS global_index_kline(
  code TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  PRIMARY KEY(code, date));
CREATE TABLE IF NOT EXISTS market_turnover(
  date TEXT PRIMARY KEY, sh_amount REAL, sz_amount REAL, total REAL);
CREATE TABLE IF NOT EXISTS margin_balance(
  date TEXT PRIMARY KEY, rzrq_ye REAL);
CREATE TABLE IF NOT EXISTS fund_nav_daily(
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  nav REAL, acc_nav REAL, chg_pct REAL, PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS sector_ohlcv(
  code TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, vol REAL, amt REAL,
  PRIMARY KEY(code, date));
CREATE TABLE IF NOT EXISTS index_daily(
  date TEXT, code TEXT, name TEXT, close REAL, PRIMARY KEY(code, date));
CREATE TABLE IF NOT EXISTS industry_push_daily(
  push_date TEXT NOT NULL, rank_no INTEGER NOT NULL,
  sector_code TEXT NOT NULL, sector_name TEXT,
  score REAL, reason TEXT, fund_code TEXT, fund_name TEXT,
  broad_state TEXT, hold_days INTEGER, stop_pct REAL, run_mode TEXT,
  PRIMARY KEY(push_date, sector_code));
CREATE TABLE IF NOT EXISTS zhuang_pick_daily(
  push_date TEXT NOT NULL, rank_no INTEGER NOT NULL,
  sector_code TEXT NOT NULL, sector_name TEXT,
  score REAL, reason TEXT, fund_code TEXT, fund_name TEXT,
  broad_state TEXT, hold_days INTEGER, stop_pct REAL,
  PRIMARY KEY(push_date, sector_code));
CREATE TABLE IF NOT EXISTS zt_pool_daily(
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  first_time TEXT, last_time TEXT, lb_cnt INTEGER, zt_days INTEGER,
  fund_amt REAL, amount REAL, hybk TEXT, market TEXT,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS dt_pool_daily(
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  last_time TEXT, dt_cnt INTEGER, amount REAL, hybk TEXT, market TEXT,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS dragon_tiger_daily(
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  close REAL, chg_pct REAL, market TEXT,
  buy_amt REAL, sell_amt REAL, net_amt REAL,
  explanation TEXT,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS etf_kline(
  code TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, close_raw REAL,
  PRIMARY KEY(code, date));
CREATE TABLE IF NOT EXISTS social_sentiment_daily(
  date TEXT NOT NULL, skey TEXT NOT NULL,
  mom_index REAL, dad_index REAL,
  total INTEGER, valid INTEGER, spam INTEGER,
  buy INTEGER, sell INTEGER, vet_buy INTEGER, vet_sell INTEGER,
  PRIMARY KEY(date, skey));
CREATE TABLE IF NOT EXISTS review_pick_daily(
  push_date TEXT NOT NULL, rank_no INTEGER NOT NULL,
  sector_code TEXT NOT NULL, sector_name TEXT,
  broad_state TEXT, score REAL, data_date TEXT, source TEXT,
  bottom_score REAL,
  score_bottom REAL, score_flow REAL, score_stability REAL,
  score_trend REAL, score_signal REAL,
  chg20 REAL, chg60 REAL, inflow REAL,
  reason TEXT, fund_code TEXT, fund_name TEXT,
  PRIMARY KEY(push_date, sector_code));
"""


# 旧库升级：SCHEMA 只对新建表生效，已存在的表靠 ALTER 补列（列已存在时报错忽略）
_MIGRATIONS = [
    "ALTER TABLE sector_daily ADD COLUMN float_mv_yi REAL",
]

# 2026-09-08 本会话曾误建旧列结构的表（列名不符/废弃），需在 SCHEMA 重建前做一次性清理。
# 注意：仅当表存在且缺新列时才 DROP（避免每次 connect 清空在用表数据）；
# 废弃表在 SCHEMA 中已无定义，DROP IF EXISTS 一次后不再存在，重复执行无副作用。
_DROP_IF_OLD = {
    "zt_pool_daily": "lb_cnt",          # 旧列 days/high_days → 新列 lb_cnt/zt_days
    "dt_pool_daily": "dt_cnt",          # 旧列 days → 新列 dt_cnt
    "dragon_tiger_daily": "buy_amt",    # 旧列 buy/sell/net/reason → 新列 buy_amt/...
}
_DROP_ABANDONED = [  # 本会话误建、现 schema 不再创建的废弃表（首次删除后即不存在）
    "limit_up_down_daily",
    "northbound_daily",
    "ipo_calendar",
]


def _reconcile(conn):
    """删旧结构表（缺新列才删）与废弃表；随后由 SCHEMA 重建/不再创建。"""
    for t in _DROP_ABANDONED:
        conn.execute("DROP TABLE IF EXISTS " + t)
    for t, need_col in _DROP_IF_OLD.items():
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)]
        if cols and need_col not in cols:   # 表存在但结构是旧的 → 删了由 SCHEMA 重建
            conn.execute("DROP TABLE IF EXISTS " + t)


def connect(readonly=False, **kwargs):
    """打开历史库连接（全仓唯一入口）。

    readonly=True 走 URI 只读模式：不执行建表/迁移（无写副作用），库文件不存在时
    抛 sqlite3.OperationalError；供研究/回测等纯读者使用。
    其余关键字参数透传 sqlite3.connect（如 check_same_thread=False）。
    """
    if readonly:
        uri = "file:" + DB_PATH.replace(os.sep, "/") + "?mode=ro"
        return sqlite3.connect(uri, uri=True, **kwargs)
    conn = sqlite3.connect(DB_PATH, **kwargs)
    _reconcile(conn)
    conn.executescript(SCHEMA)
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass
    return conn


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _wan(v):
    """元 → 万元；空值透传 None"""
    n = _num(v)
    return None if n is None else round(n / 1e4, 2)


def _warn_archive_timing(date_iso):
    """归档时点告警——两类"跑一次就静默留下错误口径"的历史坑（2026-09-18 清查）。

    ① 周末归档：快照接口给的是上一交易日收盘，按运行日归档就把周五数据错标成周六
       （库里曾留下 2026-08-29 / 2026-09-05 两个周六各一套完整快照）。
    ② 盘中归档：14:30 前后的例行运行记录的是当日未完成值，不是收盘价
       （sector_daily 近半交易日的 close/chg_pct 因此带着盘中偏差，见 _过程 修复记录）。
    仅告警不改行为——盘中归档本身是 14:30 流程的设计用途，收盘后重跑即覆盖。
    """
    d = datetime.date.fromisoformat(date_iso)
    if d.weekday() >= 5:
        print("  ⚠ %s 是周末，快照实为上一交易日数据，按此日期归档会错标（建议改到交易日重跑）" % date_iso)
        return
    now = datetime.datetime.now()
    if d == now.date() and now.time() < datetime.time(15, 5):
        print("  ⚠ 当前 %s 未到 15:05 收盘确认，归档的是盘中值（收盘后重跑即覆盖为收盘口径）"
              % now.strftime("%H:%M"))


def archive_snapshots(date=None):
    """归档三份当前快照 CSV；同日重跑覆盖。盘中运行时记录的是截至最后一次运行的值。"""
    explicit = date is not None
    date = date or datetime.date.today().isoformat()
    if not explicit:
        _warn_archive_timing(date)
    conn = connect()
    try:
        with open(SECTOR_SNAP, encoding="utf-8-sig") as f:
            sec_rows_src = list(csv.DictReader(f))
        rows = [(date, r["代码"], r["名称"], _num(r["最新价"]), _num(r["当日%"]),
                 _num(r["20日%"]), _num(r["60日%"]), _wan(r["主力净流入(万)"]),
                 int(r["涨家数"]) if r.get("涨家数") else None,
                 int(r["跌家数"]) if r.get("跌家数") else None,
                 r.get("领涨股") or None,
                 _num(r.get("流通市值(亿)")))
                for r in sec_rows_src]
        conn.executemany("INSERT OR REPLACE INTO sector_daily VALUES(%s)" % ",".join("?" * 12), rows)

        # 当日主力净流入并入资金流表（每日增量）；只更新 main_net_wan 列，
        # 不覆盖 backfill_flow 已写入的小/中/大/超大单明细
        flow_rows = [(date, r["代码"], _wan(r["主力净流入(万)"])) for r in sec_rows_src]
        conn.executemany(
            """INSERT INTO sector_flow_daily(date, code, main_net_wan) VALUES(?,?,?)
               ON CONFLICT(date, code) DO UPDATE SET main_net_wan=excluded.main_net_wan""",
            [r for r in flow_rows if r[2] is not None])

        with open(TENCENT_SNAP, encoding="utf-8-sig") as f:
            rows = [(date, r["名称"], _num(r["当日%"]), _num(r["5日%"]), _num(r["20日%"]),
                     _num(r["60日%"]), _num(r["52周%"]), _num(r["年初至今%"]),
                     _num(r["主力净流入(万)"]))
                    for r in csv.DictReader(f)]
        conn.executemany("INSERT OR REPLACE INTO industry_daily VALUES(%s)" % ",".join("?" * 9), rows)

        # 板块当日 PE(TTM) 累积入 sector_valuation（幂等；PB/分位列留待后续数据源）
        val_rows = [(date, r["代码"], r["名称"], _num(r.get("市盈率(TTM)")))
                    for r in sec_rows_src]
        conn.executemany(
            """INSERT INTO sector_valuation(date, code, name, pe) VALUES(?,?,?,?)
               ON CONFLICT(date, code) DO UPDATE SET name=excluded.name, pe=excluded.pe""",
            [r for r in val_rows if r[3] is not None])

        with open(ETF_SNAP, encoding="utf-8-sig") as f:
            rows = [(date, r["代码"], r["名称"], _num(r["最新价"]), _num(r["当日%"]),
                     _num(r["20日%"]), _num(r["60日%"]), _wan(r["成交额(万)"]))
                    for r in csv.DictReader(f)]
        conn.executemany("INSERT OR REPLACE INTO etf_daily VALUES(%s)" % ",".join("?" * 8), rows)

        conn.commit()
        for t in ("sector_daily", "industry_daily", "etf_daily", "sector_flow_daily"):
            n = conn.execute("SELECT COUNT(*) FROM %s WHERE date=?" % t, (date,)).fetchone()[0]
            print("  %s 归档 %s: %d 行" % (t, date, n))
    finally:
        conn.close()


def import_klines():
    """kline_raw.json 的板块收盘价历史导入（幂等，可重复执行）。"""
    with open(KLINE_RAW, encoding="utf-8") as f:
        klines = json.load(f).get("klines", {})
    conn = connect()
    try:
        total = 0
        for code, points in klines.items():
            conn.executemany(
                "INSERT OR REPLACE INTO sector_kline(code,date,close) VALUES(?,?,?)",
                [(code, d, c) for d, c in points])
            total += len(points)
        conn.commit()
        span = conn.execute("SELECT MIN(date), MAX(date), COUNT(DISTINCT code) FROM sector_kline").fetchone()
        print("  sector_kline 导入完成: %d 行, %d 个板块, %s ~ %s" % (total, span[2], span[0], span[1]))
    finally:
        conn.close()


if __name__ == "__main__":
    if "--import-kline" in sys.argv:
        import_klines()
    else:
        archive_snapshots()
