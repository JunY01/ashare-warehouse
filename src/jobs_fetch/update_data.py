# -*- coding: utf-8 -*-
"""A股行业数据增量更新脚本

用法:
  python update_data.py                # 全量更新（快照+分片K线，K线走 update_kline_chunk.py）
  python update_data.py --snapshot-only # 只更新快照（K线接口不可用时）
  python update_data.py --kline-only    # 只更新K线（分片：20×25 + --finish）

数据文件（data目录）:
  kline_raw.json      - 板块原始K线（增量累积，含数据截止日期）
  sector_300d.csv     - 板块各周期涨跌幅（5/20/60/120/250/300日，由K线重算）
  sector_snapshot.csv - 全部496板块当日/20日/60日快照（覆盖更新）
  tencent_sector.csv  - 31个一级行业（含52周/年度涨幅，覆盖更新）
  etf_snapshot.csv    - 全部ETF快照（覆盖更新，分析时按名称筛选）
  market_history.db   - 快照历史库（SQLite，每次快照更新后自动归档，详见 history_db.py）
                      另含联网抓取的情绪类历史表：market_turnover/margin_balance（fetch_sentiment.py）、
                      global_daily（fetch_global.py），同日重跑幂等覆盖
"""
import json, urllib.request, time, csv, os, sys, datetime, subprocess

# 统一寻址：无论从哪个目录运行都能定位仓库根（消除回退写错目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common.paths import DATA_ROOT, CONFIG_ROOT, REPO_ROOT

DATA_DIR = DATA_ROOT
KLINE_RAW = os.path.join(DATA_DIR, "cache", "kline_raw.json")
SECTOR_300D = os.path.join(DATA_DIR, "sector_300d.csv")
SECTOR_SNAP = os.path.join(DATA_DIR, "sector_snapshot.csv")
TENCENT_SNAP = os.path.join(DATA_DIR, "tencent_sector.csv")
ETF_SNAP = os.path.join(DATA_DIR, "etf_snapshot.csv")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
           "Referer": "https://quote.eastmoney.com/"}
TODAY = datetime.date.today().strftime("%Y%m%d")

# 东财K线接口有风控，裸请求会被断连；设置 EM_COOKIE 环境变量（或 config/em_cookie.txt）
# 传入浏览器会话cookie可绕过
EM_COOKIE = os.environ.get("EM_COOKIE", "")
if not EM_COOKIE:
    _cookie_file = os.path.join(CONFIG_ROOT, "em_cookie.txt")
    if not os.path.exists(_cookie_file):
        _cookie_file = os.path.join(DATA_DIR, "em_cookie.txt")
    if os.path.exists(_cookie_file):
        with open(_cookie_file, encoding="utf-8") as f:
            EM_COOKIE = f.read().strip()


_EM_COOKIE_DEAD = False


def get(url, tries=4, sleep_s=2):
    # 首选带cookie；一旦cookie被断连，本次运行内不再尝试（避免每请求多耗一轮重试）
    global _EM_COOKIE_DEAD
    for i in range(tries):
        headers = dict(HEADERS)
        if EM_COOKIE and i == 0 and not _EM_COOKIE_DEAD:
            headers["Cookie"] = EM_COOKIE
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as r:
                raw = r.read()
                try:
                    return json.loads(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    return json.loads(raw.decode("gbk"))
        except Exception:
            if i == 0 and EM_COOKIE and not _EM_COOKIE_DEAD:
                _EM_COOKIE_DEAD = True
            if i == tries - 1:
                return None
            time.sleep(sleep_s)
    return None


def _yi(v):
    """元 → 亿元；空值/'-' 占位透传 None"""
    from src.common.fetch_util import yi
    return yi(v)


# ---------- 1. 板块快照（覆盖更新，496个） ----------
def update_snapshot():
    print("== 更新板块快照 ==")
    all_items = []
    pn = 1
    while True:
        url = ("http://push2delay.eastmoney.com/api/qt/clist/get?pn=%d&pz=200&po=1&np=1&fltt=2&invt=2"
               "&fid=f3&fs=m:90+t:2&fields=f2,f3,f9,f12,f14,f20,f21,f62,f104,f105,f109,f128,f160&_=%d") % (pn, int(time.time() * 1000))
        d = get(url)
        if not d or not d.get("data") or not d["data"].get("diff"):
            print("  第%d页失败" % pn)
            break
        items = d["data"]["diff"]
        if isinstance(items, dict):
            items = [items]
        all_items.extend(items)
        if len(all_items) >= d["data"]["total"]:
            break
        pn += 1
        time.sleep(1)
    with open(SECTOR_SNAP, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["代码", "名称", "最新价", "当日%", "20日%", "60日%", "主力净流入(万)", "涨家数", "跌家数", "领涨股",
                    "总市值(亿)", "流通市值(亿)", "市盈率(TTM)"])
        for it in all_items:
            w.writerow([it.get("f12"), it.get("f14"), it.get("f2"), it.get("f3"),
                        it.get("f109"), it.get("f160"), it.get("f62"),
                        it.get("f104"), it.get("f105"), it.get("f128"),
                        _yi(it.get("f20")), _yi(it.get("f21")), it.get("f9")])
    print("  板块快照更新完成: %d个" % len(all_items))
    return len(all_items)


# ---------- 2. 腾讯一级行业（覆盖更新，31个） ----------
def update_tencent():
    print("== 更新腾讯一级行业 ==")
    all_rows = []
    offset = 0
    while True:
        url = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
               "?board_type=hy&sort_type=price&direct=down&offset=%d&count=30") % offset
        d = get(url, sleep_s=1)
        items = (d or {}).get("data", {}).get("rank_list", [])
        if not items:
            break
        all_rows.extend(items)
        if len(items) < 30:
            break
        offset += 30
    with open(TENCENT_SNAP, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["名称", "当日%", "5日%", "20日%", "60日%", "52周%", "年初至今%", "主力净流入(万)"])
        for r in all_rows:
            w.writerow([r["name"], r["zdf"], r.get("zdf_d5"), r.get("zdf_d20"),
                        r.get("zdf_d60"), r.get("zdf_w52"), r.get("zdf_y"), r.get("zljlr")])
    print("  腾讯行业更新完成: %d个" % len(all_rows))


# ---------- 3. ETF快照（覆盖更新） ----------
def update_etf():
    print("== 更新ETF快照 ==")
    all_etfs = []
    pn = 1
    while True:
        url = ("http://push2delay.eastmoney.com/api/qt/clist/get?pn=%d&pz=200&po=1&np=1&fltt=2&invt=2"
               "&fid=f3&fs=b:MK0021&fields=f2,f3,f12,f14,f62,f109,f160&_=%d") % (pn, int(time.time() * 1000))
        d = get(url)
        if not d or not d.get("data") or not d["data"].get("diff"):
            break
        items = d["data"]["diff"]
        if isinstance(items, dict):
            items = [items]
        all_etfs.extend(items)
        if len(all_etfs) >= d["data"]["total"]:
            break
        pn += 1
        time.sleep(1)
    with open(ETF_SNAP, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["代码", "名称", "最新价", "当日%", "20日%", "60日%", "成交额(万)"])
        for it in all_etfs:
            w.writerow([it.get("f12"), it.get("f14"), it.get("f2"), it.get("f3"),
                        it.get("f109"), it.get("f160"), it.get("f62")])
    print("  ETF快照更新完成: %d只" % len(all_etfs))


# ---------- 4. 板块K线增量更新 ----------
def load_kline_raw():
    if os.path.exists(KLINE_RAW):
        with open(KLINE_RAW, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {"last_date": ""}, "klines": {}}


def save_kline_raw(data):
    with open(KLINE_RAW, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def fetch_klines(secid, beg, end):
    url = ("http://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
           "&klt=101&fqt=1&beg=%s&end=%s&lmt=400&_=%d") % (secid, beg, end, int(time.time() * 1000))
    d = get(url)
    if not d or not d.get("data") or not d["data"].get("klines"):
        return None
    return [(k.split(",")[0], float(k.split(",")[2])) for k in d["data"]["klines"]]


def calc_chg(closes, days):
    """最后一天相对 days 个交易日前(不含当日)的涨幅%"""
    from src.common.kline_csv import calc_chg as _cc
    return _cc(closes, days)


def drop_incomplete(closes):
    """当日bar在15:05收盘确认前视为未完成，丢弃以免脏值入库"""
    from src.common.fetch_util import drop_incomplete as _di
    return _di(closes)


# 注：K线增量统一走 update_kline_chunk.py 分片执行（20×25 + --finish）；
# 本文件仅保留 load/save/fetch/calc/drop 基础函数供分片与回填脚本复用。
def _run_sub(rel_parts, *args):
    """以仓库根为 cwd 独立运行 src 下脚本（子进程，避免 fetch 层 import build 层）。

    check=True：子进程非零退出（如未捕获异常）会抛 CalledProcessError 交由调用方计数。
    """
    subprocess.run([sys.executable, os.path.join(REPO_ROOT, *rel_parts), *args],
                   cwd=REPO_ROOT, timeout=600, check=True)


def main():
    only = None
    if "--snapshot-only" in sys.argv:
        only = "snapshot"
    elif "--kline-only" in sys.argv:
        only = "kline"

    fails = []  # 记录失败步骤名，结尾汇报并据此置非零退出码（不再无脑打印"全部完成"）

    if only != "kline":
        update_snapshot()
        update_tencent()
        update_etf()
        try:
            try:
                from src.common import history_db
            except ImportError:
                import history_db
            print("== 归档快照到历史库 ==")
            history_db.archive_snapshots()
        except Exception as e:
            fails.append("历史库归档")
            print("历史库归档失败（不影响快照更新）: %s" % e)
        try:
            try:
                from src.jobs_fetch import fetch_global
            except ImportError:
                import fetch_global
            print("== 更新全球指数快照 ==")
            fetch_global.main()
        except Exception as e:
            fails.append("全球指数")
            print("全球指数更新失败（不影响其他更新）: %s" % e)
        try:
            try:
                from src.jobs_fetch import fetch_sentiment
            except ImportError:
                import fetch_sentiment
            fetch_sentiment.main()
        except Exception as e:
            fails.append("情绪数据")
            print("情绪数据更新失败（不影响其他更新）: %s" % e)
        try:
            try:
                from src.jobs_fetch import fetch_stock_watch
            except ImportError:
                import fetch_stock_watch
            fetch_stock_watch.main()
        except Exception as e:
            fails.append("个股观察池")
            print("个股观察池更新失败（不影响其他更新）: %s" % e)
        try:
            try:
                from src.jobs_fetch import fetch_sector_members
            except ImportError:
                import fetch_sector_members
            fetch_sector_members.main()
        except Exception as e:
            fails.append("板块成分")
            print("板块成分更新失败（不影响其他更新）: %s" % e)
        try:
            try:
                from src.jobs_fetch import fetch_fund_nav
            except ImportError:
                import fetch_fund_nav
            print("== 更新场外基金净值 ==")
            fetch_fund_nav.main()
        except Exception as e:
            fails.append("基金净值")
            print("基金净值更新失败（不影响其他更新）: %s" % e)
        try:
            try:
                from src.jobs_fetch import fetch_limit_up_down
            except ImportError:
                import fetch_limit_up_down
            print("== 更新涨跌停池数据 ==")
            fetch_limit_up_down.main()
        except Exception as e:
            fails.append("涨跌停池")
            print("涨跌停池更新失败（不影响其他更新）: %s" % e)
        try:
            try:
                from src.jobs_fetch import fetch_dragon_tiger
            except ImportError:
                import fetch_dragon_tiger
            print("== 更新龙虎榜数据 ==")
            fetch_dragon_tiger.main()
        except Exception as e:
            fails.append("龙虎榜")
            print("龙虎榜更新失败（不影响其他更新）: %s" % e)
        try:
            print("== 更新社群情绪指数 ==")
            _run_sub(["src", "jobs_fetch", "fetch_social_sentiment.py"])
        except Exception as e:
            fails.append("社群情绪")
            print("社群情绪更新失败（不影响其他更新）: %s" % e)
        try:
            # 红利家族 + 全市场基准日K：14:30 报告 E区"红利 40 日收益差"的取数层
            print("== 同步红利指数/全市场基准日K ==")
            _run_sub(["src", "jobs_fetch", "fetch_dividend_index.py"])
        except Exception as e:
            fails.append("红利指数日K")
            print("红利指数日K同步失败（不影响其他更新）: %s" % e)
    if only != "snapshot":
        # K线统一走分片脚本（单进程496连抓易被超时杀掉；每片25个落盘可续跑）
        try:
            for start in range(0, 500, 25):
                _run_sub(["src", "jobs_fetch", "update_kline_chunk.py"],
                         "--start", str(start), "--count", "25")
            _run_sub(["src", "jobs_fetch", "update_kline_chunk.py"], "--finish")
        except Exception as e:
            fails.append("K线分片")
            print("K线分片更新失败（接口可能受限）: %s" % e)
        try:
            try:
                from src.common import history_db
            except ImportError:
                import history_db
            history_db.import_klines()
        except Exception as e:
            fails.append("sector_kline同步")
            print("sector_kline 同步失败（不影响K线更新）: %s" % e)
        # build 层脚本独立进程调用，fetch 层不 import build 层（分层解耦）
        try:
            _run_sub(["src", "jobs_build", "scan_regime.py"])
        except Exception as e:
            fails.append("趋势扫描")
            print("趋势扫描失败（不影响其他更新）: %s" % e)
    try:
        _run_sub(["src", "jobs_build", "generate_dashboard.py"])
    except Exception as e:
        fails.append("看板生成")
        print("看板生成失败（不影响数据更新）: %s" % e)

    if fails:
        print("\n完成，但以下 %d 步失败: %s" % (len(fails), "、".join(fails)))
        return len(fails)
    print("\n全部完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
