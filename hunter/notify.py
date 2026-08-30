"""通知渠道。

抢首发的关键是"被叫醒"，所以默认同时走多路：手机推送 + 桌面 + 终端响铃。
每个渠道都不允许把主循环搞崩，异常一律吞掉并打日志。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import requests

TIMEOUT = 8


def _p(*a) -> None:
    print(*a, flush=True)


class Notifier:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def send(self, title: str, body: str, url: str = "", critical: bool = False) -> None:
        raise NotImplementedError


class Bark(Notifier):
    """iOS 上最好用的一路：可以强制响铃，点通知直接打开购买页。

    key 从 Bark App 首页复制，形如 https://api.day.app/xxxxxxxx/ 里的 xxxxxxxx。
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
            payload["url"] = url
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
    """桌面通知。WSL 下走 Windows 的 toast，原生 Linux 走 notify-send。"""

    name = "desktop"

    def send(self, title, body, url="", critical=False):
        if _is_wsl() and shutil.which("powershell.exe"):
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
            subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
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
    def __init__(self, cfg: dict, log=_p):
        self.log = log
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

    def send(self, title: str, body: str, url: str = "", critical: bool = False) -> None:
        self.log(f"\n{'!' if critical else '*'} {title}\n  {body}" + (f"\n  {url}" if url else ""))
        for ch in self.channels:
            try:
                ch.send(title, body, url, critical)
            except Exception as e:  # 单个渠道挂掉不能影响监控
                self.log(f"[通知] {ch.name} 发送失败: {e}")


def open_in_browser(url: str, log=_p) -> None:
    """把购买页直接推到用户面前。WSL 下用 Windows 默认浏览器打开。"""
    if not url:
        return
    try:
        if _is_wsl():
            opener = shutil.which("wslview") or shutil.which("explorer.exe")
            if opener:
                subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"[浏览器] 打开失败: {e}")


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False
