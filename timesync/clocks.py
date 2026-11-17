"""时钟模型与设备模拟器（纯模拟，不接真实硬件）。

- ReferenceClock：权威参考时钟，直接读取系统 monotonic 时间换算为模拟 epoch 毫秒。
- DeviceClock：设备本地时钟，按“折线段”演化。每段记录
  (锚点参考时间 t0, 锚点设备时间 d0, 漂移率 rate)，其中
      device_time(t) = d0 + (t - t0) * rate
  在线调整漂移率会新增一段，旧段保留，因此读取任意历史时刻仍可复现当时读数。
- DeviceSimulator：在 DeviceClock 之上产生测量片段，维护 boot_epoch 与每 boot 的
  序号 seq；重启复用序号但 boot_epoch 递增，保证标识不冲突。
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass

from .model import Measurement


class ReferenceClock:
    """模拟系统的权威时钟（接收服务与校时参考都使用它）。"""

    def __init__(self, epoch_ms: float | None = None) -> None:
        self._anchor_mono = time.monotonic()
        # 默认以真实 UTC epoch 为基准，便于在网页上看到时分秒和午夜跨越
        self._anchor_epoch = epoch_ms if epoch_ms is not None else time.time() * 1000.0

    def now_ms(self) -> float:
        return self._anchor_epoch + (time.monotonic() - self._anchor_mono) * 1000.0


@dataclass
class _ClockSegment:
    t0: float           # 段起点：参考时间
    d0: float           # 段起点：设备时间
    rate: float         # 设备时间增量 / 参考时间增量（1.0 为走时准确）


class DeviceClock:
    """带漂移的设备时钟，漂移率可在线调整，历史读数可复现。"""

    def __init__(self, ref: ReferenceClock, device_id: str,
                 initial_offset_ms: float = 0.0, initial_rate: float = 1.0) -> None:
        self.ref = ref
        self.device_id = device_id
        now = ref.now_ms()
        self._segments: list[_ClockSegment] = [
            _ClockSegment(t0=now, d0=now + initial_offset_ms, rate=initial_rate)
        ]
        self._lock = threading.Lock()

    def _segment_at(self, ref_time_ms: float) -> _ClockSegment:
        """返回参考时间 ref_time_ms 所在的折线段（早于首段则用首段外推）。"""
        seg = self._segments[0]
        for candidate in self._segments:
            if candidate.t0 <= ref_time_ms:
                seg = candidate
            else:
                break
        return seg

    def device_time_at(self, ref_time_ms: float) -> float:
        seg = self._segment_at(ref_time_ms)
        return seg.d0 + (ref_time_ms - seg.t0) * seg.rate

    def read(self) -> float:
        return self.device_time_at(self.ref.now_ms())

    def set_rate(self, rate: float) -> None:
        """在线调整漂移率：在当前时刻新增一段，设备读数保持连续。"""
        if rate <= 0:
            raise ValueError("漂移率必须为正数（设备时钟必须向前走）")
        with self._lock:
            now = self.ref.now_ms()
            current = self.device_time_at(now)
            self._segments.append(_ClockSegment(t0=now, d0=current, rate=rate))

    def current_rate(self) -> float:
        return self._segments[-1].rate

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "device_id": self.device_id,
                "segments": [
                    {"t0": s.t0, "d0": s.d0, "rate": s.rate} for s in self._segments
                ],
            }


class DeviceSimulator:
    """设备模拟器：周期性产生测量片段，可暂停、可重启。"""

    def __init__(self, ref: ReferenceClock, device_id: str,
                 initial_offset_ms: float = 0.0, initial_rate: float = 1.0,
                 interval_ms: float = 1200.0) -> None:
        self.device_id = device_id
        self.ref = ref
        self.clock = DeviceClock(ref, device_id, initial_offset_ms, initial_rate)
        self.interval_ms = interval_ms
        self.boot_epoch = 1
        self._seq = 0
        self.running = True
        self._next_emit: float | None = None
        self._lock = threading.Lock()

    def restart(self) -> int:
        """设备重启：boot_epoch 递增，序号从 1 重新开始（复用序号的来源）。"""
        with self._lock:
            self.boot_epoch += 1
            self._seq = 0
            self._next_emit = None
            return self.boot_epoch

    def set_running(self, running: bool) -> None:
        with self._lock:
            self.running = running
            if running:
                self._next_emit = None

    def set_rate(self, rate: float) -> None:
        self.clock.set_rate(rate)

    def due(self) -> bool:
        if not self.running:
            return False
        now = self.ref.now_ms()
        if self._next_emit is None:
            self._next_emit = now + self.interval_ms
            return False
        return now >= self._next_emit

    def emit(self, id_allocator) -> Measurement:
        """产生一条测量片段。id_allocator() 返回全局唯一字符串 id。"""
        with self._lock:
            if not self.running:
                raise RuntimeError(f"设备 {self.device_id} 已暂停，不上传片段")
            true_time = self.ref.now_ms()
            self._seq += 1
            seq = self._seq
            boot = self.boot_epoch
            device_time = self.clock.device_time_at(true_time)
            self._next_emit = true_time + self.interval_ms
        return Measurement(
            id=id_allocator(),
            device_id=self.device_id,
            boot_epoch=boot,
            seq=seq,
            device_time_ms=device_time,
            true_time_ms=true_time,
            arrival_time_ms=0.0,       # 由接收服务盖章
            arrival_seq=0,             # 由接收服务分配
            payload=f"segment-{boot}-{seq}",
        )

    def status(self) -> dict:
        return {
            "device_id": self.device_id,
            "boot_epoch": self.boot_epoch,
            "seq": self._seq,
            "running": self.running,
            "rate": self.clock.current_rate(),
            "interval_ms": self.interval_ms,
            "device_time_ms": self.clock.read(),
        }
