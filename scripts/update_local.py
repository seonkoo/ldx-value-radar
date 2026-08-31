#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地数据更新 —— 纯本地模式，不碰 git、不需要 PAT

流程：
    1. 构建数据   scripts/build_data.py
    2. 校验产物   条目数 / 择时信号是否正常

与 update_and_push.py 的区别：本脚本做完校验就结束，不做任何 git 操作。
适合只在本机 / 局域网使用的场景 —— PAT、cmdkey、remote 全部不需要。

日志按天滚动到 logs/daily_YYYYMMDD.log，保留 30 天。
"""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
DATA_JSON = ROOT / "data" / "data.json"

MIN_STOCKS = 50          # 低于此条目数判定为抓取异常
LOG_KEEP_DAYS = 30

_log_file = None


def log(msg=""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}" if msg else ""
    print(line, flush=True)
    if _log_file:
        try:
            with open(_log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def setup_logging():
    global _log_file
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log_file = LOG_DIR / f"daily_{datetime.now():%Y%m%d}.log"
    for f in LOG_DIR.glob("daily_*.log"):
        try:
            age = (datetime.now() - datetime.fromtimestamp(f.stat().st_mtime)).days
            if age > LOG_KEEP_DAYS:
                f.unlink()
        except Exception:
            pass


def run(cmd):
    try:
        r = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except FileNotFoundError:
        return -1, f"命令不存在: {cmd[0]}"
    except Exception as e:
        return -1, str(e)


def build():
    log("[1/2] 构建数据（约 100-150 秒）...")
    code, out = run([sys.executable, str(ROOT / "scripts" / "build_data.py")])
    if out:
        for l in [x for x in out.splitlines() if x.strip()][-15:]:
            log("      " + l)
    if code != 0:
        log(f"[FATAL] 数据构建失败 (exit={code})")
        return False
    return True


def verify():
    log("[2/2] 校验产物...")
    try:
        d = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[FATAL] 产物解析失败: {e}")
        return False

    n = len(d.get("stocks", []))
    log(f"      股票 {n} 只 | 深度分析 {d.get('deep', {}).get('count', 0)} 只")
    if n < MIN_STOCKS:
        log(f"[FATAL] 条目数 {n} < {MIN_STOCKS}，判定抓取异常")
        return False

    v = d.get("timing", {}).get("valuation", {})
    if v.get("available"):
        log(f"      大盘 PE={v.get('pe')} 分位={v.get('percentile')}%"
            f"{'  [已降级推算]' if v.get('degraded') else ''}")
    else:
        log("      [WARN] 大盘估值不可用，择时将显示为「数据不足」")
    log(f"      耗时 {d.get('meta', {}).get('cost_sec')}s")
    return True


def main():
    setup_logging()
    t0 = time.time()
    log("=" * 56)
    log("李大霄价值雷达 · 本地数据更新启动（纯本地模式）")

    if not build():
        return 1
    if not verify():
        return 1

    log(f"完成，耗时 {time.time() - t0:.1f}s")
    log("提示：数据已就绪。若页面正开着，刷新浏览器即可看到最新数据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
