"""
系统综合自动化单元测试脚本
覆盖通信协议编解码、文件存储、虚拟驱动仿真、测量摘要与 REST API 全流程
"""

import os
import unittest
from unittest.mock import patch
import numpy as np
from fastapi.testclient import TestClient

from zds_scope.protocol import (
    WfmHeadInfo, WfmItemInfo, pack_wfm, parse_wfm,
    unpack_screen_image, ScopeCommands
)
from zds_scope.storage import ScopeStorage
from capture_test_fixture import CaptureFixtureDriver
from zds_scope.server import app


class TestZLGSystem(unittest.TestCase):

    def test_removed_simulation_option_is_rejected(self):
        client = TestClient(app)
        with patch("zds_scope.server.ZDScopeDriver") as driver:
            response = client.post("/api/scope/connect", json={"use_mock": True})
            self.assertEqual(response.status_code, 422)
            driver.assert_not_called()

    def test_hardware_connection_failure_has_no_fallback(self):
        from zds_scope.server import state
        client = TestClient(app)
        previous = state.driver
        try:
            state.driver = None
            with patch("zds_scope.server.ZDScopeDriver") as driver:
                driver.return_value.connect.side_effect = TimeoutError("hardware timeout")
                response = client.post("/api/scope/connect", json={"host": "192.168.1.100"})
                self.assertEqual(response.status_code, 500)
                self.assertIn("hardware timeout", response.json()["detail"])
                driver.return_value.disconnect.assert_called_once()
                self.assertIsNone(state.driver)
                self.assertFalse(client.get("/api/scope/status").json()["connected"])
        finally:
            state.driver = previous

    def test_01_protocol_packing_and_parsing(self):
        """测试 WFM 二进制数据流打包与解析一致性"""
        head = WfmHeadInfo(dev_name="ZDS3024 Plus", hrzt_division=1e-6, sample_rate=1e8)
        item1 = WfmItemInfo(channel=0, vert_division=2.0, vert_offset=-1.0)
        raw1 = (128 + 40 * np.sin(np.linspace(0, 4 * np.pi, 500))).astype(np.uint8)

        wfm_bytes = pack_wfm(head, [(item1, raw1)])
        self.assertGreater(len(wfm_bytes), 424 + 120)

        parsed = parse_wfm(wfm_bytes)
        self.assertEqual(parsed.head.dev_name, "ZDS3024 Plus")
        self.assertIn(1, parsed.channels)
        ch1 = parsed.channels[1]
        self.assertEqual(len(ch1.voltage_array), 500)
        self.assertAlmostEqual(ch1.vpp, 6.4, delta=0.5)

    def test_02_mock_driver(self):
        """测试虚拟示波器驱动功能"""
        drv = CaptureFixtureDriver()
        self.assertTrue(drv.connect())
        self.assertIn("ZDS4054", drv.get_idn())
        self.assertEqual(drv.get_run_state(), "Run")

        # 读取波形
        wfm = drv.read_waveform(channels=[1, 2])
        self.assertIn(1, wfm.channels)
        self.assertIn(2, wfm.channels)
        self.assertGreater(len(wfm.channels[1].voltage_array), 1000)

        # 硬件测量
        meas = drv.get_measurements(1)
        self.assertGreater(meas["vpp"], 20.0)
        self.assertGreater(meas["freq"], 100e3)

        # 屏幕截图
        png = drv.capture_screen(fmt="PNG")
        self.assertGreater(len(png), 1000)

        # 环路分析
        loop = drv.get_loop_analysis()
        self.assertIn("pm", loop)
        self.assertIn("gm", loop)
        self.assertGreaterEqual(len(loop["table"]), 50)

    def test_03_storage(self):
        """测试数据存储与快照归档"""
        drv = CaptureFixtureDriver()
        drv.connect()
        wfm = drv.read_waveform(channels=[1, 2])
        meas = drv.get_measurements(1)
        loop = drv.get_loop_analysis()
        png = drv.capture_screen(fmt="PNG")

        snapshot_dir = ScopeStorage.save_snapshot(
            base_dir="data/captures",
            session_name="unittest_run",
            wfm=wfm,
            screenshot_bytes=png,
            measurements=meas,
            loop_data=loop,
            analysis_report_md="# Unit Test Report\nEverything passed.",
            export_options={"export_full_csv": True, "create_zip": True}
        )


        self.assertTrue(os.path.exists(os.path.join(snapshot_dir, "waveform.wfm")))
        self.assertTrue(os.path.exists(os.path.join(snapshot_dir, "waveform.csv")))
        self.assertTrue(os.path.exists(os.path.join(snapshot_dir, "screen.png")))
        self.assertTrue(os.path.exists(os.path.join(snapshot_dir, "measurements.json")))
        self.assertTrue(os.path.join(snapshot_dir, "bode_loop.csv"))
        self.assertTrue(os.path.join(snapshot_dir, "analysis_report.md"))


    def test_05_fastapi_rest_server(self):
        """测试 RESTful API 接口完整交互"""
        client = TestClient(app)

        # 连接虚拟设备
        with patch("zds_scope.server.ZDScopeDriver", CaptureFixtureDriver):
            r_conn = client.post("/api/scope/connect", json={"host": "192.168.1.100"})
        self.assertEqual(r_conn.status_code, 200)

        # 状态
        r_stat = client.get("/api/scope/status")
        self.assertTrue(r_stat.json()["connected"])

        # 控制
        r_ctrl = client.post("/api/scope/control", json={"action": "run"})
        self.assertEqual(r_ctrl.status_code, 200)

        # 获取波形
        r_wfm = client.get("/api/scope/waveform?channels=1,2&max_points=50")
        self.assertEqual(r_wfm.status_code, 200)
        self.assertIn("channels", r_wfm.json())

        # 获取测量
        r_meas = client.get("/api/scope/measurements?channel=1")
        self.assertEqual(r_meas.status_code, 200)
        self.assertIn("vpp", r_meas.json())

        # 已移除自动诊断接口
        r_diag = client.get("/api/ai/diagnose")
        self.assertEqual(r_diag.status_code, 404)


if __name__ == "__main__":
    unittest.main()
