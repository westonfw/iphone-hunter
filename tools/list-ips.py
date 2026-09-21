#!/usr/bin/env python3
"""枚举本机所有公网 IPv4，逐个验能不能从它出网到 Apple，生成 rotating-proxy 的 IP 池。

一台挂了很多附加 IP 的服务器上，手工列 + 逐个 curl 验太慢。这个脚本：

  1. 枚举本机所有 **global scope** 的 IPv4（自动排除 127.0.0.1 回环、169.254 链路本地）。
  2. **绑每个 IP 当源地址**去连 www.apple.com.cn:443，连得上才算这个 IP 真能出网、
     回程也能路由回来（附加 IP 常有「挂着但出不去」的，绑源一试就现原形）。
  3. 并发测（默认 100 路），200 个 IP 几秒测完。

用法
----
    python3 tools/list-ips.py                 # 枚举 + 验证，能用的打到屏幕
    python3 tools/list-ips.py -o ips.txt      # 直接写进 ips.txt（只写通过的）
    python3 tools/list-ips.py --no-check       # 只枚举、不验证（全列出来）
    python3 tools/list-ips.py --host www.apple.com.cn --port 443 --workers 100

坏 IP 会打到 stderr 说明为什么没通过，方便排查（路由没配、rp_filter 挡了等）。
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

DEFAULT_HOST = "www.apple.com.cn"
DEFAULT_PORT = 443
CONNECT_TIMEOUT = 6.0


def enumerate_ips() -> list[str]:
    """本机所有 global scope 的 IPv4，保序去重。

    用 `ip -4 -o addr show scope global`：scope global 天然把回环（scope host）
    和链路本地（scope link，169.254）排除掉，剩下的才是能对外的地址。
    """
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=10, check=True).stdout
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"跑 `ip addr` 失败：{type(e).__name__}: {e}\n"
              "这脚本要 iproute2（Ubuntu 自带）。", file=sys.stderr)
        raise SystemExit(2)

    ips: list[str] = []
    seen: set[str] = set()
    for line in out.splitlines():
        # 每行形如：  2: eth0    inet 203.0.113.11/24 brd ... scope global eth0
        parts = line.split()
        if "inet" not in parts:
            continue
        cidr = parts[parts.index("inet") + 1]
        ip = cidr.split("/", 1)[0]
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        if ip not in seen:
            seen.add(ip)
            ips.append(ip)
    return ips


def probe(ip: str, dst: str, port: int) -> tuple[str, bool, str]:
    """绑 ip 当源地址连 dst:port。返回 (ip, 能不能出网, 说明)。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(CONNECT_TIMEOUT)
        s.bind((ip, 0))
        s.connect((dst, port))
        s.close()
        return ip, True, ""
    except OSError as e:
        return ip, False, f"{type(e).__name__}: {e}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="枚举本机 IPv4 并验能否出网到 Apple")
    ap.add_argument("-o", "--out", default="",
                    help="把通过的 IP 写进这个文件（默认只打到屏幕）")
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"验证目标，默认 {DEFAULT_HOST}")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="默认 443")
    ap.add_argument("--workers", type=int, default=100, help="并发验证路数，默认 100")
    ap.add_argument("--no-check", action="store_true",
                    help="只枚举、不验证，全部列出来")
    args = ap.parse_args(argv)

    ips = enumerate_ips()
    if not ips:
        print("没枚举到任何 global scope 的 IPv4——这台机器上到底挂了 IP 没？",
              file=sys.stderr)
        return 1
    print(f"枚举到 {len(ips)} 个 IPv4", file=sys.stderr)

    if args.no_check:
        good = ips
    else:
        # 目标先解析成一个 IP，别让每路验证各做一次 DNS（DNS 走默认路由即可）
        try:
            dst = socket.gethostbyname(args.host)
        except OSError as e:
            print(f"解析 {args.host} 失败：{e}", file=sys.stderr)
            return 2
        print(f"绑每个 IP 连 {args.host}({dst}):{args.port} 验出网……", file=sys.stderr)
        good, bad = [], []
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            for ip, ok, why in pool.map(lambda x: probe(x, dst, args.port), ips):
                (good if ok else bad).append((ip, why))
        good = [ip for ip, _ in good]
        for ip, why in bad:
            print(f"  ✗ {ip}  {why}", file=sys.stderr)
        print(f"能出网 {len(good)} / {len(ips)}", file=sys.stderr)

    if not good:
        print("一个能出网的 IP 都没有——检查路由 / rp_filter（跨子网设成 2 或 0）。",
              file=sys.stderr)
        return 1

    body = "\n".join(good) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"已写入 {args.out}（{len(good)} 个）", file=sys.stderr)
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
