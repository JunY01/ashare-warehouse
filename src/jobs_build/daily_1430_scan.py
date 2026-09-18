# -*- coding: utf-8 -*-
"""daily_1430 的短线(B区)/抄底(C区)扫描逻辑（从 daily_1430.py 抽出，行为等价）。

含自适应挡位、regime/资金/K线读取、短线评分与抄底特征。daily_1430.py 仅保留
编排、A区持仓决策与报告拼装，扫描逻辑统一从此模块引用。

纯标准库。
"""
import datetime
import json
import os
import sys

try:
    from src.common.paths import DATA_ROOT
    DATA_DIR = DATA_ROOT
except ImportError:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    DATA_DIR = os.path.join(REPO_ROOT, "data")
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
from src.common.market_map import (  # noqa: E402
    SECTOR_TO_BROAD, cfg_path as _cfg_path, read_csv)

TODAY = datetime.date.today()

def adaptive_short_params():
    """牛熊自适应短线挡位。给未来亚亚：优先读 model_short.json（三挡来自十万网格
    分regime选拔+留存终验+邻域稳定）；缺挡位回退保守默认。
    牛hold30是数据验证过的（valid+3.48留存+1.28）。
    红灯默认持有10天而非3天：费率悬崖（7天内赎1.5%）+ 十万组审计熊regime费后
    hold3仅0.9%为正、hold5仅3.3%为正、hold10有72.7%为正——3天必亏手续费。
    返回 (持有天, 止盈%, 止损%, 试错仓, 挡名)。"""
    fallback = {"🟢": (20, 12.0, -8.0, "2000-3000/只", "绿灯长持"),
                "🟡": (10, 8.0, -6.0, "1000-2000/只", "黄灯中持"),
                "🔴": (10, 5.0, -4.0, "500-1000/只", "红灯短打")}
    lamp_key, ver = "🔴", ""
    try:
        from src.jobs_build import regime_lamp as _lamp
        s, _, _, _, _ = _lamp.lamp()
        lamp_key = s[0]
    except Exception:
        try:
            import regime_lamp as _lamp2
            s, _, _, _, _ = _lamp2.lamp()
            lamp_key = s[0]
        except Exception as e:
            print("  [warn] 灯色读取失败，短线挡位按红灯兜底: %s" % e)
    color = {"🟢": "bull", "🟡": "chop"}.get(lamp_key, "bear")
    try:
        import json as _json
        m = _json.load(open(_cfg_path("model_short.json"), encoding="utf-8"))
        g = (m.get("gears", {}) or {}).get(color)
        ver = m.get("version", "")
        if g:
            trial = {"bull": "2000-3000/只", "chop": "1000-2000/只", "bear": "500-1000/只"}[color]
            return (int(g["hold"]), float(g["tp"]), float(g["sl"]), trial,
                    "%s·模型%s" % (REG_CN.get(color, color), ver))
    except Exception as e:
        print("  [warn] model_short.json 读取失败，短线挡位用默认值: %s" % e)
    hold, tp, sl, trial, gear = fallback.get(lamp_key, fallback["🔴"])
    return hold, tp, sl, trial, gear + "·默认"


REG_CN = {"bull": "绿灯长持", "chop": "黄灯中持", "bear": "红灯短打"}

EXCLUDE_KW = ["半导体", "芯片", "医药", "创新药", "银行", "恒生", "港股通", "ST", "*ST"]

ZHUANG_CFG = {"dd250": -15.0, "bscore": 55.0, "cd_lo": None, "cd_hi": None,
              "flow_mode": "abs", "zthr": 0.0,
              "r20_lo": None, "r20_hi": None, "r5_lo": -4.0, "r5_hi": 6.0,
              "cutoff": 45.0, "topn": 5, "hold": 30, "tp": 12.0, "sl": -4.0}


def zhuang_cfg():
    """抄底线参数：优先 model_zhuang.json（晋升后），否则用冠军观察参数。"""
    mp = _cfg_path("model_zhuang.json")
    if os.path.exists(mp):
        try:
            p = json.load(open(mp, encoding="utf-8"))["params"]
            return {**ZHUANG_CFG, **p}
        except Exception:
            pass
    return dict(ZHUANG_CFG)


def data_date(conn):
    try:
        d = conn.execute("SELECT MAX(date) FROM sector_daily").fetchone()[0]
        return d or "未知"
    except Exception:
        return "未知"


def regime_map(conn):
    """最新 regime_daily；如过期>5天则标记 stale，短期算法降权使用。”
    """
    try:
        # 最新且有全量覆盖的日期（排除上证单条盘中行之类的污染日）
        row = conn.execute(
            "SELECT date FROM regime_daily GROUP BY date HAVING COUNT(*) >= 400 "
            "ORDER BY date DESC LIMIT 1").fetchone()
        d = row[0] if row else None
    except Exception:
        return {}, None, True
    if not d:
        return {}, None, True
    stale = (TODAY - datetime.date.fromisoformat(d)).days > 5
    rows = conn.execute(
        "SELECT code, regime, bottom_score, engine, alert, cross_age, cloud_dist "
        "FROM regime_daily WHERE date=?", (d,)).fetchall()
    m = {r[0]: {"regime": r[1], "bottom_score": r[2] or 0, "engine": r[3],
                "alert": r[4], "cross_age": r[5] or 999, "cloud_dist": r[6] or 0}
         for r in rows}
    return m, d, stale


def kline_tail(conn, days=12):
    """每板块近N日收盘，用于算5日涨幅与加速度（比快照更准）。”
    """
    rows = conn.execute(
        "SELECT code, date, close FROM sector_kline ORDER BY code, date").fetchall()
    out = {}
    for code, d, c in rows:
        out.setdefault(code, []).append((d, c))
    return {k: v[-days:] for k, v in out.items() if len(v) >= 6}


def flow_stats(conn):
    """近6日资金：最新是否流入 + 连续流入天数 + 5日累计。”
    """
    try:
        rows = conn.execute(
            "SELECT code, date, main_net_wan FROM sector_flow_daily "
            "ORDER BY code, date").fetchall()
    except Exception:
        return {}
    per = {}
    for code, d, v in rows:
        per.setdefault(code, []).append((d, v or 0))
    out = {}
    for code, pts in per.items():
        last6 = pts[-6:]
        consec, s = 0, 0.0
        for _, v in reversed(last6):
            if v > 0:
                consec += 1
            else:
                break
        s = sum(v for _, v in last6[-5:])
        out[code] = {"latest": last6[-1][1], "consec": consec, "sum5": s}
    return out


def chg5_accel(closes):
    """closes: [(date, close)...]，返回(5日%, 加速度)。”
    """
    cs = [c for _, c in closes if c]
    if len(cs) < 6:
        return None, None
    c5 = (cs[-1] / cs[-6] - 1) * 100
    acc = None
    if len(cs) >= 11 and cs[-11]:
        prev5 = (cs[-6] / cs[-11] - 1) * 100
        acc = c5 - prev5
    return round(c5, 2), (round(acc, 2) if acc is not None else None)


def short_evaluate(name, c5, acc, fl, chg20, chg_today, adv, dec, fmv, ri, stale,
                   caps=None, mom_peak=3.0, w=None, cutoff=55.0):
    """单板块短线评估（线上 scan_short 与线下回放共用同一套门槛+权重）。
    fl={latest,consec,sum5}；ri={regime,alert,bottom_score,engine,cross_age}。
    caps=(c20_lo,c20_hi,c5_lo,c5_hi,today_lo,today_hi)，默认(-10,12,-3,8,-4,6)。
    w=(动量,加速,资金,信号,广度)权重，默认(30,15,25,20,10)和为100。
    返回 None（过滤）或 dict(score, sig_txt)。"""
    c20_lo, c20_hi, c5_lo, c5_hi, t_lo, t_hi = caps or (-10, 12, -3, 8, -4, 6)
    if any(k in name for k in EXCLUDE_KW):
        return None
    if c5 is None:
        return None
    # —— 短线硬门槛（比老评分松60日、紧当日/5日）——
    if not (c20_lo <= chg20 <= c20_hi):
        return None
    if not (c5_lo <= c5 <= c5_hi):
        return None
    if not (t_lo <= chg_today <= t_hi):
        return None
    if not (fl["latest"] > 0 or fl["sum5"] > 0):
        return None
    if fmv and fmv < 20:
        return None
    regime, alert = ri.get("regime", ""), ri.get("alert", "")
    bottom = ri.get("bottom_score", 0) or 0
    if alert == "FREE_FALL" and bottom < 30:
        return None  # 自由落体不接刀
    # —— 评分：5日动量30+加速15+资金连续25+信号时效20+广度10 ——
    s_mom = max(0.0, 30 - abs(c5 - mom_peak) * 4)
    s_acc = (min(15.0, 7 + acc * 2) if acc is not None and acc > 0
             else (max(0.0, 7 + (acc or 0) * 2)))
    s_flow = 25 if fl["consec"] >= 4 else (20 if fl["consec"] == 3
            else (15 if fl["consec"] == 2 else 10))
    if stale or not ri:
        s_sig = 8.0
        sig_txt = "趋势数据过期"
    elif alert == "GOLDEN_X" and ri.get("cross_age", 999) <= 5:
        s_sig, sig_txt = 20.0, "金叉≤5天"
    elif alert == "BOTTOM_WATCH":
        s_sig, sig_txt = 15.0, "底部观察"
    elif regime == "BULL":
        s_sig, sig_txt = 14.0, "趋势多头"
    elif ri.get("engine") == "SHORT_REDUCED":
        s_sig, sig_txt = 8.0, "空头减弱"
    else:
        s_sig, sig_txt = 5.0, "无明确信号"
    tot = adv + dec
    s_bread = (10 if tot and adv / tot >= 0.6 else
               (7 if tot and adv / tot >= 0.5 else
                (4 if tot and adv / tot >= 0.4 else (5 if not tot else 0))))
    w0, w1, w2, w3, w4 = w or (30, 15, 25, 20, 10)
    total = round(w0 * s_mom / 30 + w1 * s_acc / 15 + w2 * s_flow / 25
                  + w3 * s_sig / 20 + w4 * s_bread / 10, 1)
    if total < cutoff:
        return None
    return {"score": total, "sig_txt": sig_txt}


def broad_of(name):
    short = name.replace("Ⅲ", "").replace("Ⅱ", "").replace("类", "")
    for sub, big in SECTOR_TO_BROAD.items():
        if sub and (sub in name or sub in short):
            return big
    return short if short != name else name


def zhuang_feats(conn):
    """抄底特征：dd250/回撤、5/20日、20日资金截面 z。返回 {code: dict} + 截面z统计。"""
    rows = conn.execute(
        "SELECT code, date, close FROM sector_kline ORDER BY code, date").fetchall()
    per = {}
    for code, d, c in rows:
        per.setdefault(code, []).append((d, c))
    frows = conn.execute(
        "SELECT code, date, main_net_wan FROM sector_flow_daily ORDER BY code, date").fetchall()
    fper = {}
    for code, d, v in frows:
        fper.setdefault(code, []).append((d, v or 0))
    out, s20_all = {}, []
    for code, pts in per.items():
        cs = [c for _, c in pts]
        if len(cs) < 260:
            continue
        c0 = cs[-1]
        w250 = cs[-250:]
        dd250 = (c0 / max(w250) - 1) * 100
        r5 = (c0 / cs[-6] - 1) * 100
        r20 = (c0 / cs[-21] - 1) * 100
        fpts = fper.get(code, [])[-20:]
        s20 = sum(v for _, v in fpts) if len(fpts) >= 20 else None
        if s20 is None:
            continue
        out[code] = {"dd250": dd250, "r5": r5, "r20": r20, "s20": s20, "name": ""}
        s20_all.append(s20)
    m20 = sum(s20_all) / len(s20_all) if s20_all else 0.0
    sd20 = (sum((x - m20) ** 2 for x in s20_all) / max(1, len(s20_all) - 1)) ** 0.5 or 1.0
    for code, d in out.items():
        d["z20"] = (d["s20"] - m20) / sd20
    return out


def scan_zhuang(conn, val_map):
    """抄底观察名单（庄家线·左侧）。估值安全垫：broad 命中蛋卷 pb_pct 时标注。"""
    cfg = zhuang_cfg()
    reg, _, _ = regime_map(conn)
    feats = zhuang_feats(conn)
    val = val_map or {}
    cands = []
    # 用 snapshot 取名称
    snap = {r["代码"]: r for r in read_csv(os.path.join(DATA_DIR, "sector_snapshot.csv"))}
    for code, d in feats.items():
        r = snap.get(code)
        name = r.get("名称", "") if r else code
        if any(k in name for k in EXCLUDE_KW):
            continue
        ri = reg.get(code, {})
        bs = ri.get("bottom_score", 0) or 0
        cdist = ri.get("cloud_dist", 0) or 0
        if not ri:
            continue
        if d["dd250"] > cfg["dd250"] or bs < cfg["bscore"]:
            continue
        cd_lo, cd_hi = cfg.get("cd_lo"), cfg.get("cd_hi")
        if cd_lo is not None and not (cd_lo <= cdist):
            continue
        if cd_hi is not None and not (cdist <= cd_hi):
            continue
        if cfg["flow_mode"] == "abs" and not (d["s20"] > 0):
            continue
        if cfg["flow_mode"] == "z" and not (d["z20"] >= cfg.get("zthr", 0.0)):
            continue
        if cfg["flow_mode"] == "either" and not (d["s20"] > 0 or d["z20"] >= cfg.get("zthr", 0.0)):
            continue
        if cfg["flow_mode"] == "both" and not (d["s20"] > 0 and d["z20"] >= cfg.get("zthr", 0.0)):
            continue
        r5_lo, r5_hi = cfg.get("r5_lo"), cfg.get("r5_hi")
        if r5_lo is not None and not (r5_lo <= d["r5"]):
            continue
        if r5_hi is not None and not (d["r5"] <= r5_hi):
            continue
        r20_lo, r20_hi = cfg.get("r20_lo"), cfg.get("r20_hi")
        if r20_lo is not None and not (r20_lo <= d["r20"]):
            continue
        if r20_hi is not None and not (d["r20"] <= r20_hi):
            continue
        # 评分（镜像 zhuang_line.zhuang_score，无估值数据给中性）
        broad = broad_of(name)
        vb = val.get(broad)
        pb_pct = vb.get("pb_pct") if vb else None
        s_dd = 20.0 * max(0.0, min(1.0, (max(d["dd250"], cfg["dd250"] - 20.0) - cfg["dd250"]) / -15.0))
        s_bs = 25.0 * max(0.0, min(1.0, (bs - cfg["bscore"]) / (100.0 - cfg["bscore"])))
        s_flow = min(25.0, (10.0 if d["s20"] > 0 else 0.0)
                     + 15.0 * max(0.0, min(1.0, d["z20"] / 0.5)))
        s_stab = 15.0 * (1.0 - min(abs(d["r20"]), 10.0) / 10.0) + (2.0 if -2.0 <= d["r5"] <= 4.0 else 0.0)
        s_val = (15.0 * max(0.0, min(1.0, 1.0 - (pb_pct or 1.0) / 0.5)) if pb_pct is not None else 8.0)
        total = s_dd + s_bs + s_flow + s_stab + s_val + 5.0
        if total < cfg.get("cutoff", 45.0):
            continue
        val_txt = ("估值安全垫PB分位%.2f" % pb_pct) if pb_pct is not None else "无行业估值"
        reason = "回撤%+.0f%%；底部%.0f分；20日吸筹%+.0f亿；20日%+.1f%%；%s" % (
            d["dd250"], bs, d["s20"] / 1e4, d["r20"], val_txt)
        cands.append({"code": code, "name": name, "score": round(total, 1),
                      "reason": reason, "broad": broad, "pb_pct": pb_pct})
    cands.sort(key=lambda x: -x["score"])
    seen, out = set(), []
    for c in cands:
        if c["broad"] in seen:
            continue
        seen.add(c["broad"])
        out.append(c)
        if len(out) >= cfg["topn"]:
            break
    return out


def scan_short(conn):
    """短期Top5：要爆发力不要长期持有。权重：5日动量30+加速15+资金连续25+信号时效20+广度10。”
    """
    snap = {r["代码"]: r for r in read_csv(os.path.join(DATA_DIR, "sector_snapshot.csv"))}
    reg, reg_date, stale = regime_map(conn)
    klines = kline_tail(conn)
    flows = flow_stats(conn)
    cands = []
    for code, r in snap.items():
        name = r.get("名称", "")
        try:
            chg20 = float(r.get("20日%", 0) or 0)
            chg_today = float(r.get("当日%", 0) or 0)
            adv = int(r.get("涨家数") or 0)
            dec = int(r.get("跌家数") or 0)
            fmv = float(r.get("流通市值(亿)") or 0)
        except (ValueError, TypeError):
            continue
        if code not in klines:
            continue
        c5, acc = chg5_accel(klines[code])
        fl = flows.get(code, {"latest": 0, "consec": 0, "sum5": 0})
        ev = short_evaluate(name, c5, acc, fl, chg20, chg_today,
                            adv, dec, fmv, reg.get(code, {}), stale,
                            mom_peak=4.0)  # v2：动量峰值4，回放超额-1.30→-0.50
        if ev is None:
            continue
        reasons = ["5日%+.1f" % c5, "连续流入%d天" % fl["consec"],
                   ev["sig_txt"], "20日%+.1f" % chg20]
        if acc is not None:
            reasons.insert(1, "加速%+.1f" % acc)
        broad = broad_of(name)
        cands.append({"code": code, "name": name, "score": ev["score"],
                      "reason": "；".join(reasons), "broad": broad,
                      "chg5": c5, "consec": fl["consec"]})
    cands.sort(key=lambda x: -x["score"])
    seen, out = set(), []
    for c in cands:
        if c["broad"] in seen:
            continue
        seen.add(c["broad"])
        out.append(c)
        if len(out) >= 5:
            break
    return out, reg_date, stale


# ══════════════════════════════════════════════════════════════
# D区: 全市场恐慌监控（超跌反弹线）
# ══════════════════════════════════════════════════════════════
# 口径与验证脚本一致（research/rebound_line.py、research/sweep100k_rebound.py；
# 评审 reports/超跌反弹十万次迭代_20260916.md）：
#   · 触发 = 任一指数 20 日跌幅 ≤ -20%。26 条指数 26.7 年 → 28 轮独立行情、1.05 次/年。
#   · 20 日持有：96% 的随机样本赚钱、中位 +3.62%；30 日 +8.31%（费后）。
#   · **不等止跌确认**（等“连涨两天”会把赚钱样本比例从 89% 打到 72%）。
#   · ≥28% 的极深跌 40 日后翻脸（60 日仅 29% 样本赚钱）→ 只做 20~30 日窗口。
#   · ⚠ 结构熊市历史失效：窗口终点 <2015 的样本只有 51% 赚钱，2008 型 60 日可亏 30%+。
# 本区只提示“机会来了”，不给金额、不给买卖结论——仓位归 holding_advice 与用户计划。
PANIC_DEPTH = -20.0     # 触发门槛
PANIC_NEAR = -15.0      # 接近提示（该档历史只值 +0.84%，不构成信号）
PANIC_EXTREME = -28.0   # 极深：只给 20~30 日窗口
PANIC_CLOSE_HM = (15, 5)  # 收盘确认时刻（与 fetch_index_csv_kline.unfinished 同规则）


def _kline_full_closes(name, need=260):
    """读 data/kline_full/{指数}.csv 的收盘序列（列：日期,收盘），返回 (dates, closes)。"""
    import csv
    path = os.path.join(DATA_DIR, "kline_full", name + ".csv")
    if not os.path.exists(path):
        return [], []
    ds, cs = [], []
    with open(path, encoding="utf-8-sig") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) < 2 or not row[0]:
                continue
            try:
                v = float(row[1])
            except ValueError:
                continue
            ds.append(row[0][:10])
            cs.append(v)
    return ds[-need:], cs[-need:]


def _tencent_quotes(codes):
    """腾讯批量行情 → {代码: (现价, 昨收, 日期, 时刻)}；失败返回 {}（退化为只用本地收盘）。

    腾讯为 GBK 编码，不能走 fetch_util.curl_json（按 utf-8 解析）；格式是
    v_代码="市场~名称~代码~现价~昨收~今开~…~时间戳~…" 而非 JSON，故手工拆。
    """
    import subprocess
    if not codes:
        return {}
    url = "http://qt.gtimg.cn/q=" + ",".join(codes)
    out = {}
    for _ in range(3):
        try:
            r = subprocess.run(["curl", "-s", "-m", "15", "-A", "Mozilla/5.0", url],
                               capture_output=True, timeout=25)
        except (OSError, subprocess.SubprocessError):
            return {}
        if r.returncode != 0 or not r.stdout:
            continue
        txt = r.stdout.decode("gbk", errors="replace")
        for line in txt.splitlines():
            line = line.strip()
            if not line.startswith("v_") or '"' not in line:
                continue
            code = line[2:line.index("=")]
            f = line[line.index('"') + 1:line.rindex('"')].split("~")
            if len(f) < 6:
                continue
            try:
                price, prev = float(f[3]), float(f[4])
            except (ValueError, IndexError):
                continue
            if price <= 0:
                continue
            d, hm = _quote_ts(f)
            out[code] = (price, prev, d, hm)
        if out:
            return out
    return {}


def _quote_ts(fields):
    """从腾讯行情字段里找出时间戳，返回 (YYYY-MM-DD, (时,分))。"""
    import re
    for v in fields:
        m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})\d{2}", v)   # A股 14 位
        if m:
            return "%s-%s-%s" % m.group(1, 2, 3), (int(m.group(4)), int(m.group(5)))
        m = re.fullmatch(r"(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):\d{2}", v)  # 港股
        if m:
            return "%s-%s-%s" % m.group(1, 2, 3), (int(m.group(4)), int(m.group(5)))
    return "", (0, 0)


def panic_monitor():
    """全市场恐慌监控。返回 dict：{level, asof, confirmed, items, note}

    level ∈ 触发 / 接近 / 平静。items 按 20 日跌幅升序，每条含 name/ret20/ret60/dd250/
    trigger_line（收盘低于此值才触发）。
    items 里的日期若为“今日且未收盘”，ret20 是**盘中估算**（用腾讯现价当今日点位）。
    """
    try:
        from src.jobs_fetch.fetch_index_csv_kline import INDEXES
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from jobs_fetch.fetch_index_csv_kline import INDEXES

    quotes = _tencent_quotes([v[0] for v in INDEXES.values()])
    now = datetime.datetime.now()
    items = []
    asof, any_intraday = "", False
    for name, (tcode, _secid) in INDEXES.items():
        ds, cs = _kline_full_closes(name)
        if len(cs) < 22:
            continue
        q = quotes.get(tcode)
        if q and q[2]:
            if q[2] > ds[-1]:                    # 行情比 CSV 新：追加今日点位
                ds, cs = ds + [q[2]], cs + [q[0]]
                any_intraday = True
            elif q[2] == ds[-1] and (q[3] >= (15, 5) or len(cs) < 30):
                cs[-1] = q[0]                    # 同日：用最新价刷新最后一格
        if ds[-1] > asof:
            asof = ds[-1]
        base = cs[-21]
        items.append({
            "name": name, "date": ds[-1], "ret20": (cs[-1] / base - 1) * 100,
            "ret60": (cs[-1] / cs[-61] - 1) * 100 if len(cs) >= 61 else None,
            "dd250": (cs[-1] / max(cs[-250:]) - 1) * 100 if len(cs) >= 250 else None,
            "trigger_line": base * (1 + PANIC_DEPTH / 100.0), "live": bool(q),
        })
    items.sort(key=lambda x: x["ret20"])
    confirmed = not (any_intraday and now.date().isoformat() == asof
                     and now.time() < datetime.time(*PANIC_CLOSE_HM))
    hit = [x for x in items if x["ret20"] <= PANIC_DEPTH]
    near = [x for x in items if PANIC_DEPTH < x["ret20"] <= PANIC_NEAR]
    return {"level": "触发" if hit else ("接近" if near else "平静"),
            "asof": asof, "confirmed": confirmed, "items": items,
            "hit": hit, "near": near}


def panic_lines(pan):
    """把监控结果渲染成报告行（平时一行，触发时展开）。"""
    if not pan["items"]:
        return ["全市场恐慌监控不可用（指数K线缺失）。"]
    L = []
    tag = "已收盘确认" if pan["confirmed"] else "盘中估算·待收盘确认"
    w = pan["items"][0]
    L.append("- 监控池 %d 条指数｜数据截至 %s（%s）" % (len(pan["items"]), pan["asof"], tag))
    if pan["level"] == "触发":
        L.append("- **🚨 已触发：%d 条指数 20 日跌幅 ≤ %+.0f%%**" % (len(pan["hit"]), PANIC_DEPTH))
        L.append("")
        L.append("| 指数 | 20日跌幅 | 距60日高 | 触发线（收盘低于此值才算） |")
        L.append("|---|---|---|---|")
        for x in pan["hit"][:8]:
            L.append("| %s | %+.2f%% | %s | %.2f |" % (
                x["name"], x["ret20"],
                "%+.1f%%" % x["dd250"] if x["dd250"] is not None else "—",
                x["trigger_line"]))
        if len(pan["hit"]) > 8:
            L.append("| …另 %d 条 | | | |" % (len(pan["hit"]) - 8))
        L.append("")
        L.append("> **历史依据**（26 条指数 26.7 年）：这种机会**只出现过 28 轮**、约 1 次/年。")
        L.append("> 20 日持有在 96% 的随机样本里赚钱、中位 +3.62%；30 日 +8.31%（均已扣赎回费）。")
        L.append("> **不要等止跌确认**——等“连涨两天”会把赚钱样本比例从 89% 打到 72%。")
        L.append("> **只做 20~30 日窗口**：≥28% 的极深跌 40 日后翻脸，60 日仅 29% 的样本还赚钱。")
        L.append("> ⚠ 结构熊市历史失效（2008 型可亏 30%+）；本区只提示机会，不给金额、不定买卖。")
    else:
        gap = w["ret20"] - PANIC_DEPTH
        L.append("- **未触发**。最深 **%s %+.2f%%**（门槛 %+.0f%%，还需再跌 %.2f 个百分点）。"
                 % (w["name"], w["ret20"], PANIC_DEPTH, gap))
        if pan["near"]:
            L.append("- ⚠ 接近门槛：%s。该档历史只值 +0.84%%，**不构成信号**，看看就好。"
                     % "、".join("%s %+.2f%%" % (x["name"], x["ret20"])
                                 for x in pan["near"][:5]))
    return L
