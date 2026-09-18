# -*- coding: utf-8 -*-
"""牛熊仓位灯（保守场外版）：估值+趋势双确认定上限

给未来亚亚：战略层开关，决定股票仓位上限（不是选股）。
估值看上证状态（index_valuation.json），趋势看regime_daily最新全量日
BULL占比（COUNT>=400防单条污染，过期不判只降权由调用方处理）。
规则（少亏优先，回撤10%内）：
  红灯（防御≤30%）：上证高估/泡沫（无 PE/PB，实为点位分位高位） 且 板块BULL<40%
  黄灯（均衡≤50%）：其余混合态
  绿灯（进攻≤70%）：上证合理偏低及以下 且 BULL≥55%
纯标准库，被 daily_review.generate_report 复用。
"""
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from src.common.paths import DATA_ROOT
    from src.common import history_db
    DATA_DIR = DATA_ROOT
except ImportError:
    _REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, _REPO)
    from src.common import history_db
    DATA_DIR = os.path.join(_REPO, "data")


def lamp():
    try:
        vals = json.load(open(os.path.join(DATA_DIR, "index_valuation.json"), encoding="utf-8"))
        vm = {r["name"]: r.get("state", "") for r in vals.get("rows", [])}
    except Exception as e:
        print("  [warn] regime_lamp 估值读取失败（灯色按未知估值处理）: %s" % e)
        vm = {}
    sh = vm.get("上证指数", "")
    # 趋势：regime_daily最新全量日BULL占比
    bull_n = bull_all = 0
    rdate = ""
    try:
        conn = history_db.connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT date FROM regime_daily GROUP BY date HAVING COUNT(*)>=400 "
                "ORDER BY date DESC LIMIT 1").fetchone()
            rdate = row[0] if row else ""
            if rdate:
                rows = conn.execute("SELECT regime FROM regime_daily WHERE date=?", (rdate,)).fetchall()
                bull_all = len(rows)
                bull_n = sum(1 for r in rows if r[0] == "BULL")
        finally:
            conn.close()
    except Exception as e:
        print("  [warn] regime_lamp 趋势读取失败（BULL占比按0处理）: %s" % e)
    ratio = (bull_n / bull_all * 100) if bull_all else 0
    # 上证无蛋卷 PE/PB，state 走"点位分位"档位；估值档位与点位档位都要认，
    # 两套措辞见 fetch_index_valuation.judge_by_pct / judge_by_price_pct（阈值相同）
    bad = sh in ("高估", "泡沫", "点位高位", "点位极端高位")
    good = sh in ("合理偏低", "低估", "极低估", "合理", "点位低位", "点位中下")
    if bad and ratio < 40:
        # 红灯≠空仓：轻仓只做最强1-2个方向（R2榜首），弱方向一律不碰
        return "🔴 红灯 防御", "股票≤30%且只做R2榜首最强方向；其余货基；机动子弹等止跌+点位。", sh, round(ratio, 1), rdate
    if good and ratio >= 55:
        return "🟢 绿灯 进攻", "股票≤70%，底仓+定投正常，机动可打一发。", sh, round(ratio, 1), rdate
    return "🟡 黄灯 均衡", "股票≤50%，持有不动，每月10日再平衡。", sh, round(ratio, 1), rdate


def main():
    lamp_s, advice, sh, ratio, rdate = lamp()
    print("%s 上证%s BULL%.0f%%(%s)：%s" % (lamp_s, sh, ratio, rdate, advice))


if __name__ == "__main__":
    main()
