"""Completion delivery, UI recovery, and cancellable CSV regressions."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock
import numpy as np
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication
from zds_scope.ui.main_window import MainWindow
from zds_scope.ui.record_workbench import RecordWorkbench
from zds_scope.storage import ScopeStorage
from zds_scope.server import app as control_api
from test_record_features import record


class OfflineTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def pump(self, done):
        deadline = time.monotonic() + 5
        while not done() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(done())

    def test_completion_runs_on_gui_thread_and_releases_busy_state(self):
        panel = RecordWorkbench()
        completed, busy = [], []
        panel.busy_changed.connect(busy.append)
        panel._task(lambda worker: 42,
                    lambda value: completed.append((value, QThread.currentThread() == self.app.thread())))
        self.pump(lambda: panel.worker is None)
        self.assertEqual(completed, [(42, True)])
        self.assertEqual(busy, [True, False])
        self.assertTrue(all(action.isEnabled() for action in panel.actions))
        panel.close()

    def test_watchdog_recovers_if_finished_signal_is_not_delivered(self):
        panel = RecordWorkbench()
        release = threading.Event()
        completed = []
        panel._task(lambda worker: release.wait(3), completed.append)
        panel.worker.finished.disconnect(panel._on_task_finished)
        release.set()
        self.assertTrue(panel.worker.wait(3000))
        panel._recover_finished_task()
        self.assertIsNone(panel.worker)
        self.assertEqual(completed, [True])
        panel.close()

    def test_export_does_not_disable_device_controls(self):
        window = MainWindow()
        window.driver = MagicMock()
        window.driver.is_connected = True
        release = threading.Event()
        window.record_workbench._task(lambda worker: release.wait(3), lambda result: None,
                                      cancellable=True, label="CSV 导出")
        try:
            self.assertTrue(window.control_panel.isEnabled())
            self.assertTrue(window.btn_connect.isEnabled())
            self.assertTrue(window._check_connected())
            self.assertTrue(window.record_task_cancel.isEnabled())
        finally:
            release.set()
            self.pump(lambda: window.record_workbench.worker is None)
            window.close()

    def test_product_ui_and_routes_do_not_offer_removed_assistant(self):
        from PySide6.QtWidgets import QLabel, QPushButton
        window = MainWindow()
        text = [window.windowTitle()] + [obj.text() for obj in window.findChildren(QLabel)]
        text += [obj.text() for obj in window.findChildren(QPushButton)]
        text += [action.text() for action in window.menuBar().actions()]
        self.assertFalse(any("AI" in value for value in text))
        self.assertFalse(any("/ai/" in getattr(route, "path", "") for route in control_api.routes))
        window.close()

    def test_csv_cancel_preserves_existing_output_and_original_record(self):
        wfm = record(np.full(50000, 128, dtype=np.uint8))
        cancel = [False]
        progress = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "existing.csv"
            path.write_text("previous export", encoding="utf-8")
            def update(done, total):
                progress.append(done)
                cancel[0] = True
            with self.assertRaisesRegex(InterruptedError, "已取消导出"):
                ScopeStorage.save_csv(wfm, str(path), update, lambda: cancel[0])
            self.assertEqual(path.read_text(), "previous export")
            self.assertTrue(list(Path(tmp).glob("*.partial")))
            self.assertTrue(progress)
            self.assertIsNone(wfm.channels[1]._voltage_array)

    def test_csv_completion_reports_all_points_without_leaving_partial_file(self):
        wfm = record()
        updates = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "complete.csv"
            ScopeStorage.save_csv(wfm, str(path), lambda done, total: updates.append((done, total)))
            self.assertEqual(updates[-1], (20, 20))
            self.assertEqual(len(path.read_text().splitlines()), 21)
            self.assertFalse(list(Path(tmp).glob("*.partial")))


if __name__ == "__main__":
    unittest.main()
