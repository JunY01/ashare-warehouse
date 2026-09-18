# -*- coding: utf-8 -*-
"""指数估值数据拉取与估值参考报告生成

数据源:
  蛋卷基金估值接口(PE/PB/历史分位/股息率): https://danjuanfunds.com/djapi/index_eva/dj
  东方财富实时点位(点位/涨跌幅): https://push2.eastmoney.com/api/qt/ulist.np/get

用法:
  python fetch_index_valuation.py         # 更新数据 + 生成报告(默认)
  python fetch_index_valuation.py --json  # 只更新数据到 index_valuation.json，不生成报告

输出:
  data/index_valuation.json                 - 结构化估值数据(原始+判定)
  reports/指数估值参考_YYYYMMDD.md          - 每日估值参考报告
"""
import bisect, json, urllib.request, time, os, sys, datetime

# 统一寻址：产物一律落 data/ 与 reports/
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
from src.common.paths import DATA_ROOT, REPORT_ROOT
from src.common.fetch_util import urllib_json, curl_json

DATA_DIR = DATA_ROOT
REPORT_DIR = REPORT_ROOT
OUT_JSON = os.path.join(DATA_DIR, "index_valuation.json")
HISTORY_CSV = os.path.join(DATA_DIR, "valuation_history.csv")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"}
TODAY = datetime.date.today().strftime("%Y-%m-%d")

# 分位阈值（历史百分位，0~1）
PCT_BUBBLE = 0.90   # >90% 泡沫 / 点位极端高位
PCT_HIGH = 0.70     # 70~90% 高估 / 点位高位
PCT_MID_HI = 0.50   # 50~70% 合理偏高 / 点位中上
PCT_MID_LO = 0.30   # 30~50% 合理偏低 / 点位中下

# 点位分位档位（与估值五档一一对应，但措辞刻意不含"估值/低估/高估/泡沫"——
# 点位分位不含盈利增长，只回答"当前点位在本指数自身历史中的位置"，
# 2026-09-14 前沿用估值措辞，导致"合理偏低"被当成估值结论读，故拆成独立词表）
POS_BUCKETS = ["点位低位", "点位中下", "点位中上", "点位高位", "点位极端高位"]

# 指数清单
# bands: 点位辅助判定的四档下界 [泡沫, 高估, 合理, 低估]（价格 >= 对应值落入该档）
# danjuan: 蛋卷 index_code；None 表示蛋卷未收录 PE/PB（改用本仓库 K 线自算点位分位）
# basis: 主判据分位 pe / pb（高股息防御类指数盈利波动大，PE 分位失真，改用 PB）
INDEXES = [
    {"name": "上证指数", "danjuan": None,  "secid": "1.000001", "bands": [5000, 3800, 3200, 3000], "note": "蛋卷未收录 PE/PB，用本仓库 K 线自算点位分位"},
    {"name": "深成指",   "danjuan": "SZ399001", "secid": "0.399001", "bands": [18000, 15000, 11000, 10000], "basis": "pe", "note": ""},
    {"name": "创业板指", "danjuan": "SZ399006", "secid": "0.399006", "bands": [3800, 3000, 2200, 1900], "basis": "pe", "note": ""},
    {"name": "科创50",   "danjuan": "SH000688", "secid": "1.000688", "bands": [1800, 1400, 1000, 800], "basis": "pe", "note": "2020年高点1726，高估线偏激进"},
    {"name": "恒生指数", "danjuan": "HKHSI",    "secid": "100.HSI",  "bands": [32000, 26000, 20000, 18000], "basis": "pe", "note": ""},
    {"name": "恒生科技", "danjuan": "HKHSTECH", "secid": "124.HSTECH", "bands": [9000, 7000, 4500, 3500], "basis": "pe", "note": "2021年曾冲至11001"},
    {"name": "中证A500", "danjuan": None,  "secid": "1.000510", "note": "2024年9月新发；蛋卷未收录 PE/PB，改用点位分位（数据源：本仓库 K 线）"},
    {"name": "中证500",  "danjuan": "SH000905", "secid": "1.000905", "bands": [7500, 6000, 5000, 4500], "basis": "pe", "note": ""},
    {"name": "上证50",   "danjuan": "SH000016", "secid": "1.000016", "bands": [3800, 3400, 2800, 2200], "basis": "pe", "note": ""},
    {"name": "沪深300",  "danjuan": "SH000300", "secid": "1.000300", "bands": [5500, 4800, 3800, 3100], "basis": "pe", "note": ""},
    {"name": "中证1000", "danjuan": "SH000852", "secid": "1.000852", "bands": [8000, 6500, 5500, 4500], "basis": "pe", "note": ""},
    {"name": "中证2000", "danjuan": None,  "secid": "2.932000", "note": "蛋卷未收录 PE/PB，改用点位分位（数据源：本仓库 K 线）"},
    {"name": "北证50",   "danjuan": None,  "secid": "0.899050", "bands": [1500, 1200, 900, 600], "note": "蛋卷未收录 PE/PB，用本仓库 K 线自算点位分位"},
    {"name": "中证银行", "danjuan": "SZ399986", "secid": "0.399986", "bands": [8500, 7500, 6500, 5500], "basis": "pb", "note": "高股息防御板块，估值长期偏低，主判据用PB分位"},
    {"name": "红利指数", "danjuan": "SH000015", "secid": "1.000015", "bands": [4500, 3800, 3000, 2500], "basis": "pb", "note": "高股息策略，PE受盈利波动影响，主判据用PB分位"},
    {"name": "红利低波", "danjuan": "CSIH30269", "secid": "2.H30269", "bands": [11800, 10050, 8900, 8000], "basis": "pb", "note": "2026-09-14补入：红利低波用其自身指数(CSIH30269)。此前清单与引擎都缺它，引擎误用『红利指数』(SH000015)当代理，而它恰是红利家族里分位最高的一只，导致红利低波被误判泡沫区。分档按 2016-2026 收盘四分位取整（最高11947/75%10061/50%8914/25%8056）"},
    # ── 2026-09-14 补入：蛋卷已有 PE/PB 历史分位、但此前未接入的宽基与主要指数（东财 secid 已逐个实测） ──
    {"name": "上证180",   "danjuan": "SH000010",  "secid": "1.000010", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "中证100",   "danjuan": "SH000903",  "secid": "1.000903", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "深证100",   "danjuan": "SZ399330",  "secid": "0.399330", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "央视50",    "danjuan": "SZ399550",  "secid": "0.399550", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "基本面50",  "danjuan": "SH000925",  "secid": "1.000925", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "国证A指",   "danjuan": "SZ399317",  "secid": "0.399317", "basis": "pe", "note": "2026-09-14补入：全市场加权"},
    {"name": "基本面60",  "danjuan": "SZ399701",  "secid": "0.399701", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "基本面120", "danjuan": "SZ399702",  "secid": "0.399702", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "300价值",   "danjuan": "SH000919",  "secid": "1.000919", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "50AH优选",  "danjuan": "SH000170",  "secid": "1.000170", "basis": "pe", "note": "2026-09-14补入：A+H 两地优选"},
    {"name": "500低波",   "danjuan": "CSI930782", "secid": "2.930782", "basis": "pb", "note": "2026-09-14补入"},
    {"name": "中证红利",  "danjuan": "SH000922",  "secid": "1.000922", "basis": "pb", "note": "2026-09-14补入：高股息，主判据用PB分位"},
    {"name": "深证红利",  "danjuan": "SZ399324",  "secid": "0.399324", "basis": "pb", "note": "2026-09-14补入：高股息，主判据用PB分位"},
    {"name": "国企指数",  "danjuan": "HKHSCEI",   "secid": "100.HSCEI", "basis": "pe", "note": "2026-09-14补入：港股中资大盘"},
    {"name": "纳指100",   "danjuan": "NDX",       "secid": "100.NDX",   "basis": "pe", "note": "2026-09-14补入"},
    {"name": "标普500",   "danjuan": "SP500",     "secid": "100.SPX",   "basis": "pe", "note": "2026-09-14补入"},
    {"name": "德国DAX",   "danjuan": "GDAXI",     "secid": "100.GDAXI", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "MSCI中国",  "danjuan": "CSI716567", "secid": "2.716567",  "basis": "pe", "note": "2026-09-14补入"},
    {"name": "全指信息",  "danjuan": "SH000993",  "secid": "1.000993", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "全指医药",  "danjuan": "SH000991",  "secid": "1.000991", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "中证医疗",  "danjuan": "SZ399989",  "secid": "0.399989", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "主要消费",  "danjuan": "SH000932",  "secid": "1.000932", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "全指可选",  "danjuan": "SH000989",  "secid": "1.000989", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "科技龙头",  "danjuan": "CSI931087", "secid": "2.931087", "basis": "pe", "note": "2026-09-14补入"},
    # ── 2026-09-14 补入（第二批）：蛋卷池最后 13 只。东财无点位的 secid 置 None，只出 PE/PB 分位、点位列显示 "-" ──
    {"name": "医药100",   "danjuan": "SH000978",  "secid": "1.000978", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "红利成长LV", "danjuan": "CSI931157", "secid": "2.931157", "basis": "pb", "note": "2026-09-14补入"},
    {"name": "300红利LV", "danjuan": "CSI930740", "secid": "2.930740", "basis": "pb", "note": "2026-09-14补入"},
    {"name": "东证竞争",  "danjuan": "CSI931142", "secid": "2.931142", "basis": "pe", "note": "2026-09-14补入"},
    {"name": "中概互联50", "danjuan": "CSIH30533", "secid": None, "basis": "pe", "note": "2026-09-14补入；东财无该指数点位，仅出 PE/PB 分位"},
    {"name": "中国互联",  "danjuan": "CSIH11136", "secid": None, "basis": "pe", "note": "2026-09-14补入；东财无该指数点位，仅出 PE/PB 分位"},
    {"name": "新经济",    "danjuan": "HKHSSCNE",  "secid": None, "basis": "pe", "note": "2026-09-14补入；东财无该指数点位，仅出 PE/PB 分位"},
    {"name": "香港大盘",  "danjuan": "HSFML25",   "secid": None, "basis": "pe", "note": "2026-09-14补入；东财无该指数点位，仅出 PE/PB 分位"},
    {"name": "香港中小",  "danjuan": "SPHCMSHP",  "secid": None, "basis": "pe", "note": "2026-09-14补入；东财无该指数点位，仅出 PE/PB 分位"},
    {"name": "MSCI印度",  "danjuan": "935600",    "secid": None, "basis": "pe", "note": "2026-09-14补入；东财无该指数点位，仅出 PE/PB 分位"},
    {"name": "标普红利",  "danjuan": "CSPSADRP",  "secid": None, "basis": "pb", "note": "2026-09-14补入；东财无该指数点位，仅出 PE/PB 分位"},
    {"name": "标普价值",  "danjuan": "SPACEVCP",  "secid": None, "basis": "pe", "note": "2026-09-14补入；东财无该指数点位，仅出 PE/PB 分位"},
    {"name": "标普质量",  "danjuan": "SPCQVCP",   "secid": None, "basis": "pe", "note": "2026-09-14补入；东财无该指数点位，仅出 PE/PB 分位"},
]


def get(url, tries=3, sleep_s=1):
    """优先 urllib；东财 push2 对 Python urllib 偶发掐断（限流/TLS 指纹）时改走系统 curl。

    2026-09-14：指数扩到 40 只后 urllib 被掐概率明显上升（现象是整片点位为空、
    退化成 TickFlow 兜底只剩十几只），加 curl 兜底后点位才稳定拿全。
    """
    d = urllib_json(url, headers=HEADERS, timeout=12, tries=tries, sleep_s=sleep_s)
    if d is not None:
        return d
    return curl_json(url, timeout=12, ua=HEADERS["User-Agent"], tries=2)


# ---------- 1. 蛋卷估值 ----------
def fetch_danjuan():
    """返回 {index_code: {pe, pb, pe_pct, pb_pct, yeild, date, eva_type}}"""
    d = get("https://danjuanfunds.com/djapi/index_eva/dj")
    if not d or not d.get("data") or not d["data"].get("items"):
        return {}
    out = {}
    for it in d["data"]["items"]:
        out[it["index_code"]] = {
            "pe": it.get("pe", 0) or 0,
            "pb": it.get("pb", 0) or 0,
            "pe_pct": it.get("pe_percentile", 0) or 0,
            "pb_pct": it.get("pb_percentile", 0) or 0,
            "yeild": it.get("yeild", 0) or 0,
            "date": it.get("date", ""),
            "eva_type": it.get("eva_type", ""),
        }
    return out


# ---------- 2. 东财实时点位（主源） ----------
def fetch_quotes():
    """返回 {code: {price, chg_pct}}，code 为 f12 代码（如 000300 / HSI）
    加时间戳破CDN缓存，校验返回数据合理性（昨收偏差<10%视为有效）。
    2026-09-14：指数扩到 40 只后单批请求偶发整体失败，改为每 20 只一片，单片失败不影响其余。"""
    secids = [x["secid"] for x in INDEXES if x.get("secid")]
    out = {}
    for i in range(0, len(secids), 20):
        batch = secids[i:i + 20]
        url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?secids=%s"
               "&fields=f2,f3,f4,f12,f14&_=%d") % (",".join(batch), int(time.time() * 1000))
        d = get(url)
        if not d or not d.get("data") or not d["data"].get("diff"):
            print("  [warn] 东财点位分片 %d~%d 无返回" % (i + 1, i + len(batch)))
            continue
        diff = d["data"]["diff"]
        if isinstance(diff, dict):
            diff = [diff]
        for it in diff:
            code = it.get("f12", "")
            f2 = it.get("f2")  # 当前价 * 100
            f3 = it.get("f3")  # 涨跌幅 * 100
            f4 = it.get("f4")  # 涨跌额 * 100
            if not f2:
                continue
            price = f2 / 100
            chg_pct = (f3 or 0) / 100
            # 校验：用涨跌额反推昨收，偏差超10%视为缓存脏数据
            if f4 and f4 != 0:
                prev_close = price - f4 / 100
                if prev_close > 0 and abs(f4 / 100) / prev_close > 0.10:
                    print("  ⚠️ %s 数据异常（涨跌%.2f%%超10%%），跳过" % (code, chg_pct))
                    continue
            out[code] = {"price": price, "chg_pct": chg_pct}
    return out


# ---------- 2b. TickFlow点位（东财失败时首选兜底） ----------
def fetch_quotes_tickflow():
    """东财断连时用TickFlow兜底（免费版，稳定不限流）"""
    try:
        from src.common.tickflow_source import fetch_index_klines
        data = fetch_index_klines()
        if data:
            return data
    except Exception as e:
        print("  [warn] TickFlow 兜底失败: %s" % e)
    return {}


# ---------- 2c. 新浪实时点位（TickFlow也失败时兜底） ----------
def fetch_quotes_sina():
    """东财断连时用新浪兜底。A股 s_ 前缀（名称,点位,涨跌额,涨跌%），港股 rt_hk 前缀"""
    # 每个指数的新浪代码（key 与东财 f12 代码一致）
    sina_codes = {
        "000001": "s_sh000001", "399001": "s_sz399001", "399006": "s_sz399006",
        "000688": "s_sh000688", "000300": "s_sh000300", "000905": "s_sh000905",
        "000016": "s_sh000016", "000852": "s_sh000852", "399986": "s_sz399986",
        "000015": "s_sh000015", "000510": "s_sh000510", "899050": "s_bj899050",
        "HSI": "rt_hkHSI", "HSTECH": "rt_hkHSTECH",
    }
    out = {}
    for code, sname in sina_codes.items():
        try:
            req = urllib.request.Request(
                "https://hq.sinajs.cn/list=%s" % sname,
                headers={"User-Agent": HEADERS["User-Agent"], "Referer": "https://finance.sina.com.cn"})
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read().decode("gbk", errors="replace")
            if "hq_str" not in raw or '"' not in raw:
                continue
            fields = raw.split('"')[1].split(",")
            if len(fields) < 4 or not fields[0]:
                continue
            if sname.startswith("rt_"):  # 港股: 现价在 idx2, 涨跌额 idx7, 涨跌幅 idx8
                price = float(fields[2]) if fields[2] else 0
                chg = float(fields[8]) if len(fields) > 8 and fields[8] else 0
            else:  # A股: 名称,点位,涨跌额,涨跌幅
                price = float(fields[1]) if fields[1] else 0
                chg = float(fields[3]) if len(fields) > 3 and fields[3] else 0
            if price > 0:
                out[code] = {"price": price, "chg_pct": chg}
        except Exception:
            continue
    return out


# ---------- 3. 估值判定 ----------
def judge_by_pct(pct):
    """按历史分位判定估值档位（蛋卷 PE/PB 分位专用）"""
    if pct >= PCT_BUBBLE:
        return "泡沫"
    if pct >= PCT_HIGH:
        return "高估"
    if pct >= PCT_MID_HI:
        return "合理偏高"
    if pct >= PCT_MID_LO:
        return "合理偏低"
    return "低估"


def judge_by_price_pct(pct):
    """按点位分位判定档位（返回 POS_BUCKETS 中的点位措辞，非估值措辞）

    阈值与 judge_by_pct 完全相同，只是不给结论、只报位置，
    避免"合理偏低/高估"被误读成估值判断。
    """
    if pct >= PCT_BUBBLE:
        return "点位极端高位"
    if pct >= PCT_HIGH:
        return "点位高位"
    if pct >= PCT_MID_HI:
        return "点位中上"
    if pct >= PCT_MID_LO:
        return "点位中下"
    return "点位低位"


def price_percentile(name):
    """蛋卷无估值时的兜底：用本仓库 K 线自算点位分位，返回 (分位0~100, 窗口说明) 或 None。

    2026-09-14 起替代原手写档位线（bands）——分档阈值不再手拍，改由数据决定。
    注意：点位分位 ≠ 估值分位，它不含盈利增长，只回答"当前点位在本指数自身历史中的位置"，
    对成长期指数会偏严；报告里以 state_basis 明确标注，不与 PE/PB 分位混用。
    """
    for d in (os.path.join(DATA_DIR, "kline_10y"), os.path.join(DATA_DIR, "kline_full")):
        p = os.path.join(d, name + ".csv")
        if not os.path.exists(p):
            continue
        import csv as _csv
        rows = list(_csv.DictReader(open(p, encoding="utf-8-sig")))
        closes = [float(r["收盘"]) for r in rows if r.get("收盘")]
        if len(closes) < 120:
            return None
        pct = bisect.bisect_left(sorted(closes), closes[-1]) / len(closes)  # 0~1，与蛋卷分位同口径
        return pct, "自 %s，%.1f 年" % (rows[0]["日期"], len(closes) / 244.0)
    return None


def judge_by_bands(price, bands):
    """按点位区间判定的最后兜底（同样用点位措辞，不给估值结论）"""
    b_foam, b_high, b_mid, b_low = bands
    if price >= b_foam:
        return "点位极端高位"
    if price >= b_high:
        return "点位高位"
    if price >= b_mid:
        return "点位中上"
    return "点位低位"


STATE_EMOJI = {"泡沫": "🔴", "高估": "🟠", "合理偏高": "🟡", "合理偏低": "🟢", "合理": "✅", "低估": "🟢"}
# 点位分位用 📍 前缀统一标记，一眼区分"这是位置读数、不是估值结论"
STATE_EMOJI.update({"点位极端高位": "📍🔺", "点位高位": "📍🔼",
                    "点位中上": "📍▪️", "点位中下": "📍▫️", "点位低位": "📍🔽"})

# 蛋卷 eva_type → 档位（数据源自身的分级信号，综合判据之一）
EVA_TYPE_MAP = {"low": "低估", "mid": "合理", "high": "高估"}

# 档位保守度顺序（取最保守 = 估值最高）
RANK = ["低估", "合理偏低", "合理", "合理偏高", "高估", "泡沫"]


def build_rows(danjuan, quotes):
    rows = []
    for idx in INDEXES:
        name, secid = idx["name"], idx["secid"]
        # 东财返回键是 f12 代码（如 000300 / HSI），secid 形如 1.000300 / 100.HSI
        q = quotes.get(secid.split(".")[-1], {}) if secid else {}
        ev = danjuan.get(idx["danjuan"], {}) if idx["danjuan"] else {}

        row = {"name": name, "price": q.get("price"), "chg_pct": q.get("chg_pct"),
               "note": idx["note"]}
        # 估值状态：蛋卷 PE/PB 分位 > 本仓库自算点位分位 > 手写点位区间
        if ev and (ev["pe_pct"] > 0 or ev["pb_pct"] > 0):
            row["pe"] = round(ev["pe"], 2) if ev["pe"] else None
            row["pb"] = round(ev["pb"], 3) if ev["pb"] else None
            row["pe_pct"] = round(ev["pe_pct"], 3)
            row["pb_pct"] = round(ev["pb_pct"], 3)
            row["yeild"] = round(ev["yeild"] * 100, 2)
            row["eva_date"] = ev.get("date", "")
            basis = idx.get("basis", "pe")
            if basis == "pb":
                # 高股息指数：PB 分位主判据（PE 受盈利波动失真，不参与综合）
                main_pct = ev["pb_pct"] if ev["pb_pct"] > 0 else ev["pe_pct"]
                row["state"] = judge_by_pct(main_pct)
                row["state_basis"] = "PB分位"
            else:
                # 综合判据：PE/PB 分位取较保守者（蛋卷 eva_type 与分位常矛盾，不参与）
                signals = []
                if ev["pe_pct"] > 0:
                    signals.append(("PE分位", judge_by_pct(ev["pe_pct"])))
                if ev["pb_pct"] > 0:
                    signals.append(("PB分位", judge_by_pct(ev["pb_pct"])))
                signals.sort(key=lambda s: RANK.index(s[1]), reverse=True)
                row["state"] = signals[0][1]
                row["state_basis"] = "综合" if len(signals) > 1 else signals[0][0]
        else:
            # 蛋卷无估值：改用本仓库 K 线自算点位分位（数据驱动，替代原手写档位线）
            # 走点位措辞，不套用估值档位——口径不同、不含盈利增长，不得混读
            pp = price_percentile(name)
            if pp:
                row["state"] = judge_by_price_pct(pp[0])
                row["state_basis"] = "点位分位 %.0f%%（%s）" % (pp[0] * 100, pp[1])
            elif idx.get("bands") and q.get("price"):
                row["state"] = judge_by_bands(q["price"], idx["bands"])
                row["state_basis"] = "点位区间"
            else:
                row["state"] = "未收录"
                row["state_basis"] = "无估值数据"
            row["eva_date"] = ""
        rows.append(row)
    return rows


# ---------- 4. 估值历史追加（同日重跑覆盖当日行，幂等） ----------
def append_history(rows):
    import csv
    header = ["日期", "指数", "点位", "涨跌%", "PE", "PE分位", "PB", "PB分位", "股息率%", "估值状态", "判据"]
    kept = []
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, encoding="utf-8-sig") as f:
            r = csv.reader(f)
            first = next(r, None)
            if first:
                header = first
            kept = [row for row in r if row and row[0] != TODAY]
    with open(HISTORY_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(kept)
        written = 0
        for r in rows:
            w.writerow([TODAY, r["name"], r.get("price") or "", r.get("chg_pct") or "",
                        r.get("pe") or "", r.get("pe_pct") or "", r.get("pb") or "",
                        r.get("pb_pct") or "", r.get("yeild") or "",
                        r["state"], r["state_basis"]])
            written += 1
    return written


# ---------- 5. 生成报告 ----------
def gen_report(rows):
    lines = []
    lines.append("# 📊 A股指数估值参考（数据驱动版）")
    lines.append("")
    lines.append("> 生成时间：%s　｜　数据源：蛋卷基金估值接口 + 东方财富实时点位" % TODAY)
    lines.append("> **两把不同的尺子，不要混读：**")
    lines.append("> ① 估值尺（有 PE/PB 的指数）：蛋卷 PE/PB 历史分位，两把取较保守档 → >90%泡沫 / 70~90%高估 / 50~70%合理偏高 / 30~50%合理偏低 / <30%低估；高股息指数用 PB 分位（PE 受盈利波动失真）。")
    lines.append("> ② 点位尺（蛋卷未收录 PE/PB 的指数）：本仓库 10 年 K 线自算的点位分位 → >90%点位极端高位 / 70~90%点位高位 / 50~70%点位中上 / 30~50%点位中下 / <30%点位低位。**点位尺不含盈利增长，只说明“当前点位在它自己历史里的位置”，不构成贵或便宜的结论。**")
    lines.append("")
    lines.append("## 估值总览")
    lines.append("")
    lines.append("| 指数 | 点位 | 涨跌% | PE | PE分位 | PB | PB分位 | 股息率% | 状态 | 判据（哪把尺） |")
    lines.append("|------|------|-------|----|---------|----|---------|---------|----------|------|")
    for r in rows:
        price = ("%.0f" % r["price"]) if r["price"] else "-"
        chg = ("%+.2f%%" % r["chg_pct"]) if r["chg_pct"] is not None else "-"
        pe = ("%.2f" % r["pe"]) if r.get("pe") else "-"
        pb = ("%.3f" % r["pb"]) if r.get("pb") else "-"
        pe_pct = ("%.1f%%" % (r["pe_pct"] * 100)) if r.get("pe_pct") else "-"
        pb_pct = ("%.1f%%" % (r["pb_pct"] * 100)) if r.get("pb_pct") else "-"
        yeild = ("%.2f" % r["yeild"]) if r.get("yeild") else "-"
        state = "%s %s" % (STATE_EMOJI.get(r["state"], "❓"), r["state"])
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["name"], price, chg, pe, pe_pct, pb, pb_pct, yeild, state, r["state_basis"]))
    lines.append("")
    lines.append("## 分层建议（估值尺）")
    lines.append("")
    lines.append("> 本节的档位来自 PE/PB 分位，可以直接作为买卖参考；点位尺的指数不在本节。")
    lines.append("")
    groups = {"低估": [], "合理偏低": [], "合理": [], "合理偏高": [], "高估": [], "泡沫": [], "未收录": []}
    for r in rows:
        groups.setdefault(r["state"], []).append(r["name"])
    adv = [
        ("🔴 泡沫区（优先减仓）", ["泡沫"], "大幅减仓或清仓，不追高"),
        ("🟠 高估区（逐步兑现）", ["高估"], "逐步减仓，锁定利润，回撤到合理区再考虑"),
        ("🟡 合理偏高（持有观察）", ["合理偏高"], "持有为主，不加仓，等回到合理中枢"),
        ("✅ 合理区（持有/定投）", ["合理"], "按计划持有或定投，不追高"),
        ("🟢 低估区（分批建仓）", ["合理偏低", "低估"], "可分批建仓或定投，越跌越买需设仓位上限"),
    ]
    for title, keys, action in adv:
        names = []
        for k in keys:
            names.extend(groups.get(k, []))
        if names:
            lines.append("**%s**：%s" % (title, "、".join(names)))
            lines.append("- 动作：%s" % action)
            lines.append("")
    # 点位尺单列：位置读数不等于买卖信号
    pos_items = []
    for k in reversed(POS_BUCKETS):  # 从高到低排
        for n in groups.get(k, []):
            pos_items.append("%s（%s）" % (n, k))
    if pos_items:
        lines.append("## 分层建议（点位尺 · 非估值结论）")
        lines.append("")
        lines.append("**📍 蛋卷无 PE/PB，只有点位分位**：%s" % "、".join(pos_items))
        lines.append("- 这组指数**没有估值分位**，上面那套减仓/建仓动作不适用；点位分位只回答"
                     "『当前点位在它自己历史里的位置』，不含盈利增长。")
        lines.append("- 要用它们做买卖决策，需另找该指数的 PE/PB（或基本面口径），不能拿位置读数充当估值结论。")
        lines.append("")
    if groups.get("未收录"):
        lines.append("**❓ 无估值数据（仅参考点位）**：%s" % "、".join(groups["未收录"]))
        lines.append("- 这些指数蛋卷未收录估值，无法给出分位结论，请结合点位区间和基本面研判。")
        lines.append("")
    lines.append("## 备注与风险提示")
    lines.append("")
    for r in rows:
        if r["note"]:
            lines.append("- %s：%s" % (r["name"], r["note"]))
    lines.append("- 估值分位是历史相对位置，不代表绝对贵贱；新发指数（如中证A500）历史短，分位参考意义有限。")
    lines.append("- **点位分位（📍）不是估值**：它只把当前点位放进自身历史排位，不含盈利增长、不含分红，"
                 "长期增长型指数会天然排在高位；报告里已与 PE/PB 档位分开列示，请勿混读。")
    lines.append("- 估值仅供参考，需结合宏观经济、政策面、资金面综合判断。")
    lines.append("- 本报告数据来自公开接口，仅供个人研究参考，不构成投资建议。")
    lines.append("")
    lines.append("---")
    lines.append("*由 data/fetch_index_valuation.py 自动生成*")
    return "\n".join(lines)


def main():
    print("== 拉取指数估值数据 ==")
    danjuan = fetch_danjuan()
    quotes = fetch_quotes()
    src = "东财"
    if not quotes:
        # 东财失败，优先用TickFlow（稳定不限流）
        quotes = fetch_quotes_tickflow()
        src = "TickFlow"
    if not quotes:
        # TickFlow也失败，用新浪兜底
        quotes = fetch_quotes_sina()
        src = "新浪"
        # 新浪无中证2000，单独用东财补一次
        q2 = fetch_quotes()
        if q2.get("932000"):
            quotes["932000"] = q2["932000"]
    print("  蛋卷估值指数: %d 个, 点位指数(%s): %d 个" % (len(danjuan), src, len(quotes)))
    if not danjuan and not quotes:
        print("  两个数据源均失败，请检查网络后重试")
        sys.exit(1)

    rows = build_rows(danjuan, quotes)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"date": TODAY, "rows": rows}, f, ensure_ascii=False, indent=2)
    print("  已写入 index_valuation.json")

    if "--json" not in sys.argv:
        os.makedirs(REPORT_DIR, exist_ok=True)
        report = gen_report(rows)
        report_path = os.path.join(REPORT_DIR, "指数估值参考_%s.md" % TODAY.replace("-", ""))
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
        print("  报告已生成: %s" % report_path)

    n = append_history(rows)
    print("  估值历史追加: %d 行（同日重跑覆盖当日行）" % n)

    # 控制台摘要
    for r in rows:
        price = ("%.0f" % r["price"]) if r["price"] else "?"
        print("  %-6s %8s %8s  %s" % (r["name"], price, r["state"], r["state_basis"]))


if __name__ == "__main__":
    main()
