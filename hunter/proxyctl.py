"""跟 tools/rotating-proxy.py 的固定口说话：看当前出站 IP、被 541 之后换一个。

541 是 Akamai 按「出口 IP + 端点」记的，同一个 IP 再回去还是 541，所以原来
被拦只能静默 120/240/300 秒干等。买手的 Chrome 走固定口时，被拦就让固定口切一个
干净 IP、掐断旧隧道，买手立刻回主站重建会话——静默期变成几秒。
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from urllib.parse import unquote, urlparse


class ProxyControlError(RuntimeError):
    pass


class ProxyControl:
    """`autobuy.proxy` 那个地址上的 GET /ip 和 GET /rotate。

    地址里可以带用户名密码（`http://user:pass@host:8081`）：Chrome 的 PAC 用不上它们，
    这里用来过固定口的 Basic 鉴权；没带就得靠代理那边 --allow 放行本机。
    """

    def __init__(self, url: str, timeout: float = 5.0):
        u = urlparse(url if "://" in url else "http://" + url)
        if not u.hostname or not u.port:
            raise ValueError(f"autobuy.proxy 要写成 http://IP:端口：{url!r}")
        self.base = f"http://{u.hostname}:{u.port}"
        self.timeout = float(timeout)
        self._auth = ""
        if u.username:
            # 地址里的密码是 URL 编码的（@ 写成 %40），发 Basic 之前要解回来
            self._auth = "Basic " + base64.b64encode(
                f"{unquote(u.username)}:{unquote(u.password or '')}".encode()).decode()

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(self.base + path, method="GET")
        if self._auth:
            req.add_header("Authorization", self._auth)
        try:
            # 直连代理主机本身，别让本机的代理环境变量把这一发绕出去
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            raise ProxyControlError(f"{path} 返回 {e.code}"
                                    + ("（要鉴权：地址里带 user:pass，或代理那边 --allow 本机）"
                                       if e.code == 407 else "")) from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ProxyControlError(f"{path} 失败：{type(e).__name__}: {e}") from e
        if not isinstance(data, dict) or not data.get("ip"):
            raise ProxyControlError(f"{path} 回的不是固定口的应答：{str(data)[:80]}")
        return data

    def current(self) -> dict:
        return self._get("/ip")

    def rotate(self) -> dict:
        return self._get("/rotate")
