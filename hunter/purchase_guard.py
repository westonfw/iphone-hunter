"""跨进程串行购买；提交前持久化，无法确认结果时禁止再次购买。"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path


class PurchaseBusy(RuntimeError):
    pass


class PendingOrder(RuntimeError):
    """有一笔提交记录没了结。结果不明的单**绝不能**再提交一次。"""


class QuotaReached(RuntimeError):
    """已经买够了。这不是故障，是收工。"""


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


#: 状态含义：
#:   submitted —— 提交请求已经送出去，结果还不知道。**最危险的状态**，
#:                进程这时候崩掉，订单可能已经在 Apple 那边建好了。
#:   unknown   —— 提交出去了，但轮询没拿到最终结论。同样不能再买。
#:   confirmed —— 确认建单成功，已经计入配额，可以继续买下一台。
#:   resolved  —— 人工核对过了（order-state --resolve），解除保护。
_BLOCKING = ('submitted', 'unknown')


class PurchaseGuard:
    def __init__(self, root: Path, *, inspect=False, max_orders: int = 1,
                 check_orders: bool = True):
        self.root = Path(root)
        self.path = self.root / 'order-attempt.json'
        self.inspect = inspect
        #: 最多买几台。Apple 限购 2，但默认只买 1——多买一台是不可逆的花钱动作，
        #: 必须由 config 里的 autobuy.max_orders 明确写出来才放开。
        self.max_orders = max(1, int(max_orders))
        #: prepare() 那种只要串行锁、不参与订单配额判断的调用传 False。
        self.check_orders = check_orders
        self.file = None
        self.record = {}

    @property
    def bought(self) -> list:
        """已经确认拿到的订单。"""
        got = self.record.get('confirmed') if isinstance(self.record, dict) else None
        return got if isinstance(got, list) else []

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = open(self.root / '.purchase.lock', 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.file.tell() == 0:
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.file.close()
            self.file = None
            raise PurchaseBusy('另一个购买流程正在运行，本次不操作购物袋') from e
        try:
            if self.path.exists():
                try:
                    self.record = json.loads(self.path.read_text(encoding='utf-8'))
                    if not isinstance(self.record, dict) or 'status' not in self.record:
                        raise ValueError('invalid journal')
                except (ValueError, OSError) as e:
                    raise PendingOrder('订单记录无法读取，请人工核对订单后恢复记录') from e
                if not self.inspect and self.check_orders:
                    # 顺序要紧：先挡「结果不明」，再判配额。反过来的话，买够之后
                    # 一笔不明的单会被「已买够」盖掉，人就不知道还有单要核对。
                    if self.record['status'] in _BLOCKING:
                        raise PendingOrder(
                            '已有提交记录，可能存在待付款订单；请核对订单后运行 '
                            '`python -m hunter order-state --resolve`')
                    if len(self.bought) >= self.max_orders:
                        raise QuotaReached(
                            f'已经拿到 {len(self.bought)} 台（上限 {self.max_orders}），'
                            f'停止抢购。要继续请调大 autobuy.max_orders 或运行 '
                            f'`python -m hunter order-state --resolve`')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def submitted(self, *, store: str, part: str = '') -> None:
        # 已买清单必须跨单保留：整个替换 record 会把上一台抹掉，配额就永远不满，
        # 等于「最多两台」形同虚设。
        got = list(self.bought)
        self.record = dict(attempt_id=uuid.uuid4().hex, status='submitted',
                           store=store, part=part or getattr(self, "part", ""),
                           at=time.time(), confirmed=got)
        atomic_json(self.path, self.record)

    def finish(self, status: str, url: str = '') -> None:
        if not self.record or self.record.get('status') == 'resolved':
            return
        self.record.update(status=status, url=url, updated_at=time.time())
        if status == 'confirmed':
            self._count_this_one(url)
        atomic_json(self.path, self.record)

    def _count_this_one(self, url: str = '') -> None:
        """把这一单计入已买配额。按 attempt_id 去重，重复调用不会多记一台。"""
        got = list(self.bought)
        aid = self.record.get('attempt_id')
        if aid and any(x.get('attempt_id') == aid for x in got if isinstance(x, dict)):
            return
        got.append({'attempt_id': aid, 'part': self.record.get('part'),
                    'store': self.record.get('store'), 'url': url, 'at': time.time()})
        self.record['confirmed'] = got

    def resolve(self, *, bought: bool = False) -> None:
        """人工核对完毕。bought=True 表示「这单确实建成了」，计入配额。

        默认不计：`--resolve` 的常见场景是「查过了，没建单」。要是真建成了却
        不标，配额就会少算一台，下一轮会多买一台——所以这个开关必须显式给。
        """
        if bought:
            self._count_this_one(self.record.get('url', ''))
        self.record.update(status='resolved', updated_at=time.time())
        atomic_json(self.path, self.record)

    def __exit__(self, *_):
        if self.file is not None:
            # close 释放操作系统文件锁；不要删除锁文件，否则另一进程可锁不同 inode。
            self.file.close()
            self.file = None
