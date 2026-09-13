"""把终端输出和每个 HTTP 请求落到文件里。

监控是挂几小时甚至通宵的活，而终端 scrollback 留不住——偏偏「凌晨三点被拦了
几次、什么时候恢复的、放货那一刻请求耗时多少」正是事后最需要回看的东西。
所以写两份：

  logs/<命令>-<日期>.log            —— 终端上看到的一切，每行带日期+秒级时间戳
  logs/<命令>-<日期>.requests.jsonl —— 每个请求一行：URL / 参数 / 状态码 / 耗时 / 字节数

请求明细**只落盘、不打终端**：每轮一两条，打出来会把库存变化那几行刷没。

两份都按天滚动、按 keep_days 自动清理，不依赖外部 logrotate。文件名带命令名，
因为 run.sh 会同时跑 launch 和 watch 两个进程，写同一个文件会互相插行。

**写日志失败绝不能把监控搞停**：这里所有 I/O 异常一律吞掉。日志是附属品，
盯货才是正事。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

DEFAULTS = {
    "enabled": True,
    "dir": "logs",
    "requests": True,
    "keep_days": 14,
    "echo": True,
}

# 消息里已经自带的 [HH:MM:SS]，落盘时换成完整时间戳，不要两份时间并排
_OWN_TS = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*")


def _stamp(line: str, now: datetime | None = None) -> str:
    """给一行加上落盘用的时间戳。空行保持空行——它们是有意的排版。"""
    body = _OWN_TS.sub("", line).rstrip()
    if not body.strip():
        return ""
    return f"{(now or datetime.now()):%Y-%m-%d %H:%M:%S} {body}"


class DailyFile:
    """按天滚动的追加写文件。跨零点自动换新文件，通宵挂机不会写成一个巨型文件。"""

    def __init__(self, dirpath: Path, prefix: str, suffix: str):
        self.dir = Path(dirpath)
        self.prefix = prefix
        self.suffix = suffix
        self.day: date | None = None
        self.fp = None
        self.broken = ""   # 第一次写失败的原因，报一次就不再吵

    def path_for(self, day: date | None = None) -> Path:
        return self.dir / f"{self.prefix}-{(day or date.today()).isoformat()}{self.suffix}"

    def _file(self):
        today = date.today()
        if self.fp is None or today != self.day:
            self.close()
            self.dir.mkdir(parents=True, exist_ok=True)
            self.fp = self.path_for(today).open("a", encoding="utf-8")
            self.day = today
        return self.fp

    def write(self, text: str) -> bool:
        if not text:
            return True
        try:
            fp = self._file()
            fp.write(text)
            fp.flush()
            return True
        except OSError as e:
            if not self.broken:
                self.broken = f"{type(e).__name__}: {e}"
            return False

    def close(self) -> None:
        if self.fp is not None:
            try:
                self.fp.close()
            except OSError:
                pass
            self.fp = None
            self.day = None


class Tee:
    """stdout / stderr 的分流器：原样打到终端，同时按行落盘。

    必须按行攒——一次 print 会拆成多次 write（内容、换行分开来），
    逐次盖时间戳会把戳插到半行中间。
    """

    def __init__(self, stream, sink, echo: bool = True):
        self.stream = stream
        self.sink = sink
        self.echo = echo
        self.buf = ""

    def write(self, s) -> int:
        s = s if isinstance(s, str) else str(s)
        if self.echo and self.stream is not None:
            self.stream.write(s)
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.sink(line)
        return len(s)

    def drain(self) -> None:
        """把没以换行结尾的残句刷出去（收尾时用）。"""
        if self.buf:
            line, self.buf = self.buf, ""
            self.sink(line)

    def flush(self) -> None:
        if self.stream is not None:
            try:
                self.stream.flush()
            except (OSError, ValueError):
                pass

    def isatty(self) -> bool:
        return self.stream is not None and self.stream.isatty()

    def fileno(self) -> int:
        if self.stream is None:
            raise OSError("没有底层终端")
        return self.stream.fileno()

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        if name.startswith("_") or name in ("stream", "sink", "echo", "buf"):
            raise AttributeError(name)
        return getattr(self.stream, name)


class Logbook:
    def __init__(self, root: Path, cfg: dict | None = None, command: str = "hunter"):
        c = {**DEFAULTS, **(cfg or {})}
        self.enabled = bool(c.get("enabled", True))
        d = Path(str(c.get("dir") or "logs"))
        self.dir = d if d.is_absolute() else Path(root) / d
        self.command = re.sub(r"[^\w.-]", "_", str(command or "hunter")) or "hunter"
        self.echo = bool(c.get("echo", True))
        self.keep_days = int(c.get("keep_days", 14) or 0)
        self.out = DailyFile(self.dir, self.command, ".log")
        self.req = DailyFile(self.dir, self.command, ".requests.jsonl") \
            if c.get("requests", True) else None
        self.requests_logged = 0
        self.started = time.monotonic()
        self._saved: tuple | None = None
        self._tees: list[Tee] = []

    # ---------- 路径 ----------

    @property
    def log_path(self) -> Path:
        return self.out.path_for()

    @property
    def req_path(self) -> Path | None:
        return self.req.path_for() if self.req else None

    # ---------- 写 ----------

    def line(self, text: str) -> None:
        """写一行终端日志。text 是原始行，时间戳这里补。"""
        stamped = _stamp(text)
        self.out.write(f"{stamped}\n" if stamped else "\n")

    def note(self, text: str) -> None:
        """只写文件、不打终端的元信息（会话头尾之类）。"""
        self.line(text)

    def request(self, **fields) -> None:
        """记一个 HTTP 请求。只落盘，不打终端。"""
        if not self.req:
            return
        self.requests_logged += 1
        rec = {"ts": datetime.now().isoformat(timespec="milliseconds"), "cmd": self.command}
        rec.update({k: v for k, v in fields.items() if v not in (None, "", {})})
        try:
            text = json.dumps(rec, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = json.dumps({"ts": rec["ts"], "error": "这条记录没能序列化"}, ensure_ascii=False)
        self.req.write(text + "\n")

    # ---------- 生命周期 ----------

    def install(self, argv: list[str] | None = None) -> "Logbook":
        """接上 stdout/stderr，并把自己登记为全局当前日志（供 apple.py 记请求）。"""
        global _current
        if not self.enabled or self._saved is not None:
            return self
        self._saved = (sys.stdout, sys.stderr)
        out = Tee(sys.stdout, self.line, self.echo)
        err = Tee(sys.stderr, self.line, self.echo)
        self._tees = [out, err]
        sys.stdout, sys.stderr = out, err
        _current = self
        self.prune()
        cmd = " ".join(argv or sys.argv[1:]) or self.command
        try:
            from . import __version__ as ver
        except ImportError:
            ver = "?"
        self.note(f"==== 启动 {self.command} · v{ver} · pid {os.getpid()} · 参数：{cmd} ====")
        if self.out.broken:
            self.restore()
            print(f"[日志] 写不了 {self.log_path}（{self.out.broken}），本次只打终端")
        return self

    def restore(self) -> None:
        global _current
        for t in self._tees:
            t.drain()
        self._tees = []
        if self._saved is not None:
            sys.stdout, sys.stderr = self._saved
            self._saved = None
        if _current is self:
            _current = None

    def close(self, outcome: str = "") -> None:
        if self._saved is None and self.out.fp is None:
            return
        mins = (time.monotonic() - self.started) / 60
        ran = f"{mins / 60:.1f} 小时" if mins >= 90 else f"{mins:.1f} 分钟"
        tail = f"，请求 {self.requests_logged} 条" if self.req else ""
        self.restore()
        self.note(f"==== 结束 {self.command}{('：' + outcome) if outcome else ''}"
                  f" · 运行 {ran}{tail} ====")
        self.out.close()
        if self.req:
            self.req.close()

    def prune(self) -> int:
        """删掉超过 keep_days 的日志。keep_days <= 0 表示永久保留。"""
        if self.keep_days <= 0 or not self.dir.exists():
            return 0
        cutoff = time.time() - self.keep_days * 86400
        gone = 0
        for p in self.dir.iterdir():
            if p.suffix not in (".log", ".jsonl") or not p.is_file():
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    gone += 1
            except OSError:
                pass
        return gone


# ---------- 全局当前日志 ----------
#
# 请求记录点在 AppleClient._get 里，而客户端在十来处被构造。为这一件事给每处
# 都加一个参数不值得，所以用一个模块级的「当前日志」，没装就是空操作。

_current: Logbook | None = None


def setup(root: Path, cfg: dict | None = None, command: str = "hunter",
          argv: list[str] | None = None) -> Logbook | None:
    """按配置建好日志并接上 stdout。logging.enabled 为 false 时返回 None。"""
    lb = Logbook(root, cfg, command)
    if not lb.enabled:
        return None
    return lb.install(argv)


def current() -> Logbook | None:
    return _current


def log_request(**fields) -> None:
    """记一个 HTTP 请求；没装日志时什么都不做。"""
    lb = _current
    if lb is not None:
        lb.request(**fields)
