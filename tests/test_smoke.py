# -*- coding: utf-8 -*-
"""冒烟测试：验证全仓脚本可导入且关键公共组件行为正确。

运行：python -m unittest discover -s tests -v
纯标准库。不触网、不写库（只读或独立临时库）。
"""
import importlib
import os
import sys
import tempfile
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestImports(unittest.TestCase):
    """所有活跃脚本必须可导入（import 期无副作用崩溃）。"""

    MODULES = [
        "src.common.paths", "src.common.history_db", "src.common.market_map",
        "src.common.fees", "src.common.technical_indicators", "src.common.kline_csv",
        "src.common.tickflow_source", "src.common.fetch_util", "src.common.fund_lookup",
        "src.jobs_build.daily_1430_scan", "research._engine",
        "src.jobs_fetch.update_data", "src.jobs_fetch.update_kline_chunk",
        "src.jobs_fetch.browser_fetch_klines", "src.jobs_fetch.backfill_flow",
        "src.jobs_fetch.backfill_kline_3y", "src.jobs_fetch.backfill_review_picks",
        "src.jobs_fetch.fetch_dragon_tiger", "src.jobs_fetch.fetch_etf_kline",
        "src.jobs_fetch.fetch_fund_nav", "src.jobs_fetch.fetch_global",
        "src.jobs_fetch.fetch_limit_up_down", "src.jobs_fetch.fetch_ohlcv_chunk",
        "src.jobs_fetch.fetch_sector_members", "src.jobs_fetch.fetch_sentiment",
        "src.jobs_fetch.fetch_social_sentiment",
        "src.jobs_fetch.fetch_csi_pe",
        "src.jobs_fetch.fetch_em_valuation",
        "src.jobs_fetch.fetch_stock_watch", "src.jobs_fetch.fund_realtime",
        "src.jobs_fetch.fetch_global_kline", "src.jobs_fetch.fetch_index_csv_kline",
        "src.jobs_fetch.fetch_sector_close", "src.jobs_fetch.fetch_etf_close",
        "src.jobs_fetch.import_global_indices", "src.jobs_fetch.import_investing_kline",
        "src.jobs_build.daily_1430", "src.jobs_build.daily_review",
        "src.jobs_build.fetch_index_valuation", "src.jobs_build.fetch_sector_valuation",
        "src.jobs_build.pe_percentile", "src.jobs_build.holding_advice",
        "src.jobs_build.generate_dashboard", "src.jobs_build.regime_lamp",
        "src.jobs_build.scan_regime", "src.jobs_build.t_trade_signal",
        "src.jobs_build.deepzone_monitor",
        "research.zhuang_line", "research.sweep_zhuang", "research.validate_zhuang",
        "research.train_recommend", "research.iterate", "research.pick_r2_momentum",
        "research.backtest_r2_momentum", "research.backtest_r2_index10y",
        "research.backtest_t_trade", "research.backtest_etf_macd",
        "research.sweep100k_r2", "research.sweep100k_etf_macd",
        "research.sweep100k_valuation",
        "research.sweep192k_r2", "research.sweep100k_short",
        "research.validate_short", "research.validate_lamp",
        "research.validate_social", "research.sweep10k_social",
        "research.validate_review", "research.sweep_review", "research.scan_factors",
        "research.validate_advice_rule", "research.rebound_line",
        "research.sweep100k_rebound",
        "research.divergence_line", "research.sweep100k_divergence",
        "src.jobs_fetch.fetch_dividend_index", "src.jobs_build.red40_monitor",
        "research.red40_spread", "research.sweep100k_red40", "research.redlv_line",
        "research.fed_hike_impact",
    ]

    def test_import_all(self):
        failures = []
        for m in self.MODULES:
            try:
                importlib.import_module(m)
            except Exception as e:  # noqa: BLE001
                failures.append("%s: %s: %s" % (m, type(e).__name__, e))
        self.assertEqual([], failures, "导入失败:\n" + "\n".join(failures))

    def test_no_third_party_httpx(self):
        """纯标准库约束：不得 import httpx。"""
        import src.common.tickflow_source as tf
        src = _read(tf.__file__)
        self.assertNotIn("import httpx", src)


class TestHistoryDb(unittest.TestCase):
    """history_db.connect() 是唯一 DB 入口，readonly 不得有写副作用。"""

    def test_connect_readonly_blocks_writes(self):
        import sqlite3
        from src.common import history_db
        conn = history_db.connect(readonly=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE __probe_must_fail__(x)")
        finally:
            conn.close()

    def test_connect_readonly_missing_db_raises(self):
        """库不存在时只读连接应清晰报错，而非静默空库。"""
        import sqlite3
        from src.common import history_db
        orig = history_db.DB_PATH
        history_db.DB_PATH = os.path.join(tempfile.gettempdir(), "__no_such_db__.db")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                history_db.connect(readonly=True)
        finally:
            history_db.DB_PATH = orig

    def test_normal_connect_creates_schema(self):
        import sqlite3
        from src.common import history_db
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(tmp)
        orig = history_db.DB_PATH
        history_db.DB_PATH = tmp
        try:
            conn = history_db.connect()
            n = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            conn.close()
            self.assertGreater(n, 15, "SCHEMA 未建出预期表数")
        finally:
            history_db.DB_PATH = orig
            if os.path.exists(tmp):
                os.unlink(tmp)

    def test_only_history_db_defines_schema(self):
        """DDL 唯一来源：全仓不应有第二处 sqlite3.connect（除 history_db）。"""
        bad = []
        for root, dirs, files in os.walk(_REPO):
            dirs[:] = [d for d in dirs if d not in (".git", "archive", "__pycache__",
                                                    "node_modules", "dist", ".playwright-mcp",
                                                    "tests")]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                # _tmp_* 是约定的一次性草稿（data/cache 下），不属生产写入者
                if fn.startswith("_tmp_"):
                    continue
                p = os.path.join(root, fn)
                if p.endswith(os.path.join("common", "history_db.py")):
                    continue
                txt = _read(p)
                if "sqlite3.connect(" in txt:
                    bad.append(os.path.relpath(p, _REPO))
        self.assertEqual([], bad, "存在绕过 history_db 的 raw sqlite3.connect: %s" % bad)


class TestIndicators(unittest.TestCase):
    """关键指标函数对已知输入给出可预期结果。"""

    def setUp(self):
        from src.common import technical_indicators as ti
        self.ti = ti

    def test_sma(self):
        out = self.ti.sma([1, 2, 3, 4, 5], 3)
        self.assertIsNone(out[1])
        self.assertEqual(2.0, out[2])   # (1+2+3)/3
        self.assertEqual(4.0, out[4])   # (3+4+5)/3

    def test_sma_short_series(self):
        out = self.ti.sma([1, 2], 5)
        self.assertEqual([None, None], out)

    def test_rsi_all_up_is_100(self):
        vals = list(range(1, 30))  # 单调上涨 => RSI 100
        out = self.ti.rsi(vals, 14)
        self.assertAlmostEqual(100.0, out[-1])

    def test_ema_constant_series(self):
        out = self.ti.ema([5.0] * 10, 3)
        self.assertAlmostEqual(5.0, out[-1])

    def test_macd_returns_three_series(self):
        vals = [float(i) for i in range(1, 60)]
        dif, dea, hist = self.ti.macd(vals)
        self.assertEqual(len(vals), len(dif))
        self.assertEqual(len(vals), len(hist))


class TestSector300dSingleWriter(unittest.TestCase):
    """sector_300d.csv 由 common.kline_csv 唯一产出，表头固定。"""

    def test_header_constant(self):
        from src.common.kline_csv import HEADER
        self.assertEqual(["代码", "名称", "5日%", "20日%", "60日%", "120日%",
                          "250日%", "300日%", "数据截止", "领涨股"], HEADER)

    def test_write_and_read_back(self):
        from src.common import kline_csv
        closes = [("2026-01-0%d" % i, float(100 + i)) for i in range(1, 9)]
        km = {"BK0001": closes, "BK0002": []}
        fd, tmp = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            n = kline_csv.write_sector_300d(km, {"BK0001": "测试A", "BK0002": "测试B"}, path=tmp)
            self.assertEqual(2, n)
            with open(tmp, encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
            self.assertEqual(",".join(kline_csv.HEADER), lines[0])
        finally:
            os.unlink(tmp)

    def test_no_second_writer(self):
        """两个 K 线脚本都不得再自行写 sector_300d 表头。"""
        for rel in ("src/jobs_fetch/browser_fetch_klines.py",
                    "src/jobs_fetch/update_kline_chunk.py"):
            txt = _read(os.path.join(_REPO, rel))
            self.assertNotIn('"数据截止", "领涨股"', txt, rel + " 仍内联表头")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPanicMonitor(unittest.TestCase):
    """全市场恐慌监控（D区）的渲染结构。不触网：只喂构造好的 pan 字典。

    口径依据 reports/超跌反弹十万次迭代_20260916.md：
    平静时只出一行结论、触发时展开成表格 + 历史依据。
    """

    @staticmethod
    def _item(name, r20, dd250=None):
        return {"name": name, "date": "2026-09-15", "ret20": r20, "ret60": -5.0,
                "dd250": dd250, "trigger_line": 100.0, "live": True}

    def test_flat_only_two_lines(self):
        from src.jobs_build.daily_1430_scan import panic_lines
        pan = {"level": "平静", "asof": "2026-09-15", "confirmed": True,
               "items": [self._item("A", -9.0, -12.0)], "hit": [], "near": []}
        out = panic_lines(pan)
        self.assertEqual(len(out), 2)
        self.assertFalse(any(ln.startswith("|") for ln in out))

    def test_near_adds_warning(self):
        from src.jobs_build.daily_1430_scan import panic_lines
        pan = {"level": "接近", "asof": "2026-09-15", "confirmed": True,
               "items": [self._item("A", -16.0)], "hit": [],
               "near": [self._item("A", -16.0)]}
        out = panic_lines(pan)
        self.assertEqual(len(out), 3)

    def test_trigger_renders_table_and_evidence(self):
        from src.jobs_build.daily_1430_scan import panic_lines
        hit = [self._item(n, r, -20.0) for n, r in
               (("A", -21.0), ("B", -23.0), ("C", -25.0))]
        pan = {"level": "触发", "asof": "2026-09-15", "confirmed": True,
               "items": hit, "hit": hit, "near": []}
        out = panic_lines(pan)
        rows = [ln for ln in out if ln.startswith("|")]
        self.assertEqual(len(rows), len(hit) + 2)   # 表头 + 分隔 + 每个命中一行
        self.assertTrue(any("---" in ln for ln in rows))
        # 历史依据必须随触发一起出现（否则用户只看到机会、看不到边界）
        self.assertTrue(any(x.startswith(">") for x in out))
        self.assertGreaterEqual(sum(1 for x in out if x.startswith(">")), 5)

    def test_trigger_caps_table_at_eight_rows(self):
        from src.jobs_build.daily_1430_scan import panic_lines
        hit = [self._item("I%02d" % i, -20.0 - i) for i in range(12)]
        pan = {"level": "触发", "asof": "2026-09-15", "confirmed": True,
               "items": hit, "hit": hit, "near": []}
        out = panic_lines(pan)
        rows = [ln for ln in out if ln.startswith("|")]
        self.assertEqual(len(rows), 8 + 2 + 1)      # 8 行 + 头/分隔 + "另 N 条"

    def test_missing_data_degrades(self):
        from src.jobs_build.daily_1430_scan import panic_lines
        out = panic_lines({"level": "平静", "asof": "", "confirmed": True,
                           "items": [], "hit": [], "near": []})
        self.assertEqual(len(out), 1)
