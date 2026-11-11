import csv
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np

from zds_scope.capture import capture_archive
from capture_test_fixture import CaptureFixtureDriver
from zds_scope.protocol import parse_wfm, pack_wfm
from zds_scope.storage import ScopeStorage


class FullCaptureTests(unittest.TestCase):
    def test_user_confirmed_depth_combinations(self):
        from zds_scope.memory_depth import expected_depth_m
        for channels in ([1, 3], [2, 4], [3, 1]):
            self.assertEqual(expected_depth_m(channels), 512)
        for channels in ([1, 2], [1, 2, 3, 4], [2, 3, 4], [3, 4]):
            self.assertEqual(expected_depth_m(channels), 256)
        self.assertIsNone(expected_depth_m([1]))
        self.assertIsNone(expected_depth_m([1, 4]))

    def setUp(self):
        self.driver = CaptureFixtureDriver()
        self.driver.connect()

    def test_archive_contains_all_points_and_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, wfm, meas, _ = capture_archive(
                self.driver, [1, 2, 3, 4], base_dir=tmp,
                export_options={"legacy_full_bundle": True}
            )
            self.assertEqual(self.driver.get_run_state(), "Run")
            self.assertEqual(set(meas), {1, 2, 3, 4})
            root = Path(path)
            with np.load(root / "waveform_full.npz") as arrays:
                for ch, data in wfm.channels.items():
                    np.testing.assert_array_equal(arrays[f"ch{ch}_time_s"], data.time_array)
                    np.testing.assert_array_equal(arrays[f"ch{ch}_voltage_v"], data.voltage_array)
                    with (root / f"CH{ch}_full.csv").open() as stream:
                        rows = list(csv.DictReader(stream))
                    self.assertEqual(len(rows), len(data.raw_data))
                    self.assertEqual(float(rows[-1]["time_s"]), float(data.time_array[-1]))
            manifest = json.loads((root / "manifest.json").read_text())
            for name, info in manifest.items():
                self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), info["sha256"])
            with zipfile.ZipFile(path + ".zip") as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn("metadata.json", archive.namelist())
            wfm.close()

    def test_default_archive_saves_only_raw_and_metadata(self):
        """测试默认归档只保存紧凑原始数据与元数据，不重复生成庞大 CSV/NPZ/ZIP"""
        with tempfile.TemporaryDirectory() as tmp:
            path, wfm, meas, _ = capture_archive(self.driver, [1, 2], base_dir=tmp)
            root = Path(path)
            self.assertTrue((root / "waveform.wfm").exists())
            self.assertTrue((root / "metadata.json").exists())
            self.assertTrue((root / "measurements.json").exists())
            self.assertTrue((root / "screen.png").exists())
            # 默认不生成巨量 CSV、NPZ 与 ZIP
            self.assertFalse((root / "waveform.csv").exists())
            self.assertFalse((root / "waveform_full.npz").exists())
            self.assertFalse(Path(path + ".zip").exists())

            meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
            self.assertIn("integrity_verification", meta)
            self.assertEqual(meta["integrity_verification"]["integrity_status"], "PASS")
            self.assertIn("voltage_conversion_verification", meta)
            wfm.close()

    def test_storage_requirements_estimation(self):
        """测试存储容量与磁盘剩余空间预估"""
        with tempfile.TemporaryDirectory() as tmp:
            est_default = ScopeStorage.estimate_storage_requirements(
                num_channels=2, points_per_channel=512_000_000, options={}, target_dir=tmp
            )
            # 默认仅原始数据 ~ 1GB
            self.assertLess(est_default["estimated_total_bytes"], 2 * 1024 ** 3)

            est_full = ScopeStorage.estimate_storage_requirements(
                num_channels=2, points_per_channel=512_000_000,
                options={"export_full_csv": True, "export_channel_csv": True}, target_dir=tmp
            )
            # 若勾选全量 CSV，预估体积将达到数十 GB
            self.assertGreater(est_full["estimated_total_bytes"], 20 * 1024 ** 3)


    def test_failed_memory_read_restores_running_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(self.driver, "read_waveform", side_effect=RuntimeError("transfer interrupted")):
                with self.assertRaisesRegex(RuntimeError, "transfer interrupted"):
                    capture_archive(self.driver, [1], base_dir=tmp)
            self.assertEqual(self.driver.get_run_state(), "Run")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_truncated_file_rejected_and_original_bytes_preserved(self):
        wfm = self.driver.read_waveform([1])
        binary = pack_wfm(wfm.head, [(v.info, v.raw_data) for v in wfm.channels.values()])
        with self.assertRaises(ValueError):
            parse_wfm(binary[:-1])
        parsed = parse_wfm(binary + b"vendor-extension")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved.wfm"
            ScopeStorage.save_wfm(parsed, str(path))
            self.assertEqual(path.read_bytes(), binary + b"vendor-extension")

    def test_unequal_channel_lengths_keep_independent_time(self):
        wfm = self.driver.read_waveform([1, 2])
        data = wfm.channels[2]
        data.time_array = data.time_array[:3] + 1.25
        data.voltage_array = data.voltage_array[:3]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "full.csv"
            ScopeStorage.save_csv(wfm, str(path))
            with path.open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), len(wfm.channels[1].time_array))
            self.assertEqual(float(rows[0]["CH2_Time(s)"]), data.time_array[0])
            self.assertEqual(rows[3]["CH2_Time(s)"], "")

    def test_api_snapshot_uses_fresh_memory_capture(self):
        from fastapi.testclient import TestClient
        from zds_scope.server import app, state
        client = TestClient(app)
        with patch("zds_scope.server.ZDScopeDriver", CaptureFixtureDriver):
            client.post("/api/scope/connect", json={"host": "192.168.1.100"})
        state.last_wfm = state.driver.read_waveform([1])
        with tempfile.TemporaryDirectory() as tmp:
            def capture_in_temp(driver, **kwargs):
                return capture_archive(driver, base_dir=tmp, **kwargs)
            with patch("zds_scope.server.capture_archive", side_effect=capture_in_temp):
                response = client.post("/api/scope/save_snapshot", json={"channels": [3, 4]})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual(set(result["points"]), {"3", "4"})
                self.assertTrue(Path(result["zip_path"]).exists())
                invalid = client.post("/api/scope/save_snapshot", json={"channels": [7]})
                self.assertEqual(invalid.status_code, 422)
                state.last_wfm.close()


    def test_waveform_disk_stream_and_cancellation(self):
        """测试磁盘直写流与传输取消机制（保留取消文件）"""
        from zds_scope.driver import ScopeTransferCancelled

        class FakeCancelToken:
            is_cancelled = True

        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "streamed.wfm")
            # 正常流式写入
            total_len, wfm = self.driver.read_waveform_to_file(
                channels=[1, 2], target_path=target, mode="MEMOry"
            )
            self.assertGreater(total_len, 0)
            self.assertTrue(Path(target).exists())
            wfm.close()

            # 取消写入
            target_cancel = str(Path(tmp) / "streamed_cancel.wfm")
            with self.assertRaises(ScopeTransferCancelled):
                self.driver.read_waveform_to_file(
                    channels=[1], target_path=target_cancel, mode="MEMOry",
                    cancel_token=FakeCancelToken()
                )
            self.assertTrue(Path(target_cancel + ".cancelled").exists())

    def test_waveform_conversion_verification_exactness(self):
        """测试电压换算校验算法与示波器硬件 DSP 测量的一致性"""
        from zds_scope.protocol import verify_waveform_conversion
        wfm = self.driver.read_waveform([1, 2])
        meas = {
            1: self.driver.get_measurements(1),
            2: self.driver.get_measurements(2)
        }
        res = verify_waveform_conversion(wfm, meas)
        self.assertIn(1, res)
        self.assertIn(2, res)
        self.assertEqual(res[1]["verification_status"], "MATCH")
        self.assertLess(res[1]["vpp_error_pct"], 15.0)

    def test_scope_actual_configuration_query(self):
        """测试导出前示波器真实硬件配置查询"""
        cfg = self.driver.get_scope_actual_configuration()
        self.assertIn("enabled_channels", cfg)
        self.assertIn("mdepth_raw", cfg)
        self.assertIn("sample_rate_hz", cfg)
        self.assertGreater(cfg["sample_rate_hz"], 0)


if __name__ == "__main__":
    unittest.main()

