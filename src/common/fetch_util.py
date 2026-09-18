# -*- coding: utf-8 -*-
"""抓取层公共原语（唯一来源，替代各抓取脚本里的私有副本）。

历史上 _num/_wan/fetch_json/drop_incomplete 在 6+ 个 jobs_fetch 脚本里各写一份，
重试次数/超时/传输方式（curl vs urllib）互不一致。统一到此，各脚本只引用不重定义。

纯标准库。
"""
import datetime
import json
import subprocess
import time

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"


def curl_json(url, timeout=15, referer=None, tries=3, ua=UA):
    """用系统 curl 拉取并解析 JSON（失败重试 tries 次，每次间隔 1s）。

    timeout 为 curl 的 -m 上限；子进程超时取 timeout+10。
    """
    cmd = ["curl", "-s", "-m", str(timeout), "-A", ua]
    if referer:
        cmd += ["-H", "Referer: " + referer]
    for _ in range(tries):
        r = subprocess.run(cmd + [url], capture_output=True, timeout=timeout + 10)
        if r.returncode == 0 and r.stdout:
            txt = r.stdout.decode("utf-8", errors="ignore")
            if txt.strip():
                try:
                    return json.loads(txt)
                except json.JSONDecodeError:
                    pass
        time.sleep(1)
    return None


def urllib_json(url, headers=None, timeout=12, tries=4, sleep_s=2):
    """用 urllib 拉取并解析 JSON（失败重试 tries 次）。

    headers 为额外请求头；解码先试 utf-8 再退 gbk（东财部分接口返回 GBK）。
    """
    import urllib.request
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            try:
                return json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError:
                return json.loads(raw.decode("gbk"))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(sleep_s)
    return None


def num(v):
    """宽松转 float；空/非法返回 None。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def wan(v):
    """元 → 万元；空值透传 None。"""
    n = num(v)
    return None if n is None else round(n / 1e4, 2)


def yi(v):
    """元 → 亿元；空值透传 None。"""
    n = num(v)
    return None if n is None else round(n / 1e8, 2)


def drop_incomplete(rows, now=None):
    """当日 bar 在 15:05 收盘确认前视为未完成，丢弃以免脏值入库。

    rows 为 [(date_iso, ...), ...] 升序；now 可注入以便测试。
    """
    now = now or datetime.datetime.now()
    if rows and rows[-1][0] == now.date().isoformat() and now.time() < datetime.time(15, 5):
        return rows[:-1]
    return rows
