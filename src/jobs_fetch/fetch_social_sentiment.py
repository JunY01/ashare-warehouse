# -*- coding: utf-8 -*-
"""社群情绪每日采集（mom/dad 情绪交叉验证的数据源）。

采集 → 规则打分 → 指数 → 落库 social_sentiment_daily（date+skey 双主键幂等）。

数据源：东方财富股吧 ETF 吧列表页（urllib 直抓 HTML，串行+sleep，
失败置空不硬扛）。XHS 无 Key 暂跳过（见 reports/_过程/情绪回溯可行性_*.md）。

打分规则引用 temp/mom-index（新手信号）与 temp/dad-index（经验信号）的
关键词口径，标题 only（股吧列表无正文），content 缺失记 source=title-only。
真帖与模拟/fallback 严格隔离：无 fallback 帖，抓不到即空桶。

板块口径：src/common/market_map.SOCIAL_SECTOR_MAP 唯一维护，
sector_codes 可直接 join sector_kline/sector_flow_daily。

产物：
  data/cache/social_posts/social_sentiment_posts_YYYYMMDD.json  -- 原始帖子底稿（git忽略）
  data/social_sentiment_daily.json  -- 当日双指数（调试用，线上读 DB）
  market_history.db::social_sentiment_daily  -- 指数唯一真身（验证只读此表）

用法:
  python fetch_social_sentiment.py                    # 当日采集+打分
  python fetch_social_sentiment.py --backfill-days 7  # 一次性冷启动回溯（≤14）

纯标准库。update_data.py 每日编排调用（独立进程，不 import）。
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.common.paths import DATA_ROOT
from src.common.market_map import SOCIAL_SECTOR_MAP, write_json_atomic

POSTS_DIR = os.path.join(DATA_ROOT, "cache", "social_posts")  # 原始帖子底稿（git忽略）

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0",
      "Referer": "https://guba.eastmoney.com/"}

DEVICE_ID = "e8f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5"  # gbapi 需设备号占位，固定值不影响返回

# mom 新手信号关键词（temp/mom-index 口径子集，标题匹配用）
NEWBIE_KW = ["小白", "新手", "新人", "刚入", "第一次", "菜鸟", "萌新", "宝妈",
             "不懂", "请教", "请问", "求助", "怎么买", "什么意思", "该不该",
             "要不要", "能不能", "可以吗", "靠谱吗", "能上车吗", "好慌",
             "救命", "完了", "哭了", "割肉", "后悔", "跟着买", "听说",
             "朋友说", "梭哈", "稳赚", "必涨", "满仓干", "起飞", "明天涨",
             "今天跌", "吗？", "呢？"]
# dad 经验信号关键词（temp/dad-index 口径子集）
VETERAN_KW = ["PE", "PB", "ROE", "估值", "基本面", "技术面", "定投", "仓位",
              "分散", "止损", "止盈", "季报", "年报", "GDP", "CPI", "美联储",
              "加息", "降息", "长期持有", "风险"]
BUY_KW = ["上车", "抄底", "加仓", "买入", "买了", "入手", "建仓", "补仓", "定投"]
SELL_KW = ["割肉", "止损", "清仓", "减仓", "卖了", "离场", "下车", "赎回",
           "亏了", "深套", "套牢", "被套"]
GREED_KW = ["冲", "梭哈", "稳赚", "必涨", "抄底", "起飞", "暴涨", "翻倍", "赚了"]
FEAR_KW = ["割肉", "止损", "亏", "暴跌", "崩盘", "完了", "套牢", "大跌"]
# 垃圾帖（活动/签到类，不计入指数）
SPAM_KW = ["金条", "签到", "打卡", "广告"]


def fetch_board(board, page=1):
    """抓一页股吧列表 JSON（guba 列表页已改 JS 渲染，只能走 gbapi 接口）。
    缺 plat/product/version/deviceid 时接口返回空数组。失败返回 None。"""
    url = ("https://gbapi.eastmoney.com/webarticlelist/api/Article/Articlelist"
           "?code=%s&ps=20&p=%d&type=0&sorttype=1"
           "&plat=web&product=web&version=1&deviceid=%s&needInteractData=0"
           % (board, page, DEVICE_ID))
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def parse_board(data, board):
    """解析 JSON 列表：标题/作者/阅读/回复/时间。返回帖子 dict 列表。"""
    posts = []
    for it in (data or {}).get("re") or []:
        title = (it.get("post_title") or "").strip()
        pid = it.get("post_id")
        if not title or not pid:
            continue
        code = it.get("stockbar_code") or board
        posts.append({
            "id": "guba_%s" % pid,
            "title": title,
            "url": "https://guba.eastmoney.com/news,%s,%s.html" % (code, pid),
            "platform": "guba",
            "author": it.get("user_nickname") or "未知",
            "reads": str(it.get("post_click_count") or 0),
            "replies": str(it.get("post_comment_count") or 0),
            "date": it.get("post_publish_time") or "未知",
            "source": "title-only",
        })
    return posts



def norm_date(raw, today=None):
    """股吧时间戳 → YYYY-MM-DD。支持完整 ISO（gbapi）与 MM-DD（旧页面）。未知返回 None。"""
    today = today or datetime.date.today()
    s = (raw or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    m = re.match(r"(\d{2})-(\d{2})", s)
    if not m:
        return None
    mm, dd = int(m.group(1)), int(m.group(2))
    year = today.year
    if today.month == 1 and mm == 12:
        year -= 1
    try:
        return datetime.date(year, mm, dd).isoformat()
    except ValueError:
        return None


def score_post(title):
    """标题打分：返回 (mom_score 0-100, veteran_score 0-100, intent, sentiment)。"""
    t = title or ""
    nb = sum(1 for k in NEWBIE_KW if k in t)
    ve = sum(1 for k in VETERAN_KW if k in t)
    mom = max(0.0, min(100.0, nb * 12.0 + 6.0 - ve * 8.0))
    vet = max(0.0, min(100.0, ve * 15.0 + 8.0 - nb * 6.0))
    buy = sum(1 for k in BUY_KW if k in t)
    sell = sum(1 for k in SELL_KW if k in t)
    intent = "buy" if buy > sell else ("sell" if sell > buy else "neutral")
    g = sum(1 for k in GREED_KW if k in t)
    f = sum(1 for k in FEAR_KW if k in t)
    sent = round((g - f) / (g + f), 2) if (g + f) else 0.0
    return mom, vet, intent, sent


def _score_all(posts):
    """帖子列表 → (total, spam, scored)。scored 元素含 mom/vet/intent/sent/hot。
    mom 与 dad 共用同一份打分（标题+阅读/回复），指数口径在 compute 层分开。"""
    valid = [p for p in posts if not any(k in (p.get("title") or "") for k in SPAM_KW)]
    total, spam = len(posts), len(posts) - len(valid)
    scored = []
    for p in valid:
        mom, vet, intent, sent = score_post(p.get("title", ""))
        try:
            reads = int(p.get("reads") or 0)
        except ValueError:
            reads = 0
        try:
            replies = int(p.get("replies") or 0)
        except ValueError:
            replies = 0
        scored.append({"mom": mom, "vet": vet, "intent": intent, "sent": sent,
                       "hot": reads >= 500 or replies >= 5})
    return total, spam, scored


def _interp(idx, bands):
    """bands 为 [(下限, 文案)] 降序，命中第一档。"""
    for lo, txt in bands:
        if idx >= lo:
            return txt
    return bands[-1][1]


MOM_BANDS = [(75, "🔴 极度狂热"), (60, "🟠 高度警惕"), (40, "🟡 开始升温"),
             (20, "🟢 正常区间"), (0, "🔵 极度冷清")]
DAD_BANDS = [(40, "💪 经验共识强"), (25, "👀 经验露头"), (15, "🌱 零星经验"),
             (0, "🔵 散户主导")]


def compute_mom(scored, total, spam):
    """宝妈指数：散户情绪热度 = 有买卖意图帖占比×50 + 情绪非零帖占比×30 + 高互动帖占比×20。
    分布基线 mom 单帖中位数 6（标题信号稀疏），指数中位数约 7.5。"""
    n = len(scored)
    if not n:
        return {"index": 0.0, "interpretation": "🔵 样本不足"}
    intent_n = sum(1 for s in scored if s["intent"] != "neutral")
    emo_n = sum(1 for s in scored if s["sent"] != 0)
    hot_n = sum(1 for s in scored if s["hot"])
    idx = round(intent_n / n * 50 + emo_n / n * 30 + hot_n / n * 20, 1)
    return {"index": idx, "interpretation": _interp(idx, MOM_BANDS)}


def compute_dad(scored, total, spam):
    """宝爸指数：经验共识 = 高经验帖占比(vet≥15)×50 + 经验均分归一×30 + 买入一致性×20。
    vet 单帖中位数 8，vet≥15 约占 3.4% 帖。买入一致性 50 为中性（无方向则 50），
    全买入 100、全卖出 0——dad 高表示经验型讨论偏多且偏多头一致。"""
    n = len(scored)
    if not n:
        return {"index": 0.0, "interpretation": "🔵 样本不足"}
    vet_n = sum(1 for s in scored if s["vet"] >= 15)
    vet_avg = sum(s["vet"] for s in scored) / n
    buy = sum(1 for s in scored if s["intent"] == "buy")
    sell = sum(1 for s in scored if s["intent"] == "sell")
    vet_ratio = vet_n / n * 100
    vet_norm = min(vet_avg / 30 * 100, 100)
    cons = 50 + (buy - sell) / n * 50
    idx = round(vet_ratio * 0.50 + vet_norm * 0.30 + cons * 0.20, 1)
    return {"index": idx, "interpretation": _interp(idx, DAD_BANDS)}


def compute_dual(posts):
    """帖子列表 → 双指数 {mom_index, mom_interp, dad_index, dad_interp, details}。
    details 含诊断字段：mom_avg/vet_avg、买卖计数、经验帖买卖一致性（验证层 dad veto/影子用）。"""
    total, spam, scored = _score_all(posts)
    n = len(scored)
    if not n:
        return {"mom_index": 0.0, "mom_interp": "🔵 样本不足",
                "dad_index": 0.0, "dad_interp": "🔵 样本不足",
                "details": {"total": total, "valid": 0, "spam": spam}}
    mom = compute_mom(scored, total, spam)
    dad = compute_dad(scored, total, spam)
    vet_posts = [s for s in scored if s["vet"] >= 15]
    vet_buy = sum(1 for s in vet_posts if s["intent"] == "buy")
    vet_sell = sum(1 for s in vet_posts if s["intent"] == "sell")
    vet_intent = vet_buy + vet_sell
    buy = sum(1 for s in scored if s["intent"] == "buy")
    sell = sum(1 for s in scored if s["intent"] == "sell")
    return {"mom_index": mom["index"], "mom_interp": mom["interpretation"],
            "dad_index": dad["index"], "dad_interp": dad["interpretation"],
            "details": {"total": total, "valid": n, "spam": spam,
                        "intent_ratio": round(sum(1 for s in scored if s["intent"] != "neutral") / n * 100, 1),
                        "emo_ratio": round(sum(1 for s in scored if s["sent"] != 0) / n * 100, 1),
                        "hot_ratio": round(sum(1 for s in scored if s["hot"]) / n * 100, 1),
                        "mom_avg": round(sum(s["mom"] for s in scored) / n, 1),
                        "vet_avg": round(sum(s["vet"] for s in scored) / n, 1),
                        "vet_n": len(vet_posts),
                        "vet_buy": vet_buy, "vet_sell": vet_sell,
                        "vet_sell_cons": round(vet_sell / vet_intent, 2) if vet_intent else 0.0,
                        "vet_buy_cons": round(vet_buy / vet_intent, 2) if vet_intent else 0.0,
                        "buy": buy, "sell": sell,
                        "buy_sell_diff": buy - sell}}


def compute_index(posts):
    """兼容旧口径：返回 mom 指数（旧调用方不中断）。新代码请用 compute_dual。"""
    d = compute_dual(posts)
    return {"index": d["mom_index"], "interpretation": d["mom_interp"],
            "details": d["details"]}


def collect_key(board, max_pages=1, sleep_s=2):
    """采一个吧（翻页去重），返回帖子列表。失败页置空不抛错。"""
    seen, out = set(), []
    for pg in range(1, max_pages + 1):
        data = fetch_board(board, pg)
        if data is None:
            break
        fresh = [p for p in parse_board(data, board) if p["id"] not in seen]
        seen.update(p["id"] for p in fresh)
        out.extend(fresh)
        if pg < max_pages:
            time.sleep(sleep_s)
    return out


def main():
    backfill = 0
    if "--backfill-days" in sys.argv:
        try:
            backfill = min(14, int(sys.argv[sys.argv.index("--backfill-days") + 1]))
        except (ValueError, IndexError):
            print("  --backfill-days 后需跟天数（≤14）")
            return 1
    today = datetime.date.today()
    today_s = today.isoformat()
    pages = max(1, backfill * 3) if backfill else 1
    print("== 社群情绪采集（股吧标题信号）==")
    dated = {}  # date -> key -> posts
    for key, cfg in SOCIAL_SECTOR_MAP.items():
        board = cfg.get("guba_board")
        if not board:
            continue
        posts = collect_key(board, max_pages=pages)
        print("  [%s] %s: %d 条" % (cfg["label"], board, len(posts)))
        for p in posts:
            d = norm_date(p.get("date", ""), today) or today.isoformat()
            dated.setdefault(d, {}).setdefault(key, []).append(p)
        time.sleep(2)
    if not dated:
        print("  全部采集失败，写空指数（不污染历史）")
        return 1
    # 日常模式只写当日桶（第一页混有多天帖子，历史桶只允许回溯写入，
    # 否则每天重跑会用第一页窗口截断污染已攒的历史样本）
    if not backfill:
        dated = {today_s: dated.get(today_s, {})}
        if not dated[today_s]:
            print("  当日无帖子，跳过写盘")
            return 1
    _pending_db = {}  # date -> sectors（落库用）
    for d in sorted(dated):
        sectors = {}
        merged = {}  # key -> posts（与已有快照按 id 去重合并）
        for key, posts in dated[d].items():
            os.makedirs(POSTS_DIR, exist_ok=True)
            fp = os.path.join(POSTS_DIR, "social_sentiment_posts_%s.json" % d.replace("-", ""))
            old = {}
            if os.path.exists(fp):
                try:
                    old = json.load(open(fp, encoding="utf-8"))
                except (ValueError, OSError):
                    old = {}
            have = {p.get("id") for p in old.get(key, [])}
            merged[key] = old.get(key, []) + [p for p in posts if p.get("id") not in have]
            sectors[key] = compute_dual(merged[key])
        snap = {"date": d, "provisional": d == today_s,
                "source": "guba-title-only", "sectors": sectors}
        fp = os.path.join(POSTS_DIR, "social_sentiment_posts_%s.json" % d.replace("-", ""))
        raw = {}
        if os.path.exists(fp):
            try:
                raw = json.load(open(fp, encoding="utf-8"))
            except (ValueError, OSError):
                raw = {}
        raw.update(merged)
        write_json_atomic(fp, raw, indent=2)
        _pending_db[d] = sectors
        if d == today_s:
            write_json_atomic(os.path.join(DATA_ROOT, "social_sentiment_daily.json"),
                              snap, indent=2)
        n = sum(v["details"]["total"] for v in sectors.values())
        print("  %s: %d 帖 %s" % (d, n,
              " ".join("%sM%s/D%s" % (k, v["mom_index"], v["dad_index"])
                        for k, v in sectors.items())))
    # 落库 social_sentiment_daily（date+skey 双主键幂等，同日重跑覆盖）
    try:
        from src.common import history_db
        conn = history_db.connect()
        try:
            rows = [(d, k, v["mom_index"], v["dad_index"],
                     v["details"]["total"], v["details"]["valid"], v["details"]["spam"],
                     v["details"]["buy"], v["details"]["sell"],
                     v["details"].get("vet_buy", 0), v["details"].get("vet_sell", 0))
                    for d, secs in _pending_db.items() for k, v in secs.items()]
            conn.executemany("INSERT OR REPLACE INTO social_sentiment_daily VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            print("  落库 social_sentiment_daily: %d 行" % len(rows))
        finally:
            conn.close()
    except Exception as e:
        print("  落库失败（不影响 JSON）: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
