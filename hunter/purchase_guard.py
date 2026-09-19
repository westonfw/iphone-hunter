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
    pass


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


class PurchaseGuard:
    def __init__(self, root: Path, *, inspect=False):
        self.root = Path(root)
        self.path = self.root / 'order-attempt.json'
        self.inspect = inspect
        self.file = None
        self.record = {}

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
                if not self.inspect and self.record['status'] != 'resolved':
                    raise PendingOrder('已有提交记录，可能存在待付款订单；请核对订单后运行 '
                                       '`python -m hunter order-state --resolve`')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def submitted(self, *, store: str, part: str = '') -> None:
        self.record = dict(attempt_id=uuid.uuid4().hex, status='submitted',
                           store=store, part=part or getattr(self, "part", ""), at=time.time())
        atomic_json(self.path, self.record)

    def finish(self, status: str, url: str = '') -> None:
        if not self.record or self.record.get('status') == 'resolved':
            return
        self.record.update(status=status, url=url, updated_at=time.time())
        atomic_json(self.path, self.record)

    def __exit__(self, *_):
        if self.file is not None:
            # close 释放操作系统文件锁；不要删除锁文件，否则另一进程可锁不同 inode。
            self.file.close()
            self.file = None
