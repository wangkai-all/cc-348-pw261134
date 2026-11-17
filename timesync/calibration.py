"""校时计算核心（纯函数，不涉及线程，便于单测）。

映射模型
--------
对每个 (device_id, boot_epoch) 分组，取该组的校时点按设备时间排序，
相邻两点构成一段线性映射：

    ref(t) = ref_a + (t - dev_a) * (ref_b - ref_a) / (dev_b - dev_a)

- 落在两点之间：interpolated（插值）；
- 早于首个校时点 / 晚于末个校时点：extrapolated（外推），沿用最近一段斜率，
  不确定度随距离扩大并显式标记；
- 该 boot 一个校时点都没有：uncalibrated，corrected_time 为 None，
  这些记录不能与任何记录可靠排序。

不确定度
--------
- 校时点本身：CAL_POINT_HALF_WIDTH_MS（校时操作的对时误差半宽）；
- 插值：半宽随相对位置在两端点半宽之间线性过渡；
- 外推：端点半宽 + 距离 * EXTRAPOLATION_SLOPE_PPM（百万分率），
  距离越远越不可信，区间如实变宽。

排序与边界
----------
同设备同 boot：以 seq 为权威顺序（设备本地序号是单调的，不受网络影响）。
跨设备 / 跨 boot：只能比较校正后的不确定区间：
- 两区间完全不重叠（lo_a > hi_b 或 lo_b > hi_a）才能判定先后；
- 区间重叠 => 真实先后无法可靠确定，产生 overlap 边界；
- 任一方 uncalibrated => 产生 uncalibrated 边界。
到达顺序 arrival_seq 不参与任何排序判定。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Optional

from .model import (
    Measurement,
    CalibrationPoint,
    CorrectedRecord,
    OrderBoundary,
)

CAL_POINT_HALF_WIDTH_MS = 2.0
EXTRAPOLATION_SLOPE_PPM = 500.0          # 每偏离 1 秒增加 0.5 ms 半宽
EXTRAPOLATION_FLOOR_MS = 2.0
OVERLAP_REASON = "校正不确定区间重叠，真实先后无法可靠确定（已拒绝使用到达顺序推断）"
UNCAL_REASON = "存在尚无校时的启动段（uncalibrated），该位置无法可靠排序"


def _utc_date(t_ms: float) -> str:
    return _dt.datetime.fromtimestamp(t_ms / 1000.0, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


@dataclass
class _Segment:
    dev_a: float
    ref_a: float
    ref_b: float
    dev_b: float
    cal_a: str
    cal_b: str

    @property
    def rate(self) -> float:
        return (self.ref_b - self.ref_a) / (self.dev_b - self.dev_a)


class Mapping:
    """一个 (device, boot) 分组的分段线性校时映射。"""

    def __init__(self, device_id: str, boot_epoch: int,
                 calibrations: list[CalibrationPoint]) -> None:
        self.device_id = device_id
        self.boot_epoch = boot_epoch
        points = sorted(calibrations, key=lambda c: c.device_time_ms)
        self.points = points
        self.segments: list[_Segment] = []
        for a, b in zip(points, points[1:]):
            if b.device_time_ms == a.device_time_ms:
                # 同一设备时刻的重复校时：保留后到的一条（points 已按时间排序，
                # 同时间按 id 次序，后者视为更新），跳过退化段
                continue
            self.segments.append(_Segment(
                dev_a=a.device_time_ms, ref_a=a.ref_time_ms,
                dev_b=b.device_time_ms, ref_b=b.ref_time_ms,
                cal_a=a.id, cal_b=b.id,
            ))

    @property
    def empty(self) -> bool:
        return not self.points

    def correct(self, device_time_ms: float) -> tuple[Optional[float], Optional[float],
                                                      Optional[float], str, list[str]]:
        """返回 (校正时间, 区间下限, 区间上限, 状态, 依据校时id列表)。"""
        if self.empty:
            return None, None, None, "uncalibrated", []

        pts = self.points
        # 早于首个校时点：向前外推
        if device_time_ms < pts[0].device_time_ms:
            rate = self.segments[0].rate if self.segments else 1.0
            base = pts[0]
            corrected = base.ref_time_ms + (device_time_ms - base.device_time_ms) * rate
            dist = abs(device_time_ms - base.device_time_ms)
            half = CAL_POINT_HALF_WIDTH_MS + max(
                EXTRAPOLATION_FLOOR_MS, dist * EXTRAPOLATION_SLOPE_PPM / 1e6)
            return corrected, corrected - half, corrected + half, "extrapolated", [base.id]

        # 晚于末个校时点：向后外推
        if device_time_ms > pts[-1].device_time_ms:
            rate = self.segments[-1].rate if self.segments else 1.0
            base = pts[-1]
            corrected = base.ref_time_ms + (device_time_ms - base.device_time_ms) * rate
            dist = abs(device_time_ms - base.device_time_ms)
            half = CAL_POINT_HALF_WIDTH_MS + max(
                EXTRAPOLATION_FLOOR_MS, dist * EXTRAPOLATION_SLOPE_PPM / 1e6)
            return corrected, corrected - half, corrected + half, "extrapolated", [base.id]

        # 恰为某个校时点
        for p in pts:
            if device_time_ms == p.device_time_ms:
                return (p.ref_time_ms,
                        p.ref_time_ms - CAL_POINT_HALF_WIDTH_MS,
                        p.ref_time_ms + CAL_POINT_HALF_WIDTH_MS,
                        "calibrated", [p.id])

        # 落在某段内部：插值，半宽在两端点之间线性过渡
        for seg in self.segments:
            if seg.dev_a < device_time_ms < seg.dev_b:
                frac = (device_time_ms - seg.dev_a) / (seg.dev_b - seg.dev_a)
                corrected = seg.ref_a + frac * (seg.ref_b - seg.ref_a)
                half = CAL_POINT_HALF_WIDTH_MS  # 两端点同半宽时保持常数
                return (corrected, corrected - half, corrected + half,
                        "interpolated", [seg.cal_a, seg.cal_b])

        # 理论不可达（退化数据的防御性回退）
        return None, None, None, "uncalibrated", []


def build_mappings(calibrations: list[CalibrationPoint]) -> dict[tuple[str, int], Mapping]:
    """按 (device_id, boot_epoch) 分组构建全部映射。"""
    groups: dict[tuple[str, int], list[CalibrationPoint]] = {}
    for cal in calibrations:
        groups.setdefault((cal.device_id, cal.boot_epoch), []).append(cal)
    return {key: Mapping(key[0], key[1], cals) for key, cals in groups.items()}


def calibrate_record(m: Measurement, mapping: Optional[Mapping]) -> CorrectedRecord:
    """对单条测量记录应用映射，生成校正记录（含与隐藏真值的误差，仅供审计）。"""
    corrected, lo, hi, state, cal_ids = (
        mapping.correct(m.device_time_ms) if mapping is not None
        else (None, None, None, "uncalibrated", [])
    )
    uncertainty = (hi - lo) / 2.0 if hi is not None and lo is not None else None
    true_error = corrected - m.true_time_ms if corrected is not None else None
    crosses = (
        corrected is not None
        and _utc_date(m.device_time_ms) != _utc_date(corrected)
    )
    return CorrectedRecord(
        measurement_id=m.id,
        device_id=m.device_id,
        boot_epoch=m.boot_epoch,
        seq=m.seq,
        device_time_ms=m.device_time_ms,
        corrected_time_ms=corrected,
        interval_lo_ms=lo,
        interval_hi_ms=hi,
        uncertainty_ms=uncertainty,
        calibration_state=state,
        based_on_cal_ids=cal_ids,
        true_time_ms=m.true_time_ms,
        true_error_ms=true_error,
        arrival_seq=m.arrival_seq,
        arrival_time_ms=m.arrival_time_ms,
        crosses_midnight=crosses,
    )


def calibrate_all(measurements: list[Measurement],
                  mappings: dict[tuple[str, int], Mapping]) -> list[CorrectedRecord]:
    """对全部测量记录应用映射。同组内按 seq 权威排序，保持设备内真实先后。"""
    result: list[CorrectedRecord] = []
    ordered = sorted(measurements, key=lambda m: (m.device_id, m.boot_epoch, m.seq))
    for m in ordered:
        mapping = mappings.get((m.device_id, m.boot_epoch))
        result.append(calibrate_record(m, mapping))
    return result


def _intervals_overlap(lo_a: float, hi_a: float, lo_b: float, hi_b: float) -> bool:
    return not (hi_a < lo_b or hi_b < lo_a)


def build_timeline(records: list[CorrectedRecord]) -> tuple[list[CorrectedRecord],
                                                            list[OrderBoundary]]:
    """构建全局时间轴并找出无法可靠排序的边界。

    排序键：可校正记录按校正时间；uncalibrated 记录排到末尾（它们没有可信时刻）。
    对时间轴上每一对相邻记录做区间检测：
    - 任一为 uncalibrated -> uncalibrated 边界；
    - 两者区间重叠 -> overlap 边界；
    - 否则先后可确定。
    同时回填 arrival_rank_in_timeline（到达序位置 - 校正序位置），
    供网页直接展示“到达顺序 ≠ 真实先后”的实例。
    """
    calibrated = [r for r in records if r.calibration_state != "uncalibrated"]
    uncalibrated = [r for r in records if r.calibration_state == "uncalibrated"]

    calibrated.sort(key=lambda r: (r.corrected_time_ms, r.device_id, r.boot_epoch, r.seq))
    uncalibrated.sort(key=lambda r: (r.device_id, r.boot_epoch, r.seq))
    ordered = calibrated + uncalibrated

    # 到达序排名（按接收服务 arrival_seq 的全局名次）。
    # 注意：它只用于计算下方的“序差”展示列，证明到达顺序与真实先后的偏差，
    # 时间轴本身的排序键是校正时间与设备 seq，绝不使用 arrival_seq。
    arrival_order = sorted(records, key=lambda r: r.arrival_seq)
    arrival_rank = {r.measurement_id: idx for idx, r in enumerate(arrival_order)}

    boundaries: list[OrderBoundary] = []
    for idx, rec in enumerate(ordered):
        rec.arrival_rank_in_timeline = arrival_rank.get(rec.measurement_id, 0) - idx
        if idx == 0:
            continue
        prev = ordered[idx - 1]
        if (rec.calibration_state == "uncalibrated"
                or prev.calibration_state == "uncalibrated"):
            boundaries.append(OrderBoundary(
                kind="uncalibrated",
                left_id=prev.measurement_id, right_id=rec.measurement_id,
                left_device=f"{prev.device_id}#b{prev.boot_epoch}",
                right_device=f"{rec.device_id}#b{rec.boot_epoch}",
                reason=UNCAL_REASON,
            ))
            continue
        assert prev.interval_lo_ms is not None and rec.interval_lo_ms is not None
        if _intervals_overlap(prev.interval_lo_ms, prev.interval_hi_ms,
                              rec.interval_lo_ms, rec.interval_hi_ms):
            boundaries.append(OrderBoundary(
                kind="overlap",
                left_id=prev.measurement_id, right_id=rec.measurement_id,
                left_device=f"{prev.device_id}#b{prev.boot_epoch}",
                right_device=f"{rec.device_id}#b{rec.boot_epoch}",
                reason=OVERLAP_REASON,
            ))
    return ordered, boundaries
