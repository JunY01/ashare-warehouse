# -*- coding: utf-8 -*-
"""庄家抄底线（左侧布局）——特征面板 + 规则评分

定位：与 14:30 短线（右侧动量）并行的第二条线。庄家逻辑四要素：
  ① 回撤到位（自250日高点跌够深、跌够久）  ② 结构底部（bottom_score 底底高/走平/贴云带）
  ③ 资金吸筹（价格横盘但主力资金累计净流入）  ④ 别接刀（非自由落体、跌速收敛）

数据源：sector_kline（2023起，算回撤/企稳）+ sector_flow_daily（2026-03-30起，算吸筹）
       + scan_regime.analyze_series 逐日重算趋势/底部（无未来函数）。
回测口径与 train_recommend 面板一致：信号日收盘确认、fwd=未来5日、path=归一化未来31日。

用法（供 validate_zhuang.py / sweep_zhuang.py 复用）：
  feats, dates = build_panel(kl, fl, names, turn, marg, hs, oh)
  feats[T] = {"rows": [...], "tmed": 全市场5日移动中位数}
纯标准库。
"""
import os
import sys

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DATA_DIR)
for _p in (DATA_DIR, REPO_ROOT,
           os.path.join(REPO_ROOT, "src", "common"),
           os.path.join(REPO_ROOT, "src", "jobs_build"),
           os.path.join(REPO_ROOT, "src", "jobs_fetch")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from src.jobs_build.daily_1430 import EXCLUDE_KW, broad_of  # noqa: E402
from src.jobs_build import scan_regime as SR  # noqa: E402
from train_recommend import mean, stdev  # noqa: E402
from src.common.fees import disc_fee  # noqa: E402
from src.common.paths import DATA_ROOT, SWEEP_CACHE  # noqa: E402
from src.common import history_db  # noqa: E402

PANEL_START = "2026-05-25"   # 资金 s40 需 40 个 flow 日（03-30 起）
NF_START = "2024-02-01"      # 无资金流长历史面板起点（需 260 根K线预热，2023-01 起）
FWD = 5
PATH_N = 31

# 庄家规则评分默认参数（网格扫参的起点，见 sweep_zhuang.py 的 GRID）
BASE_CFG = {
    "dd250": -20.0,     # 回撤到位：自250日高点回撤 ≤ -20%
    "bscore": 65.0,     # 结构底部：bottom_score ≥ 65
    "cd_lo": -1.5, "cd_hi": 1.5,   # 贴云带（不接自由落体，也别追已涨高）
    "flow_mode": "both", "zthr": 0.0,  # 吸筹：s20>0 且 z20>=0
    "r20_lo": -8.0, "r20_hi": 3.0,     # 企稳：20日别再暴跌，也别已暴涨
    "r5_lo": -4.0, "r5_hi": 6.0,
    "cutoff": 45.0,
    "topn": 5, "hold": 10, "tp": 8.0, "sl": -6.0, "gate": None,
}


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _lin(x, x0, x1):
    """x 从 x0→x1 线性映射 0→100，越界截断。"""
    if x1 == x0:
        return 0.0
    return _clamp((x - x0) / (x1 - x0) * 100.0, 0.0, 100.0)


def load_all():
    """从 data/ 真实路径加载面板输入（不依赖老 research 脚本的本地路径假设）。"""
    import csv
    import sqlite3
    conn = history_db.connect(readonly=True)
    try:
        krows = conn.execute(
            "SELECT code, date, close FROM sector_kline ORDER BY code, date").fetchall()
        frows = conn.execute(
            "SELECT code, date, main_net_wan FROM sector_flow_daily ORDER BY code, date").fetchall()
        names = dict(conn.execute(
            "SELECT code, name FROM sector_daily WHERE date=(SELECT MAX(date) FROM sector_daily)"))
        turn = {d: t for d, _, _, t in
                conn.execute("SELECT date, sh_amount, sz_amount, total FROM market_turnover")}
        marg = {d: m for d, m in conn.execute("SELECT date, rzrq_ye FROM margin_balance")}
        oh = {}
        try:
            for code, d, o, h, l, c, v, a in conn.execute(
                    "SELECT code, date, open, high, low, close, vol, amt FROM sector_ohlcv"):
                oh.setdefault(code, {})[d] = (o, h, l, c, v, a)
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    kl, fl = {}, {}
    for code, d, c in krows:
        kl.setdefault(code, []).append((d, c))
    for code, d, v in frows:
        fl.setdefault(code, []).append((d, v or 0))
    hs = {}
    try:
        with open(os.path.join(DATA_ROOT, "kline_full", "沪深300.csv"), encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                ks = list(r.values())
                try:
                    hs[ks[0]] = float(ks[1])
                except (ValueError, IndexError):
                    pass
    except (FileNotFoundError, OSError):
        pass
    return kl, fl, names, turn, marg, hs, oh


def build_panel(kl, fl, names, turn, marg, hs, oh, need_flow=True):
    """庄家特征面板。need_flow=True 用主力资金流（窗口 2026-05 起，约半年）；
    need_flow=False 用相对强度 z_mom20 代理"吸筹"（窗口 2024-02 起，三年）。
    返回 (feats, dates)：
    feats[T] = {"rows": [row...], "tmed": 市场近5日中位数}
    row = {date, code, name, broad, ret5, ret20, ret60, chg_today, vol20,
           dd250, draw_age, low_prox, bscore, cdist, cwidth, chan_pos, xage,
           bull, alert, flow{...}, acc_div, pb_pct, has_val, fwd, path}"""
    start = PANEL_START if need_flow else NF_START
    dates = sorted({d for pts in kl.values() for d, _ in pts})
    # 每板块趋势序列只算一次（analyze_series 最贵）
    series = {}
    for ci, code in enumerate(sorted(kl)):
        series[code] = SR.analyze_series([c for _, c in kl[code]])
    rows = []
    for di, T in enumerate(dates):
        if T < start:
            continue
        # 当日资金截面（各窗口累计；仅 need_flow）
        sall = {5: [], 10: [], 20: [], 40: []}
        fcache = {}
        if need_flow:
            for code, pts in kl.items():
                idx = next((i for i, (d, _) in enumerate(pts) if d > T), len(pts)) - 1
                if idx < 0:
                    continue
                fpts = [(d, v) for d, v in fl.get(code, []) if d <= T][-40:]
                fcache[code] = (idx, fpts)
                for w in (5, 10, 20, 40):
                    if len(fpts) >= w:
                        sall[w].append(sum(v for _, v in fpts[-w:]))
        zstat = {}
        for w, arr in sall.items():
            m, s = mean(arr), stdev(arr)
            zstat[w] = (m, s if s else 1.0)
        for code, pts in kl.items():
            if need_flow and code not in fcache:
                continue
            idx = next((i for i, (d, _) in enumerate(pts) if d > T), len(pts)) - 1
            fpts = fcache.get(code, ()) if need_flow else ()
            if (need_flow and len(fpts) < 40) or idx < 260 or idx + FWD >= len(pts):
                continue
            closes = [c for _, c in pts[:idx + 1]]
            c0 = closes[-1]
            r1 = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
            ret5 = (c0 / closes[-6] - 1) * 100
            ret20 = (c0 / closes[-21] - 1) * 100
            ret60 = (c0 / closes[-61] - 1) * 100
            chg_today = (c0 / closes[-2] - 1) * 100
            prev5 = (closes[-6] / closes[-11] - 1) * 100
            vol20 = stdev(r1[-20:]) * (20 ** 0.5)
            w250 = closes[-250:]
            pk = max(w250)
            dd250 = (c0 / pk - 1) * 100
            hi_idx = w250.index(pk)
            draw_age = (len(w250) - 1 - hi_idx) if hi_idx < len(w250) - 1 else 0
            low_prox = (c0 / min(closes[-60:]) - 1) * 100
            if need_flow:
                s = {w: sum(v for _, v in fpts[-w:]) for w in (5, 10, 20, 40)}
                z = {w: (s[w] - zstat[w][0]) / zstat[w][1] for w in (5, 10, 20, 40)}
                acc_div = 1.0 if (s[20] > 0 and ret20 <= 2.0) else 0.0
                consec = 0
                for _, v in reversed(fpts[-6:]):
                    if v > 0:
                        consec += 1
                    else:
                        break
                latest = fpts[-1][1] if fpts else 0.0
            else:
                s = {w: 0.0 for w in (5, 10, 20, 40)}
                z = {w: 0.0 for w in (5, 10, 20, 40)}
                acc_div = 0.0
                consec = 0
                latest = 0.0
            sg = series[code][idx] or {}
            bs = sg.get("bottom_score", 0) or 0
            if sg.get("regime") == "BULL":
                engine = "LONG_SIDE"
            elif bs >= 65:
                engine = "BOTTOM_WATCH"
            elif bs >= 50:
                engine = "SHORT_REDUCED"
            else:
                engine = "SHORT_ACTIVE"
            fwd = (pts[idx + FWD][1] / c0 - 1) * 100
            nm = names.get(code, code)
            rows.append({
                "date": T, "code": code, "name": nm, "broad": broad_of(nm),
                "ret5": ret5, "prev5": prev5, "ret20": ret20, "ret60": ret60,
                "chg_today": chg_today, "vol20": vol20,
                "dd250": dd250, "draw_age": draw_age, "low_prox": low_prox,
                "bscore": bs,
                "cdist": sg.get("cloud_dist", 0) or 0,
                "cwidth": sg.get("cloud_width", 0) or 0,
                "chan_pos": sg.get("chan_pos", 0) or 0,
                "xage": sg.get("cross_age", 999) or 999,
                "bull": 1.0 if sg.get("regime") == "BULL" else 0.0,
                "alert": sg.get("alert", "—"),
                "engine": engine,
                "flow": {"s5": s[5], "s10": s[10], "s20": s[20], "s40": s[40],
                         "z5": z[5], "z10": z[10], "z20": z[20], "z40": z[40],
                         "latest": latest, "consec": consec,
                         "z_mom5": 0.0, "z_mom20": 0.0},
                "acc_div": acc_div,
                "pb_pct": 0.0, "has_val": 0.0,
                "fwd": fwd,
                "path": [p[1] / c0 for p in pts[idx + 1:idx + PATH_N]],
            })
    # 事后补相对强度 z_mom（截面归一，need_flow/nf 都算）
    byT = {}
    for r in rows:
        byT.setdefault(r["date"], []).append(r)
    for T, rr in byT.items():
        m5 = mean([x["ret5"] for x in rr]); s5 = stdev([x["ret5"] for x in rr]) or 1.0
        m20 = mean([x["ret20"] for x in rr]); s20 = stdev([x["ret20"] for x in rr]) or 1.0
        for x in rr:
            x["flow"]["z_mom5"] = (x["ret5"] - m5) / s5
            x["flow"]["z_mom20"] = (x["ret20"] - m20) / s20
            if not need_flow:
                x["acc_div"] = 1.0 if (x["flow"]["z_mom20"] >= 0.0 and x["ret20"] <= 2.0) else 0.0
    feats = {}
    for r in rows:
        feats.setdefault(r["date"], {"rows": [], "tmed": 0.0})["rows"].append(r)
    for T in feats:
        feats[T]["tmed"] = trail_median(kl, T)
    return feats, sorted(feats)


def trail_median(kl, T):
    """信号日 T：全板块近5日涨幅中位数（市况判读）。"""
    rs = []
    for pts in kl.values():
        idx = next((i for i, (d, _) in enumerate(pts) if d > T), len(pts)) - 1
        if idx >= 6 and pts[idx - 5][1]:
            rs.append((pts[idx][1] / pts[idx - 5][1] - 1) * 100)
    rs.sort()
    return rs[len(rs) // 2] if rs else 0.0


def flow_pass(r, mode, zthr):
    """吸筹判定：mode ∈ abs/z/both/either/nf。
    nf=无资金流长历史模式，用相对强度 z_mom20 代理吸筹。"""
    if mode == "nf":
        return r["flow"]["z_mom20"] >= zthr
    s20, z20 = r["flow"]["s20"], r["flow"]["z20"]
    if mode == "abs":
        return s20 > 0
    if mode == "z":
        return z20 >= zthr
    if mode == "either":
        return s20 > 0 or z20 >= zthr
    return s20 > 0 and z20 >= zthr  # both


def zhuang_score(r, cfg):
    """单板块庄家评分。返回 None（过滤）或 (score, 理由段)。"""
    if any(k in r["name"] for k in EXCLUDE_KW):
        return None
    if r["dd250"] > cfg["dd250"]:
        return None
    if r["bscore"] < cfg["bscore"]:
        return None
    cd_lo, cd_hi = cfg.get("cd_lo"), cfg.get("cd_hi")
    if cd_lo is not None and not (cd_lo <= r["cdist"]):
        return None
    if cd_hi is not None and not (r["cdist"] <= cd_hi):
        return None
    if not flow_pass(r, cfg.get("flow_mode", "both"), cfg.get("zthr", 0.0)):
        return None
    r20_lo = cfg.get("r20_lo")
    r20_hi = cfg.get("r20_hi")
    if r20_lo is not None and not (r20_lo <= r["ret20"]):
        return None
    if r20_hi is not None and not (r["ret20"] <= r20_hi):
        return None
    r5_lo = cfg.get("r5_lo")
    r5_hi = cfg.get("r5_hi")
    if r5_lo is not None and not (r5_lo <= r["ret5"]):
        return None
    if r5_hi is not None and not (r["ret5"] <= r5_hi):
        return None
    if r["cdist"] < -2.0:
        return None  # 自由落体不接刀
    val_gate = cfg.get("val_gate")
    if val_gate is not None and r["has_val"] and r["pb_pct"] > val_gate:
        return None

    # —— 连续评分（满分100：回撤20 + 结构25 + 吸筹25 + 企稳15 + 估值15 + 广度10）——
    thr = cfg["dd250"]
    s_dd = 20.0 * _clamp((max(r["dd250"], thr - 20.0) - thr) / -15.0, 0.0, 1.0)
    s_bs = 25.0 * _clamp((r["bscore"] - cfg["bscore"]) / (100.0 - cfg["bscore"]), 0.0, 1.0)
    if cfg.get("flow_mode") == "nf":
        s_flow = min(25.0, 15.0 * _clamp(r["flow"]["z_mom20"] / 0.5, 0.0, 1.0)
                     + (10.0 if r["acc_div"] else 0.0))
        reason_flow = "相对强度%+.2f" % r["flow"]["z_mom20"]
    else:
        s_flow = min(25.0, (10.0 if r["flow"]["s20"] > 0 else 0.0)
                     + 15.0 * _clamp(r["flow"]["z20"] / 0.5, 0.0, 1.0)
                     + (3.0 if r["acc_div"] else 0.0))
        reason_flow = "吸筹20日%+.0f亿" % (r["flow"]["s20"] / 1e4)
    s_stab = 15.0 * (1.0 - min(abs(r["ret20"]), 10.0) / 10.0) \
        + (2.0 if -2.0 <= r["ret5"] <= 4.0 else 0.0)
    s_val = (15.0 * _clamp(1.0 - r["pb_pct"] / 0.5, 0.0, 1.0)) if r["has_val"] else 8.0
    s_bread = 5.0  # 历史无广度存档，中性
    total = s_dd + s_bs + s_flow + s_stab + s_val + s_bread
    if total < cfg.get("cutoff", 45.0):
        return None
    reason = "回撤%+.0f%%；底部%.0f分；%s；20日%+.1f%%" % (
        r["dd250"], r["bscore"], reason_flow, r["ret20"])
    return round(total, 1), reason


def score_rows(rows, cfg):
    """给定当日 rows，返回 {idx: (score, reason)}。"""
    out = {}
    for i, r in enumerate(rows):
        ev = zhuang_score(r, cfg)
        if ev is not None:
            out[i] = ev
    return out


def pick_top(rows, scores, topn):
    """按分取 topn，broad 去重（复用 iterate 口径）。scores: {idx: float 或 (score, reason)}。"""
    def sval(i):
        v = scores[i]
        return v[0] if isinstance(v, (tuple, list)) else v
    order = sorted(scores, key=lambda i: -sval(i))
    seen, top = set(), []
    for i in order:
        b = rows[i]["broad"]
        if b in seen:
            continue
        seen.add(b)
        top.append(i)
        if len(top) >= topn:
            break
    return top


CACHE_PATH = os.path.join(SWEEP_CACHE, "zhuang_panel.pkl")


def get_panel(use_cache=True, need_flow=True):
    """构建（或从缓存读取）完整面板。need_flow=False 用长历史无资金流面板（独立缓存）。"""
    import pickle
    cache = CACHE_PATH if need_flow else os.path.join(SWEEP_CACHE, "zhuang_panel_nf.pkl")
    if use_cache and os.path.exists(cache):
        try:
            with open(cache, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    kl, fl, names, turn, marg, hs, oh = load_all()
    feats, dates = build_panel(kl, fl, names, turn, marg, hs, oh, need_flow=need_flow)
    with open(cache, "wb") as f:
        pickle.dump((feats, dates), f, protocol=4)
    return feats, dates


def eval_cfg(feats, sig_days, cfg):
    """一组参数在指定信号日上评估（费后）。返回 (per_date, summary)。"""
    per_date, excs, hits, n = [], [], 0, 0
    for T in sig_days:
        F = feats[T]
        if cfg.get("gate") is not None and F["tmed"] < cfg["gate"]:
            continue
        scores = score_rows(F["rows"], cfg)
        top = pick_top(F["rows"], scores, cfg["topn"])
        if len(top) < cfg["topn"]:
            continue
        rets = [disc_fee(F["rows"][i]["path"], cfg["tp"], cfg["sl"], cfg["hold"])
                for i in top]
        uni = sorted(r["fwd"] for r in F["rows"])
        med = uni[len(uni) // 2]
        avg = mean(rets)
        exc = avg - med
        per_date.append({"T": T, "avg": avg, "disc": avg, "hit": sum(1 for x in rets if x > 0),
                         "exc": exc, "med": med, "tmed": F["tmed"], "n": len(rets)})
        excs.append(exc)
        hits += sum(1 for x in rets if x > 0)
        n += len(rets)
    if not per_date:
        return per_date, {"ndate": 0, "avg": 0.0, "hit": 0.0, "exc": 0.0,
                          "dd": 0.0, "winp": 0}
    cum, peak, dd = 0.0, 0.0, 0.0
    for e in excs:
        cum += e
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return per_date, {"ndate": len(per_date), "n": n,
                      "avg": mean([p["avg"] for p in per_date]),
                      "hit": hits / n if n else 0.0,
                      "exc": mean(excs), "dd": round(dd, 2),
                      "winp": sum(1 for e in excs if e > 0)}