"""端到端流水线测试：重启复用序号、跨午夜、版本栅栏、重算策略、发布自洽。"""

import time
import unittest

from timesync.clocks import ReferenceClock, DeviceSimulator
from timesync.receiver import Receiver
from timesync.calculator import Calculator
from timesync.publisher import Publisher
from timesync.app import App


def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestRestartAndReusedSequence(unittest.TestCase):
    def setUp(self):
        self.ref = ReferenceClock()
        self.receiver = Receiver(self.ref)
        self.calc = Calculator(self.ref, self.receiver, max_workers=2, seed=1)
        self.receiver._on_change = self.calc.trigger
        self.pub = Publisher(self.ref)
        self.calc._on_version = self.pub.try_auto_confirm
        self.calc.start()
        self.sim = DeviceSimulator(self.ref, "DEV-X", initial_offset_ms=100.0,
                                   initial_rate=1.0, interval_ms=100.0)

    def tearDown(self):
        self.calc.shutdown()

    def _emit(self):
        m = self.sim.emit(self.receiver.next_measurement_id)
        self.receiver.accept_measurement(m)

    def test_restart_reuses_seq_but_records_stay_distinct(self):
        self.receiver.accept_calibration("DEV-X", 1,
                                         self.sim.clock.read(), self.ref.now_ms())
        self._emit()
        self._emit()
        boot1 = self.sim.restart()
        self.assertEqual(boot1, 2)
        self._emit()  # 重启后的第一条，seq 重新为 1

        self.assertTrue(wait_until(
            lambda self=self: self.calc.latest_version() is not None
            and len(self.calc.latest_version().records) == 3))

        v = self.calc.latest_version()
        keys = {(r.device_id, r.boot_epoch, r.seq) for r in v.records}
        self.assertIn(("DEV-X", 1, 1), keys)
        self.assertIn(("DEV-X", 1, 2), keys)
        self.assertIn(("DEV-X", 2, 1), keys)   # 复用的 seq=1，boot 不同，不冲突

        # 新 boot 还没有校时：该记录必须是 uncalibrated 且产生边界
        new_boot = next(r for r in v.records if r.boot_epoch == 2)
        self.assertEqual(new_boot.calibration_state, "uncalibrated")
        self.assertIsNone(new_boot.corrected_time_ms)
        self.assertTrue(any(b.kind == "uncalibrated" for b in v.boundaries))

        # 给新 boot 补校后，记录变为可校正且边界消失
        self.receiver.accept_calibration("DEV-X", 2,
                                         self.sim.clock.read(), self.ref.now_ms())
        self.assertTrue(wait_until(
            lambda self=self: not any(
                r.calibration_state == "uncalibrated"
                for r in self.calc.latest_version().records)))
        v2 = self.calc.latest_version()
        self.assertTrue(all(b.kind != "uncalibrated" for b in v2.boundaries))
        self.assertGreater(v2.version, v.version)


class TestStaleJobFence(unittest.TestCase):
    def setUp(self):
        self.ref = ReferenceClock()
        self.receiver = Receiver(self.ref)
        self.events = []
        self.calc = Calculator(
            self.ref, self.receiver,
            audit=lambda k, m, d=None: self.events.append((k, m)),
            max_workers=2, seed=7)
        self.receiver._on_change = self.calc.trigger
        self.pub = Publisher(self.ref, audit=lambda k, m, d=None: None)
        self.calc._on_version = self.pub.try_auto_confirm
        self.calc.start()
        self.sim = DeviceSimulator(self.ref, "DEV-F", interval_ms=100.0)
        self.receiver.accept_calibration("DEV-F", 1,
                                         self.sim.clock.read(), self.ref.now_ms())

    def tearDown(self):
        self.calc.shutdown()

    def test_old_result_arriving_late_is_rejected_not_published(self):
        # 运行态下让 job #2 强制长耗时：它先入队、后完成；期间新数据产生
        # 更高水位的 job #3 先被接受，job #2 晚到时必须被版本栅栏整版拒绝
        self.assertTrue(wait_until(
            lambda: self.calc.latest_version() is not None, timeout=5.0))
        self.calc.test_delay_overrides[2] = 1500.0

        # 触发 job #2（低水位）
        m = self.sim.emit(self.receiver.next_measurement_id)
        self.receiver.accept_measurement(m)
        time.sleep(0.3)
        # 等 job #2 已开始计算后再制造更高水位的 job #3
        for _ in range(4):
            m = self.sim.emit(self.receiver.next_measurement_id)
            self.receiver.accept_measurement(m)
            time.sleep(0.05)

        self.assertTrue(wait_until(
            lambda self=self: any(k == "calc_stale_rejected" for k, _ in self.events),
            timeout=8.0), "应至少出现一次旧结果被版本栅栏拒绝")

        # 被拒版本在版本列表里留痕且不会成为发布版
        rejected = [v for v in self.calc.versions if v.rejected]
        self.assertTrue(rejected)
        for v in rejected:
            self.assertNotEqual(
                self.pub.current_version.version if self.pub.current_version else None,
                v.version)

        # 发布版水位单调不减且自洽：记录数等于接收端测量总数
        latest = self.calc.latest_version()
        self.assertIsNotNone(latest)
        self.assertEqual(len(latest.records), len(self.receiver.all_measurements()))
        self.assertEqual(self.pub.current_version.version, latest.version)


class TestRecomputeStrategies(unittest.TestCase):
    def setUp(self):
        self.ref = ReferenceClock()
        self.receiver = Receiver(self.ref)
        self.calc = Calculator(self.ref, self.receiver, max_workers=1, seed=3)
        self.receiver._on_change = self.calc.trigger
        self.calc.start()
        self.sim = DeviceSimulator(self.ref, "DEV-R", initial_offset_ms=50.0,
                                   interval_ms=100.0)

    def tearDown(self):
        self.calc.shutdown()

    def _emit(self, n):
        for _ in range(n):
            m = self.sim.emit(self.receiver.next_measurement_id)
            self.receiver.accept_measurement(m)

    def test_interior_calibration_triggers_local_recompute(self):
        # 布置三个校时点 c1(0s) c3(100s) c2(200s)，先只放首尾 c1/c2，
        # 随后在中间插入 c3：受影响区间严格限定在 [0s, 200s] 的设备时间内
        now = self.ref.now_ms()
        dev = self.sim.clock
        self.receiver.accept_calibration("DEV-R", 1,
                                         dev.device_time_at(now), now)
        self.receiver.accept_calibration("DEV-R", 1,
                                         dev.device_time_at(now + 200_000),
                                         now + 200_000)
        # 4 条测量落在 0~200s 区间内
        self._emit(4)
        self.assertTrue(wait_until(
            lambda self=self: self.calc.latest_version() is not None
            and len(self.calc.latest_version().records) == 4))

        # 插入内部校时点 c3（设备时间约 now+100s），只重算它两侧邻居之间的记录
        self.receiver.accept_calibration(
            "DEV-R", 1, dev.device_time_at(now + 100_000), now + 100_000)
        self.assertTrue(wait_until(
            lambda self=self: any(
                not v.rejected and v.strategy == "local_recompute"
                and v.recomputed_measurement_ids
                for v in self.calc.versions), timeout=5.0))
        local_v = [v for v in self.calc.versions
                   if not v.rejected and v.strategy == "local_recompute"
                   and v.recomputed_measurement_ids][-1]
        # 受影响区间 [c1, c2] 内的旧记录被重算；它们是当时的全部记录，
        # 因此重算数等于旧记录数，但策略与原因必须明确标注为局部重算
        self.assertEqual(len(local_v.recomputed_measurement_ids), 4)
        self.assertIn("仅重算", local_v.strategy_reason)

        # 再产生新测量：映射未再变化，只做增量校正、零重算
        self._emit(2)
        self.assertTrue(wait_until(
            lambda self=self: self.calc.latest_version() is not None
            and len(self.calc.latest_version().records) == 6))
        incremental = [v for v in self.calc.versions
                       if not v.rejected and len(v.records) == 6][-1]
        self.assertEqual(incremental.strategy, "local_recompute")
        self.assertEqual(len(incremental.recomputed_measurement_ids), 2)
        self.assertIn("新增测量", incremental.strategy_reason)

    def test_endpoint_calibration_triggers_full_rebuild(self):
        now = self.ref.now_ms()
        self.receiver.accept_calibration(
            "DEV-R", 1, self.sim.clock.device_time_at(now), now)
        self._emit(3)
        self.assertTrue(wait_until(
            lambda self=self: self.calc.latest_version() is not None
            and len(self.calc.latest_version().records) == 3))

        # 新增一个更晚的末校时点，改变外推区 => 整段重建
        future = now + 120_000
        self.receiver.accept_calibration(
            "DEV-R", 1, self.sim.clock.device_time_at(future), future)
        self.assertTrue(wait_until(
            lambda self=self: any(
                not v.rejected and v.strategy == "full_rebuild"
                and v.version > 1 for v in self.calc.versions), timeout=5.0))


class TestPublishedVersionImmutability(unittest.TestCase):
    def test_published_snapshot_stays_consistent_after_new_data(self):
        app = App(scenario="default")
        app.start_drivers()
        try:
            self.assertTrue(wait_until(
                lambda: app.publisher.current_version is not None, timeout=6.0))
            v1 = app.publisher.current_version
            n1 = len(v1.records)
            snapshot_ids = [r.measurement_id for r in v1.records]

            self.assertTrue(wait_until(
                lambda: app.publisher.current_version.version > v1.version,
                timeout=8.0))

            # 旧发布版对象内容不变：记录集合与边界保持自洽
            self.assertEqual([r.measurement_id for r in v1.records], snapshot_ids)
            self.assertEqual(len(v1.records), n1)
            for r in v1.records:
                if r.calibration_state != "uncalibrated":
                    self.assertIsNotNone(r.based_on_cal_ids)
        finally:
            app.shutdown()


if __name__ == "__main__":
    unittest.main()
