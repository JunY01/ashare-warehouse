# -*- coding: utf-8 -*-
"""持仓决策引擎 —— 唯一、确定性的买卖建议出口

给未来亚亚：这个脚本的存在是为了解决一个真实事故：同一持仓、同一数据，
我（亚亚）在不同时间给出了互相矛盾的建议（先"持有不动"、再"降到30%"、
再"卖中证500"、再"银行也可能贵"），根因是**建议由临场叙事驱动，而非固定算法**。

设计铁律（任何修改都必须守住）：
  1. **确定性**：建议是 (持仓, 估值序列) 的纯函数。同样输入必得同样输出。
     不允许在脚本外口头补理由、改结论。
  2. **只在极端出手**：仅当分位一致 ≥80%（减）或一致 ≤20%（加）才给"可操作"建议。
     中段一律 HOLD——因为实测中段分位对未来收益没有方向性（见 pe_percentile.py）。
  3. **信号打架就闭嘴**：多窗口分歧 >25pp 时，强制输出 NO_SIGNAL（不动），
     不许挑一个窗口的数字去支撑结论。这是防止"一会一个说法"的核心闸门。
  4. **无数据 = 无建议**：估值缺失的标的输出 NO_DATA，必须诚实说"我没依据"，
     不许用点位/感觉替代。
  5. **可审计**：每条建议都打印触发它的分位数，任何人可复核。

判据归属：
  该看 PE 还是 PB 由指数类型决定（防御/成长看 PB，其余看 PE），见 BASIS。
  窗口取 3/5/7/10 年四档，共识法——四档一致才算信号。

数据源：data/cache/pe_history/*.json（pe_percentile.py 抓取维护）
用法:
  python holding_advice.py           # 生成建议并出报告
  python holding_advice.py --stdout  # 只打印，不写报告

纯标准库。
"""
import bisect
import csv as csv_mod
import datetime
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from src.common.paths import DATA_ROOT, REPORT_ROOT, CONFIG_ROOT  # noqa: E402
from src.common.market_map import load_json  # noqa: E402

PE_CACHE = os.path.join(DATA_ROOT, "cache", "pe_history", "pe_history_all.json")
PB_CACHE = os.path.join(DATA_ROOT, "cache", "pe_history", "pb_history_all.json")

# 阈值（与 pe_percentile.py 的实测证据绑定，不随意调整）
PCT_HIGH = 80.0     # 一致高于此 → 减
PCT_LOW = 20.0      # 一致低于此 → 加
SPREAD_MAX = 25.0   # 多窗口最大差异超过此 → 信号冲突，不动

# 该看 PE 还是 PB —— 由数据自动选定（select_basis），不手拍。
# 历史教训：曾手工设"银行/红利看PB"，实测该假设错误——银行 PE>80% 未来1年中位 -9.6%、
# 正利率仅 11%，是最强卖出信号；而 PB 无信号(+1.1%)。故改为自动选择。
BASIS_OVERRIDE = {}   # 留空即全自动；如需人工指定可填 {指数名: 'pe'|'pb'}
DEFAULT_BASIS = "pe"

# 持仓名称 → 指数名（用于匹配估值序列）
HOLDING_INDEX = {
    "某某宽基联接C": "中证A500",
    "某某中盘联接A": "中证500",
    "某某科创联接C": "科创50",
    "某某创业联接E": "创业板指",
    "恒生科技": "恒生科技",
    "中证银行": "中证银行",
    "红利低波": "红利低波",
}


def load_hist():
    pe = json.load(open(PE_CACHE, encoding="utf-8")) if os.path.exists(PE_CACHE) else {}
    pb = json.load(open(PB_CACHE, encoding="utf-8")) if os.path.exists(PB_CACHE) else {}
    return pe, pb


def basis_score(series_vals, ts_list, dates, closes, fwd=250):
    """判据预测力打分：median(未来收益|<20%) − median(|>80%)。越大越能区分贵贱。"""
    if not series_vals:
        return None
    import statistics as st
    allv = [v for _, v in series_vals]
    lo_rs, hi_rs = [], []
    for ts, v in series_vals:
        p = bisect.bisect_left(sorted(allv), v) / len(allv)
        if not (p < 0.20 or p > 0.80):
            continue
        dt = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        i = bisect.bisect_left(dates, dt)
        if i >= len(dates) or i + fwd >= len(dates):
            continue
        r = closes[i + fwd] / closes[i] - 1
        (lo_rs if p < 0.20 else hi_rs).append(r)
    if len(lo_rs) < 20 or len(hi_rs) < 20:
        return None
    return st.median(lo_rs) - st.median(hi_rs)


def select_basis(index_name, pe, pb):
    """用实测预测力自动选 PE 还是 PB（避免再出现"手拍判据"的错误）"""
    if index_name in BASIS_OVERRIDE:
        return BASIS_OVERRIDE[index_name], "人工指定"
    path = os.path.join(DATA_ROOT, "kline_full", index_name + ".csv")
    if not os.path.exists(path):
        return DEFAULT_BASIS, "无点位数据→默认"
    import csv as _csv
    rows = list(_csv.DictReader(open(path, encoding="utf-8-sig")))
    dates = [r["日期"] for r in rows]
    closes = [float(r["收盘"]) for r in rows]
    scores = {}
    for hist, key in ((pe, "pe"), (pb, "pb")):
        for code, d in hist.items():
            if d.get("name") == index_name:
                sv = [(x["ts"], x[key]) for x in d["series"] if x.get(key)]
                s = basis_score(sv, None, dates, closes)
                if s is not None:
                    scores[key] = s
    if not scores:
        return DEFAULT_BASIS, "无法评分→默认"
    best = max(scores, key=scores.get)
    return best, "实测(PE%.2f/PB%.2f)" % (scores.get("pe", 0), scores.get("pb", 0))


def _series(index_name, key, pe, pb):
    """返回该指数的原始序列 [{'ts':..,'pe'/'pb':..}]（供组合检验用）"""
    for hist in (pe, pb):
        for code, d in hist.items():
            if d.get("name") == index_name and d.get("series"):
                if any(x.get(key) for x in d["series"]):
                    return [x for x in d["series"] if x.get(key)]
    return []


def series_for(index_name, basis, pe, pb):
    """返回该指数在指定判据下的估值序列（取 max 长度的那个来源）"""
    want = basis.lower()
    for hist in (pe, pb):
        for code, d in hist.items():
            if d.get("name") == index_name and d.get("series"):
                vals = [x[want] for x in d["series"] if x.get(want)]
                if vals:
                    return vals
    return []


def windows(vals, cur, years=(3, 5, 7, 10)):
    """各窗口分位；样本不足该窗口年限时返回 None（弃权，不参与共识）。

    2026-09-14 修复（一）：原实现在数据不足时用全序列顶替，使"7年/10年窗口"与"3年窗口"
    算出同一个数，把"多窗口一致"伪装出来。科创50 只有 6.7 年、恒生科技 6.1 年，
    这两类标的的四窗口共识实际只有 2 个独立窗口，会虚增出"一致≥80%"的减仓建议。
    2026-09-14 修复（二）：严格按 52*10=520 判，则蛋卷 day=all 只有 515 期（9.9 年）、
    10 年窗口永远拿不到——而引擎文档承诺的是"3/5/7/10 四档共识"，实际退化成 3/5/7 三档，
    使慢变量指数（红利类）的分位被系统性推高。改为允许 10 周（约 2 个月）缺口。
    """
    out = []
    for y in years:
        w = int(52 * y)
        if len(vals) < w - 10:
            out.append(None)
            continue
        sub = vals[-w:]
        out.append(bisect.bisect_left(sorted(sub), cur) / len(sub) * 100)
    return out


def combo_guard(index_name, basis, pe, pb):
    """无先例组合闸门：高分位结论只在"当前 PE/PB 组合"历史有样本时才成立。

    给未来亚亚：这是 2026-09-10 用血的教训加的。中证银行当时 PE 分位 92%（看着该卖），
    但 PB 分位仅 46%。检验发现：历史上银行 PE>80% 的样本**全部**发生在 PB≥50% 时
    （"真贵"），未来1年中位 -9.6%。而"PE高+PB低"（盈利被压低、ROE低）这个组合
    10年内**从未出现**——把那条信号套上去是错的。
    返回 (allowed, note)。allowed=False 表示当前组合无先例，禁止据此出建议。
    """
    other = "pb" if basis == "pe" else "pe"
    path = os.path.join(DATA_ROOT, "kline_full", index_name + ".csv")
    if not os.path.exists(path):
        return True, ""
    rows = list(csv_mod.DictReader(open(path, encoding="utf-8-sig")))
    dates = [r["日期"] for r in rows]
    closes = [float(r["收盘"]) for r in rows]
    main_s = _series(index_name, basis, pe, pb)
    other_s = _series(index_name, other, pe, pb)
    if len(main_s) < 50 or len(other_s) < 50:
        return True, ""
    # 对齐：用 ts 交集
    mv = {x["ts"]: x[basis] for x in main_s}
    ov = {x["ts"]: x[other] for x in other_s}
    common = sorted(set(mv) & set(ov))
    if len(common) < 50:
        return True, ""
    ma = [mv[t] for t in common]
    oa = [ov[t] for t in common]
    cur_pct_m = bisect.bisect_left(sorted(ma), ma[-1]) / len(ma) * 100
    cur_pct_o = bisect.bisect_left(sorted(oa), oa[-1]) / len(oa) * 100
    if cur_pct_m < PCT_HIGH:
        return True, ""
    # 当前 main 高分位；看历史 main>80% 且**结果已知**的样本里，other 与当前同侧的有几个。
    # 只用"结果已知"的样本：若同侧样本全发生在近月（未来收益还没走完），
    # 说明这个组合虽有先例但结局未知，不足以支撑建议。
    same_side = 0
    for t in common:
        if bisect.bisect_left(sorted(ma), mv[t]) / len(ma) * 100 <= PCT_HIGH:
            continue
        dt = datetime.datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d")
        i = bisect.bisect_left(dates, dt)
        if i >= len(dates) or i + 250 >= len(dates):
            continue   # 结果未知 → 不计入
        o_pct = bisect.bisect_left(sorted(oa), ov[t]) / len(oa) * 100
        if (cur_pct_o >= 50) == (o_pct >= 50):
            same_side += 1
    if same_side < 15:
        return False, ("无先例组合：%s分位%.0f%%但%s分位仅%.0f%%，"
                       "历史上主指标≥%.0f%%且结果已知的样本中、%s同侧（%s中位）的仅%d个 → "
                       "该信号不适用于当前组合，不动"
                       % (basis.upper(), cur_pct_m, other.upper(), cur_pct_o, PCT_HIGH,
                          other.upper(), "≥" if cur_pct_o >= 50 else "<", same_side))
    return True, ""


def verdict(ws):
    """核心判定：确定性函数。返回 (动作, 强度, 依据文本)

    只统计样本充足的窗口；可用窗口 < 2 个时不出信号——"多窗口共识"至少需要两个独立窗口。
    """
    avail = [x for x in ws if x is not None]
    if not avail:
        return "NO_DATA", "-", "无估值序列"
    if len(avail) < 2:
        return "NO_DATA", "-", "有效窗口不足（仅 %d 个够年限）" % len(avail)
    n = len(avail)
    lo, hi = min(avail), max(avail)
    spread = hi - lo
    txt = "/".join("%.0f%%" % x for x in avail)
    if spread > SPREAD_MAX:
        return "NO_SIGNAL", "冲突", "多窗口分歧%.0fpp（%s）→ 信号不可信，不动" % (spread, txt)
    if lo >= PCT_HIGH:
        return "REDUCE", "强", "%d窗口一致≥%.0f%%（%s）" % (n, PCT_HIGH, txt)
    if hi <= PCT_LOW:
        return "ADD", "强", "%d窗口一致≤%.0f%%（%s）" % (n, PCT_LOW, txt)
    # 中段细分：区分"接近便宜"与"接近贵"，避免把两者混为一谈
    if hi <= 40:
        return "HOLD", "弱", "中段偏低·近加仓区（%s）" % txt
    if lo >= 60:
        return "HOLD", "弱", "中段偏高·近减仓区（%s）" % txt
    return "HOLD", "弱", "中段·无方向（%s）" % txt


def collect_holdings():
    """从 positions.json 取全部持仓（资金A + 资金B底仓）；已清仓（市值 0）的标的不进决策表。

    指数名以台账的『跟踪指数』为准（基金实际跟踪的指数）；HOLDING_INDEX 只作没登记时的兜底。
    2026-09-14 事故：HOLDING_INDEX 把『红利低波』写成『红利指数』(SH000015) 代理，而该基金实际
    跟踪中证红利低波动指数——两只指数同期分位差 18pp，据此误发了一笔清仓建议。同一事实存两处
    必然漂移，故改为台账唯一来源，不一致时直接报错并以台账为准。
    """
    pos = load_json(os.path.join(CONFIG_ROOT, "positions.json"))
    out = []

    def add(f, default_idx):
        if f.get("市值", 0) <= 0:
            return
        name = f["名称"]
        tracked = f.get("跟踪指数")
        legacy = HOLDING_INDEX.get(name, default_idx)
        if tracked:
            if tracked != legacy:
                print("  [ERR] 持仓『%s』台账跟踪指数=%s，引擎映射=%s → 以台账为准，请修 HOLDING_INDEX"
                      % (name, tracked, legacy))
            idx = tracked
        else:
            print("  [warn] 持仓『%s』台账未登记『跟踪指数』，暂用引擎映射=%s" % (name, legacy))
            idx = legacy
        out.append((name, f.get("市值", 0), idx))

    for f in pos.get("资金A", {}).get("基金", []):
        add(f, f.get("关联指数", ""))
    for f in pos.get("资金B", {}).get("底仓", {}).get("配置", []):
        add(f, f.get("名称", ""))
    return out


def range_position(vals):
    """当前值落在"至今为止全部历史"最低~最高之间的百分比（0=历史最低，100=历史最高）。"""
    lo, hi = min(vals), max(vals)
    return 50.0 if hi <= lo else (vals[-1] - lo) / (hi - lo) * 100


def abs_level_note(vals):
    """审计披露：当前绝对水平，以及它落在自身历史区间的哪个位置。

    分位回答的是"历史上多少时间比现在便宜"，区间位置回答的是"离历史最低/最高点多远"。
    分布发生漂移时（如中证银行 ROE 十年 15.2%→9.5%），两者会给出不同读数，故一并列出。
    """
    if not vals or len(vals) < 50:
        return ""
    return " [绝对水平 %.2f，处自身区间 %.2f~%.2f 的 %.0f%% 位置]" % (
        vals[-1], min(vals), max(vals), range_position(vals))


def main():
    to_stdout = "--stdout" in sys.argv
    pe, pb = load_hist()
    rows = []
    for hname, mv, idx in collect_holdings():
        basis, bnote = select_basis(idx, pe, pb)
        vals = series_for(idx, basis, pe, pb)
        if vals:
            ws = windows(vals, vals[-1])
            act, strength, why = verdict(ws)
            basis_txt = "%s" % basis.upper()
            why = why + " [判据:%s]" % bnote
            # 无先例组合闸门：高分位建议必须先通过组合检验
            if act in ("REDUCE", "ADD"):
                ok, gnote = combo_guard(idx, basis, pe, pb)
                if not ok:
                    act, strength, why = "NO_SIGNAL", "无先例", gnote + " [判据:%s]" % bnote
                elif gnote:
                    why += " " + gnote
            # 绝对水平闸门（2026-09-16 经 research/validate_advice_rule 验证通过后启用）：
            # 分位是对"最近若干年"讲的，要"卖"还须绝对水平确实处在自身历史的中高位。
            # 实测：分位≥80% 但区间位置<50% 的 9 个独立时段，未来一年中位 +11.8%，
            # 而区间位置≥50% 那组只有 +3.2%（差 8.6pp）→ 前者不构成看跌。
            if act == "REDUCE" and range_position(vals) < 50:
                act, strength = "NO_SIGNAL", "绝对水平不支持"
                why = ("分位高（%s）但绝对水平仅处自身历史区间的中低位 → "
                       "高分位由估值中枢漂移顶起，非股价贵，不动 [判据:%s]" % 
                       ("/".join("%.0f%%" % x for x in ws if x is not None), bnote))
            # 放在所有分支之后追加，确保 stdout 与报告落盘两条路径都带上
            why += abs_level_note(vals)
        else:
            ws, act, strength = [], "NO_DATA", "-"
            basis_txt = "-"
            why = "蛋卷未收录估值 → 无可依据"
        rows.append((hname, mv, idx, basis_txt, ws, act, strength, why))

    order = {"REDUCE": 0, "ADD": 1, "NO_SIGNAL": 2, "HOLD": 3, "NO_DATA": 4}
    rows.sort(key=lambda r: order.get(r[5], 9))

    print("=" * 108)
    print("持仓决策（确定性算法 · 同样输入必得同样输出 · 信号打架时强制不动）")
    print("=" * 108)
    print("%-22s %9s %-9s %-4s %-22s %-10s %s" % ("基金", "市值", "指数", "判据", "多窗口分位", "动作", "依据"))
    print("-" * 108)
    for hname, mv, idx, bt, ws, act, strength, why in rows:
        wst = "/".join("%.0f" % x if x is not None else "-" for x in ws) if ws else "-"
        print("%-22s %9.0f %-9s %-4s %-22s %-10s %s" % (
            hname, mv, idx, bt, wst, "%s(%s)" % (act, strength), why))

    total = sum(r[1] for r in rows)
    print("-" * 108)
    print("持仓合计 %.0f 元" % total)
    reduce_amt = sum(r[1] for r in rows if r[5] == "REDUCE")
    act_any = [r for r in rows if r[5] in ("REDUCE", "ADD")]
    if act_any:
        print("\n★ 可执行动作：")
        for hname, mv, idx, bt, ws, act, strength, why in act_any:
            print("   %s %s（%.0f 元）—— %s" % (act, hname, mv, why))
    else:
        print("\n★ 无任何标的达到可操作门槛（一致≥80%或≤20%）→ 全部不动")

    if to_stdout:
        return
    rep = os.path.join(REPORT_ROOT, "持仓建议_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    with open(rep, "w", encoding="utf-8") as f:
        f.write("# 持仓决策 %s（确定性算法）\n\n" % datetime.date.today())
        f.write("> 原则：只在分位一致≥80%%（减）或一致≤20%%（加）时出手；多窗口分歧>%.0fpp 强制不动；"
                "无估值数据如实说无依据。\n\n" % SPREAD_MAX)
        f.write("| 基金 | 市值 | 指数 | 判据 | 3/5/7/10年分位 | 动作 | 依据 |\n|---|---|---|---|---|---|---|\n")
        for hname, mv, idx, bt, ws, act, strength, why in rows:
            wst = "/".join("%.0f%%" % x if x is not None else "-" for x in ws) if ws else "-"
            f.write("| %s | %.0f | %s | %s | %s | **%s**(%s) | %s |\n" % (
                hname, mv, idx, bt, wst, act, strength, why))
        f.write("\n**持仓合计 %.0f 元**\n\n" % total)
        if act_any:
            f.write("## 可执行动作\n\n")
            for hname, mv, idx, bt, ws, act, strength, why in act_any:
                f.write("- **%s** %s（%.0f 元）—— %s\n" % (act, hname, mv, why))
        else:
            f.write("## 可执行动作\n\n无——所有标的分位未达一致极端，或信号冲突 → 全部不动。\n")
        f.write("\n> 仅供个人研究，不构成投资建议。\n")
    print("\n报告:%s" % rep)


if __name__ == "__main__":
    main()
