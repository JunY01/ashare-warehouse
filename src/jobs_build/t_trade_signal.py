# -*- coding: utf-8 -*-
"""做T决策助手（多基金底仓做T版）：恒科 + 资金A三只老份额，全走“底仓1/3做T”。

每只逻辑相同（阈值共用）：
  卖出腿：T份额较锚点反弹 ≥2% 且持有满7天（过1.5%惩罚线）且未明显冲高回落 → 15:00前赎回
  买入腿：T空仓时，实时较昨收跌 ≥2% 且超卖（连跌≥3天或单日≤-3%）→ 15:00前申购买回
  每基金每周买入≤2次；科创50尘仓450不进T池

T本金与锚：
  恒科2400锚4766.16（第一批8/21指数成本）；A500取2900、中证500取1900、创业板取700
  （各约持仓1/3，锚=最新净值/(1+持仓收益率)，老份额持有天数按满7天计）
  恒科走指数点位直连；A轮三只走ETF代理估算净值（标注代理）

用法:
  python t_trade_signal.py                          # 全部基金决策检查
  python t_trade_signal.py --fund hstech             # 只看恒科
  python t_trade_signal.py --fund a500 --log-t-sell  # 执行赎回后记账（可加 --at 价格）
  python t_trade_signal.py --fund a500 --log-t-buy   # 执行申购后记账
  fund key: hstech / a500 / zz500 / cyb
"""
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
BJ = timezone(timedelta(hours=8))

try:
    from src.common.paths import DATA_ROOT, CONFIG_ROOT, REPO_ROOT
    DATA_DIR = DATA_ROOT
except ImportError:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    DATA_DIR = os.path.join(REPO_ROOT, "data")
    try:
        CONFIG_ROOT
    except NameError:
        CONFIG_ROOT = os.path.join(REPO_ROOT, "config")
# 统一引导：独立运行时也能 import src.*（原只加了 data/，独立运行会找不到 fund_realtime）
for _p in (REPO_ROOT,
           os.path.join(REPO_ROOT, "src", "common"),
           os.path.join(REPO_ROOT, "src", "jobs_build"),
           os.path.join(REPO_ROOT, "src", "jobs_fetch")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from src.common import history_db  # noqa: E402
LOG_FILE = os.path.join(DATA_DIR, "t_trade_log.json")
try:
    POS_FILE = os.path.join(CONFIG_ROOT, "positions.json")
except NameError:
    POS_FILE = os.path.join(DATA_DIR, "positions.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"

# ── 方案参数 ────────────────────────────────
START_DATE = "2026-09-03"   # 前两批已满7天随时可赎，A轮全老份额，即刻启用
BUY_DROP_PCT = -2.0         # 买入腿触发：实时较昨收跌幅
SELL_GAIN_PCT = 2.0         # 卖出腿目标：较锚点反弹
FADE_TOL = 0.992            # 现价 ≥ 日内最高×此值，否则视为冲高回落暂缓
MIN_HOLD_DAYS = 7           # 份额持有满7天才赎（过1.5%惩罚线，满30天0费最优）
MAX_HOLD_DAYS = 10          # T在场内超N个交易日未达标 → 休眠当底仓，不折腾
WEEKLY_T_LIMIT = 2          # 每基金每周最多买入次数

FUND_T = [
    # 示例1：指数点位直连做T。t_cap=底仓做T上限（元），anchor=你的建仓成本点位
    {"key": "hstech", "name": "恒科", "full": "恒生科技",
     "kind": "index", "secid": "124.HSTECH", "t_cap": 1000,
     "anchor": 0.0, "buy_date": "2026-01-01", "nav_code": "000000",
     "buy_name": "某某恒生科技联接", "note": "示例"},
    # 示例2：场外净值代理做T。fund_codes 为该基金的份额代码，pos_match 对应 positions.json 名称
    {"key": "a500", "name": "A500", "full": "中证A500",
     "kind": "nav", "fund_codes": ["000000"], "pos_match": "中证A500",
     "etf_secid": "1.512050", "t_cap": 1000, "buy_name": "某某A500联接C"},
]


def fetch_hstech():
    # ulist 批量接口（stock/get 对连续请求风控更严、间歇空返回；ulist+时间戳更稳，与 fetch_global 同款）
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?ut=fa5fd1943c7b386f172d6893dbfba10b"
           "&fltt=2&invt=2&secids=124.HSTECH&fields=f2,f3,f15,f16,f18,f124&_=%d") % int(time.time() * 1000)
    for _ in range(3):
        r = subprocess.run(["curl", "-s", "-m", "10", "-A", UA,
                            "-H", "Referer: https://quote.eastmoney.com/", url],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        if r.returncode == 0 and r.stdout:
            try:
                diff = (json.loads(r.stdout).get("data") or {}).get("diff") or []
                if diff:
                    it = diff[0]
                    # 映射为旧字段名（f43最新/f44最高/f45最低/f60昨收/f86行情时间戳），供 main/t_decide 消费
                    return {"f43": it.get("f2"), "f44": it.get("f15"),
                            "f45": it.get("f16"), "f60": it.get("f18"),
                            "f86": it.get("f124")}
            except Exception:
                pass
        time.sleep(3)
    return None


def load_log():
    if os.path.exists(LOG_FILE):
        log = json.load(open(LOG_FILE, encoding="utf-8"))
    else:
        log = {}
    log.setdefault("buys", [])
    log.setdefault("sells", [])
    log.setdefault("t_buys", {})
    t = log.setdefault("t", {})
    # 兼容旧版单基金t结构 → 迁移为hstech
    if "holding" in t:
        t = {"hstech": {"holding": t.get("holding", True),
                        "anchor": t.get("anchor", 4766.16),
                        "buy_date": t.get("buy_date", "2026-08-21"),
                        "round": t.get("round", 0)}}
        log["t"] = t
    for f in FUND_T:
        st = t.setdefault(f["key"], {"holding": True, "anchor": None,
                                     "buy_date": "2026-08-01", "round": 0})
        if f["key"] == "hstech" and st.get("anchor") is None:
            st["anchor"] = f["anchor"]
            st["buy_date"] = "2026-08-21"
    return log


def save_log(log):
    json.dump(log, open(LOG_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def open_positions(log):
    """旧版FIFO未平仓队列（兼容保留）"""
    queue = [dict(b) for b in log["buys"]]
    remain = sum(s["amount"] for s in log["sells"])
    for b in queue:
        take = min(b["amount"], remain)
        b["amount"] -= take
        remain -= take
    return [b for b in queue if b["amount"] > 0]


def workdays_between(d1, d2):
    n, cur = 0, d1
    while cur < d2:
        cur = date.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            n += 1
    return n


def nav_trend(code="000000"):
    """指定基金近况：连跌天数（超卖过滤用，DB缺失返回None降级）"""
    try:
        conn = history_db.connect(readonly=True)
        rows = conn.execute("SELECT nav FROM fund_nav_daily WHERE code=? "
                            "ORDER BY date DESC LIMIT 6", (code,)).fetchall()
        conn.close()
        navs = [r[0] for r in rows][::-1]
        if len(navs) < 6:
            return None
        down = 0
        for i in range(len(navs) - 1, 0, -1):
            if navs[i] < navs[i - 1]:
                down += 1
            else:
                break
        return {"consec_down": down}
    except Exception:
        return None


def latest_nav(codes):
    """fund_nav_daily最新净值，返回(nav, code)。"""
    try:
        conn = history_db.connect(readonly=True)
        for c in codes:
            r = conn.execute("SELECT nav FROM fund_nav_daily WHERE code=? "
                             "ORDER BY date DESC LIMIT 1", (c,)).fetchone()
            if r:
                conn.close()
                return float(r[0]), c
        conn.close()
    except Exception:
        pass
    return None, None


def decide_one(name, tcap, cur, hi, lo, chg, anchor, st, log, today, live,
               buy_name, tr, key):
    """单基金做T决策，返回展示行。"""
    pre = "" if live else "（观察）"
    if today.isoformat() < START_DATE:
        return ["%s：%s前仅观察不操作" % (name, START_DATE)]
    lines = ["%s｜T额度%d%s" % (name, tcap, "在场内" if st["holding"] else "空仓等买回")]
    week = today.isocalendar()[:2]
    n_week = len([b for b in log.get("t_buys", {}).get(key, [])
                  if date.fromisoformat(b).isocalendar()[:2] == week])
    if st["holding"]:
        gain = (cur / anchor - 1) * 100
        age = workdays_between(date.fromisoformat(st["buy_date"]), today)
        if gain >= SELL_GAIN_PCT:
            if age < MIN_HOLD_DAYS:
                lines.append("⏳ 卖出腿%s：已达%+.1f%%但持有仅%d天，未满%d天赎要交1.5%%，再等%d天" % (
                    pre, gain, age, MIN_HOLD_DAYS, MIN_HOLD_DAYS - age))
            elif cur >= hi * FADE_TOL:
                lines.append("🔔 卖出腿%s：较锚点%+.1f%%（目标+%g%%）且未回落 → **15:00前赎回%d元**%s" % (
                    pre, gain, SELL_GAIN_PCT, tcap, "" if live else "（明日再定）"))
            else:
                lines.append("👁 卖出腿%s：已达标%+.1f%%但较高点回落超0.8%%，等企稳再赎" % (pre, gain))
        elif age > MAX_HOLD_DAYS:
            lines.append("⏹ T份额持有超%d天仅%+.1f%%，休眠当底仓死拿，不折腾" % (MAX_HOLD_DAYS, gain))
        else:
            lines.append("✅ 不动%s：T在场内%+.1f%%（目标+2%%），持有%d天" % (pre, gain, age))
    else:
        oversold = True if tr is None else (tr["consec_down"] >= 3 or chg <= -3.0)
        bounce = (cur - lo) / lo * 100 if lo else 0
        if chg <= BUY_DROP_PCT and n_week < WEEKLY_T_LIMIT and oversold:
            lines.append("🔔 买入腿%s：实时%+.2f%%≤%.1f%%＋超卖确认 → **15:00前申购%s%d元**（本周%d/%d，回升>1%%慎接，现回升%.1f%%）%s" % (
                pre, chg, BUY_DROP_PCT, buy_name, tcap, n_week, WEEKLY_T_LIMIT, bounce,
                "" if live else "（明日再定）"))
        elif n_week >= WEEKLY_T_LIMIT:
            lines.append("⏹ 本周买入已满%d次，只卖不买" % WEEKLY_T_LIMIT)
        elif chg <= BUY_DROP_PCT and not oversold:
            lines.append("👁 买入腿%s：跌幅%+.1f%%达标但非超卖（连跌不足3天），再等一等防接飞刀" % (pre, chg))
        else:
            lines.append("✅ 不动%s：T空仓等买回，实时%+.2f%%未达买入阈值%.1f%%" % (pre, chg, BUY_DROP_PCT))
    return lines


def t_decide(px, hi, lo, prev, log, today, live=True):
    """兼容旧接口：恒科单基金决策。"""
    f = FUND_T[0]
    st = log["t"]["hstech"]
    if st.get("anchor") is None:
        st["anchor"] = f["anchor"]
    chg = (px / prev - 1) * 100
    head = "恒科%.0f（%+.2f%%，高%.0f/低%.0f）" % (px, chg, hi, lo)
    return [head] + decide_one("恒科", f["t_cap"], px, hi, lo, chg, st["anchor"],
                               st, log, today, live, f["buy_name"],
                               nav_trend(f["nav_code"]), "hstech")


def t_decide_all(mode):
    """全部T基金决策行（daily_1430复用）。返回(teen_lines, warn)。"""
    from datetime import date as _date
    try:
        from src.jobs_fetch import fund_realtime as fr
    except ImportError:
        import fund_realtime as fr
    log = load_log()
    today = _date.today()
    live = (mode == "抢票")
    out = []
    # 恒科：指数直连
    d = fetch_hstech()
    if d and d.get("f43") != "-":
        px, hi, lo, prev = (d.get(k) for k in ("f43", "f44", "f45", "f60"))
        out += t_decide(px, hi, lo, prev, log, today, live=live)
    else:
        out += ["恒科做T：行情暂无"]
    # A轮三只：ETF代理估净值
    try:
        pos = json.load(open(POS_FILE, encoding="utf-8"))
    except Exception:
        pos = {}
    for f in FUND_T[1:]:
        mv = pnl = None
        try:
            for x in pos.get("资金A", {}).get("基金", []):
                if f["pos_match"] in x.get("名称", ""):
                    mv, pnl = x.get("市值"), x.get("收益率", 0) or 0
                    break
        except Exception:
            pass
        nav, code = latest_nav(f["fund_codes"])
        q = fr.fetch_etf_realtime(f["etf_secid"])
        time.sleep(1.2)
        if nav is None or q is None or not mv:
            out += ["%s做T：数据暂缺（净值/行情/持仓）" % f["name"]]
            continue
        anchor = nav / (1 + (pnl or 0) / 100)
        est = round(nav * (1 + q["chg_pct"] / 100), 4)
        hi_e = round(nav * (q["high"] / q["prev_close"]), 4) if q.get("prev_close") else est
        lo_e = round(nav * (q["low"] / q["prev_close"]), 4) if q.get("prev_close") else est
        st = log["t"][f["key"]]
        if st.get("anchor") is None:
            st["anchor"] = round(anchor, 4)
        head = "%s净值约%.4f（代理%+.2f%%，成本锚%.4f）" % (f["name"], est, q["chg_pct"], st["anchor"])
        out += [head] + decide_one(f["name"], f["t_cap"], est, hi_e, lo_e,
                                   q["chg_pct"], st["anchor"], st, log, today,
                                   live, f["buy_name"], nav_trend(code), f["key"])
    return out


def main():
    args = sys.argv[1:]
    only = args[args.index("--fund") + 1] if "--fund" in args else None
    log = load_log()
    if "--log-t-sell" in args or "--log-t-buy" in args:
        key = only or "hstech"
        f = next(x for x in FUND_T if x["key"] == key)
        st = log["t"][key]
        if "--log-t-sell" in args:
            st["holding"] = False
            if "--at" in args:
                st["anchor"] = float(args[args.index("--at") + 1])
            st["sell_date"] = date.today().isoformat()
            save_log(log)
            print("已记%sT卖出：%d转空仓，锚点=%.4f" % (f["name"], f["t_cap"], st["anchor"]))
        else:
            st["holding"] = True
            if "--at" in args:
                st["anchor"] = float(args[args.index("--at") + 1])
            st["buy_date"] = date.today().isoformat()
            st["round"] = st.get("round", 0) + 1
            log["t_buys"].setdefault(key, []).append(date.today().isoformat())
            save_log(log)
            print("已记%sT买回：第%d轮，锚点=%.4f" % (f["name"], st["round"], st["anchor"]))
        return
    # 旧版记账兼容
    if "--log-buy" in args:
        amt = int(args[args.index("--log-buy") + 1])
        log["buys"].append({"date": date.today().isoformat(), "amount": amt, "ref_close": 0})
        save_log(log)
        print("已记买入 %d 元（旧版口径）" % amt)
        return
    if "--log-sell" in args:
        amt = int(args[args.index("--log-sell") + 1])
        log["sells"].append({"date": date.today().isoformat(), "amount": amt})
        save_log(log)
        print("已记赎回 %d 元（旧版口径）" % amt)
        return

    d = fetch_hstech()
    if not d or d.get("f43") == "-":
        sys.exit("恒生科技行情获取失败")
    px, hi, lo, prev, ts = (d.get(k) for k in ("f43", "f44", "f45", "f60", "f86"))
    qt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(BJ)

    print("=" * 62)
    print("恒生科技 %.2f  昨收 %.2f (%+.2f%%)  日内高 %.2f / 低 %.2f"
          % (px, prev, (px / prev - 1) * 100, hi, lo))
    print("行情时间 %s ｜ 预判收盘区间约 %.0f ~ %.0f（±1%%尾盘漂移）"
          % (qt.strftime("%H:%M"), px * 0.99, px * 1.01))
    if qt.date() != date.today():
        print("⚠️ 行情非今日（港股休市？），今日信号无效")
        return
    live = "14:00" <= qt.strftime("%H:%M") <= "14:55"
    if not live:
        print("ℹ️ 当前不在 14:00~14:55 决策窗口，以下读数仅供观察")
    funds = [f for f in FUND_T if not only or f["key"] == only]
    if funds[0]["key"] == "hstech":
        for line in t_decide(px, hi, lo, prev, log, date.today(), live=live):
            print(("- " if not line.startswith("恒科") else "") + line)
    if [f for f in funds if f["key"] != "hstech"]:
        print("-" * 62)
        print("A轮做T（ETF代理净值，老份额随时可赎）：")
        try:
            from src.jobs_fetch import fund_realtime as fr
        except ImportError:
            import fund_realtime as fr
        try:
            pos = json.load(open(POS_FILE, encoding="utf-8"))
        except Exception:
            pos = {}
        for f in funds:
            if f["key"] == "hstech":
                continue
            mv = pnl = None
            try:
                for x in pos.get("资金A", {}).get("基金", []):
                    if f["pos_match"] in x.get("名称", ""):
                        mv, pnl = x.get("市值"), x.get("收益率", 0) or 0
                        break
            except Exception:
                pass
            nav, code = latest_nav(f["fund_codes"])
            q = fr.fetch_etf_realtime(f["etf_secid"])
            if nav is None or q is None or not mv:
                print("  %s：数据暂缺" % f["name"])
                continue
            anchor_st = log["t"][f["key"]]
            if anchor_st.get("anchor") is None:
                anchor_st["anchor"] = round(nav / (1 + (pnl or 0) / 100), 4)
            est = round(nav * (1 + q["chg_pct"] / 100), 4)
            hi_e = round(nav * (q["high"] / q["prev_close"]), 4) if q.get("prev_close") else est
            lo_e = round(nav * (q["low"] / q["prev_close"]), 4) if q.get("prev_close") else est
            print("  %s净值约%.4f（代理%+.2f%%，成本锚%.4f，额度%d）" % (
                f["name"], est, q["chg_pct"], anchor_st["anchor"], f["t_cap"]))
            for line in decide_one(f["name"], f["t_cap"], est, hi_e, lo_e,
                                   q["chg_pct"], anchor_st["anchor"], anchor_st,
                                   log, date.today(), live, f["buy_name"],
                                   nav_trend(code), f["key"]):
                print("  - " + line)
    print("=" * 62)
    print("费率提醒：赎回按持有天数收费（<7天1.5%，满30天0费）。T额度只用老份额。")
    print("执行后记账：--fund KEY --log-t-sell/--log-t-buy [--at 价格]")


if __name__ == "__main__":
    main()
