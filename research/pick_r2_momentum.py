# -*- coding: utf-8 -*-
"""R2动量场外选品器（月度低频版，保守型）

给未来亚亚：算全板块最新得分，只推大流动性方向。口径与回测严格一致
（收盘>MA20>MA60 + 40日加权回归得分=年化×R2），流通市值<50亿剔除。
每月10日看一次，红灯只观察不开新仓。纯标准库。
用法: python pick_r2_momentum.py [--top 5]
输出: 控制台TOP榜 + reports/R2选品_YYYYMMDD.md
"""
import csv
import datetime
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DATA_DIR)
for _p in (DATA_DIR, REPO_ROOT,
           os.path.join(REPO_ROOT, "src", "common"),
           os.path.join(REPO_ROOT, "src", "jobs_build"),
           os.path.join(REPO_ROOT, "src", "jobs_fetch")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from src.common.paths import DATA_ROOT, CONFIG_ROOT
from src.common import history_db
from research._engine import wlinreg_stats
REPORT_DIR = os.path.join(REPO_ROOT, "reports")
WINDOW = 40  # v2：网格最优（40日TOP3/-8%/缓冲10%，回撤-5.4%换手30%）


def score_one(closes, window=WINDOW):
    # 单板块最新R2分：与回测同口径；年化<=0直接淘汰（只要多头）
    s = wlinreg_stats(closes, window)
    if s is None:
        return None
    ann, r2 = s[0], s[1]
    if ann <= 0:
        return None
    return round(ann, 3), round(r2, 3), round(ann * r2, 4)


def load_model_params():
    # 给未来亚亚：选品口径从本地模型读，训练器晋升后这里自动跟上，不用改代码
    try:
        m = json.load(open(os.path.join(CONFIG_ROOT, "model_r2.json"), encoding="utf-8"))
        p = m.get("params", m)
        return int(p.get("window", WINDOW)), float(p.get("min_cap_yi", 50.0)), m.get("version", "?")
    except Exception:
        return WINDOW, 50.0, "v2-fallback"


def main():
    topn = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 5
    window, min_cap, ver = load_model_params()
    conn = history_db.connect(readonly=True)
    try:
        rows = conn.execute("SELECT code,date,close FROM sector_kline ORDER BY code,date").fetchall()
        snap = {}
        try:
            with open(os.path.join(DATA_ROOT, "sector_snapshot.csv"), encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    try:
                        snap[r["代码"]] = float(r.get("流通市值(亿)") or 0)
                    except Exception:
                        pass
        except Exception:
            pass
        names = {}
        try:
            for c, n in conn.execute(
                    "SELECT code,name FROM sector_daily WHERE date=(SELECT MAX(date) FROM sector_daily)"):
                names[c] = n
        except Exception:
            pass
    finally:
        conn.close()
    series = {}
    for code, d, c in rows:
        series.setdefault(code, []).append(c)
    out = []
    for code, cl in series.items():
        if len(cl) < window + 60:
            continue
        m20 = sum(cl[-20:]) / 20
        m60 = sum(cl[-60:]) / 60
        if not (cl[-1] > m20 > m60):
            continue
        r = score_one(cl, window)
        if not r:
            continue
        ann, r2, s = r
        fmv = snap.get(code, 0)
        # 保守过滤：小市值不推（没人玩的直线别碰），阈值来自模型文件
        if fmv and fmv < min_cap:
            continue
        out.append((s, code, names.get(code, code), ann, r2, fmv))
    out.sort(reverse=True)
    picks = out[:topn]
    print("R2场外选品 %s 模型%s 窗口%d 共%d过趋势，TOP%d:" % (
        datetime.date.today(), ver, window, len(out), topn))
    for s, code, name, ann, r2, fmv in picks:
        print(" %.4f %s(%s) 年化%.2f R2=%.2f 流%.0f亿" % (s, name, code, ann, r2, fmv or 0))
    if not picks:
        print("今日无过线方向，空仓等（货币）。")
    os.makedirs(REPORT_DIR, exist_ok=True)
    rp = os.path.join(REPORT_DIR, "R2选品_%s.md" % datetime.date.today().strftime("%Y%m%d"))
    with open(rp, "w", encoding="utf-8") as f:
        f.write("# R2场外选品 %s\n\n" % datetime.date.today())
        f.write("口径：模型%s 窗口%d，收盘>MA20>MA60，得分=年化×R2，市值<%.0f亿剔除，月度才换。\n\n" % (
            ver, window, min_cap))
        if picks:
            for s, code, name, ann, r2, fmv in picks:
                f.write("- %.4f %s(%s) 年化%.2f R2=%.2f\n" % (s, name, code, ann, r2))
        else:
            f.write("今日无过线方向，空仓等。\n")
        f.write("\n纪律：场外至少拿1个月，-8%止损（v2），新TOP均分超旧持仓10%才换，否则不动。\n")
    print("报告:%s" % rp)


if __name__ == "__main__":
    main()
