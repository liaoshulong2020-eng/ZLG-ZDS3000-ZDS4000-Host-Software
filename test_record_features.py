import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from zds_scope.driver import ZDScopeDriver, ScopeCommunicationError
from zds_scope.instrument_settings import validate_write
from zds_scope.protocol import WfmFile, WfmHeadInfo, WfmItemInfo, ChannelWaveformData
from zds_scope.records import export_region, scan_record, save_project, load_project


def record(raw=None, probe_type=0, start=1.0, dt=.1):
    raw = np.arange(120, 140, dtype=np.uint8) if raw is None else np.asarray(raw, dtype=np.uint8)
    item = WfmItemInfo(channel=0, vert_division=.1, probe_type=probe_type)
    return WfmFile(WfmHeadInfo(sample_rate=1/dt, start_time=start),
                   {1: ChannelWaveformData(1, item, raw, start_time=start, dt=dt)})


class RecordFeatureTests(unittest.TestCase):
    def test_empty_waveform_reports_not_ready(self):
        driver = ZDScopeDriver()
        with patch.object(driver, "read_exact_bytes", return_value=b"\0" * 4):
            with self.assertRaisesRegex(ScopeCommunicationError, "当前记录尚未就绪"):
                driver._read_waveform_length()

    def test_large_realtime_payload_is_not_allocated(self):
        driver = ZDScopeDriver()
        with patch.object(driver, "send"), patch.object(driver, "disconnect") as disconnect, \
             patch.object(driver, "_read_waveform_length", return_value=420000784), \
             patch.object(driver, "read_exact_bytes") as read:
            with self.assertRaisesRegex(ScopeCommunicationError, "单次读取分块保存"):
                driver.read_waveform([1, 2, 3])
            read.assert_not_called()
            disconnect.assert_called_once()

    def test_waveform_display_does_not_materialize_full_float_arrays(self):
        from PySide6.QtWidgets import QApplication
        from zds_scope.ui.scope_plot import ScopePlotWidget
        app = QApplication.instance() or QApplication([])
        wfm = record(np.full(600000, 128, dtype=np.uint8))
        plot = ScopePlotWidget()
        plot.update_waveform(wfm)
        self.assertIsNone(wfm.channels[1]._time_array)
        self.assertIsNone(wfm.channels[1]._voltage_array)
        plot.close()

    def test_streaming_npz_preserves_units_and_values_without_full_arrays(self):
        from zds_scope.storage import ScopeStorage
        wfm = record(probe_type=1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.npz"
            ScopeStorage.save_npz_stream(wfm, path)
            with np.load(path) as saved:
                np.testing.assert_array_equal(saved["ch1_raw"], wfm.channels[1].raw_data)
                np.testing.assert_allclose(saved["ch1_time_s"], 1 + np.arange(20) * .1)
                self.assertIn("ch1_current_a", saved.files)
            self.assertIsNone(wfm.channels[1]._time_array)
            self.assertIsNone(wfm.channels[1]._voltage_array)

    def test_region_keeps_original_indices_time_units_and_every_point(self):
        wfm = record(probe_type=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = export_region(wfm, Path(tmp)/"region", [1], 1.1, 1.3, {"topology": "test"})
            with (Path(root)/"CH1_region.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([int(row["original_sample_index"]) for row in rows], [1, 2, 3])
            self.assertIn("derived_value_A", rows[0])
            self.assertAlmostEqual(float(rows[-1]["time_s"]), 1.3)
            metadata = json.loads((Path(root)/"region_metadata.json").read_text())
            self.assertFalse(metadata["downsampled"])
            self.assertEqual(metadata["channels"]["1"]["points"], 3)
            self.assertEqual(metadata["test_context"]["topology"], "test")
            self.assertIsNone(wfm.channels[1]._time_array)

    def test_invalid_region_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/"invalid"
            with self.assertRaises(ValueError):
                export_region(record(), target, [1], 4, 3)
            self.assertFalse(target.exists())

    def test_full_scan_finds_spike_at_chunk_boundary_without_full_arrays(self):
        raw = np.full(600000, 128, dtype=np.uint8)
        raw[262144] = 255
        wfm = record(raw, start=0, dt=1e-6)
        result = scan_record(wfm, {"channels": {"1": {"maximum": .05}}})
        summary = result["channels"]["1"]
        self.assertEqual(summary["scanned_points"], len(raw))
        self.assertTrue(summary["full_data_scanned"])
        self.assertTrue(any(event["sample_index"] == 262144 and event["kind"] == "isolated_spike_candidate"
                            for event in result["events"]))
        self.assertIsNone(wfm.channels[1]._time_array)
        self.assertIsNone(wfm.channels[1]._voltage_array)

    def test_missing_pulse_location_with_known_frequency(self):
        raw = np.where(np.arange(20000) % 100 < 20, 180, 100).astype(np.uint8)
        raw[9000:9100] = 100
        result = scan_record(record(raw, start=0, dt=1e-5), {"channels": {"1": {"frequency_hz": 1000}}})
        events = [event for event in result["events"] if event["kind"] == "missing_pulse_candidate"]
        self.assertTrue(any(event["sample_index"] == 9100 for event in events))

    def test_cancel_does_not_return_completed_report(self):
        with self.assertRaisesRegex(RuntimeError, "取消"):
            scan_record(record(), cancelled=lambda: True)

    def test_project_round_trip_restores_context_trigger_and_cursors(self):
        state = {"test_context": {"load": "10A"}, "cursors": {"x1": 1.1, "x2": 1.4},
                 "configuration": {"extended_settings": {":TRIGger:EDGE:SOURce": "CHANnel2"}}}
        with tempfile.TemporaryDirectory() as tmp:
            root = save_project(record(), Path(tmp)/"project", state)
            wfm, restored = load_project(root)
            try:
                self.assertEqual(restored, state)
                np.testing.assert_array_equal(wfm.channels[1].raw_data, np.arange(120, 140, dtype=np.uint8))
            finally:
                wfm.close()
            metadata = Path(root)/"project.json"
            metadata.write_text(metadata.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "校验失败"):
                load_project(root)

    def test_setting_validation_preserves_ch2_and_rejects_injected_commands(self):
        command = ":TRIGger:EDGE:SOURce"
        current = {command: "CHANnel2", ":CHANnel2:DELAy": "0", ":ACQuire:AVERages": "64"}
        self.assertEqual(validate_write(command, "CHANnel2", current), "CHANNEL2")
        for value in ("CHANnel1;:RUN", "CHANnel2\n:STOP"):
            with self.assertRaises(ValueError):
                validate_write(command, value, current)
        with self.assertRaises(ValueError):
            validate_write(":ACQuire:AVERages", "3", current)
        with self.assertRaises(ValueError):
            validate_write(":CHANnel2:DELAy", "1", current)

    def test_explicit_write_sends_only_requested_parameter(self):
        driver = ZDScopeDriver()
        driver.current_configuration = {"extended_settings": {":TRIGger:EDGE:SOURce": "CHANnel2"}}
        with patch.object(driver, "send") as send, patch.object(driver, "get_scope_actual_configuration", return_value={}) as read:
            driver.write_extended_setting(":TRIGger:EDGE:SOURce", "CHANnel2")
            send.assert_called_once_with(":TRIGger:EDGE:SOURce CHANNEL2")
            read.assert_called_once()

    def test_status_line_does_not_become_query_result_or_binary_length(self):
        driver = ZDScopeDriver()
        driver._is_connected = True
        driver.sock = MagicMock()
        data = bytearray(b'0,"No error"\r\nRun\r\n0,"No error"\r\n' + (544).to_bytes(4, "little") + b'WFM\0')

        def recv(size):
            chunk = bytes(data[:size])
            del data[:size]
            return chunk

        driver.sock.recv.side_effect = recv
        self.assertEqual(driver.get_run_state(), "Run")
        self.assertEqual(driver._read_waveform_length(), 544)
        self.assertEqual(driver.read_exact_bytes(4), b'WFM\0')

    def test_invalid_hardware_sentinel_is_not_a_real_measurement(self):
        driver = ZDScopeDriver()
        with patch.object(driver, "query", return_value="3.40282e+38"), patch.object(driver, "send"):
            measured = driver.get_measurements(1, wait_seconds=0)
        self.assertTrue(all(value is None for value in measured.values()))
        self.assertEqual(driver.last_measurement_responses[1]["vpp"]["response"], "3.40282e+38")


if __name__ == "__main__":
    unittest.main()
