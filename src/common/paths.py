# -*- coding: utf-8 -*-
"""统一寻址垫底（B方案第一步，只做垫底不搬迁）。

纯标准库。所有脚本假设代码与数据同目录（DATA_DIR=dirname(__file__)），
本模块提供仓库级绝对路径，data/ 下脚本优先从这里导入，导入失败时
回退本地 DATA_DIR 计算，保证旧运行方式不断。

目录约定：
  REPO_ROOT   仓库根（本文件向上三级）
  DATA_ROOT   数据目录（data/，兼容旧 DATA_DIR）
  CONFIG_ROOT 配置目录（config/，后续搬迁用）
  REPORT_ROOT 报告目录（reports/）
  DB_PATH     历史库（data/market_history.db）
"""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_ROOT = os.path.join(REPO_ROOT, "data")
CONFIG_ROOT = os.path.join(REPO_ROOT, "config")
REPORT_ROOT = os.path.join(REPO_ROOT, "reports")
DB_PATH = os.path.join(DATA_ROOT, "market_history.db")
# research 网格/面板中间产物目录（可再生，git 忽略；原散落在 research/ 根目录）
SWEEP_CACHE = os.path.join(DATA_ROOT, "cache", "sweeps")

# 兼容旧 DATA_DIR 导入（DATA_DIR=DATA_ROOT）
DATA_DIR = DATA_ROOT
