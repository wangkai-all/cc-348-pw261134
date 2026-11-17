"""跨午夜场景与 HTTP 层冒烟测试。"""

import datetime as dt
import json
import threading
import time
import unittest
import urllib.request

from timesync.app import App
from timesync.server import build_server
from tests.test_pipeline import wait_until


def post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


class TestMidnightScenario(unittest.TestCase):
    def test_device_crosses_midnight_while_reference_does_not(self):
        app = App(scenario="midnight")
        app.start_drivers()
        try:
            self.assertTrue(wait_until(
                lambda: len(app.receiver.all_measurements()) >= 2, timeout=8.0))
            # 场景起点固定在 UTC 午夜前 90 秒，设备时钟再快 90 秒：
            # 最早一批测量的设备读数已在“明天”，而参考时钟仍在“今天”
            m = app.receiver.all_measurements()[0]
            dev_day = dt.datetime.fromtimestamp(m.device_time_ms / 1000.0,
                                                tz=dt.timezone.utc).date()
            ref_day = dt.datetime.fromtimestamp(m.true_time_ms / 1000.0,
                                                tz=dt.timezone.utc).date()
            self.assertEqual((dev_day - ref_day).days, 1)

            # 等计算器产出包含这些测量的版本：跨午夜记录被显式标记，
            # 且校正时间落在参考时钟日期上
            self.assertTrue(wait_until(
                lambda: app.calculator.latest_version() is not None
                and any(r.crosses_midnight
                        for r in app.calculator.latest_version().records),
                timeout=10.0))
            v = app.calculator.latest_version()
            crossed = [r for r in v.records if r.crosses_midnight]
            self.assertTrue(crossed, "应出现跨午夜标记的校正记录")
            for r in crossed:
                corrected_day = dt.datetime.fromtimestamp(
                    r.corrected_time_ms / 1000.0, tz=dt.timezone.utc).date()
                self.assertEqual(corrected_day, ref_day)
        finally:
            app.shutdown()


class TestHTTPSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = build_server(host="127.0.0.1", port=8765, scenario="default")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:8765"

    @classmethod
    def tearDownClass(cls):
        cls.server.app.shutdown()
        cls.server.shutdown()

    def test_state_and_static(self):
        state = get_json(f"{self.base}/api/state")
        self.assertEqual(len(state["devices"]), 2)
        self.assertIn("candidate", state)
        with urllib.request.urlopen(f"{self.base}/", timeout=5) as resp:
            self.assertIn("text/html", resp.headers["Content-Type"])

    def test_drift_change_and_calculator_pause_resume(self):
        post_json(f"{self.base}/api/devices/rate",
                  {"device_id": "DEV-A", "rate": 1.02})
        post_json(f"{self.base}/api/calculator/pause", {})
        state = get_json(f"{self.base}/api/state")
        self.assertTrue(state["calculator"]["paused"])
        post_json(f"{self.base}/api/calculator/resume", {})
        state = get_json(f"{self.base}/api/state")
        self.assertFalse(state["calculator"]["paused"])

    def test_backfill_calibration_and_manual_confirm(self):
        post_json(f"{self.base}/api/publisher/auto", {"enabled": False})
        # 等设备继续上传产生比当前发布版更新的候选版本
        time.sleep(2.5)
        state = get_json(f"{self.base}/api/state")
        pub_v = state["publisher"]["current_version"]
        cand_v = state["candidate"]["version"] if state["candidate"] else None
        # 手动确认当前候选：候选新于发布版时成功；同版本重推则被栅栏拒绝（两种都符合设计）
        result = post_json(f"{self.base}/api/publisher/confirm", {})
        if cand_v is not None and cand_v > (pub_v or 0):
            self.assertTrue(result["ok"])
        else:
            self.assertFalse(result["ok"])
            self.assertIn("栅栏", result["reason"])
        # 补一条迟到校时
        dev = state["devices"][0]
        result = post_json(f"{self.base}/api/calibrations", {
            "device_id": dev["device_id"],
            "boot_epoch": dev["boot_epoch"],
            "device_time_ms": dev["device_time_ms"] - 15_000,
            "ref_time_ms": state["server_time_ms"] - 15_000,
            "note": "测试补校",
        })
        self.assertTrue(result["ok"])
        self.assertTrue(result["calibration"]["is_backfill"])
        post_json(f"{self.base}/api/publisher/auto", {"enabled": True})


if __name__ == "__main__":
    unittest.main()
