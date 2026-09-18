#!/usr/bin/env python3
"""场外基金盘中实时估值（纯标准库，零第三方依赖）

原理：通过东财push2接口获取ETF实时涨跌幅，推算联接基金净值变化

用法:
  python fund_realtime.py              # 显示所有持仓基金估值
  python fund_realtime.py --stop       # 只显示触及止损线的基金
  python fund_realtime.py --watch 024701  # 只查看指定基金

数据源: 东财push2实时行情接口（ETF）+ 本地fund_nav_daily历史净值
"""
import json, urllib.request, time, os, sys, datetime

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from src.common.paths import DATA_ROOT, CONFIG_ROOT
    from src.common import history_db
    from src.common.fetch_util import curl_json
    DATA_DIR = DATA_ROOT
    POSITIONS = os.path.join(CONFIG_ROOT, "positions.json")
except ImportError:
    _REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, _REPO)
    from src.common import history_db
    from src.common.fetch_util import curl_json
    DATA_DIR = os.path.join(_REPO, "data")
    POSITIONS = os.path.join(_REPO, "config", "positions.json")

# 基金配置示例：基金代码 -> {名称, 跟踪的ETF代码, 关联指数, 止损线, 持仓匹配名}
# 填你自己的观察清单即可；stop_line=None 表示不做止损提醒
FUND_CONFIG = {
    "000000": {
        "name": "某某宽基联接C",
        "etf_code": "512050",
        "etf_secid": "1.512050",
        "stop_line": None,
        "pos_match": "A500"
    },
    "000001": {
        "name": "某某科创联接C",
        "etf_code": "588000",
        "etf_secid": "1.588000",
        "stop_line": None,
        "pos_match": "科创50"
    },
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/"
}


def _quote_from(d):
    """push2 data 字段 → 行情 dict（价格字段按分换算为元）"""
    return {
        "price": d.get("f43", 0) / 100,
        "open": d.get("f46", 0) / 100,
        "high": d.get("f44", 0) / 100,
        "low": d.get("f45", 0) / 100,
        "prev_close": d.get("f60", 0) / 100,
        "chg_pct": d.get("f170", 0) / 100,
        "volume": d.get("f47", 0),
    }


def fetch_etf_realtime(secid):
    """获取ETF实时行情（东财push2接口，串行sleep防限流＋4次重试；urllib 全败后 curl 兜底）"""
    url_tpl = ("http://push2.eastmoney.com/api/qt/stock/get?secid=%s"
               "&fields=f43,f44,f45,f46,f47,f57,f58,f60,f170,f171&_=%d")
    for attempt in range(4):
        url = url_tpl % (secid, int(time.time() * 1000))
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
                if data.get("data"):
                    return _quote_from(data["data"])
        except Exception as e:
            print("  [warn] ETF实时行情获取失败 %s: %s" % (secid, e))
        time.sleep(1.0 + attempt)
    # push2 对 Python urllib 偶发掐断（限流/TLS 指纹）时，改走系统 curl
    data = curl_json(url_tpl % (secid, int(time.time() * 1000)), timeout=10,
                     referer=HEADERS["Referer"], ua=HEADERS["User-Agent"], tries=2)
    if data and data.get("data"):
        print("  [info] ETF实时行情 %s 经 curl 兜底获取成功" % secid)
        return _quote_from(data["data"])
    print("  [warn] ETF实时行情获取失败 %s: urllib 与 curl 均无返回" % secid)
    return None


def get_fund_prev_nav(fund_code):
    """从历史库或腾讯财经接口获取基金昨日净值"""
    # 方法1：从本地历史库获取
    try:
        conn = history_db.connect(readonly=True)
        cur = conn.cursor()
        cur.execute("""
            SELECT nav FROM fund_nav_daily
            WHERE code = ?
            ORDER BY date DESC LIMIT 1
        """, (fund_code,))
        row = cur.fetchone()
        conn.close()
        if row:
            return float(row[0])
    except Exception as e:
        print("  [warn] 本地净值库读取失败 %s: %s" % (fund_code, e))

    # 方法2：从腾讯财经接口获取
    try:
        url = f'https://qt.gtimg.cn/q=jj{fund_code}'
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode('gbk')
            # 格式: v_jj024701="024701~名称~...~昨日净值~..."
            data_str = raw.split('="')[1].rstrip('"')
            parts = data_str.split('~')
            if len(parts) > 5 and parts[5]:
                return float(parts[5])
    except Exception as e:
        print("  [warn] 腾讯净值接口失败 %s: %s" % (fund_code, e))

    return None


def get_position_mv():
    """从positions.json读取持仓市值"""
    try:
        with open(POSITIONS, encoding="utf-8") as f:
            data = json.load(f)
        result = {}
        for acc in ("资金A", "资金B"):
            for fund in (data.get(acc) or {}).get("基金", []):
                mv = fund.get("市值")
                if isinstance(mv, (int, float)):
                    result[fund.get("名称", "")] = mv
        return result
    except Exception as e:
        print("  [warn] 持仓文件读取失败: %s" % e)
        return {}


def estimate_fund_nav(etf_chg_pct, prev_nav):
    """根据ETF涨跌幅估算联接基金净值"""
    if prev_nav is None:
        return None
    return round(prev_nav * (1 + etf_chg_pct / 100), 4)


def format_sign(val):
    """格式化正负号"""
    return f"+{val}" if val >= 0 else str(val)


def main():
    args = sys.argv[1:]
    show_stop_only = "--stop" in args
    watch_code = None
    if "--watch" in args:
        idx = args.index("--watch")
        if idx + 1 < len(args):
            watch_code = args[idx + 1]

    print("=" * 60)
    print("📊 场外基金盘中实时估值")
    print(f"   时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print()

    pos_mv = get_position_mv()
    results = []

    for fund_code, config in FUND_CONFIG.items():
        if watch_code and fund_code != watch_code:
            continue

        etf_data = fetch_etf_realtime(config["etf_secid"])
        prev_nav = get_fund_prev_nav(fund_code)
        time.sleep(1.2)

        if etf_data is None:
            results.append({
                "code": fund_code,
                "name": config["name"],
                "status": "❌ ETF行情获取失败"
            })
            continue

        est_nav = estimate_fund_nav(etf_data["chg_pct"], prev_nav)

        # 查找匹配的持仓市值
        mv = None
        for pname, v in pos_mv.items():
            if config["pos_match"] and config["pos_match"] in pname:
                mv = v
                break

        # 计算估算盈亏
        est_pl = None
        if mv and etf_data["chg_pct"]:
            est_pl = round(mv * etf_data["chg_pct"] / 100, 2)

        # 判断止损线
        stop_warning = False
        if config["stop_line"] and est_nav:
            if est_nav < config["stop_line"]:
                stop_warning = True

        results.append({
            "code": fund_code,
            "name": config["name"],
            "etf_code": config["etf_code"],
            "etf_price": etf_data["price"],
            "etf_chg": etf_data["chg_pct"],
            "prev_nav": prev_nav,
            "est_nav": est_nav,
            "stop_line": config["stop_line"],
            "stop_warning": stop_warning,
            "mv": mv,
            "est_pl": est_pl,
        })

    # 显示结果
    if show_stop_only:
        results = [r for r in results if r.get("stop_warning")]

    if not results:
        print("  暂无数据")
        return

    # 按涨跌幅排序
    results.sort(key=lambda x: -(x.get("etf_chg") or -999))

    for r in results:
        if "status" in r:
            print(f"\n  {r['name']} ({r['code']})")
            print(f"    {r['status']}")
            continue

        # 状态标记
        if r.get("stop_warning"):
            mark = "🔴"
        elif r["etf_chg"] >= 1:
            mark = "🟢"
        elif r["etf_chg"] <= -1:
            mark = "🔴"
        else:
            mark = "⚪"

        print(f"\n  {mark} {r['name']} ({r['code']})")
        print(f"    ETF({r['etf_code']}): {r['etf_price']:.3f}  涨跌: {format_sign(r['etf_chg'])}%")
        print(f"    昨日净值: {r['prev_nav']:.4f}" if r['prev_nav'] else "    昨日净值: N/A")
        print(f"    估算净值: {r['est_nav']:.4f}" if r['est_nav'] else "    估算净值: N/A")

        if r.get("stop_line"):
            diff_pct = ((r["est_nav"] / r["stop_line"]) - 1) * 100 if r["est_nav"] else 0
            print(f"    止损线: {r['stop_line']}  差距: {format_sign(round(diff_pct, 2))}%")

        if r.get("mv") and r.get("est_pl") is not None:
            print(f"    持仓市值: ¥{r['mv']:,.0f}  估算盈亏: ¥{format_sign(r['est_pl'])}")

    # 汇总
    total_mv = sum(r.get("mv", 0) or 0 for r in results)
    total_pl = sum(r.get("est_pl", 0) or 0 for r in results)
    if total_mv > 0:
        print("\n" + "-" * 60)
        print(f"  📈 持仓汇总: ¥{total_mv:,.0f}  今日估算: ¥{format_sign(total_pl)}")

    # 止损提醒
    stop_funds = [r for r in results if r.get("stop_warning")]
    if stop_funds:
        print("\n" + "=" * 60)
        print("  ⚠️  触及止损线的基金:")
        for r in stop_funds:
            print(f"    🔴 {r['name']}: 估算净值 {r['est_nav']:.4f} < 止损线 {r['stop_line']}")
        print("  建议: 收盘前提交赎回")
        print("=" * 60)

    print()


if __name__ == "__main__":
    main()
