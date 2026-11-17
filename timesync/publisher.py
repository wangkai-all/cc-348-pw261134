"""入库端（发布器）。

规则：
1. 入库端只按“已确认的校时版本”发布，计算器的候选版本不会自动成为发布版，
   除非显式开启自动确认；
2. 版本号单调栅栏：迟到的旧版本即使被推送，也无法覆盖当前发布版，
   尝试动作留痕；
3. 每个发布版是不可变快照（直接引用计算器版本对象，其记录不再被修改），
   因此发布版永远自洽：版本内的校正结果、边界、校时依据彼此一致；
4. 设备重启复用序号、跨午夜校时等场景的自洽性由版本不可变性保证——
   旧发布版不会因为新数据到达而悄悄改变含义。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Callable, Optional

from .clocks import ReferenceClock
from .model import TimelineVersion


class Publisher:
    def __init__(self, ref: ReferenceClock,
                 audit: Optional[Callable[[str, str, dict], None]] = None) -> None:
        self.ref = ref
        self._audit = audit or (lambda kind, msg, data=None: None)
        self._lock = threading.Lock()
        self._confirmed: deque[TimelineVersion] = deque(maxlen=40)
        self.current_version: Optional[TimelineVersion] = None
        self.auto_confirm = True
        self.last_reject: Optional[dict] = None

    def set_auto_confirm(self, enabled: bool) -> None:
        with self._lock:
            self.auto_confirm = enabled
        self._audit("publish_auto",
                    f"自动确认已{'开启' if enabled else '关闭'}：候选版本需手动确认后才会入库",
                    {"auto_confirm": enabled})

    def try_auto_confirm(self, version: TimelineVersion) -> bool:
        if self.auto_confirm:
            return self.confirm(version)
        return False

    def confirm(self, version: TimelineVersion) -> bool:
        """确认一个候选版本并发布。返回是否被接受。"""
        if version is None or version.rejected:
            return False
        with self._lock:
            current_no = self.current_version.version if self.current_version else 0
            if version.version <= current_no:
                self.last_reject = {
                    "at_ms": self.ref.now_ms(),
                    "attempted_version": version.version,
                    "current_version": current_no,
                    "reason": "旧版本晚到，发布栅栏拒绝回退发布版",
                }
                self._audit("publish_stale_rejected",
                            f"入库端拒绝发布 v{version.version}：当前发布版 v{current_no} 更新，"
                            "禁止用旧计算结果回退",
                            {"attempted": version.version, "current": current_no})
                return False
            self.current_version = version
            self._confirmed.append(version)
        self._audit("publish_confirmed",
                    f"入库端发布 v{version.version}（{len(version.records)} 条记录，"
                    f"{len(version.boundaries)} 个不可排序边界）",
                    {"version": version.version})
        return True

    def reset(self) -> None:
        with self._lock:
            self._confirmed.clear()
            self.current_version = None
            self.auto_confirm = True
            self.last_reject = None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "auto_confirm": self.auto_confirm,
                "current_version": self.current_version.version
                if self.current_version else None,
                "history": [v.summary() for v in reversed(self._confirmed)],
                "last_reject": dict(self.last_reject) if self.last_reject else None,
            }
