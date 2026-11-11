import os, unittest
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPoint, QSignalBlocker
from PySide6.QtTest import QTest
from zds_scope.ui.main_window import MainWindow

class CompactLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.window = MainWindow()
        self.window.resize(1360, 900)
        self.window.show()
        self.app.processEvents()
    def tearDown(self):
        self.window.close()
        self.app.processEvents()
    def test_tools_expand_and_divider_collapses_and_restores(self):
        w = self.window
        full_height = w.plot_widget.height()
        w.function_buttons['测量'].click()
        self.app.processEvents()
        self.assertIs(w.bottom_tabs.currentWidget(), w.measure_dock)
        self.assertGreater(w.waveform_splitter.sizes()[1], 100)
        self.assertLess(w.plot_widget.height(), full_height)
        handle = w.waveform_splitter.handle(1)
        QTest.mouseDClick(handle, Qt.LeftButton)
        self.app.processEvents()
        self.assertEqual(w.waveform_splitter.sizes()[1], 0)
        QTest.mouseDClick(handle, Qt.LeftButton)
        self.app.processEvents()
        self.assertGreater(w.waveform_splitter.sizes()[1], 100)
        w.waveform_splitter.moveSplitter(w.waveform_splitter.height(), 1)
        self.app.processEvents()
        self.assertEqual(w.waveform_splitter.sizes()[1], 0)
        self.assertGreaterEqual(w.plot_widget.height(), full_height)
    def test_shared_y_cursors_update_all_enabled_channels(self):
        plot = self.window.plot_widget
        plot.update_scope_config({"channels": {1: {"enabled": True}, 2: {"enabled": True}, 3: {"enabled": True}}})
        plot.cb_cursor.setChecked(True)
        plot.line_y1.setValue(7.25)
        plot.line_y2.setValue(-2.5)
        for channel in (1, 2, 3):
            card = plot.bottom_bar.ch_cards[channel]
            self.assertIn("7.250", card.lbl_ya.text())
            self.assertIn("-2.500", card.lbl_yb.text())
            self.assertIn("9.750", card.lbl_dy.text())
        self.assertFalse(hasattr(plot, "combo_cursor_ch"))

    def test_popup_outside_click_and_combobox_selection(self):
        w = self.window
        self.assertTrue(w.windowFlags() & Qt.FramelessWindowHint)
        w._open_channel_settings(3)
        self.app.processEvents()
        popup = w.control_panel.channel_dialog
        combo = w.control_panel.ch_widgets[3]["combo_scale"]
        with QSignalBlocker(combo):
            combo.showPopup()
            self.app.processEvents()
            view = combo.view()
            index = combo.model().index(2,0)
            view.scrollTo(index)
            self.app.processEvents()
            QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualRect(index).center())
            self.app.processEvents()
        self.assertTrue(popup.isVisible())
        self.assertEqual(combo.currentIndex(),2)
        QTest.mouseClick(popup, Qt.LeftButton, pos=QPoint(-10,-10))
        self.app.processEvents()
        self.assertFalse(popup.isVisible())
        w.control_panel.show_timebase_settings()
        self.app.processEvents()
        QTest.keyClick(w.control_panel.timebase_dialog,Qt.Key_Escape)
        self.assertFalse(w.control_panel.timebase_dialog.isVisible())

    def test_compact_controls_and_popup_selection(self):
        w = self.window
        self.assertFalse(w.menuBar().isVisible())
        self.assertFalse(w.bottom_tabs.tabBar().isVisible())
        self.assertFalse(w.control_panel.memory_group.isVisible())
        w._open_channel_settings(3)
        self.assertTrue(w.control_panel.channel_dialog.isVisible())
        self.assertEqual(w.control_panel.ch_tabs.currentIndex(), 2)
        w.control_panel.show_timebase_settings()
        self.assertTrue(w.control_panel.timebase_dialog.isVisible())
        w._show_parameter_settings((':TRIG',), '触发设置')
        self.assertFalse(w.control_panel.channel_dialog.isVisible())
        self.assertTrue(w.settings_dialog.isVisible())

if __name__ == '__main__':
    unittest.main()
