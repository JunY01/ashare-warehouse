# -*- coding: utf-8 -*-
"""每日14:30抢票机（手动运行，场外专用）

六区制：
  A区 持仓决策（赶15:00截点）：买/卖/等 + QDII限购T+2提示
  B区 行业关注（不赶截点）：短期Top5 + 场外C类代码 + 持有期/止损，可观察一天再进。
      ⚠ 本区**选股未经留存验证**（`validate_short.py` 2026-09-17 独立回放：线上 v2 超额 −0.03%≈随机；
      仅"持有挡位"bull 一挡晋升、留存只有 6 个信号日）。当参考，不当交易依据。
  C区 抄底观察（庄家线·左侧）：观察名单，不直接交易
  D区 全市场恐慌监控（超跌反弹线）：任一指数 20 日跌幅≤-20% 才展开；平时只一行。
      验证与边界见 reports/超跌反弹十万次迭代_20260916.md。只提示机会，不给金额、不定买卖。
  E区 红利 40 日收益差（红利择时尺）：买/卖点分位读数；见 red40_monitor.py。
  F区 宽基深跌区（半年线位置尺）：偏离 MA120 ≤-15% 才展开；平时一行。
      验证见 reports/三重背离验证_20260917.md（背离无效、位置有效）。只提示位置，不定买卖。

用法:
  python daily_1430.py              # 一键：刷新快照+估值+净值 → 六区 → 落库+报告
  python daily_1430.py --no-refresh # 跳过联网刷新，只用本地数据出六区（离线复核）

纯标准库。报告写 reports/每日14点30_YYYYMMDD.md（同日重跑覆盖）。
落库 industry_push_daily（幂等），次日复盘可算命中。
"""
import datetime
import json
import os
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

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
try:
    from src.common import history_db  # noqa: E402
except ImportError:
    import history_db  # noqa: E402
from src.common.market_map import (  # noqa: E402
    PROXY_ETF, cfg_path as _cfg_path, load_json)
# 短线/抄底扫描逻辑已抽出，此处原位再导出（research 仍 `from daily_1430 import broad_of/short_evaluate/EXCLUDE_KW/chg5_accel`）
from src.jobs_build.red40_monitor import red40_monitor, red40_lines  # noqa: E402
from src.jobs_build.deepzone_monitor import deepzone_monitor, deepzone_lines  # noqa: E402
from src.jobs_build.daily_1430_scan import (  # noqa: E402,F401
    adaptive_short_params, zhuang_cfg, data_date, chg5_accel, short_evaluate,
    broad_of, scan_zhuang, scan_short, panic_monitor, panic_lines,
    EXCLUDE_KW, ZHUANG_CFG)


TODAY = datetime.date.today()
TODAY_S = TODAY.strftime("%Y-%m-%d")
TODAY_SHORT = TODAY.strftime("%Y%m%d")
HOLD_DAYS = 5       # 短期持有期（交易日，v2：回放验证持有5天纪律最优）
STOP_PCT = -6.0     # 短期止损线%（v2）
TAKE_PCT = 8.0      # 短期止盈线%（v2：不提前截断加速段）
TRIAL_PER = "1000-2000/只"  # 试错仓




# 已持仓/清仓过的方向：短期不再推（避免重复下注）

# 持仓/底仓 → ETF代理（盘中估算用，纯标准库 urllib，见 fund_realtime）
# 定义见 src/common/market_map.py（与行业映射同处唯一维护）

# 抄底观察名单参数：来源 research/sweep_zhuang.py 十四万组选拔冠军（valid+1.55/留存-0.91未晋升，
# 故仅作观察名单不直接交易）。若日后 model_zhuang.json 晋升则优先读它。

def now_mode():
    """抢票/复盘/预览三态：15:00是场外截点，不是收盘意义。”
    """
    t = datetime.datetime.now().strftime("%H:%M")
    if "14:00" <= t <= "15:00":
        return "抢票", t
    if t > "15:00":
        return "复盘", t
    return "预览", t


def refresh_light():
    """轻量刷新：快照 + 估值json + 基金净值。失败降级，不阻断出报告。”
    14:30复盘必须依据最新实时数据：默认联网刷新，只有--no-refresh才用本地。
    """
    steps = [
        ([sys.executable, os.path.join(REPO_ROOT, "src", "jobs_fetch", "update_data.py"), "--snapshot-only"], "快照", 300),
        ([sys.executable, os.path.join(REPO_ROOT, "src", "jobs_build", "fetch_index_valuation.py"), "--json"], "估值", 180),
        ([sys.executable, os.path.join(REPO_ROOT, "src", "jobs_fetch", "fetch_fund_nav.py")], "基金净值", 180),
    ]
    warns = []
    for cmd, label, to in steps:
        try:
            r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=to)
            if r.returncode != 0:
                first = (r.stderr or "").strip().splitlines()
                warns.append("%s失败（用本地旧数据）：%s" % (label, first[0][:120] if first else "无输出"))
        except Exception as e:
            warns.append("%s异常（用本地旧数据）：%s" % (label, str(e)[:120]))
    return warns




def load_fund_list():
    from src.common.fund_lookup import load_fund_list as _lfl
    return _lfl()


def find_c_funds(fund_list, sector_name, broad=""):
    from src.common.fund_lookup import find_c_funds_broad
    return find_c_funds_broad(fund_list, sector_name, broad)


def intraday_line():
    """A区盘中一句话：ETF代理估算持仓今日方向（纯标准库，不用AkShare）。”
    实时数据：东财push2接口当时涨跌幅，仅供15:00前决策参考，晚间以净值为准。
    """
    try:
        try:
            from src.jobs_fetch import fund_realtime as fr
        except ImportError:
            import fund_realtime as fr
        pos = load_json(_cfg_path("positions.json"))
        names = [f["名称"] for f in pos.get("资金A", {}).get("基金", [])]
    except Exception as e:
        return ["盘中估算跳过：%s" % str(e)[:100]]
    lines = []
    for name in names:
        secid = next((v for k, v in PROXY_ETF.items() if k in name), None)
        if not secid:
            continue
        try:
            q = fr.fetch_etf_realtime(secid)
            time.sleep(1.2)
            if not q:
                lines.append("%s：行情暂无" % name)
                continue
            flag = "🔴" if q["chg_pct"] <= -1 else ("🟢" if q["chg_pct"] >= 1 else "⚪")
            lines.append("%s %s 代理约%+.2f%%（仅参考，晚间以净值为准）" % (flag, name, q["chg_pct"]))
        except Exception:
            lines.append("%s：行情暂无" % name)
    return lines or ["盘中估算暂无"]


def t_leg_lines(mode):
    """做T腿并入A区：恒科＋A轮三只，全走底仓1/3做T（复用 t_trade_signal，记账走 t_trade_log.json）。"""
    try:
        try:
            from src.jobs_build import t_trade_signal as tt
        except ImportError:
            import t_trade_signal as tt
        return tt.t_decide_all(mode)
    except Exception as e:
        return ["做T跳过：%s" % str(e)[:100]]


def review_history(conn):
    """往期命中复盘：8天前推送的行业，用 sector_kline 算随后5个交易日涨幅。”
    """
    try:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT push_date FROM industry_push_daily "
            "WHERE push_date <= date(?, '-8 days') ORDER BY push_date DESC LIMIT 3", (TODAY_S,))]
    except Exception:
        return []
    if not dates:
        return ["\n> 往期复盘：暂无8天前推送（新表刚建，攒几天就有命中率了）。"]
    out = ["\n**往期命中（推送日→随后5交易日板块涨幅）**"]
    for pd in dates:
        rows = conn.execute(
            "SELECT sector_code, sector_name, score FROM industry_push_daily "
            "WHERE push_date=? ORDER BY rank_no", (pd,)).fetchall()
        parts = []
        for code, name, score in rows:
            kl = conn.execute(
                "SELECT close FROM sector_kline WHERE code=? AND date>=? "
                "ORDER BY date LIMIT 7", (code, pd)).fetchall()
            if len(kl) >= 6 and kl[0][0]:
                r = (kl[5][0] / kl[0][0] - 1) * 100
                parts.append("%s%+.1f%%" % (name, r))
        if parts:
            out.append("- %s：%s" % (pd, "、".join(parts)))
    return out


def main():
    no_refresh = "--no-refresh" in sys.argv
    mode, hm = now_mode()
    warns = [] if no_refresh else refresh_light()
    if no_refresh:
        warns.append("离线模式：未刷新，只用本地数据")

    conn = history_db.connect()
    try:
        ddate = data_date(conn)
        vals = load_json(os.path.join(DATA_DIR, "index_valuation.json"))
        poss = load_json(_cfg_path("positions.json"))
        val_map = {r["name"]: r for r in vals.get("rows", [])}
        sh_state = val_map.get("上证指数", {}).get("state", "未知")

        # —— A区：复用 daily_review 持仓复盘 + 截点头 ——
        try:
            from src.jobs_build import daily_review as dr
        except ImportError:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import daily_review as dr
        a_body = dr.review_holdings(poss, vals)
        # daily_review 沿用了旧文案“第2批”，按已完成批次自动纠正
        try:
            done = poss.get("资金B", {}).get("底仓", {}).get("已完成批次", 2)
            a_body = a_body.replace("今天是底仓第2批执行日", "今天是底仓第%d批执行日" % (done + 1))
        except Exception:
            pass
        a_head = ("🔔 **A区·持仓决策（%s %s，截点15:00）**：%s\n" % (
            TODAY_S, hm, "现在下单算今日净值，抓紧" if mode == "抢票"
            else ("已过截点，今日只复盘、操作顺延明日" if mode == "复盘" else "盘前预览，开盘后以14:00后数据为准")))
        a_mid = "\n".join("- " + x for x in intraday_line())

        # —— B区：短期Top5 ——
        hold_days, take_pct, stop_pct, trial_per, gear = adaptive_short_params()
        cands, reg_date, stale = scan_short(conn)
        funds = load_fund_list()
        push_rows, b_lines = [], []
        for i, c in enumerate(cands, 1):
            hits = find_c_funds(funds, c["name"], c["broad"])
            fstr = "、".join("`%s`%s" % (cc, nn[:14]) for cc, nn in hits) if hits else "暂无匹配（先观察，不追）"
            b_lines.append("| %d | %s | %.0f | %s | %s | %d天%+.0f%%/%+.0f%% |" % (
                i, c["name"], c["score"], c["reason"], fstr, hold_days, take_pct, stop_pct))
            push_rows.append((TODAY_S, i, c["code"], c["name"], c["score"], c["reason"],
                              hits[0][0] if hits else "", hits[0][1] if hits else "",
                              "上证%s" % sh_state, hold_days, stop_pct, mode))
        if push_rows:
            conn.execute("DELETE FROM industry_push_daily WHERE push_date=?", (TODAY_S,))
            conn.executemany("INSERT OR REPLACE INTO industry_push_daily VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", push_rows)
            conn.commit()

        # —— C区：抄底观察名单（庄家线·左侧·观察） ——
        try:
            sv = load_json(os.path.join(DATA_DIR, "sector_valuation_map.json"))
            val_map = sv.get("map", {})
        except Exception:
            val_map = {}
        zhuang = scan_zhuang(conn, val_map)
        zcfg = zhuang_cfg()
        zh_hold, zh_tp, zh_sl = zcfg["hold"], zcfg["tp"], zcfg["sl"]
        zh_rows = []
        zh_lines = []
        for i, c in enumerate(zhuang, 1):
            hits = find_c_funds(funds, c["name"], c["broad"])
            fstr = "、".join("`%s`%s" % (cc, nn[:14]) for cc, nn in hits) if hits else "暂无匹配"
            shield = "🟢" if (c["pb_pct"] is not None and c["pb_pct"] <= 0.3) else (
                "🟠" if (c["pb_pct"] is not None and c["pb_pct"] >= 0.6) else "⚪")
            zh_lines.append("| %d | %s%s | %.0f | %s | %s | %d天%+.0f%%/%+.0f%% |" % (
                i, shield, c["name"], c["score"], c["reason"], fstr, zh_hold, zh_tp, zh_sl))
            zh_rows.append((TODAY_S, i, c["code"], c["name"], c["score"], c["reason"],
                            hits[0][0] if hits else "", hits[0][1] if hits else "",
                            "上证%s" % sh_state, zh_hold, zh_sl))
        if zh_rows:
            conn.execute("DELETE FROM zhuang_pick_daily WHERE push_date=?", (TODAY_S,))
            conn.executemany(
                "INSERT OR REPLACE INTO zhuang_pick_daily VALUES(?,?,?,?,?,?,?,?,?,?,?)", zh_rows)
            conn.commit()

        # —— 报告 ——
        L = []
        L.append("# ⏰ 每日14点30 %s（%s·%s）" % (TODAY_S, mode, hm))
        L.append("> 数据日 %s｜趋势表 %s%s｜估值日 %s｜短线%s·试错仓%s｜持有%d天 止盈%+.0f%%/止损%+.0f%%" % (
            ddate, reg_date or "无", "（过期降权）" if stale else "",
            vals.get("date", "?"), gear, trial_per, hold_days, take_pct, stop_pct))
        for w in warns:
            L.append("> ⚠️ " + w)
        L.append("\n## A区·持仓决策（赶15:00）\n")
        L.append(a_head)
        L.append("**盘中代理参考**")
        L.append(a_mid)
        L.append("\n**做T腿（底仓1/3，老份额）**")
        L.append("\n".join("- " + x for x in t_leg_lines(mode)))
        L.append("\n" + a_body)
        L.append("\nQDII（恒生科技）限购拆3~5天、T+2确认；15:00前提交按今日净值。\n")
        L.append("## B区·短期行业（可等，不抢截点）\n")
        L.append("短期=爆发力（5日动量30+加速15+连续流入25+信号时效20+广度10），满分100，≥55入选。")
        if b_lines:
            L.append("| # | 行业 | 评分 | 理由 | 场外C类 | 纪律 |")
            L.append("|---|---|---|---|---|---|")
            L.extend(b_lines)
            L.append("\n大盘%s，短期试错不看估值下单、但标红提示；单只%s，总试错不超机动1/3。" % (sh_state, trial_per))
        else:
            L.append("今日无达标短期行业，不追。")
        L.append("> ⚠ **本区选股未经留存验证**（2026-09-17 独立回放：线上 v2 超额 −0.03%，≈随机；"
                 "仅持有挡位 bull 一挡晋升、留存仅 6 个信号日）。**当参考，不当交易依据**；"
                 "真金白银走 A 区纪律。")
        for hl in review_history(conn):
            L.append(hl)
        L.append("\n## C区·抄底观察（庄家线·左侧·观察名单）\n")
        L.append("回撤≤%+.0f%%、底部≥%.0f分、20日主力净流入>0、5日%+.0f%%~%+.0f%%。🟢估值安全垫(低价位)/🟠偏贵/⚪无行业估值。" % (
            ZHUANG_CFG["dd250"], ZHUANG_CFG["bscore"], ZHUANG_CFG["r5_lo"], ZHUANG_CFG["r5_hi"]))
        if zh_lines:
            L.append("| # | 行业 | 评分 | 理由 | 场外C类 | 纪律 |")
            L.append("|---|---|---|---|---|---|")
            L.extend(zh_lines)
            L.append("\n> 说明：十四万网格选拔留存超额为负，本区仅作**观察名单**不直接交易；左侧分批（半仓+破位止损），右侧确认（5日动量转正）再加仓。")
        else:
            L.append("今日无达标抄底候选，观望（抄底要等回撤到位+资金转正，宁缺毋滥）。")
        # —— D区：全市场恐慌监控（超跌反弹线；平时一行，触发才展开） ——
        L.append("\n## D区·全市场恐慌监控（超跌反弹线·只在触发时给机会）\n")
        try:
            pan = panic_monitor()
            L.extend(panic_lines(pan))
        except Exception as e:  # 监控失败不影响 A/B/C 三区出报告
            L.append("- 监控不可用（%s：%s）" % (type(e).__name__, e))
        # —— E区：红利 40 日收益差（红利择时尺；平时一行读数，卡线才给动作） ——
        try:
            L.extend(red40_lines(red40_monitor()))
        except Exception as e:  # 尺子失败不影响 A~D 四区出报告
            L.append("\n## E区·红利 40 日收益差（红利择时尺·不直接下单）\n")
            L.append("- 监控不可用（%s：%s）" % (type(e).__name__, e))
        # —— F区：宽基深跌区（半年线位置尺；平时一行，到区间才展开） ——
        try:
            L.extend(deepzone_lines(deepzone_monitor()))
        except Exception as e:  # 尺子失败不影响 A~E 五区出报告
            L.append("\n## F区·宽基深跌区（半年线位置尺·只在到区间时才展开）\n")
            L.append("- 监控不可用（%s：%s）" % (type(e).__name__, e))
        L.append("\n---\n仅个人研究，不构成投资建议。数据：东方财富/蛋卷/腾讯/中证指数。")
        report = "\n".join(L)
        os.makedirs(REPORT_DIR, exist_ok=True)
        rp = os.path.join(REPORT_DIR, "每日14点30_%s.md" % TODAY_SHORT)
        with open(rp, "w", encoding="utf-8") as f:
            f.write(report)
        print("模式:%s 数据日:%s 趋势:%s%s" % (mode, ddate, reg_date, "过期" if stale else ""))
        print("B区Top%d已落库 industry_push_daily" % len(push_rows))
        print("报告:%s" % rp)
        for w in warns:
            print("WARN:", w)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
