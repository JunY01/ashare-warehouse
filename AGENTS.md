# A股行业数据仓库 — 工作区指令

## 项目概述

纯 Python 标准库（零第三方依赖）的 A 股行业数据管线。从东方财富、腾讯财经、蛋卷基金抓取数据，存入 CSV/JSON，归档进 SQLite 历史库，最终生成 Markdown 分析报告和 HTML 投资看板。

## 目录结构（2026-09-10 全盘修复后）

```
config/         用户配置与模型唯一目录（positions.json/watchlist.json/model_r2.json/model_short.json/fundcode_all.js/em_cookie.txt——cookie唯一副本；model_zhuang*.json 晋升写盘后才有，缺席为正常态）
src/common/     基础与公共层（paths.py：REPO_ROOT/DATA_ROOT/CONFIG_ROOT/REPORT_ROOT/DB_PATH/SWEEP_CACHE；history_db.py：25表DDL唯一来源 + connect(readonly=)统一DB入口；market_map.py：SECTOR_TO_BROAD/SECTOR_FUND_MAP/INDEX_BROAD/PROXY_ETF/HOLDING_INDEX_MAP + cfg_path/load_json/read_csv/write_json_atomic；fetch_util.py：curl_json/urllib_json/num/wan/yi/drop_incomplete抓取原语；fund_lookup.py：load_fund_list/find_c_funds_broad/find_c_funds_simple；kline_csv.py：write_sector_300d唯一写入者；technical_indicators.py：ema/sma/rsi/macd/atr等；fees.py：disc_fee费后收益；tickflow_source.py：TickFlow兜底源(纯stdlib)）
src/jobs_fetch/ 抓取作业，只抓取+落库不生成报告（update_data.py一站式编排/fetch_global.py/fetch_sentiment.py/fetch_stock_watch.py/fetch_sector_members.py/fetch_fund_nav.py/fund_realtime.py/browser_fetch_klines.py/fetch_ohlcv_chunk.py/update_kline_chunk.py/backfill_flow.py/backfill_kline_3y.py/backfill_review_picks.py/import_global_indices.py/import_investing_kline.py/fetch_etf_kline.py/fetch_limit_up_down.py/fetch_dragon_tiger.py；缺口补齐四件套 fetch_global_kline.py 外盘日K / fetch_index_csv_kline.py 指数CSV / fetch_sector_close.py 板块收盘[须09:15前跑] / fetch_etf_close.py ETF收盘，均带重叠日校验，不靠被限流的东财K线端点）
src/jobs_build/ 构建作业，算+报（scan_regime.py/regime_lamp.py/fetch_index_valuation.py/fetch_sector_valuation.py/daily_1430.py+daily_1430_scan.py+red40_monitor.py+deepzone_monitor.py/daily_review.py/t_trade_signal.py/generate_dashboard.py+templates/看板模板；daily_1430 为六区制 A持仓/B短期行业/C抄底观察/D全市场恐慌监控/E红利40日收益差/F宽基深跌区——**只有 A 区是可交易依据**（且是"减震器"控回撤、非"发动机"，见下方「各区验证状态」）；B 区选股实测零超额、C/D/E/F 均为观察/提示——各区证据见下方「14:30 六区验证状态」）
research/       研究回测（_engine.py共享回测原语；zhuang_line.py/sweep_zhuang.py/validate_zhuang.py/train_recommend.py/iterate.py/sweep100k_r2.py/sweep192k_r2.py/sweep100k_short.py/sweep100k_etf_macd.py/validate_short.py/validate_lamp.py/validate_review.py/sweep_review.py/scan_factors.py/pick_r2_momentum.py/backtest_r2_momentum.py/backtest_r2_index10y.py/backtest_t_trade.py/backtest_etf_macd.py；validate_advice_rule.py=持股建议引擎的规则验证台：任何想加进 holding_advice 的规则先过它（无前视分位+独立时段计数）；rebound_line.py=超跌反弹验证台（--scan/--trigger/--judge/--sens 板块层，--index/--rule 指数层）：板块层不成立、指数层"20日跌≥20%"成立且不需止跌确认，评审见 reports/超跌反弹验证_20260916.md；sweep100k_rebound.py=十万次随机取样迭代（随机指数子集×随机时间窗×320组参数）验证这条规则换样本还成不成立，结论见 reports/超跌反弹十万次迭代_20260916.md；divergence_line.py=三重背离验证台（--scan/--control/--diag/--system/--judge/--current）：把知乎"布林+MACD+RSI三重背离"去主观化成确定性判据——顶背离增量不硬、底背离是负增量，真正有效的是"离半年线多远"这个位置，见 reports/三重背离验证_20260917.md；sweep100k_divergence.py=十万次随机取样迭代验证，结论见 reports/三重背离十万次迭代_20260917.md）
archive/        封存区（scripts/全目录、research_deprecated/退役研究脚本、fetch_fund_estimation.py(akshare版)、config_deprecated/死配置、data/历史审计文档；替代关系见 archive/README.md）
data/           纯数据+运行产物（快照csv/json/市场历史库 market_history.db 唯一真身；cache/ 放K线原始缓存与进度文件 kline_raw.json/backfill_3y_progress.json；cache/sweeps/ 放研究网格与面板中间产物*jsonl/*pkl，git忽略；kline_10y//kline_full/ 指数K线）
reports/        生成的报告与看板（.md + 投资看板.html；_过程/放迭代日志/方案验证状态/每日复盘交接文件）
tests/          冒烟测试（python -m unittest discover -s tests：全脚本可导入/DB入口/指标/单写入者）
```

## 分层与寻址约定（新增脚本必须遵守）

- **所有脚本顶部自引导 REPO_ROOT 进 sys.path 后直连 `src.common.paths`**，禁止 `os.path.dirname(__file__)` 自行寻址（历史上曾导致估值/DB/报告写错目录、三份孤儿库）
- 产物落位固定：数据 → `data/`，模型/配置 → `config/`（线上唯一读取处），报告 → `reports/`
- **分层**：jobs_fetch 不 import jobs_build（update_data 对 scan_regime/generate_dashboard 用 subprocess 独立进程调用，且不生成任何报告文件）；jobs_build 可复用 jobs_fetch 的数据能力（fund_realtime）
- 建表 DDL 一律集中在 `src/common/history_db.py` 的 SCHEMA（25 表），各脚本只 `history_db.connect()`，不自行建表；纯读者用 `history_db.connect(readonly=True)`（不建表、无写副作用）
- 行业映射与基金搜索词、费用函数在 `src/common/` 唯一维护，两侧脚本只引用不重定义
- research 引用 jobs_build 模块一律用包式路径（`from src.jobs_build.daily_1430 import ...`），不靠 sys.path 裸名导入

## 常用命令

在仓库根 `D:\DOC\大A` 执行（所有脚本均自引导，但保持根目录运行习惯）：

```bash
python src/jobs_fetch/update_data.py                 # 全量更新（快照 + 分片K线，自动编排build层）
python src/jobs_fetch/update_data.py --snapshot-only # 仅快照（约30秒）
python src/jobs_fetch/update_data.py --kline-only    # 仅K线（分片：20×25 + --finish，每片落盘可续跑）
python src/jobs_fetch/fetch_global_kline.py          # 外盘日K缺口补齐（--target YYYY-MM-DD，默认今天）
python src/jobs_fetch/fetch_index_csv_kline.py       # kline_10y/kline_full 指数CSV补齐（--dry-run 可试算）
python src/jobs_fetch/fetch_sector_close.py --target YYYY-MM-DD   # 板块收盘补齐（须09:15前跑，过时拒写）
python src/jobs_fetch/fetch_etf_close.py             # ETF收盘补齐（日期由腾讯时间戳推断）
python src/jobs_fetch/fetch_ohlcv_chunk.py --patient # 板块OHLCV(含量额)限流期补齐：轮询多轮，可中断续跑
python src/jobs_fetch/fetch_limit_up_down.py --date 2026-09-15  # 回补指定日涨跌停池（不带 --date 则取当天）
python src/jobs_build/fetch_index_valuation.py       # 指数估值 + 生成估值参考报告（--json 只更新数据）
python src/jobs_build/fetch_sector_valuation.py      # 板块级估值（蛋卷行业PB分位 + 东财板块PE快照）
python src/jobs_build/generate_dashboard.py          # 生成投资看板 HTML
python src/jobs_build/daily_1430.py --no-refresh     # 14:30 抢票机（离线复核）
python src/jobs_fetch/import_global_indices.py          # 导入全球指数日K（investing.com）
python -m unittest discover -s tests                # 冒烟测试（可导入/DB入口/指标/单写入者）
```

## 数据源与反爬

- **东方财富 K 线接口**首选浏览器自动化（无需 cookie，稳定可靠）；备选 Python urllib 需 `config/em_cookie.txt`（唯一副本）或 `EM_COOKIE` 环境变量
- 所有接口串行请求 + sleep 防限流，失败自动重试 4 次
- `python3` 在 Windows 上可能是 Store 占位符，统一用 `python`

### K线抓取（浏览器自动化 — 首选方案）

用 ZCode 浏览器自动化直接调东财API，无需 cookie，99.8%成功率：

**流程：**
1. 打开东财页面获取 cookie（自动完成）
2. 获取全部板块列表（496个，分页获取）
3. 逐个导航到 K线 API URL，解析 JSON 响应
4. 保存到 `data/cache/kline_raw.json`，重算 `sector_300d.csv`，落库 `sector_kline`

**关键参数：**
- API: `http://push2his.eastmoney.com/api/qt/stock/kline/get?secid=90.{代码}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&beg={起始日}&end={结束日}&lmt=400`
- 板块列表: `http://push2delay.eastmoney.com/api/qt/clist/get?pn={页码}&pz=200&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f2,f12,f14,f128`
- 增量抓取: `beg` 设为上次 `last_date`，`end` 设为今天
- 全量抓取: `beg=20250601`

**限流规则：**
- 每批 50 个板块，间隔 1.2-1.5 秒
- 约 100 次请求后可能触发限流，需等待 15-20 分钟恢复
- 失败的板块单独重试，间隔 2-3 秒

**调用方式（ZCode 会话中）：**
```
用浏览器自动化抓取K线（ZCode 会话中可直接让助手执行）
```

### 英为财情（investing.com）指数日K接口

**用途**：获取全球主要指数（含A股）的历史日K OHLC 数据，补充东财/腾讯不覆盖的指数。

**API 格式**：
```
https://endpoints.investing.com/pd-instruments/v1/instruments/{instrument_id}/charts/candles/p1d?limit=10000&domain_id=6
```

**请求头**：必须带 `Referer: https://cn.investing.com/`，否则 403。

**instrument_id 映射表**（通过 investing.com 搜索页面获取）：

| 指数 | 代码 | instrument_id | 历史起点 |
|---|---|---|---|
| 上证指数 | SSEC | 40820 | 1990-12-19 |
| 深证成指 | SZI | 942630 | 1995-01-23 |
| 沪深300 | CSI300 | 40823 | 2013-04-12 |
| 富时中国A50 | FTSE50 | 40824 | 2011-04-11 |
| 道琼斯指数 | DJIA | 169 | 1994-08-26 |
| 标普500指数 | SPX | 166 | 1986-12-26 |
| 纳斯达克综合 | IXIC | 14958 | 1986-12-26 |
| 费城半导体 | SOX | 40034 | 1994-05-04 |
| 英国富时100 | FTSE | 27 | 2001-01-02 |
| 德国DAX30 | GDAXI | 172 | 1993-12-14 |
| 恒生科技 | HSTECH | 1164092 | 2020-07-27 |
| 恒生指数 | HSI | 179 | 1987-06-10 |
| 日经225 | N225 | 178 | 2003-08-04 |
| 韩国KOSPI | KS11 | 37426 | 1994-10-31 |
| 台湾加权 | TWII | 38017 | 1992-03-12 |

**数据清洗规则**：
- 剔除 `o=h=l=c` 的脏数据（周末/节假日重复行）
- 剔除 OHLC 任一为 None/0 的无效行
- 日期格式：`YYYY-MM-DD`（从 ISO 时间戳截取）

**抓取流程**：
1. 浏览器导航到 `https://cn.investing.com/indices/{index-slug}` 获取 instrument_id
2. 浏览器内 fetch API URL 获取 JSON
3. 清洗后通过 Blob 下载为 JSON 文件
4. 运行 `python src/jobs_fetch/import_global_indices.py` 落库

**数据落库**：
- 表：`global_index_kline(code, date, open, high, low, close)`
- 脚本：`src/jobs_fetch/import_global_indices.py`
- 缓存：`data/cache/investing/` 目录下的 JSON 文件

**注意事项**：
- investing.com 只有**指数级**数据，没有板块和个股数据
- 不能替代东财/腾讯的板块和个股数据源
- 适合作为指数级历史数据的补充源（回测、长期趋势分析）

## 历史库规范（market_history.db）

联网抓到的数据**必须**落库，date 主键幂等覆盖（同日重跑不重复）。共 25 表，DDL 全部集中在 `src/common/history_db.py`：

| 表 | 内容 |
|---|---|
| `sector_daily` | 东财 496 板块快照（archive_snapshots） |
| `industry_daily` | 腾讯 31 一级行业 |
| `etf_daily` | ETF 快照 |
| `etf_kline` | ETF 日K OHLC（fetch_etf_kline） |
| `sector_kline` | 板块日收盘价（import_klines） |
| `sector_flow_daily` | 板块主力资金流（backfill_flow + 每日增量） |
| `market_turnover` | 两市成交额（fetch_sentiment） |
| `margin_balance` | 两融余额（fetch_sentiment） |
| `global_daily` | 全球指数快照（fetch_global） |
| `global_index_kline` | 全球指数日K OHLC（investing.com，15个指数，见下方映射表） |
| `regime_daily` | 趋势状态扫描（scan_regime） |
| `sector_ohlcv` | 板块 OHLCV（fetch_ohlcv_chunk） |
| `sector_member_daily` | 板块成分（fetch_sector_members） |
| `index_daily` | 指数日K（`fetch_dividend_index.py` 写入的红利系长历史 2013→今，E区红利40日收益差/`red40_monitor` 的唯一数据源；另有一批旧 secid 式代码停在 2026-08-26 无写入者，勿据其判定本表已废弃） |
| `fund_nav_daily` | 场外基金净值（fetch_fund_nav） |
| `stock_quote_daily` | 个股观察池行情（fetch_stock_watch） |
| `stock_fundamental_daily` | 个股基本面 |
| `industry_push_daily` | 14:30推送记录（daily_1430 B区） |
| `sector_valuation` | 板块当日PE(TTM)累积（archive_snapshots/fetch_sector_valuation，攒历史可自算分位） |
| `zhuang_pick_daily` | 抄底观察名单（daily_1430 C区） |
| `review_pick_daily` | 每日复盘"关注行业"五因子榜单（daily_review；含 data_date 与5个分项；source 区分 live 推送/backfill 历史回填，回测见 research/validate_review.py） |
| `social_sentiment_daily` | 社群情绪双指数（fetch_social_sentiment：妈妈/爸爸指数 mom_index/dad_index） |
| `zt_pool_daily` | 涨停池明细（fetch_limit_up_down/push2ex：连板数 lb_cnt、近期涨停天数 zt_days、封单 fund_amt） |
| `dt_pool_daily` | 跌停池明细（fetch_limit_up_down/push2ex） |
| `dragon_tiger_daily` | 龙虎榜明细（fetch_dragon_tiger/datacenter，T-1 回溯；仅股票剔除转债） |

## 报告命名规范

- 自动生成报告统一 `主题_YYYYMMDD.md`（中文主题 + 日期后缀），如 `每日复盘_20260907.md`
- 历史报告文件不重命名；看板固定 `reports/投资看板.html`
- 报告的生成脚本映射见各脚本 docstring；人工交接文档放 `reports/_过程/`

## research/ 命名约定（历史遗留，暂不重命名）

- `sweep<网格量级>_<策略>.py`：前缀是网格规模（如 `sweep100k_r2`=十万组 R2 动量、`sweep192k_r2`=19.2万组、`sweep100k_etf_macd`），**不代表策略轴**；同前缀的 `sweep100k_r2` 与 `sweep100k_short` 是无关策略，勿混淆。
- 同名算法（加权回归、MACD 金叉死叉）已在 `research/_engine.py` 收敛为唯一实现，各 sweep/backtest 只调用。
- 是否重命名按策略轴（如 `sweep_r2_momentum_grid.py`）待定：重命名会破坏报告/日志中的历史引用，收益有限，暂缓。

## 庄家抄底线（左侧抄底，与14:30短线互补）

研究闭环：`zhuang_line.py`（特征面板+规则评分）→ `sweep_zhuang.py`（双轨：资金流短窗14万组 + `--nf`长历史18.6万组，留存终验+邻域稳定，不达标不晋升）→ `validate_zhuang.py`（与现v2同场对打+重叠分析；`--nf` 三年窗口单独验证）。每日推荐走 daily_1430 的 **C区·抄底观察名单**（仅观察，不直接交易；晋升后 `sweep_zhuang --verify` 写 `config/model_zhuang.json` 才转交易，C区已只认 config/）。核心四要素：250日回撤到位 + bottom_score结构底 + 主力净流入吸筹 + 贴云带不接刀。**数据源限制**：东财主力资金流历史硬上限约120交易日（无法回补更早），板块K线到2023；`--nf` 轨用相对强度 z_mom20 代理吸筹，把验证窗口拉到 2024-02 起三年。

## 超跌反弹监控（14:30 的 D区；只做指数，不做行业）

**结论分两层，别混**：以"超跌反弹"选**行业**已验证**不成立**（496 板块 2023 起，11206 个"跌够 15%"的板块日经三级独立性折算后只剩 **9 轮**独立行情；每次超跌行情含 158~732 个板块，是"全市场一起跌"而非截面信号；"跑输市场"版本 3.7 年只有 **1 轮**）。用在**指数/宽基**上成立，故只做指数。

口径（`research/rebound_line.py` + `research/sweep100k_rebound.py`，评审 `reports/超跌反弹验证_20260916.md`、`reports/超跌反弹十万次迭代_20260916.md`）：触发 = 任一指数 **20 日跌幅 ≤ -20%**；26 条指数 26.7 年 → **28 轮独立行情、约 1 次/年**；20 日持有在 96% 的随机样本里赚钱（中位 +3.62%）、30 日 +8.31%（费后）。**四条硬边界**：① **不等止跌确认**（等"连涨两天"赚钱样本比例 89%→72%）；② **只做 20~30 日窗口**（≥28% 极深跌 40 日后翻脸，60 日仅 29% 样本赚钱）；③ **门槛放宽买不到更多机会**（-20%→-15% 机会 1.05→2.06 次/年但每次 +3.62%→+0.84%）；④ **结构熊市历史失效**（窗口终点<2015 的样本只有 51% 赚钱，2008 型 60 日可亏 30%+）。

落地：`daily_1430_scan.panic_monitor()/panic_lines()` → **D区**，平时只输出一行（最深指数 + 距门槛还差几个点），触发才展开表格与历史边界。数据用 `kline_full` 收盘（26 条，代码表 = `fetch_index_csv_kline.INDEXES`，唯一来源）+ 腾讯批量行情补当日点位（GBK，不能走 curl_json）；**盘中估算会标注"待收盘确认"**，收盘确认线为 15:05（同 `fetch_index_csv_kline.unfinished`）。本区**只提示机会，不给金额、不定买卖**——仓位归 `holding_advice.py` 与用户计划。**2026 年实测 8 次触发、20 日后 7/8 为正（中位 +9.2%）**。

## 宽基深跌区（14:30 的 F区；只做宽基，不做行业）

**来历**：2026-09-17 验证知乎「三重背离」（布林+MACD+RSI 共振）时的副产品。把主观描述去主观化成确定性判据后，长历史 + 十万次迭代一致得出：**顶背离增量不硬**（得分单调但只值约 0.7pp，且只在终点≥2020 的样本成立，三个历史年代全反）、**底背离是负增量**（深跌区里不加背离 +2.54%、加了三重背离降到 +1.45%，越共振越差；时段内第 1 根 −0.77 → 第 3 根 −1.40）。**真正有效的是"位置"——离半年线 MA120 多远**，不是背离。工具 `research/divergence_line.py`（`--scan/--control/--diag/--system/--judge/--current`）+ `research/sweep100k_divergence.py`，评审 `reports/三重背离验证_20260917.md`、`reports/三重背离十万次迭代_20260917.md`。

口径（`src/jobs_build/deepzone_monitor.py` → **F区**）：偏离度 = 收盘/MA120 − 1；**≤ −15%** 进深跌观察区、**≤ −18%** 极深。**12 条境内宽基** 26.3 年、独立行情级：≤−15% **36 轮、约 1.4 次/年**，30 日真实收益中位 **+1.84%**、事件胜率 69%；≤−18% **19 轮、约 0.7 次/年**，30 日 **+6.09%**、胜率 79%（**越深越好**）。对照"随便哪天买"20 日中位仅 +0.29%。**三条边界**：① **持有 30 日最好**（5 日内赎回费 1.5% 吃掉大半，费后 −0.09%）；② **状态依赖**，结构熊市（2010~2015 型）失效；③ **只提示位置不预测底**，不给金额、不定买卖（仓位归 `holding_advice.py`）。**不叠背离**——那已被证伪。数据纯读 `data/kline_full/*.csv`，与 D 区同源；**引用数字必须用宽基口径**（混合 26 条含行业口径 30 日 +2.47% 偏高，不适用宽基）。

## 14:30 六区验证状态（哪些能当真、哪些只能看）

**别把"区"当成"都能交易"**：六区里**只有 A 区是可交易依据**，其余全是观察/提示。2026-09-17 逐区重跑验证脚本核实（这份表就是结论）：

| 区 | 内容 | 独立验证结果 | 能否作交易依据 |
|---|---|---|---|
| A 持仓决策 | 分位共识 + 仓位灯 | 灯仓位回撤减半（-7.8 vs -17.0；指数级 -16 vs -28）；**但十万网格诚实结论：74.5% 概率跑输"什么都不做"、参数样本外相关 0.10** | ✅ 可交易，**是"减震器"控回撤/仓位上限，不是"发动机"** |
| B 短期行业选股 | 爆发力评分 Top5 | **未达标**：`validate_short.py` 独立回放线上 v2 超额 **−0.03%**（≈随机）；`reports/_过程/方案验证状态_20260904.md` 记"留存+0.61% 未达标" | ❌ **不可**（选股无超额） |
| B 短线挡位 | `model_short.json` 的 hold/tp/sl | 仅 bull 挡晋升（valid+3.48/留存+1.28，**留存只有 6 个信号日**）；bear/chop 否决用保守默认 | ⚠️ 只是"纪律参数"，**救不了选股** |
| C 抄底观察 | 庄家线 | **不晋升**：`validate_zhuang --nf` 三年窗口超额回撤 **−24.7%**；十万网格前 30 候选留存全负 | ❌ 只作观察 |
| D 恐慌监控 | 超跌反弹（指数） | 成立：28 轮、20 日 96% 赚钱中位 +3.62% | ⚠️ 只提示机会（见上节） |
| E 红利40日收益差 | 仓位尺 | 平均超额 +0.27%（可实盘含分红口径仅 +0.19%；单样本 +1.5% 被迭代修正） | ⚠️ 只提示档位 |
| F 宽基深跌区 | 半年线位置尺 | 36 轮、30 日 +1.84%、胜率 69% | ⚠️ 只提示位置（见上节） |

**教训（写给未来的维护者）**：**"挡位晋升"≠"选股有效"**——B 区的 `model_short.json` 晋升的是"持有多少天/止盈止损"，不是"买哪只"；把前者当成后者，就会误判一整个区可以交易。引用晋升状态**必须读 `reports/_过程/方案验证状态_*.md` 或重跑验证脚本，不能凭文档措辞推断**。

## 编码规范

- 纯标准库，不引入第三方依赖（archive/fetch_fund_estimation.py 是 akshare 旧版，已封存不启用）
- CSV 用 `utf-8-sig` 编码（兼容 Excel 打开）
- 交易日收盘后（15:30 后）运行数据更新最准确
- 数据来源仅供个人研究参考，不构成投资建议