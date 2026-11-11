"""Screenshot workflows use test images only; no device communication."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSettings, QTimer
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from zds_scope.ui.screenshot_panel import (ScreenshotPanel, decode_screenshot,
                                         save_named_screenshot, inverted_screenshot)
from zds_scope.ui.main_window import MainWindow


def image_bytes(fmt="PNG"):
    image = QImage(320, 240, QImage.Format_RGB32)
    image.fill(QColor("#012345"))
    image.setPixelColor(170, 100, QColor("#fedcba"))
    output = QByteArray()
    buffer = QBuffer(output)
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, fmt)
    return bytes(output)


class ScreenshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.IniFormat)
        self.panel = ScreenshotPanel(settings=self.settings)
        self.panel.directory.setText(str(self.root / "screens"))
        self.window = None
        self.release = threading.Event()
        self.entered = threading.Event()

    def tearDown(self):
        self.release.set()
        if self.window is not None:
            self.pump_until(lambda: self.window.screenshot_thread is None)
            self.window.close()
        self.panel.close()
        self.app.processEvents()
        self.tmp.cleanup()

    def pump_until(self, condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(condition())

    def main_window(self):
        self.window = MainWindow()
        self.window.tab_screenshot.settings = self.settings
        self.window.tab_screenshot.directory.setText(str(self.root / "screens"))
        self.window.driver = MagicMock()
        self.window.driver.is_connected = True
        self.window.driver.capture_screen.return_value = image_bytes()
        return self.window

    def test_naming_collision_native_bytes_and_sanitized_prefix(self):
        data = image_bytes()
        stamp = datetime(2026, 10, 16, 14, 1, 2, 123456)
        first = save_named_screenshot(data, self.root, "my:/scope", stamp)
        second = save_named_screenshot(data, self.root, "my:/scope", stamp)
        self.assertEqual(Path(first).name, "my__scope_20261016_140102_123456.png")
        self.assertTrue(second.endswith("_1.png"))
        self.assertEqual(Path(first).read_bytes(), data)
        self.assertEqual(Path(second).read_bytes(), data)

    def test_invalid_image_is_not_saved(self):
        for data in (b"error", b"\x89PNG\r\n\x1a\ntruncated"):
            with self.assertRaises(ValueError):
                save_named_screenshot(data, self.root / "invalid")
        self.assertFalse((self.root / "invalid").exists())

    def test_bmp_detected_from_native_payload(self):
        data = image_bytes("BMP")
        image, fmt = decode_screenshot(data)
        self.assertEqual((image.width(), image.height(), fmt), (320, 240, "bmp"))
        path = save_named_screenshot(data, self.root)
        self.assertTrue(path.endswith(".bmp"))
        self.assertEqual(Path(path).read_bytes(), data)

    def test_clipboard_preserves_resolution_and_pixels(self):
        self.panel.set_image(image_bytes(), auto_copy=False)
        self.assertTrue(self.panel.copy_image())
        copied = QApplication.clipboard().image()
        self.assertEqual((copied.width(), copied.height()), (320, 240))
        self.assertEqual(copied.pixelColor(170, 100), QColor("#fedcba"))

    def test_settings_are_remembered(self):
        self.panel.prefix.setText("电源启动")
        self.panel.format.setCurrentText("BMP")
        self.panel.auto_copy.setChecked(False)
        self.panel.invert.setChecked(True)
        self.panel.options()
        second = ScreenshotPanel(settings=self.settings)
        self.assertEqual(second.prefix.text(), "电源启动")
        self.assertEqual(second.format.currentText(), "BMP")
        self.assertEqual(second.directory.text(), self.panel.directory.text())
        self.assertFalse(second.auto_copy.isChecked())
        self.assertTrue(second.invert.isChecked())
        second.close()

    def test_save_as_keeps_original_encoding_and_bytes(self):
        data = image_bytes()
        self.panel.set_image(data, auto_copy=False)
        target = str(self.root / "manual")
        with patch.object(QFileDialog, "getSaveFileName", return_value=(target, "PNG")):
            self.panel.save_image_as()
        self.assertEqual((self.root / "manual.png").read_bytes(), data)

    def test_invert_preview_clipboard_and_save_preserve_native_original(self):
        data = image_bytes()
        self.panel.set_image(data, auto_copy=False)
        self.panel.invert.setChecked(True)
        self.assertEqual(self.panel.payload, data)
        self.assertEqual(self.panel.native_image.pixelColor(170, 100), QColor("#fedcba"))
        self.assertEqual(self.panel.image.pixelColor(170, 100), QColor("#012345"))
        self.panel.copy_image()
        self.assertEqual(QApplication.clipboard().image().pixelColor(170, 100), QColor("#012345"))
        target = str(self.root / "inverted.png")
        with patch.object(QFileDialog, "getSaveFileName", return_value=(target, "PNG")):
            self.panel.save_image_as()
        saved, _ = decode_screenshot(Path(target).read_bytes())
        self.assertEqual(saved.pixelColor(170, 100), QColor("#012345"))
        self.panel.invert.setChecked(False)
        self.assertEqual(self.panel.image.pixelColor(170, 100), QColor("#fedcba"))

    def test_unchecked_clipboard_does_not_replace_existing_image(self):
        before = QImage(11, 12, QImage.Format_RGB32)
        before.fill(QColor("red"))
        QApplication.clipboard().setImage(before)
        self.panel.auto_copy.setChecked(False)
        self.panel.set_image(image_bytes())
        self.assertEqual(QApplication.clipboard().image().size(), before.size())

    def test_capture_inverted_creates_both_original_and_inverted_files(self):
        window = self.main_window()
        window.tab_screenshot.invert.setChecked(True)
        window._capture_screen_from_scope()
        self.pump_until(lambda: window.screenshot_thread is None)
        paths = list((self.root / "screens").glob("*.png"))
        self.assertEqual(len(paths), 2)
        native = Path(window.tab_screenshot.saved_path)
        self.assertEqual(native.read_bytes(), window.last_screenshot)
        transformed = next(path for path in paths if path != native)
        result, _ = decode_screenshot(transformed.read_bytes())
        self.assertEqual(result.pixelColor(170, 100), QColor("#012345"))

    def test_view_picture_uses_full_size_image(self):
        self.panel.set_image(image_bytes(), auto_copy=False)
        self.panel.invert.setChecked(True)
        self.panel.show_original()
        viewer = self.panel.viewers[-1]
        self.assertEqual(viewer.widget().pixmap().size(), self.panel.native_image.size())
        self.assertIn("反色", viewer.windowTitle())
        viewer.close()

    def test_screenshot_does_not_grow_panel_or_squeeze_waveform(self):
        window = self.main_window()
        window.resize(1360, 850)
        window.bottom_tabs.setCurrentWidget(window.tab_screenshot)
        window.show()
        for _ in range(5):
            self.app.processEvents()
        before = window.plot_widget.height()
        for _ in range(5):
            window.tab_screenshot.set_image(image_bytes(), str(self.root / ("long_path_" * 10 + ".png")),
                                           auto_copy=False)
            self.app.processEvents()
        self.assertGreaterEqual(window.plot_widget.height(), before - 40)
        self.assertGreater(window.plot_widget.height(), 250)
        window.resize(1100, 720)
        self.app.processEvents()
        window.resize(1360, 850)
        self.app.processEvents()
        self.assertGreaterEqual(window.plot_widget.height(), before - 40)

    def test_double_click_opens_independent_full_size_preview(self):
        self.panel.resize(900, 350)
        self.panel.set_image(image_bytes(), auto_copy=False)
        self.panel.show()
        self.app.processEvents()
        QTest.mouseDClick(self.panel.preview, Qt.LeftButton)
        self.app.processEvents()
        self.assertEqual(len(self.panel.viewers), 1)
        viewer = self.panel.viewers[0]
        self.assertTrue(viewer.isWindow())
        self.assertEqual(viewer.widget().pixmap().size(), self.panel.image.size())
        viewer.close()

    def test_background_capture_saves_copies_and_keeps_ui_responsive(self):
        window = self.main_window()

        def capture(**kwargs):
            self.entered.set()
            self.release.wait(4)
            return image_bytes()

        window.driver.capture_screen.side_effect = capture
        window._capture_screen_from_scope()
        self.pump_until(self.entered.is_set)
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        self.pump_until(lambda: bool(ticks))
        self.assertFalse(window.btn_connect.isEnabled())
        window._capture_screen_from_scope()  # Re-entry must not queue another screenshot.
        self.assertEqual(window.driver.capture_screen.call_count, 1)
        self.release.set()
        self.pump_until(lambda: window.screenshot_thread is None)
        self.assertEqual(Path(window.tab_screenshot.saved_path).read_bytes(), window.last_screenshot)
        self.assertEqual(QApplication.clipboard().image().size(), window.tab_screenshot.image.size())
        self.assertTrue(window.btn_connect.isEnabled())
        window.driver.single.assert_not_called()
        window.driver.stop.assert_not_called()

    def test_capture_failure_preserves_previous_image_and_reports_error(self):
        window = self.main_window()
        previous = image_bytes()
        window.last_screenshot = previous
        window.tab_screenshot.set_image(previous, auto_copy=False)
        window.driver.capture_screen.return_value = b"bad response"
        window._capture_screen_from_scope()
        self.pump_until(lambda: window.screenshot_thread is None)
        self.assertEqual(window.last_screenshot, previous)
        self.assertEqual(window.tab_screenshot.payload, previous)
        self.assertIn("截图失败", window.tab_screenshot.message.text())
        self.assertFalse((self.root / "screens").exists())

    def test_save_failure_retains_image_for_retry(self):
        window = self.main_window()
        blocker = self.root / "file_not_directory"
        blocker.write_text("occupied")
        window.tab_screenshot.directory.setText(str(blocker))
        window._capture_screen_from_scope()
        self.pump_until(lambda: window.screenshot_thread is None)
        self.assertIsNotNone(window.last_screenshot)
        self.assertIsNone(window.tab_screenshot.saved_path)
        self.assertIn("自动保存失败", window.tab_screenshot.message.text())
        self.assertTrue(window.tab_screenshot.save_as.isEnabled())

    def test_close_waits_for_capture_to_finish(self):
        window = self.main_window()

        def capture(**kwargs):
            self.entered.set()
            self.release.wait(4)
            return image_bytes()

        window.driver.capture_screen.side_effect = capture
        window._capture_screen_from_scope()
        self.pump_until(self.entered.is_set)
        self.assertFalse(window.close())
        self.assertTrue(window._close_after_screenshot)
        self.release.set()
        self.pump_until(lambda: window.screenshot_thread is None)
        self.assertIsNotNone(window.tab_screenshot.saved_path)


if __name__ == "__main__":
    unittest.main()
