#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地每日更新 —— 供 Windows 任务计划调用（不依赖 GitHub Actions）

流程：
    1. 构建数据   scripts/build_data.py
    2. 校验产物   条目数 / 择时信号是否正常
    3. git 提交
    4. git 推送   GitHub Pages 只做静态托管，不再跑任何计算

设计要点：
    - 逻辑放在 Python 而非 .bat，便于跨平台测试与排查
    - 日志按天滚动到 logs/daily_YYYYMMDD.log，保留 30 天
    - 任一环节失败立即中止，绝不推送残缺数据
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
VAL_CACHE = ROOT / "data" / "valuation_cache.json"

MIN_STOCKS = 50          # 低于此条目数判定为抓取异常，拒绝推送
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
    """按天滚动日志，并清理过期文件"""
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
    """在项目根目录执行命令，返回 (returncode, output)"""
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
    log("[1/4] 构建数据（约 100-150 秒）...")
    code, out = run([sys.executable, str(ROOT / "scripts" / "build_data.py")])
    # 构建脚本自带进度条，只回显关键尾部
    if out:
        tail = [l for l in out.splitlines() if l.strip()][-15:]
        for l in tail:
            log("      " + l)
    if code != 0:
        log(f"[FATAL] 数据构建失败 (exit={code})")
        return False
    return True


def verify():
    log("[2/4] 校验产物...")
    try:
        d = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[FATAL] 产物解析失败: {e}")
        return False

    n = len(d.get("stocks", []))
    deep = d.get("deep", {})
    log(f"      股票 {n} 只 | 深度分析 {deep.get('count', 0)} 只")
    if n < MIN_STOCKS:
        log(f"[FATAL] 条目数 {n} < {MIN_STOCKS}，判定抓取异常，拒绝推送")
        return False

    v = d.get("timing", {}).get("valuation", {})
    if v.get("available"):
        log(f"      大盘 PE={v.get('pe')} 分位={v.get('percentile')}%"
            f"{'  [已降级推算]' if v.get('degraded') else ''}")
    else:
        log("      [WARN] 大盘估值不可用，择时将显示为「数据不足」")
    log(f"      耗时 {d.get('meta', {}).get('cost_sec')}s")
    return True


def commit_and_push():
    log("[3/4] 提交到 git...")
    code, out = run(["git", "add", str(DATA_JSON), str(VAL_CACHE)])
    if code != 0:
        log(f"[FATAL] git add 失败: {out}")
        return False

    code, _ = run(["git", "diff", "--cached", "--quiet"])
    if code == 0:
        log("      数据无变化，跳过提交（非节假日需注意是否停牌）")
        return True

    msg = f"chore(data): 更新蓝筹价值数据 {datetime.now():%Y-%m-%d %H:%M}"
    # 确保中文 commit message 编码正确
    run(["git", "config", "i18n.commitEncoding", "utf-8"])
    code, out = run(["git", "commit", "-m", msg])
    if code != 0:
        log(f"[FATAL] git commit 失败: {out}")
        return False
    log(f"      已提交: {msg}")

    log("[4/4] 推送到 GitHub Pages...")
    code, out = run(["git", "push"])
    if code != 0:
        log(f"[FATAL] git push 失败: {out}")
        log("      若提示认证失败，先在 CMD 里执行一次（PAT 不落盘到脚本）：")
        log('      cmdkey /generic:git:https://github.com /user:<你的GitHub用户名> /pass:<PAT>')
        return False
    return True


def main():
    setup_logging()
    t0 = time.time()
    log("=" * 56)
    log("李大霄价值雷达 · 本地每日更新启动")

    if not build():
        return 1
    if not verify():
        return 1
    if not commit_and_push():
        return 1

    log(f"完成，耗时 {time.time() - t0:.1f}s；GitHub Pages 通常 1-2 分钟内生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
