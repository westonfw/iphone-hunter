"""通知渠道。

抢首发的关键是"被叫醒"，所以默认同时走多路：手机推送 + 桌面 + 终端响铃。
每个渠道都不允许把主循环搞崩，异常一律吞掉并打日志。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from urllib.parse import urlparse

import requests

from .pacing import in_window, parse_window

TIMEOUT = 8


def store_app_url(url: str) -> str:
    """官网购买页换成 Apple Store App 的 URL Scheme，点开直接进 App，不经过 Safari。

    App 注册了 applestore:// 。https://store.apple.com/... 是 Universal Link，
    iOS 仍可能先落到 Safari 再跳 App。换成 scheme 后系统直接交给 App。
    路径沿用 App 分享格式 /<区>/xc/product/<part>；part 含颜色和容量。
    """
    if not url:
        return url
    p = urlparse(url)
    host = (p.netloc or "").lower()
    if "apple.com" not in host and p.scheme != "applestore":
        return url
    segs = [s for s in p.path.split("/") if s]
    part = _part_from_path(segs)
    if not part:
        return url
    if host.endswith("apple.com.cn"):
        region = "cn"
    elif segs and segs[0] not in ("shop", "xc", "buy-iphone", "product"):
        region = segs[0]
    else:
        region = "cn" if p.scheme == "applestore" else ""
    path = f"/{region}/xc/product/{part}" if region else f"/xc/product/{part}"
    return f"applestore://store.apple.com{path}"


def _part_from_path(segs: list[str]) -> str:
    if "product" in segs:
        i = segs.index("product")
        if len(segs) > i + 1:
            return "/".join(segs[i + 1:])
    if "buy-iphone" in segs:
        i = segs.index("buy-iphone")
        if len(segs) > i + 2:
            return "/".join(segs[i + 2:])
    return ""


def _p(*a) -> None:
    print(*a, flush=True)


class QuietHours:
    """睡觉时段：这段时间里只放行**需要你动手**的消息，其余一律只记终端。

    只有「订单已创建、等着你扫码付款」这一条会把人叫醒（`wake=True`）。命中、
    下单进度、失败重试这些半夜发出来也做不了什么——一旦被吵醒几次，人就会
    直接把通知整个关掉，真该付款时反而没人应。

    时段格式跟 `pacing.hot_windows` 一样，支持跨零点（`23:30-08:00`）。
    时段外完全不生效，该怎么推还怎么推。
    """

    def __init__(self, windows=None, enabled: bool = True, calendar=datetime.now):
        self.windows = list(windows or [])
        # 没有时段 = 没开静音。别让一个空 windows 的 enabled:true 把全天都静音了
        self.enabled = bool(enabled) and bool(self.windows)
        self.calendar = calendar

    @classmethod
    def from_config(cls, cfg, log=_p) -> "QuietHours":
        """从 config.json 的 `quiet_hours` 装一个出来。解析不了的时段跳过并告警。"""
        cfg = cfg if isinstance(cfg, dict) else {}
        raw = cfg.get("windows") or []
        if isinstance(raw, str):      # 只配一段时写成字符串也认
            raw = [raw]
        windows = []
        for text in raw:
            try:
                windows.append(parse_window(str(text)))
            except ValueError as e:
                log(f"[通知] 忽略无法解析的睡觉时段：{e}")
        # 没写 enabled 视为开：手动填了 windows 就是想让它生效
        return cls(windows, enabled=cfg.get("enabled", True))

    def muted(self) -> bool:
        if not self.enabled:
            return False
        t = self.calendar()
        return any(in_window(t.hour * 60 + t.minute, w) for w in self.windows)

    def describe(self) -> str:
        return "、".join(f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}"
                        for a, b in self.windows)


class Notifier:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def send(self, title: str, body: str, url: str = "", critical: bool = False) -> None:
        raise NotImplementedError


class Bark(Notifier):
    """iOS 上最好用的一路：可以强制响铃，点通知直接打开购买页。

    key 从 Bark App 首页复制，形如 https://api.day.app/xxxxxxxx/ 里的 xxxxxxxx。
    购买链接改成 applestore://store.apple.com/cn/xc/product/<part>，
    点通知直接进 Apple Store App，不经过 Safari。
    part 已包含颜色和容量；折抵、AppleCare、门店没有公开深链参数。
    """

    name = "bark"

    def send(self, title, body, url="", critical=False):
        server = (self.cfg.get("server") or "https://api.day.app").rstrip("/")
        key = self.cfg["key"]
        payload = {
            "title": title,
            "body": body,
            "group": self.cfg.get("group", "iPhone"),
            "sound": self.cfg.get("sound", "alarm"),
            "isArchive": 1,
        }
        if url:
            payload["url"] = store_app_url(url)
        if critical:
            # level=critical 会无视静音和专注模式，volume 最大 10
            payload["level"] = "critical"
            payload["volume"] = int(self.cfg.get("volume", 10))
            payload["call"] = 1  # 持续响铃直到手动关掉
        r = requests.post(f"{server}/{key}", json=payload, timeout=TIMEOUT)
        r.raise_for_status()


class ServerChan(Notifier):
    """Server酱（微信推送）。key 是 SCT 开头的 sendkey。"""

    name = "serverchan"

    def send(self, title, body, url="", critical=False):
        desp = body + (f"\n\n[立即购买]({url})" if url else "")
        r = requests.post(
            f"https://sctapi.ftqq.com/{self.cfg['key']}.send",
            data={"title": title, "desp": desp},
            timeout=TIMEOUT,
        )
        r.raise_for_status()


class Telegram(Notifier):
    name = "telegram"

    def send(self, title, body, url="", critical=False):
        text = f"*{title}*\n{body}"
        if url:
            text += f"\n{url}"
        r = requests.post(
            f"https://api.telegram.org/bot{self.cfg['token']}/sendMessage",
            json={
                "chat_id": self.cfg["chat_id"],
                "text": text,
                "parse_mode": "Markdown",
                "disable_notification": not critical,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()


class WeCom(Notifier):
    """企业微信群机器人 webhook。"""

    name = "wecom"

    def send(self, title, body, url="", critical=False):
        content = f"**{title}**\n{body}"
        if url:
            content += f"\n[立即购买]({url})"
        r = requests.post(
            self.cfg["webhook"],
            json={"msgtype": "markdown", "markdown": {"content": content}},
            timeout=TIMEOUT,
        )
        r.raise_for_status()


class Webhook(Notifier):
    """通用 webhook，POST 一个 JSON，自己接去做别的事。"""

    name = "webhook"

    def send(self, title, body, url="", critical=False):
        r = requests.post(
            self.cfg["url"],
            json={"title": title, "body": body, "url": url, "critical": critical},
            timeout=TIMEOUT,
        )
        r.raise_for_status()


class Desktop(Notifier):
    """桌面通知。Windows（原生或 WSL）走 toast，原生 Linux 走 notify-send。"""

    name = "desktop"

    def send(self, title, body, url="", critical=False):
        ps_exe = _powershell()
        if ps_exe:
            text = (body + (f"\n{url}" if url else "")).replace("'", "")
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                "ContentType=WindowsRuntime] > $null; "
                "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{title}')) > $null; "
                f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode('{text}')) > $null; "
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('iPhone Hunter')"
                ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
            )
            subprocess.run([ps_exe, "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=15)
        elif shutil.which("notify-send"):
            args = ["notify-send"]
            if critical:
                args += ["-u", "critical"]
            subprocess.run(args + [title, body + (f"\n{url}" if url else "")],
                           capture_output=True, timeout=15)


class Bell(Notifier):
    """终端响铃 + 高亮，人在电脑前时最快。"""

    name = "sound"

    def send(self, title, body, url="", critical=False):
        n = int(self.cfg.get("repeat", 5)) if critical else 1
        for _ in range(n):
            sys.stdout.write("\a")
            sys.stdout.flush()


class Command(Notifier):
    """执行自定义命令，标题/内容/链接通过环境变量传进去。"""

    name = "command"

    def send(self, title, body, url="", critical=False):
        env = dict(os.environ,
                   HUNTER_TITLE=title, HUNTER_BODY=body,
                   HUNTER_URL=url, HUNTER_CRITICAL="1" if critical else "0")
        subprocess.run(self.cfg["cmd"], shell=True, env=env, timeout=30)


REGISTRY = {c.name: c for c in (Bark, ServerChan, Telegram, WeCom, Webhook, Desktop, Bell, Command)}


class Broadcaster:
    def __init__(self, cfg: dict, log=_p, quiet: QuietHours | None = None):
        self.log = log
        self.quiet = quiet or QuietHours()
        self.channels: list[Notifier] = []
        for name, sub in (cfg or {}).items():
            if not isinstance(sub, dict) or not sub.get("enabled"):
                continue
            cls = REGISTRY.get(name)
            if not cls:
                self.log(f"[通知] 未知渠道 {name}，跳过")
                continue
            self.channels.append(cls(sub))
        if not self.channels:
            self.log("[通知] 没有启用任何渠道，只会打印到终端")
        if self.quiet.enabled:
            self.log(f"[通知] 睡觉时段 {self.quiet.describe()}，"
                     f"这段时间只推送待付款提醒，其余只打印到终端")

    @classmethod
    def from_config(cls, cfg: dict, log=_p) -> "Broadcaster":
        """从整份 config 装配：渠道取 `notifiers`，睡觉时段取 `quiet_hours`。"""
        cfg = cfg or {}
        return cls(cfg.get("notifiers"), log=log,
                   quiet=QuietHours.from_config(cfg.get("quiet_hours"), log=log))

    def send(self, title: str, body: str, url: str = "", critical: bool = False,
             wake: bool = False) -> None:
        """wake=True 的消息无视睡觉时段——留给「订单等你付款」这种必须动手的。"""
        muted = not wake and self.quiet.muted()
        self.log(f"\n{'!' if critical else '*'} {title}\n  {body}"
                 + (f"\n  {url}" if url else "")
                 + ("\n  （睡觉时段，只记终端不推送）" if muted else ""))
        if muted:
            return
        for ch in self.channels:
            try:
                ch.send(title, body, url, critical)
            except Exception as e:  # 单个渠道挂掉不能影响监控
                self.log(f"[通知] {ch.name} 发送失败: {e}")


def open_in_browser(url: str, log=_p) -> None:
    """把购买页直接推到用户面前。Windows（原生或 WSL）用系统默认浏览器打开。"""
    if not url:
        return
    try:
        if os.name == "nt":
            os.startfile(url)          # type: ignore[attr-defined]  # 只有 Windows 有
            return
        if _is_wsl():
            opener = shutil.which("wslview") or shutil.which("explorer.exe")
            if opener:
                subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"[浏览器] 打开失败: {e}")


def _powershell() -> str | None:
    """能用来弹 toast 的 PowerShell，没有就返回 None。

    原生 Windows 上叫 `powershell`、WSL 里得带 .exe 才找得到。这里**不认 pwsh**：
    PowerShell 7 加载不了 toast 用的那套 WinRT 类型，挂了还是静默的，
    还不如让调用方直接走不发通知这条路。
    """
    if os.name == "nt":
        return shutil.which("powershell")
    if _is_wsl():
        return shutil.which("powershell.exe")
    return None


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False
