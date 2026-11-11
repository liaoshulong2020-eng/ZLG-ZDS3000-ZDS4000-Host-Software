"""Instrument-computed FFT, transported as its native screen (not numeric bins)."""
import json
import logging
from pathlib import Path
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton
from .screenshot_panel import ScreenshotPanel, ScreenshotThread

logger = logging.getLogger('zds_scope.fft')


class HardwareFFTPanel(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel('FFT 信源'))
        self.source = QComboBox()
        self.source.addItem('沿用示波器当前信源', None)
        for channel in range(1, 5):
            self.source.addItem(f'CH{channel}', channel)
        row.addWidget(self.source)
        self.calculate = QPushButton('仪器单次计算')
        self.read_screen = QPushButton('读取当前 FFT 画面')
        row.addWidget(self.calculate)
        row.addWidget(self.read_screen)
        row.addStretch()
        layout.addLayout(row)
        self.message = QLabel('由示波器执行 FFT，沿用仪器窗函数与幅度单位。返回的是仪器画面；官方接口未提供频谱数组导出。')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.images = ScreenshotPanel(settings=QSettings('ZLG_Scope_Team', 'ZLG_ZDS_FFT_Images'))
        if not self.images.settings.contains('screenshot/directory'):
            self.images.directory.setText(str(Path('data/fft').resolve()))
        if not self.images.settings.contains('screenshot/prefix'):
            self.images.prefix.setText('FFT')
        self.images.capture.hide()
        self.preview = self.images.preview
        layout.addWidget(self.images, 1)

    def set_busy(self, busy):
        for widget in (self.calculate, self.read_screen, self.source):
            widget.setEnabled(not busy)
        self.images.set_busy(busy)


class HardwareFFTThread(ScreenshotThread):
    def __init__(self, driver, directory, parent=None, source=None, calculate=True, fmt='PNG', prefix='FFT', invert=False):
        super().__init__(driver, fmt, directory, prefix, parent, invert=invert)
        self.source, self.calculate = source, calculate
        self.fft_settings = {}
        self.stage = '准备 FFT'

    def run(self):
        try:
            with self.driver.io_lock:
                original_timeout = self.driver.timeout
                connection = self.driver.sock
                socket_timeout = connection.gettimeout() if connection is not None else None
                try:
                    self.driver.timeout = max(original_timeout, 30.0)
                    if connection is not None:
                        connection.settimeout(self.driver.timeout)
                    if self.source is not None and self.calculate:
                        self.stage = '设置 FFT 信源'
                        self.driver.send(f':MATH:FFT:SOURce CHANnel{int(self.source)}')
                    if self.calculate:
                        self.stage = '开启 FFT'
                        self.driver.send(':MATH:FFT:STATe ON')
                        self.stage = '启动 FFT 单次计算'
                        self.driver.send(':MATH:FFT:SINGle')
                        self.msleep(2000)
                    # Do not query optional FFT settings: some firmware leaves
                    # these unanswered, contaminating the subsequent image read.
                    # Reading an existing FFT screen must only request an image.
                    self.stage = '读取仪器截图'
                    super().run()
                    if self.error:
                        self.error = f'{self.stage}失败：{self.error}'
                        logger.error(self.error)
                finally:
                    self.driver.timeout = original_timeout
                    if connection is not None and self.driver.sock is connection:
                        connection.settimeout(socket_timeout)
            if self.path:
                Path(self.path).with_suffix('.json').write_text(json.dumps({
                    'type': 'instrument_fft_screen', 'numeric_spectrum_available': False,
                    'settings_queried': False,
                    'requested_source': self.source if self.calculate else None,
                    'requested_single_calculation': self.calculate,
                }, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as error:
            self.error = f'{self.stage}失败：{error}'
            logger.exception(self.error)
