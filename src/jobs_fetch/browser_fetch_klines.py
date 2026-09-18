# -*- coding: utf-8 -*-
"""通过浏览器cookie调用东财K线API，增量更新kline_raw.json

用法：被浏览器自动化调用，或者手动传入cookie:
  python browser_fetch_klines.py --cookie "your_cookie_here"

也可从 em_cookie.txt 读取cookie（与update_data.py兼容）
"""
import json, urllib.request, time, os, sys, datetime

# 统一寻址：K线缓存与重算产物一律落 data/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common.paths import DATA_ROOT, CONFIG_ROOT
from src.common.fetch_util import drop_incomplete

DATA_DIR = DATA_ROOT
KLINE_RAW = os.path.join(DATA_DIR, "cache", "kline_raw.json")
TODAY = datetime.date.today().strftime("%Y%m%d")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/"
}


def get_cookie():
    cookie = os.environ.get("EM_COOKIE", "")
    if not cookie:
        cookie_file = os.path.join(CONFIG_ROOT, "em_cookie.txt")
        if os.path.exists(cookie_file):
            with open(cookie_file, encoding="utf-8") as f:
                cookie = f.read().strip()
    # 命令行参数优先
    for i, arg in enumerate(sys.argv):
        if arg == "--cookie" and i + 1 < len(sys.argv):
            cookie = sys.argv[i + 1]
    return cookie


def get(url, cookie="", tries=4, sleep_s=2):
    for i in range(tries):
        headers = dict(HEADERS)
        if cookie and i == 0:
            headers["Cookie"] = cookie
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as r:
                raw = r.read()
                try:
                    return json.loads(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    return json.loads(raw.decode("gbk"))
        except Exception:
            if i < tries - 1:
                time.sleep(sleep_s * (i + 1))
    return None


def fetch_klines(secid, beg, end, cookie=""):
    url = ("http://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
           "&klt=101&fqt=1&beg=%s&end=%s&lmt=400&_=%d") % (secid, beg, end, int(time.time() * 1000))
    d = get(url, cookie)
    if not d or not d.get("data") or not d["data"].get("klines"):
        return None
    return [(k.split(",")[0], float(k.split(",")[2])) for k in d["data"]["klines"]]


# 限流检测：连续失败数达到阈值时自动暂停
_consecutive_fails = 0
_PAUSE_THRESHOLD = 3   # 连续失败3次触发暂停
_PAUSE_SECONDS = 600   # 暂停10分钟（东财限流恢复需15-20分钟）



def main():
    cookie = get_cookie()
    print(f"Cookie: {'有' if cookie else '无'}")

    with open(KLINE_RAW, encoding="utf-8") as f:
        raw = json.load(f)
    klines_map = raw["klines"]
    last_date = raw["meta"].get("last_date", "")

    # 获取板块列表
    all_items = []
    for page in range(1, 5):
        url = ("http://push2delay.eastmoney.com/api/qt/clist/get?pn=%d&pz=200&po=1&np=1"
               "&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14") % page
        d = get(url, cookie)
        if d and d.get("data") and d["data"].get("diff"):
            all_items.extend(d["data"]["diff"])
        else:
            break
        time.sleep(0.5)

    print(f"板块总数: {len(all_items)}, 已有K线板块: {len(klines_map)}")

    # 强制刷新最近5天的数据（解决盘前抓取的旧数据问题）
    beg = (datetime.date.today() - datetime.timedelta(days=5)).strftime("%Y%m%d")
    today_str = datetime.date.today().isoformat()
    updated = 0
    failed = 0
    save_interval = 50  # 每50个板块保存一次
    consecutive_fails = 0

    for idx, item in enumerate(all_items):
        code = item["f12"]
        name = item.get("f14", "")
        old = klines_map.get(code, [])

        # 始终抓取最近5天数据并覆盖合并
        closes = fetch_klines("90." + code, beg, TODAY, cookie)
        time.sleep(1.0)  # 防限流

        if not closes:
            failed += 1
            consecutive_fails += 1
            # 连续失败过多，自动暂停等待限流恢复
            if consecutive_fails >= _PAUSE_THRESHOLD:
                print(f"  ⚠️ 连续失败{consecutive_fails}次，疑似限流，暂停{_PAUSE_SECONDS}秒...")
                time.sleep(_PAUSE_SECONDS)
                consecutive_fails = 0  # 重置计数
        else:
            consecutive_fails = 0  # 成功则重置

        if not closes:
            if (idx + 1) % save_interval == 0:
                print(f"  [{idx+1}/{len(all_items)}] 已完成, 更新{updated} 失败{failed}")
                raw["meta"]["last_update"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(KLINE_RAW, "w", encoding="utf-8") as f:
                    json.dump(raw, f, ensure_ascii=False)
                print(f"  已保存进度到文件")
            continue

        # 用新数据覆盖旧数据（保留更早的历史）
        old_map = {c[0]: c[1] for c in old}
        for c in closes:
            old_map[c[0]] = c[1]
        old = [(d, p) for d, p in sorted(old_map.items())]

        # 截断当日未完成bar
        old = drop_incomplete(old)

        klines_map[code] = old
        updated += 1

        if (idx + 1) % save_interval == 0:
            print(f"  [{idx+1}/{len(all_items)}] 已完成, 更新{updated} 失败{failed}")
            # 每50个保存一次进度
            raw["meta"]["last_update"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(KLINE_RAW, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False)
            print(f"  已保存进度到文件")

    # 更新meta
    new_last = max((c[-1][0] for c in klines_map.values() if c), default=last_date)
    raw["meta"]["last_date"] = new_last
    raw["meta"]["last_update"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    raw["meta"]["total_sectors"] = len(klines_map)

    with open(KLINE_RAW, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    print(f"\nK线更新完成: 更新{updated} 失败{failed}")
    print(f"最新日期: {new_last}")

    # 重算 sector_300d.csv（统一写入者，与 update_kline_chunk --finish 同表头）
    print("\n重算 sector_300d.csv ...")
    from src.common.kline_csv import write_sector_300d
    names = {it["f12"]: it.get("f14", "") for it in all_items}
    n = write_sector_300d(klines_map, names)
    print("sector_300d.csv 重算完成: %d个板块" % n)


if __name__ == "__main__":
    main()
