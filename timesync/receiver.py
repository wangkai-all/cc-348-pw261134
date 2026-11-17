"""接收服务。

职责（且仅限于此）：
1. 保存设备上传的测量片段原始记录，永不修改；
2. 给每条记录盖到达时间戳与全局到达序号 arrival_seq；
3. 保存迟到的校时记录（同样只追加、不修改）；
4. 在新数据落库后通知计算器（回调），通知动作在锁外执行，避免重入死锁。

注意：arrival_seq 表达的是“接收服务看到的顺序”，网络抖动会让两台设备的
真实先后与到达先后不一致，因此下游排序严禁直接使用它。
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from .clocks import ReferenceClock
from .model import Measurement, CalibrationPoint


class Receiver:
    def __init__(self, ref: ReferenceClock,
                 on_change: Optional[Callable[[], None]] = None) -> None:
        self.ref = ref
        self._lock = threading.Lock()
        self._measurements: list[Measurement] = []
        self._calibrations: list[CalibrationPoint] = []
        self._arrival_counter = 0
        self._m_counter = 0
        self._c_counter = 0
        self._on_change = on_change

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def next_measurement_id(self) -> str:
        with self._lock:
            self._m_counter += 1
            return f"m{self._m_counter}"

    def next_calibration_id(self) -> str:
        with self._lock:
            self._c_counter += 1
            return f"c{self._c_counter}"

    def accept_measurement(self, m: Measurement) -> Measurement:
        """盖章并保存一条原始测量记录。"""
        notify = False
        with self._lock:
            self._arrival_counter += 1
            m.arrival_seq = self._arrival_counter
            m.arrival_time_ms = self.ref.now_ms()
            self._measurements.append(m)
            notify = self._on_change is not None
        if notify:
            self._on_change()
        return m

    def accept_calibration(self, device_id: str, boot_epoch: int,
                           device_time_ms: float, ref_time_ms: float,
                           is_backfill: bool = False, note: str = "") -> CalibrationPoint:
        """保存一条校时记录（允许迟到/补校）。"""
        notify = False
        with self._lock:
            self._c_counter += 1
            cal = CalibrationPoint(
                id=f"c{self._c_counter}",
                device_id=device_id,
                boot_epoch=boot_epoch,
                device_time_ms=float(device_time_ms),
                ref_time_ms=float(ref_time_ms),
                received_at_ms=self.ref.now_ms(),
                is_backfill=is_backfill,
                note=note,
            )
            self._calibrations.append(cal)
            notify = self._on_change is not None
        if notify:
            self._on_change()
        return cal

    def watermark(self) -> int:
        """接收端原始数据水位：测量与校时的总落库条数，单调递增。"""
        with self._lock:
            return len(self._measurements) + len(self._calibrations)

    def snapshot(self, measurements_limit: Optional[int] = None) -> dict:
        with self._lock:
            measurements = list(self._measurements)
            calibrations = list(self._calibrations)
        if measurements_limit is not None and len(measurements) > measurements_limit:
            measurements = measurements[-measurements_limit:]
        return {
            "measurements": [m.to_dict() for m in measurements],
            "calibrations": [c.to_dict() for c in calibrations],
            "total_measurements": len(self._measurements),
            "total_calibrations": len(self._calibrations),
        }

    def all_measurements(self) -> list[Measurement]:
        with self._lock:
            return list(self._measurements)

    def all_calibrations(self) -> list[CalibrationPoint]:
        with self._lock:
            return list(self._calibrations)

    def measurements_for(self, device_id: str, boot_epoch: int) -> list[Measurement]:
        with self._lock:
            return [
                m for m in self._measurements
                if m.device_id == device_id and m.boot_epoch == boot_epoch
            ]

    def reset(self) -> None:
        with self._lock:
            self._measurements.clear()
            self._calibrations.clear()
            self._arrival_counter = 0
            self._m_counter = 0
            self._c_counter = 0
