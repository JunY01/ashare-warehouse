# data/ 目录说明

这里放**运行产物**（行情快照、历史库、缓存、长历史 CSV）。除了样例，全部不入库（见根目录 `.gitignore`）——克隆下来是干净的，跑一次取数就会自己长出来。

## 目录与产物

| 路径 | 内容 | 谁写 |
|---|---|---|
| `market_history.db` | **历史库唯一真身**（25 表，date 主键幂等覆盖） | 各抓取脚本经 `src/common/history_db.py` 写入 |
| `market_history.sample.db` | **样例库**：25 表 + 最近约 20 个交易日的真实行情（~10MB） | 随仓库提供 |
| `sector_snapshot.csv` | 东财板块快照（496 个板块） | `jobs_fetch/update_data.py --snapshot-only` |
| `etf_snapshot.csv` | ETF 快照 | 同上 |
| `tencent_sector.csv` | 腾讯 31 个一级行业 | 同上 |
| `stock_quote.csv` / `watchlist` 相关 | 个股观察池行情 | `jobs_fetch/fetch_stock_watch.py` |
| `index_valuation.json` | 指数估值与分位（估值尺） | `jobs_build/fetch_index_valuation.py` |
| `sector_valuation.json` / `sector_valuation_map.json` | 蛋卷行业 PB/PE 分位与安全垫映射 | `jobs_build/fetch_sector_valuation.py` |
| `kline_10y/` `kline_full/` | 指数长历史日K CSV（外盘日历闸也读它） | `jobs_fetch/fetch_index_csv_kline.py` |
| `kline_etf/` | 策略宇宙 ETF 日K CSV | `jobs_fetch/fetch_etf_kline.py` |
| `cache/` | 抓取原始缓存与进度文件（K线原始 JSON、回填进度） | 各抓取脚本 |
| `samples/` | **样例 CSV**（表头 + 12 行），只为让人看懂列结构 | 随仓库提供 |

## 怎么开始

```bash
cp data/market_history.sample.db data/market_history.db   # 用样例库起步
python src/jobs_fetch/update_data.py --snapshot-only      # 抓当日快照并归档
python src/jobs_build/daily_1430.py --no-refresh          # 离线跑一次六区研判
```

样例库只含最近约 20 个交易日：够跑通全部流程与看板，不够做长历史回测——要回测就按 `README.md` §快速开始 里的脚本分别补齐（指数长历史走 `fetch_index_csv_kline.py`，板块日K走 `update_data.py --kline-only`）。

## 口径提醒（踩过的坑）

- **盘中运行记录的是盘中值**：快照类表（`sector_daily`/`etf_daily`/`industry_daily`/`sector_valuation`）按运行日归档，收盘后重跑一次才会覆盖为收盘口径；`archive_snapshots()` 会对盘中/周末归档打告警。
- **当日半截 bar 不写**：成交额、ETF K线、指数 CSV、外盘日K都有 15:05 收盘确认闸。
- **非交易日占位行不入库**：外盘日K有两道闸（零振幅、按代码挂交易日历）。
- **债券 ETF 会分红**：它的前复权序列可能整体重算（表现为"重叠日校验偏差 >0.5%"），遇到先想除息，别当脏数据删。
