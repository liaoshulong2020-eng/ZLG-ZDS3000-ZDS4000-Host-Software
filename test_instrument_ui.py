import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings, QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QImage, QColor
from zds_scope.ui.main_window import MainWindow, SettingsThread
from zds_scope.ui.hardware_fft import HardwareFFTThread
from zds_scope.driver import ZDScopeDriver
from test_record_features import record


class InstrumentUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_fft_uses_device_commands_and_never_retriggers_acquisition(self):
        driver = Mock(io_lock=threading.RLock(), timeout=5.0)
        driver.query.return_value = 'HANNing'
        with tempfile.TemporaryDirectory() as directory:
            worker = HardwareFFTThread(driver, directory, source=2)
            with patch.object(HardwareFFTThread, 'msleep'), patch('zds_scope.ui.hardware_fft.ScreenshotThread.run') as capture:
                worker.run()
            self.assertIsNone(worker.error)
            self.assertEqual([c.args[0] for c in driver.send.call_args_list], [':MATH:FFT:SOURce CHANnel2', ':MATH:FFT:STATe ON', ':MATH:FFT:SINGle'])
            capture.assert_called_once()
            driver.single.assert_not_called()
            driver.query.assert_not_called()
            self.assertEqual(driver.timeout, 5)

    def test_fft_read_screen_never_sends_fft_commands_and_restores_timeout(self):
        driver = Mock(io_lock=threading.RLock(), timeout=5.0)
        driver.sock.gettimeout.return_value = 5
        with tempfile.TemporaryDirectory() as directory:
            worker = HardwareFFTThread(driver, directory, source=4, calculate=False)
            def failure():
                self.assertEqual(driver.timeout,30)
                raise TimeoutError('test timeout')
            with patch('zds_scope.ui.hardware_fft.ScreenshotThread.run', side_effect=failure):
                worker.run()
            driver.send.assert_not_called()
            driver.query.assert_not_called()
            self.assertEqual(driver.timeout,5)
            driver.sock.settimeout.assert_called_with(5)
            self.assertIn('读取仪器截图',worker.error)

    def test_fft_image_actions_keep_full_resolution(self):
        window = MainWindow()
        try:
            panel = window.hardware_fft.images
            picture = QImage(800,480,QImage.Format_RGB32)
            picture.fill(QColor('yellow'))
            data = QByteArray()
            buffer = QBuffer(data)
            buffer.open(QIODevice.WriteOnly)
            picture.save(buffer,'PNG')
            panel.invert.setChecked(False)
            panel.set_image(bytes(data),auto_copy=False)
            self.assertTrue(panel.copy.isEnabled())
            self.assertTrue(panel.save_as.isEnabled())
            self.assertTrue(panel.original.isEnabled())
            panel.copy_image()
            self.assertEqual(self.app.clipboard().image().size(),picture.size())
            panel.show_original()
            self.assertEqual(len(panel.viewers),1)
            for viewer in list(panel.viewers):
                viewer.close()
        finally:
            window.close()

    def test_measurement_reads_do_not_change_screen_and_restore_known_baseline(self):
        driver = ZDScopeDriver()
        with patch.object(driver, 'query', return_value='3.40282e38'), patch.object(driver, 'send') as send:
            result = driver.get_measurements(4, wait_seconds=0)
            send.assert_not_called()
            self.assertTrue(all(v is None for v in result.values()))
            worker = SettingsThread(driver, restore_measurements=True)
            worker.run()
            self.assertIsNone(worker.error)
            self.assertEqual([c.args[0] for c in send.call_args_list], [':MEASure:CLEar', ':MEASure:VPP CHANnel1'])

    def test_labels_local_center_and_csv_directory(self):
        w = MainWindow()
        try:
            with tempfile.TemporaryDirectory() as directory:
                w.ui_settings = QSettings(str(Path(directory)/'prefs.ini'), QSettings.IniFormat)
                w.current_wfm = record()
                w.plot_widget.update_waveform(w.current_wfm)
                w._rename_channel(1, 'Vbus')
                self.assertIn('Vbus', w.plot_widget.channel_markers[1].text())
                self.assertFalse(w.plot_widget.btn_center.isVisible())
                w.control_panel.ch_widgets[1]['cb_center'].setChecked(True)
                self.assertIn(1, w.plot_widget._center_overrides)
                self.assertNotIn(2, w.plot_widget._center_overrides)
                w.ui_settings.setValue('export/csv_directory', directory)
                with patch('zds_scope.ui.main_window.QFileDialog.getSaveFileName', return_value=('', '')) as dialog:
                    w._save_csv_file()
                    self.assertEqual(Path(dialog.call_args.args[2]).parent, Path(directory))
                self.assertEqual(w.control_panel.layout().itemAt(0).widget(), w.device_menu_button)
                self.assertEqual(w.bottom_tabs.indexOf(w.data_export_dock), -1)
        finally:
            w.close()

    def test_refresh_clears_display_and_new_waveform_removes_notice(self):
        w = MainWindow()
        try:
            old = record()
            w.current_wfm = old
            w.plot_widget.update_waveform(old)
            w.plot_widget.clear_for_refresh()
            self.assertIs(w.current_wfm, old)
            self.assertIsNone(w.plot_widget.current_wfm)
            self.assertTrue(all(curve.xData is None for curve in w.plot_widget.channel_curves.values()))
            self.assertFalse(w.plot_widget.read_status.isHidden())
            w.plot_widget.show_read_status('读取失败，请重试')
            w.plot_widget.auto_fit()
            self.assertIsNone(w.plot_widget.channel_curves[1].xData)
            w.plot_widget.update_waveform(record(raw=[129,130]))
            self.assertTrue(w.plot_widget.read_status.isHidden())
            self.assertEqual(len(w.plot_widget.channel_curves[1].xData),2)
        finally:
            w.close()

    def test_wfm_save_directory_survives_settings_reload(self):
        w = MainWindow()
        try:
            with tempfile.TemporaryDirectory() as directory:
                prefs = str(Path(directory)/'prefs.ini')
                w.ui_settings = QSettings(prefs, QSettings.IniFormat)
                w.current_wfm = record()
                target = str(Path(directory)/'capture.wfm')
                with patch('zds_scope.ui.main_window.QFileDialog.getSaveFileName', return_value=(target,'')), patch.object(w.record_workbench,'_task'):
                    w._save_wfm_file()
                w.ui_settings.sync()
                w.ui_settings = QSettings(prefs,QSettings.IniFormat)
                with patch('zds_scope.ui.main_window.QFileDialog.getSaveFileName', return_value=('','')) as dialog:
                    w._save_wfm_file()
                    self.assertEqual(Path(dialog.call_args.args[2]).parent,Path(directory))
        finally:
            w.close()
