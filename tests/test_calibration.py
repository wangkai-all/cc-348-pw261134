"""校时数学的单元测试：插值、外推、无校时、不确定度、边界检测。"""

import unittest

from timesync.model import Measurement, CalibrationPoint, CorrectedRecord
from timesync.calibration import (
    Mapping, build_mappings, calibrate_record, calibrate_all, build_timeline,
    CAL_POINT_HALF_WIDTH_MS, EXTRAPOLATION_SLOPE_PPM,
)


def cal(cid, dev_t, ref_t, device="DEV-A", boot=1):
    return CalibrationPoint(id=cid, device_id=device, boot_epoch=boot,
                            device_time_ms=dev_t, ref_time_ms=ref_t,
                            received_at_ms=ref_t)


def meas(mid, dev_t, true_t, arrival_seq, device="DEV-A", boot=1, seq=1):
    return Measurement(id=mid, device_id=device, boot_epoch=boot, seq=seq,
                       device_time_ms=dev_t, true_time_ms=true_t,
                       arrival_time_ms=true_t + 100, arrival_seq=arrival_seq)


class TestMapping(unittest.TestCase):
    def test_interpolation_linear(self):
        # 设备时钟走快 1%：dev 1000ms 对应 ref 1010ms
        m = Mapping("DEV-A", 1, [cal("c1", 0.0, 10.0), cal("c2", 1000.0, 1010.0)])
        corrected, lo, hi, state, ids = m.correct(500.0)
        self.assertAlmostEqual(corrected, 510.0)
        self.assertEqual(state, "interpolated")
        self.assertEqual(ids, ["c1", "c2"])
        self.assertAlmostEqual(hi - lo, 2 * CAL_POINT_HALF_WIDTH_MS)

    def test_exact_calibration_point(self):
        m = Mapping("DEV-A", 1, [cal("c1", 100.0, 200.0), cal("c2", 300.0, 400.0)])
        corrected, lo, hi, state, ids = m.correct(100.0)
        self.assertEqual(corrected, 200.0)
        self.assertEqual(state, "calibrated")
        self.assertEqual(ids, ["c1"])

    def test_extrapolation_uncertainty_grows_with_distance(self):
        m = Mapping("DEV-A", 1, [cal("c1", 1000.0, 2000.0), cal("c2", 2000.0, 3000.0)])
        near = m.correct(2100.0)
        far = m.correct(1_002_000.0)
        self.assertEqual(near[3], "extrapolated")
        self.assertEqual(far[3], "extrapolated")
        self.assertLess(near[2] - near[1], far[2] - far[1])
        # 外推 1000 秒时半宽至少包含 ppm 距离项
        expected_extra = 1_000_000.0 * EXTRAPOLATION_SLOPE_PPM / 1e6
        self.assertGreaterEqual((far[2] - far[1]) / 2,
                                CAL_POINT_HALF_WIDTH_MS + expected_extra - 1e-9)

    def test_pre_first_extrapolation_uses_first_segment_rate(self):
        m = Mapping("DEV-A", 1, [cal("c1", 1000.0, 5000.0), cal("c2", 2000.0, 5500.0)])
        corrected, *_ = m.correct(0.0)
        # 斜率 0.5：dev=0 比 c1 早 1000ms => ref 早 500ms
        self.assertAlmostEqual(corrected, 4500.0)

    def test_uncalibrated_boot(self):
        m = Mapping("DEV-A", 2, [])
        self.assertTrue(m.empty)
        corrected, lo, hi, state, ids = m.correct(12345.0)
        self.assertIsNone(corrected)
        self.assertEqual(state, "uncalibrated")
        self.assertEqual(ids, [])

    def test_late_calibration_sorted_by_device_time(self):
        # 校时记录乱序到达，映射必须按设备时间排序使用
        m = Mapping("DEV-A", 1, [
            cal("c2", 2000.0, 3000.0),
            cal("c1", 1000.0, 2000.0),
            cal("c3", 3000.0, 4000.0),
        ])
        corrected, _, _, state, ids = m.correct(1500.0)
        self.assertAlmostEqual(corrected, 2500.0)
        self.assertEqual(state, "interpolated")
        self.assertEqual(ids, ["c1", "c2"])


class TestTimelineBoundaries(unittest.TestCase):
    def _rec(self, mid, corrected, half, arrival_seq, device, boot=1, seq=1,
             state="interpolated"):
        return CorrectedRecord(
            measurement_id=mid, device_id=device, boot_epoch=boot, seq=seq,
            device_time_ms=corrected, corrected_time_ms=corrected,
            interval_lo_ms=corrected - half, interval_hi_ms=corrected + half,
            uncertainty_ms=half, calibration_state=state,
            arrival_seq=arrival_seq, arrival_time_ms=corrected)

    def test_non_overlapping_intervals_have_no_boundary(self):
        recs = [
            self._rec("m1", 1000.0, 2.0, arrival_seq=2, device="DEV-A"),
            self._rec("m2", 2000.0, 2.0, arrival_seq=1, device="DEV-B"),
        ]
        ordered, boundaries = build_timeline(recs)
        self.assertEqual([r.measurement_id for r in ordered], ["m1", "m2"])
        self.assertEqual(boundaries, [])
        # 到达序与校正序不同的实例被显式标出
        self.assertNotEqual(ordered[0].arrival_rank_in_timeline, 0)

    def test_overlapping_intervals_produce_boundary(self):
        recs = [
            self._rec("m1", 1000.0, 10.0, arrival_seq=1, device="DEV-A"),
            self._rec("m2", 1005.0, 10.0, arrival_seq=2, device="DEV-B"),
        ]
        _, boundaries = build_timeline(recs)
        self.assertEqual(len(boundaries), 1)
        self.assertEqual(boundaries[0].kind, "overlap")

    def test_uncalibrated_records_go_last_and_make_boundary(self):
        recs = [
            self._rec("m1", 1000.0, 2.0, arrival_seq=1, device="DEV-A"),
            CorrectedRecord(
                measurement_id="m2", device_id="DEV-B", boot_epoch=2, seq=1,
                device_time_ms=999.0, corrected_time_ms=None,
                interval_lo_ms=None, interval_hi_ms=None, uncertainty_ms=None,
                calibration_state="uncalibrated", arrival_seq=2,
                arrival_time_ms=1100.0),
        ]
        ordered, boundaries = build_timeline(recs)
        self.assertEqual(ordered[-1].measurement_id, "m2")
        self.assertEqual(len(boundaries), 1)
        self.assertEqual(boundaries[0].kind, "uncalibrated")

    def test_same_device_sequence_is_authoritative_order(self):
        # 即使到达序颠倒，同 boot 内 seq 小的必须排在前面
        records = [
            meas("m2", dev_t=2000.0, true_t=2100.0, arrival_seq=99, seq=2),
            meas("m1", dev_t=1000.0, true_t=1100.0, arrival_seq=1, seq=1),
        ]
        mappings = build_mappings([cal("c1", 0.0, 100.0), cal("c2", 3000.0, 3100.0)])
        corrected = calibrate_all(records, mappings)
        ordered, _ = build_timeline(corrected)
        self.assertEqual([r.measurement_id for r in ordered], ["m1", "m2"])

    def test_arrival_order_never_used_for_sorting(self):
        # 两台设备：DEV-B 的片段先到达但真实时刻更晚
        records = [
            meas("mA", dev_t=1000.0, true_t=1100.0, arrival_seq=2,
                 device="DEV-A", seq=1),
            meas("mB", dev_t=5000.0, true_t=5100.0, arrival_seq=1,
                 device="DEV-B", seq=1),
        ]
        cals = [
            cal("cA", 0.0, 100.0, device="DEV-A"),
            cal("cA2", 9000.0, 9100.0, device="DEV-A"),
            cal("cB", 4000.0, 4100.0, device="DEV-B"),
            cal("cB2", 9000.0, 9100.0, device="DEV-B"),
        ]
        corrected = calibrate_all(records, build_mappings(cals))
        ordered, boundaries = build_timeline(corrected)
        self.assertEqual(ordered[0].measurement_id, "mA")
        self.assertEqual(boundaries, [])


if __name__ == "__main__":
    unittest.main()
