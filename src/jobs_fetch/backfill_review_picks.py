# -*- coding: utf-8 -*-
"""从历史报告回填"每日复盘·关注行业"榜单到 review_pick_daily

reports/每日复盘_YYYYMMDD.md 的行业榜单表格（旧标题"三、行业ETF机会扫描"、新标题"三、行业观察名单"）只有 名称/评分/理由/基金，
没有板块代码与分项分数。本脚本：
  1. 从理由文本反推 bottom_score / chg20 / inflow 与信号分项
  2. 按名称反查板块代码，写入 review_pick_daily（source='backfill'）

注意：
  1. 报告只披露总分，故 score_trend 由"总分 − 其余四项"倒推（保证分项之和与报告一致）；
     倒推值超出 [0,15] 合法区间时置空并计入 warns——说明当时算法口径不同。
  2. chg60 报告未披露，且 sector_kline 的历史段与东财快照口径不一致（见 validate_review.py
     的数据说明），故留空，不用 K 线硬凑。
  3. 早期报告（08-27~09-02）行业列无"（大类类）"后缀，按 SECTOR_TO_BROAD 反推大类。

一次性回填，幂等：按 push_date + source='backfill' 先删后插。
已有 live 记录的推送日会被跳过——表主键是 (push_date, sector_code)，不含 source，
同一天两个来源会互相覆盖，而 live 是当日权威值。

用法: python src/jobs_fetch/backfill_review_picks.py
"""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common.paths import REPORT_ROOT  # noqa: E402
from src.common.market_map import SECTOR_TO_BROAD  # noqa: E402
from src.common import history_db  # noqa: E402

SEC_RE = re.compile(r"## 三、行业(?:ETF机会扫描|观察名单)\n(.*?)(?=\n## |\Z)", re.S)
DATA_DATE_RE = re.compile(r"BULL\d+%\s+(\d{4}-\d{2}-\d{2})")
NAME_RE = re.compile(r"^(.*?)（(.*)）$")
FUND_RE = re.compile(r"`(\d{6})`\s*([^、|]*)")
BS_RE = re.compile(r"底部评分(\d+(?:\.\d+)?)")
C20_RE = re.compile(r"20日([+-]?\d+(?:\.\d+)?)%")
C60_RE = re.compile(r"60日([+-]?\d+(?:\.\d+)?)%")
FLOW_RE = re.compile(r"资金流入([+-]?\d+(?:\.\d+)?)万")
DIVIDER_RE = re.compile(r"^\|[\s\-:|]+\|$")


def load_trading_dates(conn):
    """交易日历（只用日期，不涉及价格值）"""
    return [r[0] for r in conn.execute(
        "SELECT date FROM sector_kline GROUP BY date HAVING COUNT(*) >= 300 ORDER BY date")]


def load_name_map(conn):
    """板块名 -> 代码（名称基本稳定，取任意一次出现的映射）"""
    m = {}
    for code, name in conn.execute(
            "SELECT code, name FROM sector_daily WHERE name IS NOT NULL AND name != ''"):
        m.setdefault(name, code)
    return m


def score_flow_of(inflow):
    """照搬 daily_review.score_candidate 的因子2"""
    if inflow > 50000:
        return 25
    if inflow > 20000:
        return 20
    if inflow > 5000:
        return 15
    if inflow > 0:
        return 10
    return 0


def signal_of(reason):
    """照搬因子5：金叉 > 底部观察 > 趋势多头（elif 链，前两者优先）"""
    if "金叉信号" in reason:
        return 10
    if "底部观察" in reason:
        return 7
    if "趋势多头" in reason:
        return 8
    return 0


def broad_of(name):
    """行业名 → 大类（与算法同一张 SECTOR_TO_BROAD 表）"""
    for sub_kw, broad_kw in SECTOR_TO_BROAD.items():
        if sub_kw in name:
            return broad_kw
    return name


def parse_report(path, name_map, dates):
    """解析单份报告，返回 (rows, warns)"""
    fn = os.path.basename(path)
    d8 = re.search(r"(\d{8})", fn).group(1)
    push_date = "%s-%s-%s" % (d8[:4], d8[4:6], d8[6:])
    text = open(path, encoding="utf-8").read()

    m = SEC_RE.search(text)
    if not m:
        return [], []
    lines = [l for l in m.group(1).splitlines() if l.startswith("|")]

    dm = DATA_DATE_RE.search(text)
    if dm:
        data_date = dm.group(1)
    else:
        # 早期报告无仓位灯行，退化为"不晚于推送日的最近交易日"
        prior = [d for d in dates if d <= push_date]
        data_date = prior[-1] if prior else None

    rows, warns = [], []
    for line in lines:
        if DIVIDER_RE.match(line):
            continue
        cells = [c.strip() for c in line.split("|")][1:-1]
        if len(cells) != 5 or not cells[0].isdigit():
            continue
        rank, industry, score_s, reason, fund_cell = cells

        nm = NAME_RE.match(industry)
        if nm:
            sec_name, broad = nm.group(1), nm.group(2)
            if broad.endswith("类"):
                broad = broad[:-1]
        else:
            # 早期报告无大类后缀
            sec_name = industry
            broad = broad_of(industry)

        code = name_map.get(sec_name)
        if code is None:
            warns.append("%s #%s 名称反查代码失败: %s" % (push_date, rank, sec_name))
            continue

        mb = BS_RE.search(reason)
        if not mb:
            warns.append("%s #%s 理由无法反推底部评分: %s" % (push_date, rank, reason))
            continue
        mc, mf, m60 = C20_RE.search(reason), FLOW_RE.search(reason), C60_RE.search(reason)
        bottom_score = float(mb.group(1))
        report_score = float(score_s)
        chg20 = float(mc.group(1)) if mc else None
        inflow = float(mf.group(1)) if mf else None
        chg60 = float(m60.group(1)) if m60 else None

        # 早期报告（08-27）只披露 60日涨幅、不含 20日与金额 → 分项整体留空，仅存总分与理由
        if chg20 is None or inflow is None:
            rows.append((
                push_date, int(rank), code, sec_name, broad, report_score, data_date, "backfill",
                bottom_score, None, None, None, None, None,
                None, chg60, None, reason,
                *(FUND_RE.search(fund_cell).groups()[:2] if FUND_RE.search(fund_cell) else (None, None)),
            ))
            warns.append("%s #%s %s 早期格式（无20日/金额），仅存总分与理由"
                         % (push_date, rank, sec_name))
            continue

        score_bottom = bottom_score / 100.0 * 30
        score_flow = score_flow_of(inflow)
        score_stability = 20 - abs(chg20) * 2
        score_signal = signal_of(reason)

        # 报告只给总分，故 trend 分项倒推。满分 15，报告里的 chg20/底部评分已四舍五入
        # （如 -0.27% 显示为 -0.3%），故留 0.6 的舍入容差后截断；越界过多才算口径不同。
        trend_raw = report_score - (score_bottom + score_flow + score_stability + score_signal)
        if trend_raw < -0.6 or trend_raw > 16.0:
            warns.append("%s #%s %s 倒推 trend=%.1f 越界（口径可能不同），已置空"
                         % (push_date, rank, sec_name, trend_raw))
            score_trend = None
        else:
            score_trend = min(15.0, max(0.0, trend_raw))

        fm = FUND_RE.search(fund_cell)
        fund_code, fund_name = (fm.group(1), fm.group(2).strip()) if fm else (None, None)

        rows.append((
            push_date, int(rank), code, sec_name, broad, report_score, data_date, "backfill",
            bottom_score, score_bottom, score_flow, score_stability, score_trend, score_signal,
            chg20, chg60, inflow, reason, fund_code, fund_name,
        ))
    return rows, warns


def main():
    conn = history_db.connect()
    try:
        dates = load_trading_dates(conn)
        name_map = load_name_map(conn)
        print("交易日历 %d 天（%s ~ %s），板块名映射 %d 条"
              % (len(dates), dates[0], dates[-1], len(name_map)))

        paths = sorted(glob.glob(os.path.join(REPORT_ROOT, "每日复盘_*.md")))
        live_dates = {r[0] for r in conn.execute(
            "SELECT DISTINCT push_date FROM review_pick_daily WHERE source='live'")}
        all_rows, all_warns, scanned = [], [], 0
        for p in paths:
            rows, warns = parse_report(p, name_map, dates)
            all_warns += warns
            if not rows:
                print("  %-24s 无表格（观望），跳过" % os.path.basename(p))
                continue
            if rows[0][0] in live_dates:
                print("  %-24s 已有 live 记录，跳过回填" % os.path.basename(p))
                continue
            scanned += 1
            print("  %-24s 解析 %d 行，data_date=%s" % (
                os.path.basename(p), len(rows), rows[0][6]))
            all_rows += rows

        if all_rows:
            dates_in = sorted({r[0] for r in all_rows})
            conn.executemany("DELETE FROM review_pick_daily WHERE push_date=? AND source='backfill'",
                             [(d,) for d in dates_in])
            conn.executemany(
                "INSERT OR REPLACE INTO review_pick_daily("
                "push_date, rank_no, sector_code, sector_name, broad_state, score,"
                "data_date, source, bottom_score, score_bottom, score_flow,"
                "score_stability, score_trend, score_signal, chg20, chg60, inflow,"
                "reason, fund_code, fund_name)"
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", all_rows)
            conn.commit()

        print("\n回填完成: %d 份报告 / %d 行" % (scanned, len(all_rows)))
        if all_warns:
            print("⚠️ 需人工核对 %d 条:" % len(all_warns))
            for w in all_warns:
                print("  - " + w)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
