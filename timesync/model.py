"""核心数据模型。

时间单位全部为“模拟参考时钟的 epoch 毫秒”（整数/浮点毫秒），
不使用字符串时间参与计算；设备本地时间同样以毫秒表达，可带任意偏移，
因此设备时钟天然可以跨越午夜日期边界。

关键不变量：
- 测量记录一旦写入接收服务即不可变；
- (device_id, boot_epoch, seq) 是测量记录的唯一标识，设备重启后复用 seq
  不会冲突，因为 boot_epoch 不同；
- 到达顺序 arrival_seq 仅用于审计与展示，校时与排序逻辑禁止以它推断真实先后。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


def iso_ms(t_ms: Optional[float]) -> Optional[str]:
    """把 epoch 毫秒格式化为 'YYYY-MM-DD HH:MM:SS.mmm'（浏览器侧另有 JS 版）。"""
    if t_ms is None:
        return None
    import datetime as _dt

    dt = _dt.datetime.fromtimestamp(t_ms / 1000.0, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


@dataclass
class Measurement:
    """设备上传的测量片段（接收服务保存的原始记录，不可变）。"""

    id: str                      # m<全局自增>，接收端分配
    device_id: str               # 设备标识
    boot_epoch: int              # 本次启动代号；重启后 +1，seq 重新从 1 开始
    seq: int                     # 设备在本次启动内的片段序号（重启会复用）
    device_time_ms: float        # 设备自己时钟显示的时间
    true_time_ms: float          # 模拟层隐藏的参考时钟真实时间（仅用于审计/误差展示）
    arrival_time_ms: float       # 接收服务时钟（=参考时钟）记录的到达时刻
    arrival_seq: int             # 全局到达序号，仅审计用，禁止用于真实排序
    payload: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["device_time_iso"] = iso_ms(self.device_time_ms)
        d["true_time_iso"] = iso_ms(self.true_time_ms)
        d["arrival_time_iso"] = iso_ms(self.arrival_time_ms)
        return d


@dataclass
class CalibrationPoint:
    """一次校时记录：设备时钟读数 device_time 对应参考时钟读数 ref_time。

    校时记录可以迟到（device_time 远早于当前时间），按到达顺序追加保存，
    但计算时始终按 (boot_epoch, device_time_ms) 排序使用。
    """

    id: str                      # c<全局自增>
    device_id: str
    boot_epoch: int              # 该校时属于设备的哪一次启动
    device_time_ms: float        # 校时时设备时钟读数
    ref_time_ms: float           # 校时时参考时钟读数
    received_at_ms: float        # 校时记录到达接收服务的时刻
    is_backfill: bool = False    # 是否为迟到补校
    note: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["device_time_iso"] = iso_ms(self.device_time_ms)
        d["ref_time_iso"] = iso_ms(self.ref_time_ms)
        d["received_iso"] = iso_ms(self.received_at_ms)
        return d


@dataclass
class CorrectedRecord:
    """一条测量记录在某个校时版本下的校正结果（随版本不可变）。"""

    measurement_id: str
    device_id: str
    boot_epoch: int
    seq: int
    device_time_ms: float
    corrected_time_ms: Optional[float]   # None = 本次启动尚无校时，无法校正
    interval_lo_ms: Optional[float]
    interval_hi_ms: Optional[float]
    uncertainty_ms: Optional[float]
    calibration_state: str               # calibrated | interpolated | extrapolated | uncalibrated
    based_on_cal_ids: list[str] = field(default_factory=list)
    true_time_ms: Optional[float] = None
    true_error_ms: Optional[float] = None
    arrival_seq: int = 0
    arrival_rank_in_timeline: Optional[int] = None   # 校正序中的位置 vs 到达序位置的差，供 UI 高亮
    arrival_time_ms: float = 0.0
    crosses_midnight: bool = False       # 设备时间与校正结果不在同一 UTC 日期

    def to_dict(self) -> dict:
        d = asdict(self)
        d["device_time_iso"] = iso_ms(self.device_time_ms)
        d["corrected_iso"] = iso_ms(self.corrected_time_ms)
        d["interval_lo_iso"] = iso_ms(self.interval_lo_ms)
        d["interval_hi_iso"] = iso_ms(self.interval_hi_ms)
        d["true_iso"] = iso_ms(self.true_time_ms)
        return d


@dataclass
class OrderBoundary:
    """时间轴上无法可靠确定先后的边界。"""

    kind: str                  # overlap | uncalibrated
    left_id: str
    right_id: str
    left_device: str
    right_device: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TimelineVersion:
    """计算器产出的一个不可变时间轴版本。"""

    version: int
    created_at_ms: float
    watermark_seq: int                       # 本版本已纳入的接收端原始记录/校时水位
    records: list[CorrectedRecord] = field(default_factory=list)
    boundaries: list[OrderBoundary] = field(default_factory=list)
    strategy: str = ""                       # full_rebuild | local_recompute | mixed
    strategy_reason: str = ""
    recomputed_measurement_ids: list[str] = field(default_factory=list)
    calibration_ids: list[str] = field(default_factory=list)
    rejected: bool = False                   # 被版本栅栏拒绝的迟到旧结果
    reject_reason: str = ""
    job_id: Optional[int] = None
    compute_delay_ms: float = 0.0

    def summary(self) -> dict:
        """版本列表用的轻量摘要（不含全部记录）。"""
        states: dict[str, int] = {}
        for r in self.records:
            states[r.calibration_state] = states.get(r.calibration_state, 0) + 1
        return {
            "version": self.version,
            "created_at_ms": self.created_at_ms,
            "created_iso": iso_ms(self.created_at_ms),
            "watermark_seq": self.watermark_seq,
            "strategy": self.strategy,
            "strategy_reason": self.strategy_reason,
            "n_records": len(self.records),
            "n_boundaries": len(self.boundaries),
            "n_recomputed": len(self.recomputed_measurement_ids),
            "calibration_ids": list(self.calibration_ids),
            "rejected": self.rejected,
            "reject_reason": self.reject_reason,
            "job_id": self.job_id,
            "compute_delay_ms": round(self.compute_delay_ms, 1),
            "state_counts": states,
        }

    def to_dict(self, full: bool = True) -> dict:
        out = self.summary()
        if full:
            out["records"] = [r.to_dict() for r in self.records]
            out["boundaries"] = [b.to_dict() for b in self.boundaries]
        return out
