#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
互联网服务 —— 本机跑数据，手机在外网（4G / 公司 WiFi）也能看，完全不依赖 GitHub

原理：
    本机起一个静态 HTTP 服务（把你电脑变成一台小服务器），
    再用「内网穿透」把这个服务映射到一个公网地址（如 https://xxxx.cpolar.cn）。
    手机扫这个公网地址的二维码，走到哪都能看，跟 GitHub 没关系。

用法：
    python scripts/start_net.py                 # 自动探测已装的穿透工具
    python scripts/start_net.py --tunnel cpolar # 指定用 cpolar
    python scripts/start_net.py --update        # 启动前先更新数据
    python scripts/start_net.py --port 8000     # 指定本地端口
    python scripts/start_net.py --no-browser    # 不开本地浏览器

支持的穿透（任选其一，脚本按优先级自动探测；--tunnel 可强制）：
    cpolar      国产，微信扫码登录，免费版有随机域名（每天变）+ 限速；
                Windows 装好并 cpolar authtoken 登录后开箱即用。最推荐。
    ngrok       国际工具，免费版随机域名（每次变），需免费注册拿 authtoken。
    cloudflared 零账号免登录（quick tunnel），随机域名（每次变），最省事但需装二进制。

注意：
    - 免费穿透域名通常「每次/每天变」，所以每次启动后扫新的二维码最稳。
      想要固定域名需穿透工具的付费版（cpolar 穿透固定为付费项）。
    - 本机电脑需保持开机、此脚本保持运行，外网才能访问。
    - 端口被占用时自动向后顺延。
"""

import argparse
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"


# ---------------------------------------------------------------- 网络 / 服务
def get_lan_ips():
    ips, seen = [], set()
    for probe in ("223.5.5.5", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127.") and ip not in seen:
                ips.append(ip)
                seen.add(ip)
            break
        except Exception:
            continue
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in seen:
                ips.append(ip)
                seen.add(ip)
    except Exception:
        pass
    return ips


def pick_port(start, tries=12):
    for p in range(start, start + tries):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", p))
            s.close()
            return p
        except OSError:
            continue
    return None


class NoCacheHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        path = self.path.split("?")[0]
        if path.endswith((".json", ".js", ".css", ".html")) or path == "/":
            print(f"  {datetime.now():%H:%M:%S}  {self.address_string()}  {path}",
                  flush=True)


# ---------------------------------------------------------------- 二维码
def make_qr(url, name="qrcode_net.png"):
    try:
        import qrcode
    except ImportError:
        return None
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        img = qrcode.make(url)
        p = LOG_DIR / name
        img.save(p)
        return p
    except Exception:
        return None


def open_file_crossplatform(p):
    import os
    import subprocess as _sp
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))                                  # noqa: S606
        elif sys.platform == "darwin":
            _sp.run(["open", str(p)], check=False)
        else:
            _sp.run(["xdg-open", str(p)], check=False)
    except Exception:
        pass


# ---------------------------------------------------------------- 穿透后端
# 每个后端：exe 名称、启动参数模板、获取公网地址的方式
#  - api：HTTP API 轮询（cpolar / ngrok）
#  - stdout：解析进程标准输出（cloudflared）
TUNNELS = {
    "cpolar": {
        "exe": "cpolar",
        "args": ["http", "{port}"],
        "api": "http://127.0.0.1:9200/api/v1/tunnels",
        "kind": "api",
        "note": "国产，微信扫码登录；免费版随机域名(每天变)+限速。",
    },
    "ngrok": {
        "exe": "ngrok",
        "args": ["http", "{port}"],
        "api": "http://127.0.0.1:4040/api/tunnels",
        "kind": "api",
        "note": "国际工具；免费注册拿 authtoken：ngrok config add-authtoken xxxx。",
    },
    "cloudflared": {
        "exe": "cloudflared",
        "args": ["tunnel", "--url", "http://localhost:{port}"],
        "kind": "stdout",
        "pattern": "https://",
        "note": "零账号免登录 quick tunnel，随机域名(每次变)，最省事。",
    },
}
DEFAULT_ORDER = ["cpolar", "ngrok", "cloudflared"]


def find_exe(name):
    return shutil.which(name)


def choose_backend(requested):
    if requested and requested not in TUNNELS:
        print(f"[FATAL] 未知穿透工具：{requested}（可选：{', '.join(TUNNELS)}）")
        return None
    if requested:
        if not find_exe(TUNNELS[requested]["exe"]):
            print(f"[FATAL] 未找到 {TUNNELS[requested]['exe']} 命令，请先安装。")
            return None
        return requested
    for name in DEFAULT_ORDER:
        if find_exe(TUNNELS[name]["exe"]):
            return name
    return None


def launch_tunnel(backend, port):
    cfg = TUNNELS[backend]
    args = [cfg["exe"]] + [a.format(port=port) for a in cfg["args"]]
    print(f"\n>>> 启动穿透：{' '.join(args)}", flush=True)
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    # 后台把穿透日志转印出来，方便排错
    threading.Thread(target=_pipe_log, args=(proc, backend), daemon=True).start()
    api = cfg.get("api")
    return proc, api


def _pipe_log(proc, backend):
    try:
        for line in proc.stdout:
            if backend == "cloudflared":
                print(f"    [cfl] {line.rstrip()}", flush=True)
            else:
                low = line.lower()
                if "error" in low or "fail" in low or "url" in low:
                    print(f"    [tun] {line.rstrip()}", flush=True)
    except Exception:
        pass


def _api_public_url(api_url, timeout=1):
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "ldx-radar"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        for t in data.get("tunnels", []):
            u = t.get("public_url") or t.get("publicUrl")
            if u and u.startswith("http"):
                return u
    except Exception:
        return None
    return None


def wait_for_url(backend, proc, api_url, timeout=30):
    cfg = TUNNELS[backend]
    deadline = time.time() + timeout
    # 先确认进程还活着
    if proc.poll() is not None:
        print("    [FATAL] 穿透进程已退出，请检查安装 / 登录状态。")
        return None

    if cfg["kind"] == "stdout":
        # cloudflared：从 stdout 抓 https://xxx.trycloudflare.com
        import re
        seen = ""
        while time.time() < deadline:
            if proc.poll() is not None:
                return None
            try:
                line = proc.stdout.readline()
            except Exception:
                line = ""
            if line:
                seen += line
                m = re.search(r"(https://[a-z0-9\-]+\.trycloudflare\.com)", line)
                if m:
                    return m.group(1)
        return None

    # cpolar / ngrok：轮询本地 API
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        u = _api_public_url(api_url)
        if u:
            return u
        time.sleep(1)
    return None


# ---------------------------------------------------------------- 主流程
def run_update():
    print("\n>>> 更新数据（约 100-150 秒，可 Ctrl+C 跳过）...", flush=True)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "update_local.py")])
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser(description="李大霄价值雷达 · 互联网服务（内网穿透）")
    ap.add_argument("--tunnel", choices=list(TUNNELS.keys()),
                    help="指定穿透工具（默认自动探测已安装的）")
    ap.add_argument("--update", action="store_true", help="启动前先更新数据")
    ap.add_argument("--port", type=int, default=8000, help="本地起始端口（默认 8000）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    if args.update:
        if not run_update():
            print("\n[WARN] 数据更新未成功，将使用现有数据启动服务。")

    backend = choose_backend(args.tunnel)
    if not backend:
        print("\n未检测到任何穿透工具。三选一安装后重跑本脚本：\n")
        print("  [1] cpolar（推荐，国产微信登录）")
        print("      官网 https://www.cpolar.com/ 下载 Windows 版 → 安装 → 微信扫码登录")
        print("      cpolar authtoken <你的token>   # 在官网控制台拿到")
        print("      cpolar http 8000               # 手动验证能出地址即可")
        print("\n  [2] ngrok（国际）")
        print("      官网 https://ngrok.com/ 注册 → 拿 authtoken")
        print("      ngrok config add-authtoken <token>   # 写入配置")
        print("\n  [3] cloudflared（零账号，最省事）")
        print("      官网 https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/")
        print("      下载 cloudflared-windows-amd64.exe 放到 PATH\n")
        print("装好后重新运行：python scripts/start_net.py")
        return 1

    print(f"\n>>> 使用穿透后端：{backend} —— {TUNNELS[backend]['note']}")

    port = pick_port(args.port)
    if port is None:
        print(f"[FATAL] 端口 {args.port} ~ {args.port + 11} 均被占用，用 --port 换一个。")
        return 1
    if port != args.port:
        print(f"[INFO] 端口 {args.port} 被占用，改用 {port}")

    handler = partial(NoCacheHandler, directory=str(ROOT))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    httpd.daemon_threads = True

    proc, api = launch_tunnel(backend, port)
    public_url = wait_for_url(backend, proc, api, timeout=30)

    ips = get_lan_ips()
    local = f"http://127.0.0.1:{port}/"
    lan_urls = [f"http://{ip}:{port}/" for ip in ips]

    print("\n" + "=" * 62)
    print("  李大霄价值雷达 · 互联网服务已启动")
    print("=" * 62)

    if public_url:
        print(f"\n  ✅ 手机 / 任意外网访问（重点看这个）：\n        {public_url}\n")
        qr = make_qr(public_url, "qrcode_net.png")
        if qr:
            print(f"  二维码已生成：{qr}（已尝试自动打开，手机扫一下即可）")
            threading.Timer(0.6, lambda: open_file_crossplatform(qr)).start()
        print("  ⚠ 免费穿透域名通常每次/每天变化，换设备看就重扫本二维码。")
    else:
        print("\n  [WARN] 没拿到公网地址。请确认穿透工具已登录/已配置。")
        print("        本机仍可访问（见下），也可改用 --tunnel 换一个工具。")

    print(f"\n  本机访问      {local}")
    if lan_urls:
        print(f"  局域网访问    {lan_urls[0]}（同 WiFi 可用，作备份）")

    print(f"\n  数据目录 {ROOT / 'data' / 'data.json'}")
    print("\n  保持本窗口运行；按 Ctrl+C 停止服务并关闭穿透。")
    print("=" * 62 + "\n")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(public_url or local)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止...")
    finally:
        httpd.server_close()
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        print("已停止服务与穿透。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
