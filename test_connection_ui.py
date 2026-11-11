"""GUI connection tests use isolated doubles; no real device is contacted."""
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from zds_scope.ui.main_window import MainWindow


class ConnectionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.driver = MagicMock()
        self.driver.device_info = "TEST,DEVICE"
        self.driver.get_scope_actual_configuration.return_value = {
            "run_state": "Stop", "timebase_scale_s": 0.002,
            "timebase_offset_s": 0.003, "mdepth_raw": "140000",
            "sample_rate_hz": 1e6,
            "channels": {1: {"enabled": False, "scale": 0.2, "offset": 0.75,
                             "coupling": "AC", "bw_limit": True, "probe": 10.0},
                         3: {"enabled": True, "scale": 2.0, "offset": -0.5}},
        }

        def connect():
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError("test did not release connection")

        self.driver.connect.side_effect = connect
        self.factory = patch("zds_scope.ui.main_window.ZDScopeDriver", return_value=self.driver)
        self.factory.start()
        self.window = MainWindow()

    def pump_until(self, condition):
        deadline = time.monotonic() + 4
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.assertTrue(condition())

    def tearDown(self):
        self.release.set()
        self.pump_until(lambda: self.window.connect_thread is None and self.window.control_thread is None
                        and self.window.settings_thread is None)
        self.window.close()
        self.app.processEvents()
        self.factory.stop()

    def test_connection_keeps_event_loop_responsive_and_does_not_auto_read(self):
        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        try:
            self.window._connect_scope()
            self.pump_until(lambda: self.entered.is_set() and len(ticks) >= 3)
            self.assertIsNotNone(self.window.connect_thread)
            self.assertFalse(self.window.txt_ip.isEnabled())
            worker = self.window.connect_thread
            self.window._connect_scope()
            self.assertIs(self.window.connect_thread, worker)
            self.release.set()
            self.pump_until(lambda: self.window.connect_thread is None)
            self.assertIs(self.window.driver, self.driver)
            self.assertTrue(self.window.txt_ip.isEnabled())
            self.driver.read_waveform.assert_not_called()
            self.driver.get_measurements.assert_not_called()
            self.driver.get_scope_actual_configuration.assert_called_once()
            self.assertEqual(self.window.control_panel.combo_tb_scale.currentData(), 0.002)
            self.assertFalse(self.window.control_panel.ch_widgets[1]["cb_enable"].isChecked())
            self.driver.set_channel.assert_not_called()
            self.driver.set_timebase.assert_not_called()
            self.driver.set_memory_depth.assert_not_called()
        finally:
            timer.stop()

    def test_refresh_connects_then_resumes_pending_read(self):
        self.window._read_waveform_from_scope('SCREEN')
        self.pump_until(self.entered.is_set)
        self.assertEqual(self.window._pending_refresh, 'SCREEN')
        with patch.object(self.window, '_read_waveform_from_scope') as read:
            self.release.set()
            self.pump_until(lambda: read.called)
            read.assert_called_once_with('SCREEN')

    def test_silent_failure_retries_and_manual_disconnect_stops(self):
        self.driver.connect.side_effect = TimeoutError('offline')
        with patch.object(QMessageBox, 'critical') as dialog:
            self.window._auto_reconnect = True
            self.window._retry_connection()
            self.pump_until(lambda: self.window.connect_thread is None)
            dialog.assert_not_called()
            self.assertTrue(self.window.reconnect_timer.isActive())
            self.window._disconnect_scope()
            self.assertFalse(self.window.reconnect_timer.isActive())
            self.assertFalse(self.window._auto_reconnect)

    def test_success_events_have_timestamps(self):
        self.window._stamp_event('读取完成')
        self.window._wfm_saved('sample.wfm')
        text = self.window.timestamp_label.text()
        self.assertIn('读取完成', text)
        self.assertIn('WFM 已保存', text)
        self.assertRegex(text, r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')

    def test_cancel_does_not_publish_connected_driver(self):
        self.window._connect_scope()
        self.pump_until(self.entered.is_set)
        self.window._toggle_connection()
        self.release.set()
        self.pump_until(lambda: self.window.connect_thread is None)
        self.assertIsNone(self.window.driver)
        self.driver.disconnect.assert_called_once()
        self.assertTrue(self.window.btn_connect.isEnabled())

    def test_failure_restores_connection_controls(self):
        self.driver.connect.side_effect = TimeoutError("connection timeout")
        with patch.object(QMessageBox, "critical") as dialog:
            self.window._connect_scope()
            self.pump_until(lambda: self.window.connect_thread is None)
            dialog.assert_called_once()
        self.assertIsNone(self.window.driver)
        self.assertTrue(self.window.txt_ip.isEnabled())
        self.assertTrue(self.window.btn_connect.isEnabled())
        self.driver.disconnect.assert_called_once()

    def test_close_during_connection_defers_without_destroying_running_thread(self):
        self.window.show()
        self.window._connect_scope()
        self.pump_until(self.entered.is_set)
        self.window.close()
        self.assertTrue(self.window._close_after_connect)
        self.assertIsNotNone(self.window.connect_thread)
        self.release.set()
        self.pump_until(lambda: self.window.connect_thread is None and not self.window.isVisible())
        self.assertIsNone(self.window.driver)

    def test_lost_connection_updates_badge_before_single(self):
        self.window.driver = self.driver
        self.window.lbl_status_badge.setText("已连接")
        self.driver.is_connected = False
        self.driver.last_error = "查询测量值超时"
        self.pump_until(lambda: self.window.lbl_status_badge.text() == "连接已断开")
        self.assertEqual("连接", self.window.btn_connect.text())
        with patch.object(QMessageBox, "warning") as warning:
            self.window._on_run_action("single")
            self.assertIn("查询测量值超时", warning.call_args.args[2])
        self.driver.single.assert_not_called()

    def test_single_executes_once_in_background(self):
        self.window.driver = self.driver
        self.driver.is_connected = True

        def state():
            self.entered.set()
            self.release.wait(3)
            return "Single"

        self.driver.get_run_state.side_effect = state
        self.window._on_run_action("single")
        self.pump_until(self.entered.is_set)
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        self.pump_until(lambda: bool(ticks))
        self.window._on_run_action("single")
        self.release.set()
        self.pump_until(lambda: self.window.control_thread is None)
        self.driver.single.assert_called_once()
        self.assertIn("Single", self.window.status_bar.currentMessage())

    def test_single_query_failure_reports_command_was_already_sent(self):
        self.window.driver = self.driver
        self.driver.is_connected = True

        def state():
            self.driver.is_connected = False
            self.driver.last_error = "run-state timeout"
            raise TimeoutError("run-state timeout")

        self.driver.get_run_state.side_effect = state
        self.window._on_run_action("single")
        self.pump_until(lambda: self.window.control_thread is None)
        self.driver.single.assert_called_once()
        self.assertEqual(self.window.lbl_status_badge.text(), "连接已断开")
        self.assertIn("命令已发送", self.window.status_bar.currentMessage())
        self.assertIn("run-state timeout", self.window.status_bar.currentMessage())

    def test_manual_settings_sync_runs_in_background_without_writes(self):
        self.window.driver = self.driver
        self.driver.is_connected = True
        config = self.driver.get_scope_actual_configuration.return_value

        def read():
            self.entered.set()
            self.release.wait(3)
            return config

        self.driver.get_scope_actual_configuration.side_effect = read
        self.window._sync_current_settings()
        self.pump_until(self.entered.is_set)
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        self.pump_until(lambda: bool(ticks))
        self.release.set()
        self.pump_until(lambda: self.window.settings_thread is None)
        self.assertEqual(self.window.control_panel.ch_widgets[1]["spin_offset"].value(), 0.75)
        self.assertEqual(self.window.control_panel.combo_mdep.currentData(), "140000")
        self.driver.set_channel.assert_not_called()
        self.driver.set_timebase.assert_not_called()
        self.driver.set_memory_depth.assert_not_called()

    def test_waveform_read_keeps_ui_responsive(self):
        from test_record_features import record
        self.window.driver = self.driver
        self.driver.is_connected = True
        self.driver.get_run_state.return_value = "Run"

        def read(*args, **kwargs):
            self.entered.set()
            self.release.wait(3)
            return record()

        self.driver.read_waveform_to_file.side_effect = lambda *args, **kwargs: (20, read())
        self.driver.get_measurements.return_value = {}
        self.window._read_waveform_from_scope()
        self.pump_until(self.entered.is_set)
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        self.pump_until(lambda: bool(ticks))
        self.assertIsNotNone(self.window.read_thread)
        self.release.set()
        self.pump_until(lambda: self.window.read_thread is None)
        self.assertIsNotNone(self.window.current_wfm)
        self.assertNotIn("失败", self.window.status_bar.currentMessage())
        self.driver.stop_and_wait.assert_called_once()
        self.driver.run.assert_called_once()
        self.driver.single.assert_not_called()

    def test_measurement_refresh_runs_in_background_without_waveform_download(self):
        self.window.driver = self.driver
        self.driver.is_connected = True
        def measure(channel, **kwargs):
            self.entered.set()
            self.release.wait(3)
            return {"vpp": 2.5, "freq": 500.0}
        self.driver.get_measurements.side_effect = measure
        self.window._query_measurements()
        self.pump_until(self.entered.is_set)
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        self.pump_until(lambda: bool(ticks))
        self.assertIsNotNone(self.window.settings_thread)
        self.release.set()
        self.pump_until(lambda: self.window.settings_thread is None)
        self.assertIn("2.500", self.window.measure_dock.table.item(0,0).text())
        self.driver.read_waveform_to_file.assert_not_called()
        self.driver.single.assert_not_called()

    def test_sync_preserves_ch2_trigger_without_writing(self):
        config = dict(self.driver.get_scope_actual_configuration.return_value)
        config["extended_settings"] = {":TRIGger:MODE": "EDGE", ":TRIGger:EDGE:SOURce": "CHANnel2"}
        self.window._apply_scope_settings(config)
        self.assertEqual(self.window.last_configuration["extended_settings"][":TRIGger:EDGE:SOURce"], "CHANnel2")
        self.driver.send.assert_not_called()
        self.driver.write_extended_setting.assert_not_called()


if __name__ == "__main__":
    unittest.main()
