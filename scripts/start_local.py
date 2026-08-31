#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网服务 —— 完全不依赖 GitHub，访问地址就是你本机的局域网 IP

用法：
    python scripts/start_local.py              # 直接起服务（秒开）
    python scripts/start_local.py --update     # 先更新数据再起服务
    python scripts/start_local.py --port 8080  # 指定端口
    python scripts/start_local.py --no-browser # 不开本地浏览器

启动后会打印形如 http://192.168.1.23:8000 的地址，
手机连同一个 WiFi 直接打开即可；也可以扫终端旁弹出的二维码。

要点：
    - 绑定 0.0.0.0，局域网内其他设备才能访问（127.0.0.1 只有本机能开）
    - 响应头强制 no-store，避免手机浏览器缓存旧数据
    - 端口被占用时自动向后顺延
"""

import argparse
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"


# ---------------------------------------------------------------- 网络
def get_lan_ips():
    """取本机所有局域网 IPv4（多网卡时会列出多个）"""
    ips, seen = [], set()

    # 借 UDP 连接拿默认出口 IP —— 不会真的发包，只是让内核选路由
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

    # 兜底：枚举主机名解析到的所有 IPv4
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
    """从 start 开始找第一个能绑定的端口"""
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


# ---------------------------------------------------------------- 服务
class NoCacheHandler(SimpleHTTPRequestHandler):
    """静态服务 + 禁用缓存（否则手机刷新看到的还是旧数据）"""

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        # 只打印页面/数据请求，忽略 favicon 之类的噪音
        path = self.path.split("?")[0]
        if path.endswith((".json", ".js", ".css", ".html")) or path == "/":
            print(f"  {datetime.now():%H:%M:%S}  {self.address_string()}  {path}",
                  flush=True)


# ---------------------------------------------------------------- 二维码
def make_qr(url):
    """生成二维码 PNG；缺依赖时优雅返回 None"""
    try:
        import qrcode
    except ImportError:
        return None
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        img = qrcode.make(url)
        p = LOG_DIR / "qrcode_lan.png"
        img.save(p)
        return p
    except Exception:
        return None


def open_file_crossplatform(p):
    try:
        if sys.platform.startswith("win"):
            import os
            os.startfile(str(p))          # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(p)], check=False)
        else:
            subprocess.run(["xdg-open", str(p)], check=False)
    except Exception:
        pass


# ---------------------------------------------------------------- 主流程
def run_update():
    print("\n>>> 更新数据（约 100-150 秒，可 Ctrl+C 跳过）...", flush=True)
    import subprocess
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "update_local.py")])
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser(description="李大霄价值雷达 · 局域网服务")
    ap.add_argument("--update", action="store_true", help="启动前先更新数据")
    ap.add_argument("--port", type=int, default=8000, help="起始端口（默认 8000）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    if args.update:
        if not run_update():
            print("\n[WARN] 数据更新未成功，将使用现有数据启动服务。")

    port = pick_port(args.port)
    if port is None:
        print(f"[FATAL] 端口 {args.port} ~ {args.port + 11} 均被占用，用 --port 换一个。")
        return 1
    if port != args.port:
        print(f"[INFO] 端口 {args.port} 被占用，改用 {port}")

    handler = partial(NoCacheHandler, directory=str(ROOT))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    httpd.daemon_threads = True

    ips = get_lan_ips()
    local = f"http://127.0.0.1:{port}/"
    lan_urls = [f"http://{ip}:{port}/" for ip in ips]

    print("\n" + "=" * 60)
    print("  李大霄价值雷达 · 局域网服务已启动")
    print("=" * 60)
    print(f"\n  本机访问      {local}")
    if lan_urls:
        print(f"\n  手机 / 其他设备访问（需连同一个 WiFi）：")
        for u in lan_urls:
            print(f"      {u}")
    else:
        print("\n  [WARN] 未能识别局域网 IP，请检查网络连接。")
        print("        Windows 上可用 ipconfig 查看「IPv4 地址」。")

    if lan_urls:
        qr = make_qr(lan_urls[0])
        if qr:
            print(f"\n  二维码已生成：{qr}")
            print("  （已尝试自动打开，手机扫一下即可访问）")
            threading.Timer(0.6, lambda: open_file_crossplatform(qr)).start()

    print("\n  数据目录 " + str(ROOT / "data" / "data.json"))
    print("\n  按 Ctrl+C 停止服务")
    print("=" * 60 + "\n")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(local)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止服务。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
