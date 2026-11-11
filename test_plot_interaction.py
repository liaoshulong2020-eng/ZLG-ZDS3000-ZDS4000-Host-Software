import os
import time
import unittest
import numpy as np
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from zds_scope.ui.main_window import MainWindow, LiveStreamThread
from test_record_features import record as make_record
from zds_scope.protocol import ChannelWaveformData, WfmItemInfo


class PlotInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_auto_fit_recovers_waveform_outside_view(self):
        window = MainWindow()
        plot = window.plot_widget
        record = make_record(raw=[103, 128, 153], start=0, dt=.01)
        try:
            plot.update_waveform(record)
            plot.plot_item.setXRange(100, 101, padding=0)
            plot.auto_fit()
            left, right = plot.plot_item.viewRange()[0]
            self.assertLessEqual(left, 0)
            self.assertGreaterEqual(right, .02)
            y = plot.channel_curves[1].yData
            self.assertAlmostEqual(float(y.min() + y.max()), 0)
            np.testing.assert_array_equal(record.channels[1].raw_data, [103, 128, 153])
        finally:
            window.close()

    def test_live_preserves_cursors_and_skips_hidden_analysis(self):
        window = MainWindow()
        record = make_record(raw=[128] * 30000)
        plot = window.plot_widget
        plot.line_y1.setValue(7.25)
        plot.line_y2.setValue(-2.5)
        try:
            with patch.object(window.data_export_dock, 'update_data_view') as analysis:
                window._on_live_waveform_ready(record, {})
                analysis.assert_not_called()
            self.assertEqual(plot.line_y1.value(), 7.25)
            self.assertEqual(plot.line_y2.value(), -2.5)
            self.assertIs(window.data_export_dock.current_wfm, record)
            self.assertIsNone(record.channels[1]._voltage_array)
            with patch.object(plot, 'update_waveform') as refresh:
                plot.auto_fit()
                refresh.assert_not_called()
        finally:
            window.close()

    def test_live_waits_for_display_acknowledgement_and_stops(self):
        driver = Mock(is_connected=True)
        driver.read_waveform.return_value = object()
        worker = LiveStreamThread(driver, channels=[], interval_ms=5)
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while driver.read_waveform.call_count == 0 and time.monotonic() < deadline:
                time.sleep(.01)
            time.sleep(.1)
            self.assertEqual(driver.read_waveform.call_count, 1)
            worker.frame_consumed.set()
            deadline = time.monotonic() + 2
            while driver.read_waveform.call_count < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(driver.read_waveform.call_count, 2)
        finally:
            worker.stop()
        self.assertFalse(worker.isRunning())

    def test_independent_scales_centering_and_physical_cursor_readout(self):
        window = MainWindow()
        plot = window.plot_widget
        record = make_record(raw=[103, 128, 153], start=0, dt=.01)
        record.channels[1].info.vert_division = .2
        record.channels[4] = ChannelWaveformData(4, WfmItemInfo(channel=3, vert_division=100, vert_offset=-300), np.array([148,153,158], dtype=np.uint8), start_time=0, dt=.01)
        original = record.channels[4].raw_data.copy()
        try:
            plot.update_waveform(record)
            plot.plot_item.setXRange(0, .02, padding=0)
            self.assertAlmostEqual(np.ptp(plot.channel_curves[1].yData), 2)
            plot.center_channels()
            for channel in (1,4):
                y = plot.channel_curves[channel].yData
                self.assertAlmostEqual(float(y.min()+y.max()), 0)
            self.assertEqual(plot._display_centers[4],400)
            plot.line_y1.setValue(1)
            plot.line_y2.setValue(0)
            self.assertIn('500.000',plot.bottom_bar.ch_cards[4].lbl_ya.text())
            self.assertIn('400.000',plot.bottom_bar.ch_cards[4].lbl_yb.text())
            self.assertIn('0.200',plot.bottom_bar.ch_cards[1].lbl_ya.text())
            np.testing.assert_array_equal(record.channels[4].raw_data, original)
            self.assertEqual(record.channels[4].info.vert_offset,-300)
            plot.restore_channel_positions()
            self.assertEqual(plot._display_centers[4],300)
        finally:
            window.close()
