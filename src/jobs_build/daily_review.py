# -*- coding: utf-8 -*-
"""每日复盘自动化脚本 — 14:30 工作日运行

功能:
  1. 刷新快照+估值数据
  2. 持仓复盘（资金A + 资金B）
  3. 行业观察名单（多因子评分，仅供观察非买入依据）
  4. 推荐场外C类基金代码
  5. 生成报告 reports/每日复盘_YYYYMMDD.md

用法: python daily_review.py
"""
import json
import os
import subprocess
import sys
import datetime

try:
    from src.common.paths import DATA_ROOT, REPORT_ROOT, REPO_ROOT, CONFIG_ROOT
    DATA_DIR = DATA_ROOT
    REPORT_DIR = REPORT_ROOT
except ImportError:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    DATA_ROOT = os.path.join(REPO_ROOT, "data")
    DATA_DIR = DATA_ROOT
    REPORT_DIR = os.path.join(REPO_ROOT, "reports")
    REPORT_ROOT = REPORT_DIR
    CONFIG_ROOT = os.path.join(REPO_ROOT, "config")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from src.common.market_map import (  # noqa: E402
    SECTOR_TO_BROAD, HOLDING_INDEX_MAP,
    cfg_path as _cfg_path, load_json, read_csv)
from src.common import history_db  # noqa: E402
# 点位分位档位（蛋卷无 PE/PB 的指数走这套措辞）；词表唯一来源在 fetch_index_valuation，
# 不在此重复定义，避免两处措辞漂移
from src.jobs_build.fetch_index_valuation import POS_BUCKETS as _POS_BUCKETS  # noqa: E402
POS_STATES = tuple(_POS_BUCKETS)


TODAY = datetime.date.today().strftime("%Y-%m-%d")
TODAY_SHORT = datetime.date.today().strftime("%Y%m%d")

# ── 持仓关联指数映射 ──（定义见 src/common/market_map.py）

# ── 行业关键词 → 场外C类基金搜索词映射 ──
# key: 行业名称中的关键词, value: (基金搜索关键词, 排除词列表)
# 定义见 src/common/market_map.py（daily_1430 与 daily_review 共用超集）


# ══════════════════════════════════════════════════════════════
# 模块A: 数据刷新
# ══════════════════════════════════════════════════════════════
def refresh_data():
    """运行快照更新 + 估值更新，返回是否成功"""
    print("[刷新] 正在刷新数据...")
    steps = [
        ([sys.executable, os.path.join(REPO_ROOT, "src", "jobs_fetch", "update_data.py"), "--snapshot-only"], "快照更新"),
        ([sys.executable, os.path.join(REPO_ROOT, "src", "jobs_build", "fetch_index_valuation.py")], "估值更新"),
    ]
    ok = True
    for cmd, label in steps:
        print(f"  → {label}...")
        r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if r.returncode != 0:
            print(f"  [FAIL] {label}失败: {r.stderr[:200]}")
            ok = False
        else:
            print(f"  [OK] {label}完成")
    return ok


# ══════════════════════════════════════════════════════════════
# 模块B: 持仓复盘
# ══════════════════════════════════════════════════════════════
def review_holdings(positions, valuations):
    """分析资金A和资金B的持仓状态，返回分析列表"""
    # 构建估值查找表: {指数名: {state, price, ...}}
    val_map = {r["name"]: r for r in valuations.get("rows", [])}

    lines = []
    lines.append("### 资金A：存量持仓\n")
    lines.append("| 基金 | 市值 | 收益率 | 关联指数 | 估值状态 | 操作建议 |")
    lines.append("|------|------|--------|---------|---------|---------|")

    for fund in positions.get("资金A", {}).get("基金", []):
        name = fund["名称"]
        mv = fund["市值"]
        pnl = fund["收益率"]
        idx_name = HOLDING_INDEX_MAP.get(name, "")
        val = val_map.get(idx_name, {})
        state = val.get("state", "未收录")
        price = val.get("price", 0)

        # 操作建议逻辑
        advice = "持有观望"
        if mv < 1000:
            advice = "灰尘仓位，持有不动"  # 金额太小不值得操作
        elif state in POS_STATES:
            # 点位分位不是估值：只知道位置、不知道贵贱，不据此给买卖动作
            advice = "仅点位分位（非估值），持有观望"
        elif state == "泡沫":
            advice = "⚠️ 泡沫区，反弹减仓"
        elif state == "高估":
            if pnl > 3:
                advice = "高估+盈利，逢高止盈"
            else:
                advice = "高估区，不补仓"
        elif state == "合理偏高":
            if pnl > 3:
                advice = "盈利回落至3%可止盈"
            else:
                advice = "持有，暂不操作"
        elif state in ("合理偏低", "低估"):
            if pnl < -3:
                advice = "低估区亏损，可小额补仓"
            else:
                advice = "合理偏低，可定投积累"
        elif state == "未收录":
            if idx_name == "中证A500":
                advice = "无估值分位，按指数点位参考，持有"
            else:
                advice = "估值未收录，持有观望"

        lines.append(f"| {name} | ¥{mv:,.0f} | {pnl:+.2f}% | {idx_name} | {state} | {advice} |")

    # 资金B
    lines.append("")
    lines.append("### 资金B：6万投资计划\n")

    fund_b = positions.get("资金B", {})
    account = fund_b.get("账户现金", {})
    base = fund_b.get("底仓", {})
    bullet = fund_b.get("机动子弹", {})

    lines.append(f"- 账户现金: ¥{account.get('金额', 0):,.0f}")
    lines.append(f"- 底仓进度: 第{base.get('已完成批次', 0)}/{base.get('总批次', 3)}批（每批¥{base.get('每批', 0):,}）")

    # 检查批次日程
    batch_schedule = base.get("批次日程", [])
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    lines.append(f"- 批次日程: {' / '.join(batch_schedule)}")

    # 判断今天是否需要执行
    for batch in batch_schedule:
        if today_str in batch and "待执行" in batch:
            lines.append(f"\n> 🔔 **今天是底仓第2批执行日！** 按计划买入：")
            for item in base.get("配置", []):
                lines.append(f"  - {item['名称']}（{item['代码']}）¥{item['每批金额']}")
        elif today_str in batch and "已买入" in batch:
            lines.append(f"\n> ✅ 今日批次已完成")

    # 机动子弹检查
    lines.append(f"\n#### 机动子弹触发条件\n")
    val_map_lower = {r["name"]: r for r in valuations.get("rows", [])}
    for trigger in bullet.get("触发条件", []):
        idx = trigger.get("指数", "")
        cond = trigger.get("条件", "")
        batch = trigger.get("批次", "")
        val = val_map_lower.get(idx, {})
        price = val.get("price", 0)
        state = val.get("state", "未知")
        triggered = False

        # 简单条件判断
        if "<3800" in cond and "上证" in idx and price < 3800:
            triggered = True
        elif "<2400" in cond and "创业板" in idx and price < 2400:
            triggered = True
        elif "<3400" in cond and "3200" in cond and "上证" in idx and 3200 <= price < 3400:
            triggered = True

        status = "🟢 已触发！" if triggered else "🔴 未触发"
        lines.append(f"- {batch}: {idx} {price:.0f}（{state}）{status} — {cond}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# 模块C: 行业观察名单（多因子评分）
# ══════════════════════════════════════════════════════════════
# 排除用户已持仓的行业关键词（线上扫描与线下回测共用）
EXCLUDE_KEYWORDS = ["半导体", "芯片", "医药", "创新药", "银行", "恒生", "港股通"]

# 五因子满分权重（线上默认）。线下回测（research/validate_review.py）可传入候选方案做对比。
# 各因子先归一化到 0~1 再乘权重，故权重之和恒为 100，评分仍是 0~100。
FACTOR_WEIGHTS = {"bottom": 30.0, "flow": 25.0, "stability": 20.0, "trend": 15.0, "signal": 10.0}


def score_candidate(name, code, chg20, chg60, inflow, regime_info,
                    weights=None, min_total=50.0):
    """对单个板块应用硬性门槛与五因子评分：未过门槛返回 None，否则返回含分项与理由的 dict。

    纯函数、无 I/O。线上扫描（scan_sectors）与线下回测
    （research/validate_review.py）共用本函数，保证两侧口径一致。
    weights 为各因子满分权重（默认 FACTOR_WEIGHTS），min_total 为评分后二次过滤线。
    """
    # 排除已持仓
    if any(kw in name for kw in EXCLUDE_KEYWORDS):
        return None

    # ═══ 硬性门槛（必须全部满足）═══
    # 1. 20日动量: 不能暴跌（<-8%说明还在崩），不能暴涨（>5%说明已涨过）
    if chg20 < -8 or chg20 > 5:
        return None

    # 2. 60日动量: 不能深套（<-20%），不能过热（>15%）
    if chg60 < -20 or chg60 > 15:
        return None

    # 3. 资金必须流入（机构在买，不是在卖）
    if inflow <= 0:
        return None

    # 4. 底部评分 >= 60（结构底部已形成）
    bottom_score = regime_info.get("bottom_score", 0) or 0
    if bottom_score < 60:
        return None

    # 5. 趋势状态: 必须有反转信号
    regime = regime_info.get("regime", "")
    alert = regime_info.get("alert", "")
    engine = regime_info.get("engine", "")
    has_reversal_signal = (
        (regime == "BULL") or
        (alert in ("BOTTOM_WATCH", "GOLDEN_X")) or
        (engine in ("BOTTOM_WATCH", "SHORT_REDUCED") and bottom_score >= 70)
    )
    if not has_reversal_signal:
        return None

    # ═══ 评分因子（各因子先归一化到 0~1，再乘权重）═══
    w = weights or FACTOR_WEIGHTS

    # 因子1: 底部评分归一化
    score_bottom = bottom_score / 100.0 * w["bottom"]

    # 因子2: 资金流入强度 — 按流入金额分档（0/0.4/0.6/0.8/1.0）
    if inflow > 50000:
        tier_flow = 1.0
    elif inflow > 20000:
        tier_flow = 0.8
    elif inflow > 5000:
        tier_flow = 0.6
    elif inflow > 0:
        tier_flow = 0.4
    else:
        tier_flow = 0.0
    score_flow = tier_flow * w["flow"]

    # 因子3: 20日动量稳定性 — 越接近0越好（刚企稳）
    score_stability = max(0.0, (20 - abs(chg20) * 2) / 20.0) * w["stability"]

    # 因子4: 60日趋势健康度 — 在-5%~+10%区间最佳
    if -5 <= chg60 <= 10:
        tier_trend = 1.0
    elif chg60 < -5:
        tier_trend = max(0.0, (10 + (chg60 + 5)) / 15.0)  # 越跌越低
    else:  # chg60 > 10
        tier_trend = max(0.0, (15 - (chg60 - 10) * 2) / 15.0)  # 过热扣分
    score_trend = tier_trend * w["trend"]

    # 因子5: 趋势信号加成（金叉 > 底部观察 > 趋势多头，elif 链前后者优先）
    if alert == "GOLDEN_X":
        tier_signal = 1.0  # 金叉信号最强
    elif alert == "BOTTOM_WATCH":
        tier_signal = 0.7
    elif regime == "BULL":
        tier_signal = 0.8
    else:
        tier_signal = 0.0
    score_signal = tier_signal * w["signal"]

    total = score_bottom + score_flow + score_stability + score_trend + score_signal
    if total < min_total:
        return None

    # 生成理由
    reasons = []
    reasons.append(f"底部评分{bottom_score:.0f}")
    reasons.append(f"20日{chg20:+.1f}%（企稳）")
    reasons.append(f"资金流入{inflow:+.0f}万")
    if alert == "GOLDEN_X":
        reasons.append("金叉信号")
    elif alert == "BOTTOM_WATCH":
        reasons.append("底部观察")
    if regime == "BULL":
        reasons.append("趋势多头")

    # 映射到大类行业（用于基金匹配和去重）
    broad_name = name
    for sub_kw, broad_kw in SECTOR_TO_BROAD.items():
        if sub_kw in name:
            broad_name = broad_kw
            break

    return {
        "name": name, "code": code, "broad": broad_name, "score": total,
        "reason": "；".join(reasons),
        "bottom_score": bottom_score, "chg20": chg20, "chg60": chg60, "inflow": inflow,
        "score_bottom": score_bottom, "score_flow": score_flow,
        "score_stability": score_stability, "score_trend": score_trend,
        "score_signal": score_signal,
        # 归一化档位（0~1）：网格搜索按任意权重组合重算总分时用，与线上口径同源
        "tier_bottom": bottom_score / 100.0, "tier_flow": tier_flow,
        "tier_stability": max(0.0, (20 - abs(chg20) * 2) / 20.0), "tier_trend": tier_trend,
        "tier_signal": tier_signal,
    }


def rank_candidates(records, topn=10):
    """按总分降序排列，同一大类只保留最高分（纯函数）。"""
    seen_fund_keys = set()
    deduped = []
    for rec in sorted(records, key=lambda r: r["score"], reverse=True):
        # 用大类行业名作为去重key（如保险Ⅱ/Ⅲ 只留一个）
        fund_key = rec["broad"]
        if fund_key in seen_fund_keys:
            continue
        seen_fund_keys.add(fund_key)
        deduped.append(rec)
    return deduped[:topn]


def scan_sectors():
    """多因子评分筛选行业机会，返回按评分降序的候选记录（含分项，供报告与落库共用）"""
    # 读取数据
    sector_snap = read_csv(os.path.join(DATA_DIR, "sector_snapshot.csv"))

    # 读取 regime_daily（最新日期）
    regime_map = {}
    data_date = None
    db_path = os.path.join(DATA_DIR, "market_history.db")
    if os.path.exists(db_path):
        con = history_db.connect(readonly=True)
        cur = con.cursor()
        cur.execute("SELECT MAX(date) FROM regime_daily")
        latest_date = cur.fetchone()[0]
        if latest_date:
            data_date = latest_date
            cur.execute(
                "SELECT code, regime, bottom_score, engine, alert "
                "FROM regime_daily WHERE date = ?", (latest_date,))
            for row in cur.fetchall():
                regime_map[row[0]] = {
                    "regime": row[1], "bottom_score": row[2],
                    "engine": row[3], "alert": row[4],
                }
        con.close()

    # 构建 sector_snapshot 查找表
    sector_map = {}
    for s in sector_snap:
        code = s.get("代码", "")
        try:
            chg20 = float(s.get("20日%", 0) or 0)
            chg60 = float(s.get("60日%", 0) or 0)
            # CSV里是元，转换为万
            inflow_raw = float(s.get("主力净流入(万)", 0) or 0)
            inflow = inflow_raw / 10000 if abs(inflow_raw) > 100000 else inflow_raw
        except (ValueError, TypeError):
            continue
        sector_map[code] = {
            "name": s.get("名称", ""),
            "chg20": chg20,
            "chg60": chg60,
            "inflow": inflow,
            "close": float(s.get("最新价", 0) or 0),
        }

    # 评分并筛选（熊市量化策略：底部确认 + 资金确认 + 动量确认）
    records = []
    for code, info in sector_map.items():
        rec = score_candidate(info["name"], code, info["chg20"], info["chg60"],
                              info["inflow"], regime_map.get(code, {}))
        if rec is not None:
            records.append(rec)

    ranked = rank_candidates(records)
    for i, rec in enumerate(ranked, 1):
        rec["rank"] = i
        rec["data_date"] = data_date
    return ranked


# ══════════════════════════════════════════════════════════════
# 模块D: C类基金代码映射
# ══════════════════════════════════════════════════════════════
def load_fund_list():
    """加载全量基金列表，返回 [(code, name, type)]"""
    from src.common.fund_lookup import load_fund_list as _lfl
    return _lfl()


def find_c_funds(fund_list, sector_name, cap=3):
    """根据行业名搜索场外C类基金，返回 [(code, name)]"""
    from src.common.fund_lookup import find_c_funds_simple
    return find_c_funds_simple(fund_list, sector_name, cap)


# ══════════════════════════════════════════════════════════════
# 模块D2: 榜单落库（回测与历史复盘的样本来源）
# ══════════════════════════════════════════════════════════════
def save_picks(opportunities, fund_list):
    """把当日榜单写入 review_pick_daily，同日重跑幂等覆盖 live 行"""
    if not opportunities:
        return 0
    rows = []
    for rec in opportunities:
        funds = find_c_funds(fund_list, rec["broad"], cap=1)
        fund_code, fund_name = funds[0] if funds else (None, None)
        rows.append((
            TODAY, rec["rank"], rec["code"], rec["name"], rec["broad"], rec["score"],
            rec["data_date"], "live",
            rec["bottom_score"], rec["score_bottom"], rec["score_flow"],
            rec["score_stability"], rec["score_trend"], rec["score_signal"],
            rec["chg20"], rec["chg60"], rec["inflow"],
            rec["reason"], fund_code, fund_name,
        ))
    conn = history_db.connect()
    try:
        conn.execute("DELETE FROM review_pick_daily WHERE push_date=? AND source='live'", (TODAY,))
        conn.executemany(
            "INSERT OR REPLACE INTO review_pick_daily("
            "push_date, rank_no, sector_code, sector_name, broad_state, score,"
            "data_date, source, bottom_score, score_bottom, score_flow,"
            "score_stability, score_trend, score_signal, chg20, chg60, inflow,"
            "reason, fund_code, fund_name)"
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


# ══════════════════════════════════════════════════════════════
# 模块E: 生成报告
# ══════════════════════════════════════════════════════════════
def generate_report(valuation_data, holding_analysis, sector_opportunities, fund_list):
    """生成完整复盘报告"""
    lines = []
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append(f"# 📊 每日复盘 {TODAY}")
    lines.append(f"> 自动生成于 {now}\n")

    # ── 一、市场概览 ──
    lines.append("## 一、市场概览\n")
    try:
        try:
            from src.jobs_build import regime_lamp as _lamp
        except ImportError:
            import regime_lamp as _lamp
        _s, _adv, _sh, _ratio, _rdate = _lamp.lamp()
        lines.append("> %s（上证%s｜BULL%.0f%% %s）：%s\n" % (_s, _sh, _ratio, _rdate, _adv))
    except Exception:
        pass
    lines.append("| 指数 | 点位 | 涨跌 | 估值状态 | 判据 |")
    lines.append("|------|------|------|---------|------|")
    for r in valuation_data.get("rows", []):
        name = r["name"]
        price = r.get("price") or 0
        chg = r.get("chg_pct") or 0
        state = r.get("state", "未知")
        basis = r.get("state_basis", "") or ""
        state_emoji = {
            "泡沫": "🔴", "高估": "🟠", "合理偏高": "🟡",
            "合理": "⚪", "合理偏低": "🟢", "低估": "🔵", "未收录": "⚫",
        }.get(state) or ("📍" if state in POS_STATES else "⚪")
        price_str = f"{price:.0f}" if price else "?"
        lines.append(f"| {name} | {price_str} | {chg:+.2f}% | {state_emoji} {state} | {basis} |")

    # ── 二、持仓复盘 ──
    lines.append("\n## 二、持仓复盘\n")
    lines.append(holding_analysis)

    # ── 三、行业观察名单（非买入依据）──
    lines.append("\n## 三、行业观察名单\n")
    lines.append("> ⚠️ **仅供观察，不作为买入依据**：2026-09-14 三轮验证（38 个因子 IC 扫描、"
                 "11.9 万组权重网格、776 天长历史）均未发现可用超额，详见 "
                 "`reports/因子寻找结论_20260914.md`。\n")
    if not sector_opportunities:
        lines.append("今日无明显行业机会，建议观望。\n")
    else:
        lines.append("| 排名 | 行业 | 评分 | 推荐理由 | 场外C类基金 |")
        lines.append("|------|------|------|---------|------------|")
        for i, rec in enumerate(sector_opportunities, 1):
            funds = find_c_funds(fund_list, rec["broad"], cap=2)
            fund_str = "、".join([f"`{c}` {n[:12]}" for c, n in funds]) if funds else "暂无匹配"
            lines.append(f"| {i} | {rec['name']}（{rec['broad']}类） | {rec['score']:.0f} | {rec['reason']} | {fund_str} |")

        lines.append("\n**评分说明**（实际权重）: 底部评分(30分) + 资金流入(25分) "
                     "+ 5日动量稳定性(20分) + 10日趋势健康度(15分) + 趋势信号加成(10分)\n")

    # ── 四、操作建议汇总 ──
    lines.append("## 四、今日操作建议\n")
    lines.append(_summarize_actions(valuation_data, holding_analysis, sector_opportunities))

    # ── 五、风险提示 ──
    lines.append("\n---")
    lines.append("**⚠️ 风险提示**: 以上分析仅供参考，不构成投资建议。")
    lines.append("市场有风险，投资需谨慎。数据来源：东方财富、蛋卷基金、腾讯财经。\n")

    return "\n".join(lines)


def _summarize_actions(valuation_data, holding_analysis, opportunities):
    """根据分析结果生成简洁的操作建议"""
    actions = []

    # 检查是否有泡沫/高估指数（仅估值尺；点位分位不是估值，不列入减仓提示）
    bubble_idx = [r["name"] for r in valuation_data.get("rows", []) if r.get("state") == "泡沫"]
    high_idx = [r["name"] for r in valuation_data.get("rows", []) if r.get("state") == "高估"]

    if bubble_idx:
        actions.append(f"- 🔴 **泡沫区指数**: {', '.join(bubble_idx)} — 相关持仓逢反弹减仓")
    if high_idx:
        actions.append(f"- 🟠 **高估区指数**: {', '.join(high_idx)} — 不补仓，盈利达标可止盈")

    # 点位尺单独提示：只有位置读数，没有估值结论
    pos_idx = [r["name"] for r in valuation_data.get("rows", []) if r.get("state") in POS_STATES]
    if pos_idx:
        actions.append(f"- 📍 **仅点位分位（无 PE/PB，非估值）**: {', '.join(pos_idx)}"
                       " — 位置读数不能单独作为买卖依据")

    # 检查底仓批次
    today = datetime.date.today().strftime("%Y-%m-%d")
    if "8/27" in holding_analysis or today == "2026-08-27":
        actions.append("- 🎯 **资金B底仓第2批**: 今日应执行，按计划买入恒科/银行/红利低波")

    # 行业观察（非买入依据，见第三节说明）
    if opportunities:
        top = opportunities[0]
        actions.append(f"- 👀 **观察行业**（非买入依据）: {top['name']}（评分{top['score']:.0f}）")

    if not actions:
        actions.append("- ✅ 今日无特殊操作，维持现有持仓")

    return "\n".join(actions)


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════
def main():
    print(f"== 每日复盘 {TODAY} ==")
    print("=" * 50)

    # A. 数据刷新
    refresh_data()

    # B. 加载数据
    val_path = os.path.join(DATA_DIR, "index_valuation.json")
    pos_path = _cfg_path("positions.json")

    if not os.path.exists(val_path):
        print("[ERR] index_valuation.json 不存在，请先运行 fetch_index_valuation.py")
        sys.exit(1)
    if not os.path.exists(pos_path):
        print("[ERR] positions.json 不存在")
        sys.exit(1)

    valuations = load_json(val_path)
    positions = load_json(pos_path)

    # B. 持仓复盘
    print("\n== 持仓复盘... ==")
    holding_analysis = review_holdings(positions, valuations)

    # C. 行业扫描
    print("== 行业观察名单扫描... ==")
    opportunities = scan_sectors()
    print(f"   找到 {len(opportunities)} 个候选行业")

    # D. 加载基金列表
    print("== 加载基金代码库... ==")
    fund_list = load_fund_list()
    print(f"   共 {len(fund_list)} 只基金")

    # D2. 榜单落库（同日重跑幂等覆盖）
    n_saved = save_picks(opportunities, fund_list)
    print(f"   榜单已落库 review_pick_daily: {n_saved} 行")

    # E. 生成报告
    print("== 生成报告... ==")
    report = generate_report(valuations, holding_analysis, opportunities, fund_list)

    # 写入文件
    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, f"每日复盘_{TODAY_SHORT}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"[OK] 报告已保存: {report_path}")

    # 输出到 stdout（供自动化任务读取）
    print("\n" + "=" * 50)
    print(report)

    return report_path


if __name__ == "__main__":
    main()
