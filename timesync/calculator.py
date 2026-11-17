"""校时计算器。

独立于接收服务运行的计算组件：

- 接收服务每次落库新数据都会触发一次“重算请求”，请求在计算器内部合并
  （coalesce）：正在计算时到达的多个触发只保留一次 catch-up；
- 计算在线程池中执行，人为加入计算耗时，因此旧作业可能比新作业更晚完成；
- 版本栅栏：作业完成时若其数据水位不高于最近已接受版本的水位，判定为
  “迟到的旧计算结果”，整版拒绝并留痕，绝不用旧结果覆盖新结果；
- 重算策略按 (device, boot) 分组决策：
  * 新分组 / 校时集合发生结构性变化（新增首末点、改斜率覆盖外推区等）
    -> full_rebuild 整段重建；
  * 新校时点严格落在两个已知校时点之间 -> local_recompute，只重算受影响
    区间内的记录，其余校正结果原样保留；
  * 校时映射未变、只有新测量 -> 仅校正新记录（也算局部重算）。
- 可暂停：暂停期间不启动新作业，恢复后自动补一次 catch-up；在途作业
  跑完后同样接受版本栅栏检查。
"""

from __future__ import annotations

import threading
import random
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

from .clocks import ReferenceClock
from .receiver import Receiver
from .model import Measurement, CalibrationPoint, TimelineVersion
from .calibration import Mapping, calibrate_record, build_timeline


@dataclass
class _GroupCache:
    """某个 (device, boot) 分组上一版本的计算缓存，用于局部重算。"""

    cal_set: frozenset[str]
    calibrations: list[CalibrationPoint]
    records: dict[str, object]                      # measurement_id -> CorrectedRecord
    last_strategy: str = ""


@dataclass
class CalcJob:
    job_id: int
    created_at_ms: float
    watermark: int
    compute_delay_ms: float
    status: str = "running"                          # running | accepted | rejected
    version: Optional[int] = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "created_at_ms": self.created_at_ms,
            "watermark": self.watermark,
            "compute_delay_ms": round(self.compute_delay_ms, 1),
            "status": self.status,
            "version": self.version,
            "detail": self.detail,
        }


class Calculator:
    def __init__(self, ref: ReferenceClock, receiver: Receiver,
                 audit: Optional[Callable[[str, str, dict], None]] = None,
                 on_version: Optional[Callable[[TimelineVersion], None]] = None,
                 max_workers: int = 2, seed: int = 2611) -> None:
        self.ref = ref
        self.receiver = receiver
        self._audit = audit or (lambda kind, msg, data=None: None)
        self._on_version = on_version or (lambda v: None)
        self._rng = random.Random(seed)
        # 计算作业专用池：多留 1 个槽位，保证主循环线程在任何时刻都能提交，
        # 不会因“池满 + submit 阻塞”与在途长作业形成死锁
        self._pool = ThreadPoolExecutor(max_workers=max_workers + 1,
                                        thread_name_prefix="calccalc")
        self._cond = threading.Condition()
        self._paused = False
        self._pending = False
        self._stopped = False
        self._job_counter = 0
        self._version_counter = 0
        self._last_accepted_watermark = -1
        self.jobs: deque[CalcJob] = deque(maxlen=60)
        self.versions: deque[TimelineVersion] = deque(maxlen=40)
        self._group_cache: dict[tuple[str, int], _GroupCache] = {}
        self._loop_thread: Optional[threading.Thread] = None
        # 测试钩子：dict[job_id -> 强制耗时ms]，注入后该作业忽略随机耗时
        self.test_delay_overrides: dict[int, float] = {}

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._loop_thread is not None:
            return
        t = threading.Thread(target=self._loop, name="calc-loop", daemon=True)
        self._loop_thread = t
        t.start()

    def pause(self) -> None:
        with self._cond:
            self._paused = True
        self._audit("calc_paused", "校时计算器已暂停：新触发排队，在途作业跑完后受版本栅栏约束", {})

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._pending = True                  # 恢复后强制补一次 catch-up
            self._cond.notify_all()
        self._audit("calc_resumed", "校时计算器已恢复，排队的重算请求开始处理", {})

    @property
    def paused(self) -> bool:
        return self._paused

    def trigger(self) -> None:
        """接收服务数据变化时调用（非阻塞）。"""
        with self._cond:
            if self._stopped:
                return
            self._pending = True
            self._cond.notify_all()

    def shutdown(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        self._pool.shutdown(wait=False)

    def reset(self) -> None:
        """场景重置：清空全部计算状态（在途作业靠水位栅栏自然失效）。"""
        with self._cond:
            self.jobs.clear()
            self.versions.clear()
            self._group_cache.clear()
            self._job_counter = 0
            self._version_counter = 0
            self._last_accepted_watermark = -1
            self._pending = False
            self._paused = False
            self._cond.notify_all()

    # ---------- 主循环 ----------

    def _loop(self) -> None:
        while True:
            with self._cond:
                # 单层条件循环：等待直到停止，或出现待处理触发且未暂停。
                # 不使用嵌套 wait，避免信号被内层吃掉后外层永久阻塞。
                while (not self._stopped
                       and (not self._pending or self._paused)):
                    self._cond.wait(timeout=1.0)
                if self._stopped:
                    return
                self._pending = False
                self._job_counter += 1
                job_id = self._job_counter
                watermark = self.receiver.watermark()
                if job_id in self.test_delay_overrides:
                    delay_ms = self.test_delay_overrides[job_id]
                else:
                    # 少量作业注入较长耗时，制造“旧结果晚到”的确定性演示条件
                    long = self._rng.random() < 0.25
                    delay_ms = (self._rng.uniform(0.6, 1.4) if long
                                else self._rng.uniform(0.05, 0.35)) * 1000.0
                job = CalcJob(job_id=job_id, created_at_ms=self.ref.now_ms(),
                              watermark=watermark, compute_delay_ms=delay_ms)
                self.jobs.append(job)
            # 线程池专门用于计算作业，主循环自身不占池内槽位
            self._pool.submit(self._run_job, job)

    def _run_job(self, job: CalcJob) -> None:
        # 模拟计算耗时：这是旧作业可能晚于新作业完成的根因
        threading.Event().wait(job.compute_delay_ms / 1000.0)
        try:
            version = self._compute(job)
        except Exception as exc:  # 防御：计算异常不能杀死工作线程
            with self._cond:
                job.status = "rejected"
                job.detail = f"计算异常: {exc!r}"
            self._audit("calc_error", f"作业 {job.job_id} 计算异常", {"error": repr(exc)})
            return

        with self._cond:
            # 版本栅栏：只接受水位更高的计算结果
            if job.watermark <= self._last_accepted_watermark:
                job.status = "rejected"
                job.detail = (f"水位 {job.watermark} 不高于已接受水位 "
                              f"{self._last_accepted_watermark}，迟到的旧结果整版拒绝")
                version.rejected = True
                version.reject_reason = job.detail
                version.job_id = job.job_id
                self.versions.append(version)
                self._audit("calc_stale_rejected",
                            f"作业 {job.job_id} 的旧计算结果晚到，被版本栅栏拒绝",
                            {"job_id": job.job_id, "watermark": job.watermark,
                             "accepted_watermark": self._last_accepted_watermark})
                return

            self._version_counter += 1
            version.version = self._version_counter
            version.job_id = job.job_id
            self._last_accepted_watermark = job.watermark
            self.versions.append(version)
            job.status = "accepted"
            job.version = version.version
            job.detail = version.strategy_reason
        self._audit(
            "calc_version",
            f"计算器发布候选版本 v{version.version}（{version.strategy}，"
            f"{len(version.records)} 条，边界 {len(version.boundaries)} 个）",
            {"version": version.version, "strategy": version.strategy,
             "watermark": job.watermark},
        )
        self._on_version(version)

    # ---------- 纯计算部分 ----------

    def _compute(self, job: CalcJob) -> TimelineVersion:
        measurements = self.receiver.all_measurements()
        calibrations = self.receiver.all_calibrations()

        groups_m: dict[tuple[str, int], list[Measurement]] = {}
        for m in measurements:
            groups_m.setdefault((m.device_id, m.boot_epoch), []).append(m)
        groups_c: dict[tuple[str, int], list[CalibrationPoint]] = {}
        for c in calibrations:
            groups_c.setdefault((c.device_id, c.boot_epoch), []).append(c)

        all_records = []
        recomputed_ids: list[str] = []
        strategies: list[str] = []
        reasons: list[str] = []
        new_cache: dict[tuple[str, int], _GroupCache] = {}

        for key in sorted(set(groups_m) | set(groups_c)):
            ms = sorted(groups_m.get(key, []),
                        key=lambda m: (m.boot_epoch, m.seq))
            cals = sorted(groups_c.get(key, []), key=lambda c: c.device_time_ms)
            recs, strategy, reason, recomputed, cache = self._compute_group(
                key, ms, cals, self._group_cache.get(key))
            all_records.extend(recs)
            recomputed_ids.extend(sorted(recomputed))
            if strategy != "unchanged":
                strategies.append(strategy)
                reasons.append(f"{key[0]}#b{key[1]}: {reason}")
            new_cache[key] = cache

        ordered, boundaries = build_timeline(all_records)
        self._group_cache = new_cache

        if not strategies:
            global_strategy = "full_rebuild"
            reason_text = "无有效变化的防御性重建"
        elif all(s == "local_recompute" for s in strategies):
            global_strategy = "local_recompute"
        elif all(s == "full_rebuild" for s in strategies):
            global_strategy = "full_rebuild"
        else:
            global_strategy = "mixed"
        reason_text = "；".join(reasons)

        return TimelineVersion(
            version=0,  # 接受时由栅栏部分赋号
            created_at_ms=self.ref.now_ms(),
            watermark_seq=job.watermark,
            records=ordered,
            boundaries=boundaries,
            strategy=global_strategy,
            strategy_reason=reason_text,
            recomputed_measurement_ids=recomputed_ids,
            calibration_ids=[c.id for c in calibrations],
            job_id=job.job_id,
            compute_delay_ms=job.compute_delay_ms,
        )

    def _compute_group(self, key: tuple[str, int],
                       measurements: list[Measurement],
                       calibrations: list[CalibrationPoint],
                       prev: Optional[_GroupCache]):
        """单分组计算，返回 (校正记录列表, 策略, 原因, 重算id集合, 新缓存)。"""
        device_id, boot_epoch = key
        cal_set = frozenset(c.id for c in calibrations)
        mapping = Mapping(device_id, boot_epoch, calibrations)
        cache = _GroupCache(cal_set=cal_set, calibrations=list(calibrations),
                            records={})

        if prev is None:
            recs = [calibrate_record(m, mapping) for m in measurements]
            cache.records = {r.measurement_id: r for r in recs}
            cache.last_strategy = "full_rebuild"
            return recs, "full_rebuild", "新启动分组，首次构建整段时间轴", \
                {r.measurement_id for r in recs}, cache

        prev_records: dict = dict(prev.records)
        cache.records = prev_records

        if cal_set == prev.cal_set:
            # 映射未变：仅校正新到达的测量记录
            new_ms = [m for m in measurements if m.id not in prev_records]
            for m in new_ms:
                prev_records[m.id] = calibrate_record(m, mapping)
            cache.last_strategy = "local_recompute"
            if not new_ms:
                return list(prev_records.values()), "unchanged", "分组无变化", set(), cache
            return (list(prev_records.values()), "local_recompute",
                    "校时映射未变，仅对新增测量做局部校正",
                    {m.id for m in new_ms}, cache)

        # 校时集合变化：判断是否为“两点之间插入一个新校时点”的局部重算场景
        added = cal_set - prev.cal_set
        interior = False
        span_lo = span_hi = None
        if len(added) == 1:
            new_cal = next(c for c in calibrations if c.id in added)
            pts = sorted(calibrations, key=lambda c: c.device_time_ms)
            idx = next(i for i, c in enumerate(pts) if c.id == new_cal.id)
            if 0 < idx < len(pts) - 1:
                interior = True
                span_lo = pts[idx - 1].device_time_ms
                span_hi = pts[idx + 1].device_time_ms

        known_ids = set(prev.records)
        new_ms = [m for m in measurements if m.id not in known_ids]

        if interior:
            affected = {m.id for m in measurements
                        if span_lo <= m.device_time_ms <= span_hi}
            affected |= {m.id for m in new_ms}
            for m in measurements:
                if m.id in affected:
                    prev_records[m.id] = calibrate_record(m, mapping)
            cache.last_strategy = "local_recompute"
            return (list(prev_records.values()), "local_recompute",
                    f"新校时点落在已知两点之间，仅重算设备时间 "
                    f"[{span_lo:.0f}, {span_hi:.0f}] 区间内的 {len(affected)} 条记录",
                    affected, cache)

        # 其余结构性变化（新增首/末校时点改变外推区、批量补校等）：整段重建
        recs = [calibrate_record(m, mapping) for m in measurements]
        cache.records = {r.measurement_id: r for r in recs}
        cache.last_strategy = "full_rebuild"
        label = "新增首末校时点，外推区间斜率改变" if len(added) == 1 \
            else f"校时集合结构性变化（+{len(added)} 点），外推/插值区间整体改变"
        return recs, "full_rebuild", label, {r.measurement_id for r in recs}, cache

    # ---------- 查询 ----------

    def latest_version(self) -> Optional[TimelineVersion]:
        with self._cond:
            accepted = [v for v in self.versions if not v.rejected]
            return accepted[-1] if accepted else None

    def version_by_number(self, number: int) -> Optional[TimelineVersion]:
        with self._cond:
            for v in self.versions:
                if v.version == number and not v.rejected:
                    return v
            return None

    def snapshot(self) -> dict:
        with self._cond:
            return {
                "paused": self._paused,
                "last_accepted_watermark": self._last_accepted_watermark,
                "jobs": [j.to_dict() for j in reversed(self.jobs)],
                "versions": [v.summary() for v in reversed(self.versions)],
            }
