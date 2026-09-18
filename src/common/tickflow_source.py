# -*- coding: utf-8 -*-
"""TickFlow数据源 - 作为东财/新浪的稳定备选

提供指数K线数据，用于替代东财实时点位接口（东财限流时可用）。

用法:
  from src.common.tickflow_source import fetch_index_klines
  data = fetch_index_klines()  # 返回 {code: {price, chg_pct, prev_close}}

纯标准库（原实现依赖第三方 httpx，已改 urllib）。
"""
import json
import urllib.parse
import urllib.request

# TickFlow指数代码映射 (东财secid -> TickFlow symbol)
# 格式: 东财secid -> (TickFlow代码, 东财f12代码)
TICKFLOW_INDEX_MAP = {
    "1.000001":  ("000001.SH", "000001"),   # 上证指数
    "0.399001":  ("399001.SZ", "399001"),   # 深成指
    "0.399006":  ("399006.SZ", "399006"),   # 创业板指
    "1.000688":  ("000688.SH", "000688"),   # 科创50
    "1.000300":  ("000300.SH", "000300"),   # 沪深300
    "1.000905":  ("000905.SH", "000905"),   # 中证500
    "1.000016":  ("000016.SH", "000016"),   # 上证50
    "1.000852":  ("000852.SH", "000852"),   # 中证1000
    "1.000510":  ("000510.SH", "000510"),   # 中证A500
    "0.399986":  ("399986.SZ", "399986"),   # 中证银行
    "1.000015":  ("000015.SH", "000015"),   # 红利指数
    "0.899050":  ("899050.BJ", "899050"),   # 北证50
    # 港股指数TickFlow免费版可能不支持，保留映射以备后用
    "100.HSI":   ("HSI.HK", "HSI"),         # 恒生指数
    "124.HSTECH": ("HSTECH.HK", "HSTECH"),  # 恒生科技
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _tickflow_get(symbol, period="1d", count=5):
    """调用TickFlow免费API获取K线数据"""
    params = urllib.parse.urlencode(
        {"symbol": symbol, "period": period, "count": count, "adjust": "none"})
    url = "https://free-api.tickflow.org/v1/klines?" + params
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("data", {})
    except Exception:
        return None


def fetch_index_klines():
    """获取所有指数的最新K线数据

    Returns:
        dict: {f12_code: {price, chg_pct, prev_close}}
              例如 {"000300": {"price": 4590.79, "chg_pct": 0.32, "prev_close": 4576.12}}
    """
    results = {}
    errs = 0

    for secid, (tf_symbol, f12_code) in TICKFLOW_INDEX_MAP.items():
        try:
            data = _tickflow_get(tf_symbol, period="1d", count=5)
            if not data or not data.get("close"):
                errs += 1
                continue

            closes = data["close"]
            if len(closes) < 1:
                errs += 1
                continue

            price = closes[-1]
            prev_close = closes[-2] if len(closes) >= 2 else price
            chg_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0

            results[f12_code] = {
                "price": price,
                "chg_pct": chg_pct,
                "prev_close": prev_close,
                "source": "tickflow"
            }
        except Exception:
            errs += 1
            continue

    if not results:
        print("  [warn] TickFlow 全部 %d 个指数均未取到（网络或接口不可用）" % len(TICKFLOW_INDEX_MAP))
    return results


def test():
    """测试函数"""
    print("=== TickFlow指数K线测试 ===\n")

    data = fetch_index_klines()
    print(f"获取到 {len(data)} 个指数数据:\n")

    for code, info in sorted(data.items()):
        print(f"  {code}: {info['price']:.2f} ({info['chg_pct']:+.2f}%) [来源: {info['source']}]")

    return data


if __name__ == "__main__":
    test()
