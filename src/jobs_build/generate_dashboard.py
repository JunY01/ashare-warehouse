# -*- coding: utf-8 -*-
"""生成《投资看板》单文件 HTML

读取 market_history.db（含 global_daily 全球快照）/ index_valuation.json / positions.json，
输出 reports/投资看板.html。图表用 ECharts CDN（断网时表格仍可读），纯标准库零依赖。
用法：python generate_dashboard.py ；update_data.py 快照更新后也会自动调用。
"""
import datetime
import json
import os
import sys

# 统一寻址：无论从哪个目录运行都能定位仓库根（独立运行/被调用均不写错路径）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common.paths import DATA_ROOT, CONFIG_ROOT, REPORT_ROOT
from src.common import history_db

DATA_DIR = DATA_ROOT
IV_PATH = os.path.join(DATA_ROOT, "index_valuation.json")
POS_PATH = os.path.join(CONFIG_ROOT, "positions.json")
OUT_PATH = os.path.join(REPORT_ROOT, "投资看板.html")

STATE_COLOR = {
    "泡沫": "#d63031", "高估": "#e17055", "合理偏高": "#e8a33d",
    "合理": "#95a5a6", "合理偏低": "#1a9c6b", "低估": "#0b7285",
    "未收录": "#b2bec3",
    # 点位分位（非估值）：统一灰蓝系，与估值档位的红绿黄刻意区分开
    "点位极端高位": "#7f8c8d", "点位高位": "#95a5a6",
    "点位中上": "#a9b3b8", "点位中下": "#b6c0c4", "点位低位": "#c3ccd0",
}

# 资金流 ratio 信号阈值（单位 bp）：|主力净流入/流通市值| 达标时标色
# 按 2026-08-25 全板块截面校准：WARN≈P90(16bp)、CRIT≈P98(42bp) 取整
RATIO_WARN_BP = 15
RATIO_CRIT_BP = 40


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_global():
    """全球指数快照：读 global_daily 最新一天（fetch_global.py 落库）"""
    con = history_db.connect(readonly=True)
    try:
        date = con.execute("SELECT MAX(date) FROM global_daily").fetchone()[0]
        if not date:
            return "", []
        data = con.execute(
            "SELECT name, close, chg_pct FROM global_daily WHERE date=? ORDER BY rowid",
            (date,)).fetchall()
    finally:
        con.close()
    return date, data


def q_market(conn):
    """当日板块快照：广度 + 主力净流入/流出 TOP10"""
    date = conn.execute("SELECT MAX(date) FROM sector_daily").fetchone()[0]
    rows = conn.execute(
        "SELECT name, chg_pct, main_inflow_wan FROM sector_daily WHERE date=?", (date,)).fetchall()
    up = sum(1 for r in rows if (r[1] or 0) > 0)
    down = sum(1 for r in rows if (r[1] or 0) < 0)
    ranked = sorted([r for r in rows if r[2] is not None], key=lambda x: -x[2])
    return date, {"up": up, "down": down, "total": len(rows)}, ranked


def chg_20d_map(conn):
    """真 20 日涨幅：sector_daily.chg_20d 是快照里骗人的列（实为 f109=5 日涨幅），从 sector_kline 重算"""
    kd = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM sector_kline ORDER BY date DESC LIMIT 21")]
    if len(kd) < 21:
        return {}
    return {code: (a / b - 1) * 100 for code, a, b in conn.execute(
        """SELECT a.code, a.close, b.close FROM sector_kline a JOIN sector_kline b
           ON b.code=a.code AND b.date=? WHERE a.date=?""", (kd[20], kd[0])) if b}


def q_accumulation(conn, days=5):
    """近 N 日连续吸筹榜：≥3 天净流入且累计为正，按累计额排序"""
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM sector_flow_daily ORDER BY date DESC LIMIT ?", (days,))]
    if not dates:
        return [], []
    ph = ",".join("?" * len(dates))
    rows = conn.execute(f"""
        SELECT s.code, s.name, SUM(CASE WHEN f.main_net_wan>0 THEN 1 ELSE 0 END),
               ROUND(SUM(f.main_net_wan)/1e4, 1), s.chg_pct
        FROM sector_flow_daily f JOIN sector_daily s ON s.code=f.code AND s.date=(SELECT MAX(date) FROM sector_daily)
        WHERE f.date IN ({ph})
        GROUP BY f.code HAVING COUNT(*)>={min(3, len(dates))} AND SUM(f.main_net_wan)>0
        ORDER BY SUM(f.main_net_wan) DESC LIMIT 12""", dates).fetchall()
    return dates[::-1], rows


def q_ratio(conn):
    """当日板块资金流 ratio（主力净流入/流通市值，万元÷亿元恰好=bp）"""
    date = conn.execute("SELECT MAX(date) FROM sector_flow_daily").fetchone()[0]
    if not date:
        return "", []
    rows = conn.execute(
        """SELECT s.name, f.main_net_wan, s.float_mv_yi,
                  ROUND(f.main_net_wan / s.float_mv_yi, 1)
           FROM sector_flow_daily f JOIN sector_daily s ON s.code=f.code AND s.date=f.date
           WHERE f.date=? AND s.float_mv_yi > 0 AND f.main_net_wan IS NOT NULL""",
        (date,)).fetchall()
    return date, rows


def build_triggers(pos, valuations):
    vmap = {v["name"]: v for v in valuations}
    out = []

    sh = vmap.get("上证指数", {})
    price = sh.get("price")
    if price:
        dist = (price - 3800) / 3800 * 100
        out.append({"title": "机动子弹 · 上证<3800", "value": f"{price:.0f}",
                    "detail": f"距触发线还有 {dist:+.1f}%", "hot": abs(dist) < 2})

    for fund in pos["资金A"]["基金"]:
        if "止盈线" in fund:
            line = fund["止盈线"]
            pct = fund["收益率"]
            hit = pct <= line
            out.append({"title": f"止盈监控 · {fund['名称'][:10]}",
                        "value": f"{pct:+.2f}%",
                        "detail": ("已触发！按纪律止盈 1/3~1/2" if hit else f"止盈线 {line}% · 未触发"),
                        "hot": hit})

    today = datetime.date.today()
    days_ahead = (3 - today.weekday()) % 7  # 周四=3
    nxt = today + datetime.timedelta(days=days_ahead)
    label = "就是今天！15:00 前" if days_ahead == 0 else f"{nxt.isoformat()}（还差 {days_ahead} 天）"
    b = pos["资金B"]["底仓"]
    done = b["已完成批次"]
    sched = next((s for s in b["批次日程"] if "待执行" in s), "全部完成")
    out.append({"title": "买入窗口 · 下一个周四", "value": label,
                "detail": f"底仓进度 {done}/{b['总批次']}批 · 下一步：{sched}", "hot": days_ahead == 0})
    return out


def state_badge(state):
    color = STATE_COLOR.get(state, "#b2bec3")
    return f'<span class="badge" style="background:{color}">{state}</span>'


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_html(stamp, market, ranked, acc_dates, acc_rows, ratio_date, ratio_rows,
               valuations, gstamp, gdata, pos, triggers, acc_chg20):
    gen_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    vmap = {v["name"]: v for v in valuations}

    # ---- 触发器卡 ----
    trig_html = "".join(
        f'<div class="card trig{" hot" if t["hot"] else ""}"><div class="trig-title">{esc(t["title"])}</div>'
        f'<div class="trig-value">{esc(t["value"])}</div><div class="trig-detail">{esc(t["detail"])}</div></div>'
        for t in triggers)

    # ---- 指数估值表 ----
    vrows = ""
    for v in valuations:
        pe = f'{v["pe_pct"]*100:.0f}%' if v.get("pe_pct") is not None else "-"
        pb = f'{v["pb_pct"]*100:.0f}%' if v.get("pb_pct") is not None else "-"
        pe_bar = f'<div class="bar"><i style="width:{v["pe_pct"]*100:.0f}%"></i></div>' if v.get("pe_pct") is not None else ""
        pb_bar = f'<div class="bar"><i style="width:{v["pb_pct"]*100:.0f}%"></i></div>' if v.get("pb_pct") is not None else ""
        chg = v.get("chg_pct")
        chg_s = f'<span class="{"up" if chg and chg > 0 else "dn"}">{chg:+.2f}%</span>' if chg is not None else "-"
        vrows += (f'<tr><td>{esc(v["name"])}</td><td>{v.get("price","-")}</td><td>{chg_s}</td>'
                  f'<td>{state_badge(v["state"])}</td>'
                  f'<td>{pe}{pe_bar}</td><td>{pb}{pb_bar}</td></tr>')
    val_table = f'''<table><tr><th>指数</th><th>点位</th><th>今日</th><th>档位</th><th>PE分位</th><th>PB分位</th></tr>{vrows}</table>'''

    # ---- 持仓A ----
    funds = pos["资金A"]["基金"]
    total_a = sum(f["市值"] for f in funds)
    pnl_a = sum(f["收益额"] for f in funds)
    semi = sum(f["市值"] for f in funds if f["分类"] == "半导体")
    conc = semi / total_a * 100
    conc_banner = (f'<div class="warn">⚠️ 单一行业（半导体）集中度 {conc:.0f}%，已超 25% 上限</div>'
                   if conc > 25 else f'<div class="ok">✓ 行业集中度 {conc:.0f}%（≤25% 达标）</div>')
    frows = ""
    for f_ in funds:
        idx_state = vmap.get(f_["关联指数"], {}).get("state", "未收录")
        pcls = "up" if f_["收益率"] > 0 else "dn"
        frows += (f'<tr><td>{esc(f_["名称"])}</td><td>{f_["市值"]:,.0f}</td>'
                  f'<td class="{pcls}">{f_["收益率"]:+.2f}%</td>'
                  f'<td>{esc(f_["分类"])}</td><td>{state_badge(idx_state)}</td></tr>')
    pos_a_html = (f'<p class="sum">合计 <b>{total_a:,.0f}</b> 元 · 总收益 '
                  f'<b class="{"up" if pnl_a > 0 else "dn"}">{pnl_a:+,.0f}</b> 元'
                  f'（数据截至 {pos["updated_at"]}）</p>{conc_banner}'
                  f'<table><tr><th>基金</th><th>市值</th><th>收益率</th><th>分类</th><th>关联档位</th></tr>{frows}</table>')

    # ---- 资金B ----
    b = pos["资金B"]
    batch_pct = b["底仓"]["已完成批次"] / b["底仓"]["总批次"] * 100
    alloc = " + ".join(f'{a["名称"]}{a["占比"]}%' for a in b["底仓"]["配置"])
    dingtou = "已启动" if b["定投池"].get("已启动") else "未启动（底仓建完后）"
    bullets = "；".join(f'{t["指数"]}{t["条件"]}' for t in b["机动子弹"]["触发条件"])
    pos_b_html = f'''
    <p class="sum">底仓 <b>{b["底仓"]["总额"]:,}</b> 元 · 配置：{esc(alloc)}</p>
    <div class="progress"><i style="width:{batch_pct:.0f}%"></i><span>{b["底仓"]["已完成批次"]}/{b["底仓"]["总批次"]} 批</span></div>
    <table>
      <tr><th>模块</th><th>金额</th><th>状态</th></tr>
      <tr><td>定投池</td><td>{b["定投池"]["总额"]:,} 元</td><td>{dingtou}</td></tr>
      <tr><td>机动子弹</td><td>{b["机动子弹"]["总额"]:,} 元</td><td>{esc(bullets)}</td></tr>
    </table>'''

    # ---- 全球市场 ----
    grow = "".join(
        f'<tr><td>{esc(n)}</td><td>{p:,.0f}</td>'
        f'<td class="{"up" if c > 0 else "dn"}">{c:+.2f}%</td></tr>' for n, p, c in gdata)

    # ---- 吸筹榜 ----
    arows = "".join(
        f'<tr><td>{esc(r[1])}</td><td>{r[2]}/{len(acc_dates)}</td><td class="up">+{r[3]:,.1f}亿</td>'
        f'<td>{(r[4] or 0):+.2f}%</td><td>{acc_chg20.get(r[0], 0):+.1f}%</td></tr>' for r in acc_rows)
    acc_html = (f'<p class="sum">统计窗口 {acc_dates[0]} ~ {acc_dates[-1]}（≥3天净流入且累计为正）</p>'
                f'<table><tr><th>板块</th><th>吸筹天数</th><th>累计</th><th>今日</th><th>20日</th></tr>{arows}</table>')

    # ---- 资金流ratio信号 ----
    def ratio_row(r):
        name, net_wan, mv_yi, bp = r
        cls = "up" if bp > 0 else "dn"
        badge = ""
        if abs(bp) >= RATIO_CRIT_BP:
            badge = ' <span class="badge" style="background:#d63031">CRIT</span>'
        elif abs(bp) >= RATIO_WARN_BP:
            badge = ' <span class="badge" style="background:#e8a33d">WARN</span>'
        return (f'<tr><td>{esc(name)}</td><td class="{cls}">{net_wan / 1e4:+,.1f}亿</td>'
                f'<td>{mv_yi:,.0f}亿</td><td class="{cls}">{bp:+.1f}bp</td>{badge}</tr>')

    if ratio_rows:
        in8 = sorted(ratio_rows, key=lambda r: -r[3])[:8]
        out8 = sorted(ratio_rows, key=lambda r: r[3])[:8]
        ratio_html = (
            f'<p class="sum">{ratio_date} · ratio=主力净流入/流通市值'
            f'（WARN ±{RATIO_WARN_BP}bp / CRIT ±{RATIO_CRIT_BP}bp）</p>'
            '<table><tr><th colspan="4" style="color:#d63031">吸筹 TOP8（净流入强度）</th></tr>'
            + "".join(ratio_row(r) for r in in8)
            + '<tr><th colspan="4" style="color:#1a9c6b">出逃 TOP8（净流出强度）</th></tr>'
            + "".join(ratio_row(r) for r in out8) + '</table>')
    else:
        ratio_html = '<p class="sum">暂无资金流/市值数据</p>'

    flow_data = [{"name": n, "inflow": round(mi / 1e4, 2)} for n, _, mi in ranked[:10]]
    flow_out = [{"name": n, "inflow": round(mi / 1e4, 2)} for n, _, mi in reversed(ranked[-10:])]

    html = load_template()
    for token, value in {
        "__GEN_TIME__": gen_time,
        "__UP__": str(market["up"]),
        "__DOWN__": str(market["down"]),
        "__MKT_DATE__": market["date"],
        "{{TRIGGERS}}": trig_html,
        "{{VAL_TABLE}}": val_table,
        "{{POS_A}}": pos_a_html,
        "{{POS_B}}": pos_b_html,
        "__GLOBAL_STAMP__": gstamp or "-",
        "{{GLOBAL_ROWS}}": grow,
        "{{ACC_HTML}}": acc_html,
        "{{FLOW_RATIO}}": ratio_html,
        "__FLOW_IN__": json.dumps(flow_data, ensure_ascii=False),
        "__FLOW_OUT__": json.dumps(flow_out, ensure_ascii=False),
    }.items():
        html = html.replace(token, value)
    return html


_TPL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "dashboard.html")


def load_template():
    """看板 HTML 模板（外置于 templates/dashboard.html，避免巨型字符串内嵌）。"""
    with open(_TPL_PATH, encoding="utf-8") as f:
        return f.read()




def main():
    pos = load_json(POS_PATH)
    iv = load_json(IV_PATH)
    valuations = iv["rows"]
    for v in valuations:
        v["_src_date"] = iv.get("date", "")
    conn = history_db.connect(readonly=True)
    try:
        mkt_date, market, ranked = q_market(conn)
        market["date"] = mkt_date
        acc_dates, acc_rows = q_accumulation(conn)
        ratio_date, ratio_rows = q_ratio(conn)
        acc_chg20 = chg_20d_map(conn)
    finally:
        conn.close()
    gstamp, gdata = load_global()
    triggers = build_triggers(pos, valuations)
    html = build_html(None, market, ranked, acc_dates, acc_rows, ratio_date, ratio_rows,
                      valuations, gstamp, gdata, pos, triggers, acc_chg20)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"投资看板已生成 -> {OUT_PATH}")


if __name__ == "__main__":
    main()
