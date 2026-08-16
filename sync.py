# -*- coding: utf-8 -*-
"""维护门禁:维护操作(备份/导入/恢复)与普通请求的并发协调。"""

import threading
from contextlib import contextmanager


class MaintenanceGate:
    def __init__(self):
        self.condition = threading.Condition(threading.RLock())
        self.readers = 0
        self.writer = None
        self.writer_depth = 0
        self.waiting_writers = 0
        self.local = threading.local()

    @contextmanager
    def shared(self):
        ident = threading.get_ident()
        if self.writer == ident:
            yield
            return
        depth = getattr(self.local, "shared_depth", 0)
        with self.condition:
            if depth == 0:
                while self.writer is not None or self.waiting_writers:
                    self.condition.wait()
                self.readers += 1
            self.local.shared_depth = depth + 1
        try:
            yield
        finally:
            with self.condition:
                depth = self.local.shared_depth - 1
                self.local.shared_depth = depth
                if depth == 0:
                    self.readers -= 1
                    self.condition.notify_all()

    @contextmanager
    def exclusive(self):
        ident = threading.get_ident()
        with self.condition:
            if self.writer == ident:
                self.writer_depth += 1
            else:
                if getattr(self.local, "shared_depth", 0):
                    raise RuntimeError("不能从共享维护门禁升级为独占门禁")
                self.waiting_writers += 1
                try:
                    while self.writer is not None or self.readers:
                        self.condition.wait()
                    self.writer = ident
                    self.writer_depth = 1
                finally:
                    self.waiting_writers -= 1
        try:
            yield
        finally:
            with self.condition:
                self.writer_depth -= 1
                if self.writer_depth == 0:
                    self.writer = None
                    self.condition.notify_all()


MAINTENANCE_GATE = MaintenanceGate()
DB_MAINTENANCE_LOCK = threading.RLock()
MAINTENANCE_REPORT = {"missing": [], "orphans": [], "size_mismatch": [], "cleanup": []}
