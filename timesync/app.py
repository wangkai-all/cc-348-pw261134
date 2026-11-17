"""应用编排层：设备模拟驱动、组件装配、场景预设与审计日志。"""

from __future__ import annotations

import threading
import random
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .clocks import ReferenceClock, DeviceSimulator
from .receiver import Receiver
from .calculator import Calculator
from .publisher import Publisher


# 场景预设：两台设备的初始偏移 / 漂移率，以及网络延迟区间（毫秒）
SCENARIOS = {
    "default": {
        "label": "常规漂移",
        "devices": [
            {"device_id": "DEV-A", "offset_ms": 1500.0, "rate": 1.004,
             "net_delay": (40.0, 260.0)},
            {"device_id": "DEV-B", "offset_ms": -900.0, "rate": 0.997,
             "net_delay": (40.0, 260.0)},
        ],
        "midnight": False,
    },
    "midnight": {
        "label": "跨午夜校时",
        # 设备时钟比参考时钟快 90 秒，启动点选在 UTC 23:58:30 附近，
        # 设备读数很快越过午夜而参考时钟还在前一天
        "devices": [
            {"device_id": "DEV-A", "offset_ms": 90_000.0, "rate": 1.002,
             "net_delay": (30.0, 200.0)},
            {"device_id": "DEV-B", "offset_ms": 90_000.0, "rate": 0.998,
             "net_delay": (30.0, 200.0)},
        ],
        "midnight": True,
    },
    "extreme": {
        "label": "强漂移 + 高乱序",
        "devices": [
            {"device_id": "DEV-A", "offset_ms": 5_000.0, "rate": 1.015,
             "net_delay": (80.0, 900.0)},
            {"device_id": "DEV-B", "offset_ms": -4_000.0, "rate": 0.985,
             "net_delay": (80.0, 900.0)},
        ],
        "midnight": False,
    },
}


class AuditLog:
    def __init__(self, maxlen: int = 200) -> None:
        self._events: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._counter = 0

    def add(self, kind: str, msg: str, data: Optional[dict] = None) -> None:
        with self._lock:
            self._counter += 1
            self._events.append({
                "seq": self._counter,
                "kind": kind,
                "msg": msg,
                "data": data or {},
            })

    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._counter = 0


class DeviceDriver(threading.Thread):
    """设备上传驱动线程：按设备节奏产生测量片段并经模拟网络延迟后送达。"""

    def __init__(self, sim: DeviceSimulator, app: "App",
                 net_delay_ms: tuple[float, float], rng: random.Random) -> None:
        super().__init__(name=f"driver-{sim.device_id}", daemon=True)
        self.sim = sim
        self.app = app
        self.net_delay_ms = net_delay_ms
        self.rng = rng
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            if self.sim.due():
                m = self.sim.emit(self.app.receiver.next_measurement_id)
                delay = self.rng.uniform(*self.net_delay_ms) / 1000.0
                # 模拟网络传输：延迟后再投递，不同设备的片段因此可能乱序到达
                self.app.transport_pool.submit(self._deliver, m, delay)
            self._stop.wait(0.15)

    def _deliver(self, m, delay_s: float) -> None:
        if self._stop.wait(delay_s):
            return
        try:
            self.app.receiver.accept_measurement(m)
        except Exception:
            # 重置过程中组件可能已清空，忽略该批次在途片段
            pass


class App:
    def __init__(self, scenario: str = "default", seed: int = 2611) -> None:
        self.scenario_name = scenario
        self._rng = random.Random(seed)
        self.audit = AuditLog()
        self.transport_pool = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="transport")
        self.ref = ReferenceClock()
        self.receiver = Receiver(self.ref, on_change=self._on_data)
        self.calculator = Calculator(
            self.ref, self.receiver,
            audit=self.audit.add, on_version=self._on_version)
        self.publisher = Publisher(self.ref, audit=self.audit.add)
        self.sims: dict[str, DeviceSimulator] = {}
        self.drivers: list[DeviceDriver] = []
        self._net_delays: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._load_scenario(scenario, announce=False)
        self.calculator.start()

    # ---------- 内部回调 ----------

    def _on_data(self) -> None:
        self.calculator.trigger()

    def _on_version(self, version) -> None:
        self.publisher.try_auto_confirm(version)

    def _load_scenario(self, name: str, announce: bool = True) -> None:
        spec = SCENARIOS[name]
        if spec["midnight"]:
            # 锚定到“下一个 UTC 午夜前 90 秒”，保证演示窗口内发生跨午夜
            import datetime as dt
            now = self.ref.now_ms()
            today = dt.datetime.fromtimestamp(now / 1000.0, tz=dt.timezone.utc)
            midnight = (today + dt.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            anchor = midnight.timestamp() * 1000.0 - 90_000.0
            self.ref = ReferenceClock(epoch_ms=anchor)
        else:
            self.ref = ReferenceClock()

        self.sims.clear()
        for dspec in spec["devices"]:
            sim = DeviceSimulator(
                self.ref, dspec["device_id"],
                initial_offset_ms=dspec["offset_ms"],
                initial_rate=dspec["rate"],
                interval_ms=1200.0,
            )
            self.sims[sim.device_id] = sim
            self._net_delays[sim.device_id] = dspec["net_delay"]
            self._seed_calibration(sim)
        self.scenario_name = name
        if announce:
            self.audit.add("scenario", f"已切换到场景「{spec['label']}」", {"scenario": name})

    def _seed_calibration(self, sim: DeviceSimulator) -> None:
        """场景启动时给每个 boot 一条初始校时（否则首段全部 uncalibrated）。"""
        now = self.ref.now_ms()
        dev_now = sim.clock.device_time_at(now)
        cal = self.receiver.accept_calibration(
            device_id=sim.device_id, boot_epoch=sim.boot_epoch,
            device_time_ms=dev_now, ref_time_ms=now,
            is_backfill=False, note="场景初始校时")
        self.audit.add("cal_seed",
                       f"设备 {sim.device_id} 启动校时 {cal.id}："
                       f"设备读数 {dev_now:.0f} -> 参考 {now:.0f}",
                       {"cal_id": cal.id, "device": sim.device_id})

    def start_drivers(self) -> None:
        for sim in self.sims.values():
            delay_range = self._net_delays[sim.device_id]
            driver = DeviceDriver(sim, self, delay_range, self._rng)
            driver.start()
            self.drivers.append(driver)

    # ---------- 面向 HTTP 的操作 ----------

    def set_device_rate(self, device_id: str, rate: float) -> None:
        sim = self._sim(device_id)
        sim.set_rate(rate)
        self.audit.add("drift",
                       f"设备 {device_id} 漂移率调整为 {rate:.5f}（新增一段时钟折线）",
                       {"device": device_id, "rate": rate})

    def pause_device(self, device_id: str) -> None:
        sim = self._sim(device_id)
        sim.set_running(False)
        self.audit.add("device_pause", f"设备 {device_id} 已暂停上传", {"device": device_id})

    def resume_device(self, device_id: str) -> None:
        sim = self._sim(device_id)
        sim.set_running(True)
        self.audit.add("device_resume", f"设备 {device_id} 恢复上传", {"device": device_id})

    def restart_device(self, device_id: str) -> None:
        sim = self._sim(device_id)
        boot = sim.restart()
        self.audit.add("device_restart",
                       f"设备 {device_id} 重启：boot_epoch={boot}，片段序号从 1 重新计数",
                       {"device": device_id, "boot_epoch": boot})

    def add_calibration(self, device_id: str, boot_epoch: Optional[int],
                        device_time_ms: Optional[float], ref_time_ms: Optional[float],
                        note: str = "") -> dict:
        sim = self._sim(device_id)
        if boot_epoch is None:
            boot_epoch = sim.boot_epoch
        now = self.ref.now_ms()
        if ref_time_ms is None:
            ref_time_ms = now
        if device_time_ms is None:
            device_time_ms = sim.clock.device_time_at(ref_time_ms)
        # 设备时刻早于当前设备读数（容差 1ms）即视为迟到补校
        is_backfill = device_time_ms < sim.clock.read() - 1.0
        cal = self.receiver.accept_calibration(
            device_id, boot_epoch, device_time_ms, ref_time_ms,
            is_backfill=is_backfill, note=note or ("迟到补校" if is_backfill else "即时校时"))
        self.audit.add(
            "cal_add",
            f"收到校时 {cal.id}（{cal.device_id} boot {boot_epoch}，"
            f"{'迟到补校' if is_backfill else '即时校时'}）",
            {"cal_id": cal.id, "backfill": is_backfill,
             "device_time_ms": device_time_ms, "ref_time_ms": ref_time_ms})
        return cal.to_dict()

    def confirm_current_candidate(self) -> dict:
        version = self.calculator.latest_version()
        if version is None:
            return {"ok": False, "reason": "当前没有候选版本"}
        ok = self.publisher.confirm(version)
        return {"ok": ok, "version": version.version,
                "reason": "" if ok else "发布栅栏拒绝（候选版本不新于当前发布版）"}

    def reset(self, scenario: Optional[str] = None) -> None:
        """全量重置：停驱动、清接收/计算/发布状态并重建场景。"""
        for d in self.drivers:
            d.stop()
        self.drivers.clear()
        with self._lock:
            self.receiver.reset()
            self.calculator.reset()
            self.publisher.reset()
            self.audit.clear()
            self._load_scenario(scenario or self.scenario_name, announce=False)
            self.calculator.trigger()
        self.audit.add("reset", "系统已重置，原始记录与全部版本清空",
                       {"scenario": self.scenario_name})
        self.start_drivers()

    def shutdown(self) -> None:
        for d in self.drivers:
            d.stop()
        self.calculator.shutdown()
        self.transport_pool.shutdown(wait=False)

    # ---------- 查询 ----------

    def _sim(self, device_id: str) -> DeviceSimulator:
        if device_id not in self.sims:
            raise KeyError(f"未知设备 {device_id}")
        return self.sims[device_id]

    def state(self) -> dict:
        latest = self.calculator.latest_version()
        published = self.publisher.current_version
        return {
            "server_time_ms": self.ref.now_ms(),
            "scenario": self.scenario_name,
            "scenario_label": SCENARIOS[self.scenario_name]["label"],
            "devices": [s.status() for s in self.sims.values()],
            "receiver": self.receiver.snapshot(measurements_limit=150),
            "calculator": self.calculator.snapshot(),
            "publisher": self.publisher.snapshot(),
            "candidate": latest.to_dict(full=True) if latest else None,
            "published": published.to_dict(full=True) if published else None,
            "audit": list(reversed(self.audit.events()))[:120],
        }
