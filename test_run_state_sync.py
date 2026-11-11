import os
import threading
import unittest
from unittest.mock import MagicMock, patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from zds_scope.ui.main_window import RunStateThread, MainWindow, ControlThread

class RunStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_idle_query_restores_timeout_and_never_writes_settings(self):
        driver = MagicMock()
        driver.io_lock = threading.RLock()
        driver.sock.gettimeout.return_value = 5.0
        driver.get_run_state.return_value = 'Stop'
        worker = RunStateThread(driver)
        worker.run()
        self.assertEqual(worker.state, 'Stop')
        self.assertEqual(driver.sock.settimeout.call_args_list[-1].args, (5.0,))
        driver.send.assert_not_called()

    def test_busy_transfer_skips_query(self):
        driver = MagicMock()
        driver.io_lock = threading.Lock()
        driver.io_lock.acquire()
        try:
            RunStateThread(driver).run()
            driver.get_run_state.assert_not_called()
        finally:
            driver.io_lock.release()

    def test_front_panel_stop_updates_gui_without_wave_read(self):
        window = MainWindow()
        driver = MagicMock()
        driver.is_connected = True
        window.driver = driver
        worker = RunStateThread(driver)
        worker.state = 'Stop'
        window.run_state_thread = worker
        with patch.object(window.control_panel, 'set_run_state_visual') as visual:
            window._on_run_state_finished()
            visual.assert_called_once_with('Stop')
        self.assertEqual(window.last_configuration['run_state'], 'Stop')
        driver.read_waveform.assert_not_called()
        window.close()

    def test_stop_state_survives_secondary_trigger_query_failure(self):
        window = MainWindow()
        driver = MagicMock()
        driver.io_lock = threading.RLock()
        driver.get_run_state.return_value = 'Stop'
        driver.query.side_effect = TimeoutError('trigger unavailable')
        window.driver = driver
        worker = ControlThread(driver, 'stop')
        worker.run()
        window.control_thread = worker
        window._on_control_finished()
        driver.stop.assert_called_once()
        self.assertEqual(window.last_configuration['run_state'], 'Stop')
        self.assertTrue(window.control_panel.isEnabled())
        window.close()

if __name__ == '__main__':
    unittest.main()
