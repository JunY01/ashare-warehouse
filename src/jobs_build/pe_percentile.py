# -*- coding: utf-8 -*-
"""宽基指数 PE/PB 历史分位分析（蛋卷历史估值接口）

给未来亚亚：这是"估值分位体系"的唯一数据源与唯一分析入口，回答三件事：
  1. 各指数当前 PE/PB 处自身历史什么分位（10年周频，蛋卷口径）
  2. 该看 PE 还是 PB（大盘看PE、防御类银行/红利看PB、成长看PB）
  3. 分位对未来 1 年收益的真实预测力（分档中位收益/正利率）

数据源：
  https://danjuanfunds.com/djapi/index_eva/pe_history/{code}?day=all
  https://danjuanfunds.com/djapi/index_eva/pb_history/{code}?day=all
  指数清单复用 fetch_index_valuation.INDEXES（不重复维护 danjuan 代码）

样本限制（2026-09-10 已穷尽验证，不可突破）：
  · 蛋卷 day=all 即上限：516 期 = 10 年周频（2016-09 起），无 15y/20y/日频参数。
    10 年之前的 PE 序列拿不到。
  · 曾试：中证官网 API（鉴权 500/404）、理杏仁（接口失效 404）、
    且慢/好买（无公开接口）、反推外推（盈利序列本身不平滑，不可行）。
  · 后果：漏掉 2007（沪深300 PE~50x）、2008（~10x）、2013（~9x）三个极端。
    "泡沫"刻度被低估，"便宜"刻度被高估——当前看着便宜的，可能还不够便宜。
  · 自算 20 年点位分位对照：10 年窗口普遍让指数"看着更便宜"
    （恒生-22pp/上证50-16pp/沪深300-8pp），即分位结论系统性偏乐观。
  · 缓解：valuation_history.csv 每日落库攒自己的序列（当前仅 14 天）；
    每日 PE/PB 快照从现在起天天存，3 年后即有自己的一手 750 天数据。

产物：
  data/cache/pe_history/pe_history_all.json  -- PE 原始周频序列（git忽略）
  data/cache/pe_history/pb_history_all.json  -- PB 原始周频序列（git忽略）
  reports/宽基估值分位体系_YYYYMMDD.md        -- 分析报告

用法:
  python pe_percentile.py            # 抓取+分析+出报告（联网）
  python pe_percentile.py --offline  # 用本地缓存分析（不联网）

纯标准库。局限：蛋卷窗口仅10年（缺2008/2015极端样本）；周频样本高度重叠，
报告中 n 非独立样本数，独立行情段约 n/50，结论须按此打折。
"""
import bisect
import csv
import datetime
import json
import os
import statistics as st
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from src.common.paths import DATA_ROOT, REPORT_ROOT  # noqa: E402
from src.common.fetch_util import urllib_json  # noqa: E402

CACHE_DIR = os.path.join(DATA_ROOT, "cache", "pe_history")
PE_CACHE = os.path.join(CACHE_DIR, "pe_history_all.json")
PB_CACHE = os.path.join(CACHE_DIR, "pb_history_all.json")
KLINE_DIR = os.path.join(DATA_ROOT, "kline_full")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"}

# 指数中文名 -> kline_full 的 CSV 名（用于算未来收益）；缺失则该指数跳过收益统计
KLINE_NAME = {
    "沪深300": "沪深300", "中证500": "中证500", "上证50": "上证50", "创业板指": "创业板指",
    "科创50": "科创50", "深成指": "深成指", "中证1000": "中证1000", "恒生指数": "恒生指数",
    "恒生科技": "恒生科技", "中证银行": "中证银行", "红利指数": "红利指数",
}
FWD = 250  # 未来收益窗口（交易日）
BUCKETS = [(0, 0.20, "<20%"), (0.20, 0.40, "20-40%"), (0.40, 0.60, "40-60%"),
           (0.60, 0.80, "60-80%"), (0.80, 1.01, ">80%")]

# 结论与解读（脚本每次生成报告时附带，避免只剩裸表）
INTERPRETATION = """
## 五、怎么用（可执行结论）

### 1. 该看 PE 还是 PB

| 指数类型 | 主判据 | 原因 |
|---|---|---|
| 绝大多数宽基（沪深300/中证500/上证50/深成指/创业板/科创50/恒生指数） | **PE** | 盈利相对平稳 |
| **中证银行 / 红利指数**（高股息防御） | **PB** | 盈利周期波动大，坏年份 PE 被动抬高，PE 分位失真 |
| **恒生科技**（成长） | **PB 更强** | 盈利波动大，PB 反映资产定价 |

### 2. 分位信号的强弱（从上面两表读出）

- **分位 >80%：不买、可减**。证据：上证50 PE>80% → 中位 -15%/正利率仅 3%（跨 2017/2018/2020/2021/2025/2026 六时段，最可信）；恒生科技 PE>80% → -25%/8%；中证1000 PB>80% → -18%/5%。
- **分位 <20%：最有把握的买点区**。证据：恒生科技 PB<20% → +38%/94%（但见下方折扣）；上证50 PB<20% → +17%/96%；创业板 PB<20% → +28%/97%。
- **分位 20-80%：估值几乎不提供方向**。40-60% 档各指数收益紊乱（沪深300 +16%、创业板 -17%、恒生 -7%），**此时不该拿分位当买卖理由**，改按趋势/纪律。

### 3. 银行/红利的特殊处理

银行 PE 分位 92%（看着极危险），但 PB 各档未来收益都是 -1%~+1% 量级——**PB 对银行基本无预测力**。红利同理。
→ 结论：**银行/红利是"防御腿"，持有依据是股息率与低波动，不是估值分位。不要因为它们 PE 分位高就当"高估要卖"。**

### 4. 必须打的折扣（诚实说明）

- 恒生科技 PB<20% 的 +38%/94% **基本只对应一轮行情**（64 个样本中 62 个落在 2022-2024），不能当稳定胜率用。
- 所有 n 都是重叠样本，独立行情段约 n/50。
- 窗口仅 10 年，缺 2008/2015 极端样本，"最贵"被低估。

### 5. 对当前持仓的直接含义

| 持仓 | 对应指数 | 该看 | 读数 | 含义 |
|---|---|---|---|---|
| 南方中证500 | 中证500 | PE | 80% | **贵** |
| 易方达科创50 | 科创50 | PE | 80% | 贵（仓位极小） |
| 广发创业板E | 创业板指 | PE | 33% | 不贵 |
| 华安恒生科技 | 恒生科技 | PB | **19%** | **全组合最便宜** |
| 易方达银行 | 中证银行 | PB | 46% | 中性，无择时信号 |
| 华泰柏瑞红利 | 红利指数 | PB | 65% | 偏高，但 PB 对红利预测力弱 |
| 广发中证A500 | 中证A500 | — | 蛋卷未收录 | **无估值依据**，只能靠点位/规则 |
"""


def index_list():
    """蛋卷指数清单：INDEXES 的名字优先，再用蛋卷在收录清单补缺。

    2026-09-16 改动：原先只取 INDEXES 里带 danjuan 的 49 个（那是估值报告的关注清单），
    结果蛋卷**已收录但报告没关注**的 14 个行业/主题指数拿不到历史（中证白酒/军工/证券/
    煤炭/电子/地产/传媒/新能源车/环保/食品/养老/5G/TMT50/消费红利）——而"按行业做历史
    验证"正需要这些原料。现以蛋卷在收录清单为全集，行业指数一并入库。

    名字必须让 INDEXES 优先：蛋卷对同一代码的名字与本地不同（蛋卷"创业板" vs 本地"创业板指"），
    而 holding_advice 是**按名字**查序列的，用蛋卷名会让引擎查不到自己持仓的指数。
    """
    from src.jobs_build.fetch_index_valuation import INDEXES
    from src.jobs_build.fetch_sector_valuation import fetch_danjuan
    out = {}
    for x in INDEXES:
        if x.get("danjuan"):
            out[x["danjuan"]] = x["name"]
    for it in fetch_danjuan():
        if it.get("code"):
            out.setdefault(it["code"], it["name"])
    return list(out.items())


def fetch_history(kind):
    """kind='pe'|'pb'，返回 {code: {name, series}}"""
    out = {}
    for code, name in index_list():
        url = "https://danjuanfunds.com/djapi/index_eva/%s_history/%s?day=all" % (kind, code)
        d = urllib_json(url, headers=HEADERS, timeout=20, tries=2)
        g = (d or {}).get("data", {}).get("index_eva_%s_growths" % kind)
        if not g:
            print("  %-10s 无 %s 历史" % (name, kind.upper()))
            continue
        out[code] = {"name": name, "series": g}
        vals = [x.get(kind) for x in g if x.get(kind)]
        print("  %-10s %s n=%-4d 最低%.2f 最高%.2f 最新%.2f" % (
            name, kind.upper(), len(g), min(vals), max(vals), vals[-1]))
        time.sleep(1.2)
    return out


def pct_of(series, v):
    s = sorted(series)
    return bisect.bisect_left(s, v) / len(s)


def multi_window_pct(series, cur, years=(3, 5, 7, 10)):
    """自算多窗口分位：同一估值序列在 3/5/7/10 年窗口下的分位。

    给未来亚亚：窗口选择会显著改变结论（实测差 0~42pp），单一窗口不可全信。
    - 短窗口（3年）反映"当前周期内的相对位置"
    - 长窗口（10年）会被早期极值拉偏（如银行/红利 2016-2018 的极低估值）
    判读原则：多窗口一致 → 结论可靠；分歧大 → 处在周期切换，需谨慎。
    """
    out = []
    for y in years:
        w = int(52 * y)
        sub = series[-w:] if len(series) >= w else series
        out.append(bisect.bisect_left(sorted(sub), cur) / len(sub) * 100)
    return out


def window_table(hist, key):
    """多窗口分位表：暴露窗口敏感性"""
    lines = ["| 指数 | 3年 | 5年 | 7年 | 10年 | 最大差异 | 判读 |",
             "|---|---|---|---|---|---|---|"]
    for code, name in index_list():
        ser = [x[key] for x in hist.get(code, {}).get("series", []) if x.get(key)]
        if len(ser) < 60:
            continue
        cur = ser[-1]
        ws = multi_window_pct(ser, cur)
        spread = max(ws) - min(ws)
        if spread <= 15:
            verdict = "一致"
        elif spread <= 25:
            verdict = "略有分歧"
        else:
            verdict = "**分歧大（周期切换）**"
        lines.append("| %s | %s | **%.0fpp** | %s |" % (
            name, " | ".join("%.0f%%" % x for x in ws), spread, verdict))
    return lines


def kline_close(name):
    path = os.path.join(KLINE_DIR, name + ".csv")
    if not os.path.exists(path):
        return [], []
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    return [r["日期"] for r in rows], [float(r["收盘"]) for r in rows]


def current_table(pe, pb):
    """当前 PE/PB 及分位"""
    lines = []
    for code, name in index_list():
        pes = [x["pe"] for x in pe.get(code, {}).get("series", []) if x.get("pe")]
        pbs = [x["pb"] for x in pb.get(code, {}).get("series", []) if x.get("pb")]
        if not pes:
            continue
        cpe, cpb = pes[-1], (pbs[-1] if pbs else None)
        pe_p = pct_of(pes, cpe) * 100
        pb_p = pct_of(pbs, cpb) * 100 if pbs else None
        lines.append("| %s | %.2f | **%.0f%%** | %.2f–%.2f | %s | %s | %s |" % (
            name, cpe, pe_p, min(pes), max(pes),
            "%.2f" % cpb if cpb else "-",
            "**%.0f%%**" % pb_p if pb_p is not None else "-",
            ("%.2f–%.2f" % (min(pbs), max(pbs))) if pbs else "-"))
    return lines


def fwd_table(hist, basis, pe, pb):
    """按分位分档算未来 FWD 日收益"""
    key = basis.lower()
    out = ["| 指数 | %s |" % " | ".join(b[2] for b in BUCKETS),
           "|" + "---|" * (len(BUCKETS) + 1)]
    for code, name in index_list():
        ser = [(x["ts"], x[key]) for x in hist.get(code, {}).get("series", []) if x.get(key)]
        if not ser or name not in KLINE_NAME:
            continue
        dates, closes = kline_close(KLINE_NAME[name])
        if not dates:
            continue
        allv = [v for _, v in ser]
        cells = []
        for lo, hi, _ in BUCKETS:
            rs = []
            for ts, v in ser:
                if not (lo <= pct_of(allv, v) < hi):
                    continue
                dt = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                i = bisect.bisect_left(dates, dt)
                if i >= len(dates) or i + FWD >= len(dates):
                    continue
                rs.append(closes[i + FWD] / closes[i] - 1)
            if rs:
                med = st.median(rs) * 100
                wr = 100 * sum(1 for x in rs if x > 0) / len(rs)
                cells.append("%+.0f%%/%.0f%%(%d)" % (med, wr, len(rs)))
            else:
                cells.append("--")
        out.append("| %s | %s |" % (name, " | ".join(cells)))
    return out


def main():
    offline = "--offline" in sys.argv
    os.makedirs(CACHE_DIR, exist_ok=True)
    if offline:
        pe = json.load(open(PE_CACHE, encoding="utf-8"))
        pb = json.load(open(PB_CACHE, encoding="utf-8"))
        print("== 离线模式：读本地缓存 ==")
    else:
        print("== 抓取 PE 历史 ==")
        pe = fetch_history("pe")
        print("== 抓取 PB 历史 ==")
        pb = fetch_history("pb")
        json.dump(pe, open(PE_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        json.dump(pb, open(PB_CACHE, "w", encoding="utf-8"), ensure_ascii=False)

    today = datetime.date.today().strftime("%Y%m%d")
    rep = os.path.join(REPORT_ROOT, "宽基估值分位体系_%s.md" % today)
    span = {}
    for code, d in pe.items():
        s = d["series"]
        span[d["name"]] = (
            datetime.datetime.fromtimestamp(s[0]["ts"] / 1000).strftime("%Y-%m-%d"),
            datetime.datetime.fromtimestamp(s[-1]["ts"] / 1000).strftime("%Y-%m-%d"),
            len(s))

    with open(rep, "w", encoding="utf-8") as f:
        f.write("# 宽基指数 PE/PB 分位体系 %s\n\n" % datetime.date.today())
        f.write("> 数据源：蛋卷历史估值接口（10年周频）｜样本期见下表｜仅个人研究\n\n")
        f.write("## 一、样本期\n\n| 指数 | 起 | 止 | 期数 |\n|---|---|---|---|\n")
        for n, (a, b, c) in span.items():
            f.write("| %s | %s | %s | %d |\n" % (n, a, b, c))
        f.write("\n## 二、当前分位\n\n")
        f.write("| 指数 | PE | PE分位 | PE区间 | PB | PB分位 | PB区间 |\n|---|---|---|---|---|---|---|\n")
        f.write("\n".join(current_table(pe, pb)))
        f.write("\n\n## 三、PE 分位 → 未来%d日收益（中位/正利率(n)）\n\n" % FWD)
        f.write("\n".join(fwd_table(pe, "PE", pe, pb)))
        f.write("\n\n## 四、PB 分位 → 未来%d日收益（中位/正利率(n)）\n\n" % FWD)
        f.write("\n".join(fwd_table(pb, "PB", pe, pb)))
        f.write("\n\n## 五、多窗口分位对照（自算，检验窗口敏感性）\n\n")
        f.write("### PE 多窗口\n\n" + "\n".join(window_table(pe, "pe")))
        f.write("\n\n### PB 多窗口\n\n" + "\n".join(window_table(pb, "pb")))
        f.write("\n\n> 窗口敏感性说明：同一指数在不同窗口下分位可差 0~42pp。"
                "实测 中证银行 PB 3年80%/10年46%、红利 PB 3年95%/10年65%——"
                "长窗口被 2016-2018 极低估值期拉低。**多窗口一致才可信；分歧大说明处周期切换。**\n")
        f.write("\n\n> 局限：蛋卷窗口仅10年（缺2008/2015极端样本，详见文件头docstring）；"
                "周频样本重叠，独立行情段约 n/50；"
                "**10年窗口系统性偏乐观**：自算20年点位分位对照显示，10年分位普遍低于"
                "20年分位（恒生-22pp/上证50-16pp/沪深300-8pp），即当前'看着便宜'"
                "可能还不够便宜；风险提示：仅供参考，不构成投资建议。\n")
        f.write(INTERPRETATION)
    print("\n报告:%s" % rep)


if __name__ == "__main__":
    main()
