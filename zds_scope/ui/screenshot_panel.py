"""Instrument screenshots: preserve native bytes; copy full-resolution images."""
import os
import re
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QThread, QSettings, QSaveFile, QIODevice, QUrl, QBuffer, QByteArray, QSize, QRect
from PySide6.QtGui import QImage, QPixmap, QDesktopServices, QPainter
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                              QPushButton, QCheckBox, QComboBox, QFileDialog,
                              QScrollArea, QApplication, QSizePolicy)


class ScreenshotPreview(QWidget):
    """Paint inside allocated space; image size must never affect the layout."""
    double_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QImage()
        self.setMinimumSize(100, 60)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setToolTip("双击图片，在独立窗口按原尺寸预览")

    def sizeHint(self):
        return QSize(100, 60)

    def minimumSizeHint(self):
        return QSize(100, 60)

    def set_image(self, image):
        self.image = image
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self.image.isNull():
            painter.drawText(self.rect(), Qt.AlignCenter, "尚未截图；截图后双击图片可单独预览")
            return
        # Preview can shrink to fit, but never enlarges the original image.
        scale = min(1.0, self.width() / self.image.width(), self.height() / self.image.height())
        width, height = max(1, int(self.image.width() * scale)), max(1, int(self.image.height() * scale))
        target = QRect((self.width() - width) // 2, (self.height() - height) // 2, width, height)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawImage(target, self.image)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and not self.image.isNull():
            self.double_clicked.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


def decode_screenshot(payload):
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = "png"
    elif payload.startswith(b"BM"):
        extension = "bmp"
    else:
        raise ValueError("仪器未返回 PNG/BMP 图片，截图无效")
    image = QImage.fromData(payload)
    if image.isNull():
        raise ValueError("截图图片解码失败，未保存或复制")
    return image, extension


def save_named_screenshot(payload, directory, prefix="ZDS", timestamp=None):
    """Exclusive create prevents collisions, including concurrent app instances."""
    _, extension = decode_screenshot(payload)
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    prefix = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", prefix).strip(" .")[:60] or "ZDS"
    stem = f"{prefix}_{(timestamp or datetime.now()).strftime('%Y%m%d_%H%M%S_%f')}"
    for number in range(10000):
        path = directory / f"{stem}{'_' + str(number) if number else ''}.{extension}"
        try:
            stream = path.open("xb")
        except FileExistsError:
            continue
        try:
            with stream:
                stream.write(payload)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return str(path)
    raise OSError("自动命名冲突过多，请更换保存目录")


def inverted_screenshot(payload):
    image, extension = decode_screenshot(payload)
    image.invertPixels(QImage.InvertRgb)
    output = QByteArray()
    buffer = QBuffer(output)
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, extension.upper()):
        raise ValueError("无法编码反色图片")
    return bytes(output)


class ScreenshotThread(QThread):
    def __init__(self, driver, fmt, directory, prefix, parent=None, invert=False):
        super().__init__(parent)
        self.driver, self.fmt = driver, fmt
        self.directory, self.prefix = directory, prefix
        self.payload = None
        self.path = None
        self.error = None
        self.save_error = None
        self.invert = invert
        self.inverted_path = None

    def run(self):
        try:
            payload = self.driver.capture_screen(fmt=self.fmt)
            decode_screenshot(payload)
            self.payload = payload
            try:
                stamp = datetime.now()
                self.path = save_named_screenshot(payload, self.directory, self.prefix, stamp)
                if self.invert:
                    self.inverted_path = save_named_screenshot(inverted_screenshot(payload), self.directory,
                                                              self.prefix[:48] + "_inverted", stamp)
            except Exception as error:
                self.save_error = str(error)
        except Exception as error:
            self.error = str(error)


class ScreenshotPanel(QWidget):
    capture_requested = Signal()

    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.settings = settings if settings is not None else QSettings("ZLG_Scope_Team", "ZLG_ZDS_Scope_AI_Upper")
        self.payload = None
        self.image = QImage()
        self.native_image = QImage()
        self.saved_path = None
        self.extension = "png"
        self.viewers = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        row = QHBoxLayout()
        self.capture = QPushButton("截图并自动保存")
        self.capture.clicked.connect(self.capture_requested.emit)
        self.format = QComboBox()
        self.format.addItems(["PNG", "BMP"])
        self.format.setCurrentText(self.settings.value("screenshot/format", "PNG"))
        self.prefix = QLineEdit(self.settings.value("screenshot/prefix", "ZDS"))
        self.prefix.setMaximumWidth(140)
        self.prefix.setPlaceholderText("文件名前缀")
        self.invert = QCheckBox("图片反色")
        self.invert.setChecked(self.settings.value("screenshot/invert", False, type=bool))
        self.invert.toggled.connect(self._effect_changed)
        self.auto_copy = QCheckBox("保存至剪贴板")
        self.auto_copy.setToolTip("勾选后，每次成功截图自动复制完整图片；也可点击复制图片。")
        self.auto_copy.setChecked(self.settings.value("screenshot/auto_copy", True, type=bool))
        self.copy = QPushButton("复制图片")
        self.copy.clicked.connect(self.copy_image)
        self.save_as = QPushButton("另存为…")
        self.save_as.setToolTip("保存当前反色/原色效果；默认自动保存的仪器原图不变。")
        self.save_as.clicked.connect(self.save_image_as)
        self.original = QPushButton("查看图片")
        self.original.setToolTip("以 100% 原尺寸查看当前图片，可滚动查看。")
        self.original.clicked.connect(self.show_original)
        for item in (self.capture, self.format, QLabel("前缀:"), self.prefix, self.invert, self.auto_copy):
            row.addWidget(item)
        row.addStretch()
        layout.addLayout(row)
        row = QHBoxLayout()
        for item in (self.copy, self.save_as, self.original):
            row.addWidget(item)
        row.addStretch()
        layout.addLayout(row)
        row = QHBoxLayout()
        self.directory = QLineEdit(self.settings.value("screenshot/directory", os.path.abspath("data/screenshots")))
        choose = QPushButton("选择目录…")
        choose.clicked.connect(self.choose_directory)
        open_dir = QPushButton("打开目录")
        open_dir.clicked.connect(self.open_directory)
        row.addWidget(QLabel("保存到:"))
        row.addWidget(self.directory, 1)
        row.addWidget(choose)
        row.addWidget(open_dir)
        layout.addLayout(row)
        self.message = QLabel("点击截图后自动保存完整图片；文件名：前缀_年月日_时分秒_微秒。")
        self.message.setWordWrap(True)
        self.message.setMaximumHeight(36)
        self.message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.message)
        self.preview = ScreenshotPreview()
        self.preview.double_clicked.connect(self.show_original)
        layout.addWidget(self.preview, 1)
        self.set_busy(False)

    def options(self):
        directory = self.directory.text().strip()
        if not directory:
            raise ValueError("请先填写截图保存目录")
        for key, value in (("directory", directory), ("prefix", self.prefix.text()),
                           ("format", self.format.currentText()), ("auto_copy", self.auto_copy.isChecked()),
                           ("invert", self.invert.isChecked())):
            self.settings.setValue("screenshot/" + key, value)
        return self.format.currentText(), directory, self.prefix.text()

    def set_busy(self, busy):
        self.capture.setEnabled(not busy)
        self.capture.setText("正在截图…" if busy else "截图并自动保存")
        for item in (self.format, self.prefix, self.directory, self.auto_copy, self.invert):
            item.setEnabled(not busy)
        for item in (self.copy, self.save_as, self.original):
            item.setEnabled(not busy and self.payload is not None)

    def set_image(self, payload, path=None, save_error=None, auto_copy=True, inverted_path=None):
        image, self.extension = decode_screenshot(payload)
        self.payload, self.native_image, self.saved_path = payload, image, path
        self.inverted_path = inverted_path
        self.image = self.native_image.copy()
        if self.invert.isChecked():
            self.image.invertPixels(QImage.InvertRgb)
        self._refresh_preview()
        copied = self.copy_image(update_status=False) if auto_copy and self.auto_copy.isChecked() else False
        message = f"截图 {image.width()} × {image.height()}，{len(payload):,} 字节。"
        message += f"已保存：{path}" if path else "图片已接收，尚未保存。"
        if save_error:
            message += f" 自动保存失败：{save_error}；可点击另存为重试。"
        if inverted_path:
            message += f" 反色图：{inverted_path}"
        if copied:
            message += " 已复制完整图片，可直接粘贴到聊天或 Word。"
        self.message.setText(message)
        self.message.setToolTip(message)
        self.set_busy(False)

    def _effect_changed(self):
        self.settings.setValue("screenshot/invert", self.invert.isChecked())
        if self.native_image.isNull():
            return
        self.image = self.native_image.copy()
        if self.invert.isChecked():
            self.image.invertPixels(QImage.InvertRgb)
        self._refresh_preview()
        self.message.setText("当前为反色效果。复制、查看、另存为均使用此效果；仪器原图保留。" if self.invert.isChecked()
                             else "当前为仪器原色。复制、查看、另存为均使用此效果。")

    def copy_image(self, checked=False, update_status=True):
        if self.image.isNull():
            return False
        QApplication.clipboard().setImage(self.image)
        if update_status:
            self.message.setText("已复制原尺寸图片（" + ("反色" if self.invert.isChecked() else "原色") + "），可直接粘贴到聊天、Word 或画图。")
        return True

    def save_image_as(self):
        if self.payload is None:
            return
        default = self.saved_path or str(Path(self.directory.text()) / f"ZDS.{self.extension}")
        if self.invert.isChecked():
            original = Path(default)
            default = str(original.with_name(original.stem + "_inverted" + original.suffix))
        # Save native bytes only; changing a suffix must not silently change the encoding.
        path, _ = QFileDialog.getSaveFileName(self, "保存原始截图", default,
                                             f"{self.extension.upper()} 图片 (*.{self.extension})")
        if not path:
            return
        if not path.lower().endswith("." + self.extension):
            path += "." + self.extension
        target = QSaveFile(path)
        if not target.open(QIODevice.WriteOnly):
            self.message.setText("保存失败：" + target.errorString())
            return
        payload = inverted_screenshot(self.payload) if self.invert.isChecked() else self.payload
        if target.write(payload) != len(payload) or not target.commit():
            self.message.setText("保存失败：" + target.errorString())
            target.cancelWriting()
            return
        self.message.setText("已保存原尺寸" + ("反色" if self.invert.isChecked() else "原色") + "截图：" + path)

    def choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, "选择截图目录", self.directory.text())
        if path:
            self.directory.setText(path)
            self.settings.setValue("screenshot/directory", path)

    def open_directory(self):
        try:
            path = Path(self.directory.text()).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                raise OSError("无法打开目录")
        except Exception as error:
            self.message.setText(str(error))

    def show_original(self):
        if self.image.isNull():
            return

        # 若存在已保存的本地图片文件，优先唤起 Windows 系统默认图片查看器
        target_path = self.saved_path
        if self.invert.isChecked():
            target_path = getattr(self, "inverted_path", None)

        if target_path and os.path.exists(target_path):
            try:
                if os.name == "nt":
                    os.startfile(target_path)
                    return
                elif QDesktopServices.openUrl(QUrl.fromLocalFile(str(target_path))):
                    return
            except Exception:
                pass

        # 内部预览查看器（开启自适应与居中对齐，避免最大化时内容偏移或裁切）
        viewer = QScrollArea(self)
        viewer.setWindowFlag(Qt.Window, True)
        viewer.setAttribute(Qt.WA_DeleteOnClose)
        viewer.setWindowTitle("查看图片 — 100%（" + ("反色" if self.invert.isChecked() else "原色") + "）")
        label = QLabel()
        label.setAlignment(Qt.AlignCenter)
        label.setPixmap(QPixmap.fromImage(self.image))
        viewer.setWidget(label)
        viewer.setWidgetResizable(True)
        viewer.resize(min(self.image.width() + 40, 1200), min(self.image.height() + 60, 800))
        self.viewers.append(viewer)
        viewer.destroyed.connect(lambda: self.viewers.remove(viewer) if viewer in self.viewers else None)
        viewer.show()

    def _refresh_preview(self):
        self.preview.set_image(self.image)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_preview()
