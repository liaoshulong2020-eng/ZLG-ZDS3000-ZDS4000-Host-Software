"""
致远 ZDS3000 / ZDS4000 Plus 示波器专业上位机主窗口
集成硬件通信、波形可视化、实时硬件测量、电源环路 Bode 分析、
数据导出以及嵌入式 RESTful API 自动化服务
"""

import os
import sys
import threading
import time
import logging
from datetime import datetime
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, Slot, QThread, Signal, QSettings, QUrl
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QSpinBox, QPushButton, QTabWidget,
    QMessageBox, QFileDialog, QSplitter, QStatusBar, QDialog,
    QApplication, QScrollArea, QProgressBar, QGridLayout, QGroupBox, QSizePolicy, QMenu, QWidgetAction, QFormLayout
)
from PySide6.QtGui import QIcon, QPixmap, QImage, QPainter, QColor, QDesktopServices

from ..driver import ZDScopeDriver, ScopeCommunicationError
from ..storage import ScopeStorage
from ..capture import capture_archive
from ..protocol import parse_wfm
from ..data_processor import DigitalDataProcessor
from ..server import app as fastapi_app, state as api_server_state
import uvicorn

from .scope_plot import ScopePlotWidget
from .control_panel import ControlPanel
from .measure_dock import MeasureDockWidget
from .bode_dock import BodeDockWidget
from .data_export_dock import DataExportDockWidget
from .export_dialog import ExportConfigDialog
from .advanced_settings import AdvancedSettings
from .record_workbench import RecordWorkbench
from .screenshot_panel import ScreenshotPanel, ScreenshotThread
from .tool_splitter import ToolSplitter
from .fonts import configure_ui_font
from .window_chrome import SettingsPopup, WindowChrome
from .hardware_fft import HardwareFFTPanel, HardwareFFTThread
from .instrument_analysis import MathPanel, DecodePanel, InstrumentAnalysisThread


logger = logging.getLogger("zds_scope.ui")


class ConnectThread(QThread):
    """Keep TCP connection retries and IDN reads outside the GUI event loop."""

    def __init__(self, host, port, parent=None):
        super().__init__(parent)
        self.host, self.port = host, port
        self.driver = None
        self.error = None
        self.cancelled = False
        self.configuration = None

    def cancel(self):
        # QThread's interruption flag may reset when run() finishes.
        self.cancelled = True
        self.requestInterruption()

    def run(self):
        driver = None
        try:
            driver = ZDScopeDriver(host=self.host, port=self.port)
            driver.connect()
            if not self.cancelled:
                self.configuration = driver.get_scope_actual_configuration()
            if self.cancelled:
                driver.disconnect()
            else:
                self.driver = driver
        except Exception as exc:
            if driver is not None:
                driver.disconnect()
            self.error = str(exc)


class SettingsThread(QThread):
    def __init__(self, driver, parent=None, write=None, measurement_channels=None, restore_measurements=False):
        super().__init__(parent)
        self.driver = driver
        self.configuration = None
        self.error = None
        self.write = write
        self.measurement_channels = measurement_channels
        self.measurements = {}
        self.restore_measurements = restore_measurements

    def run(self):
        try:
            if self.restore_measurements:
                with self.driver.io_lock:
                    # User-specified baseline; the instrument protocol does not
                    # expose the selected measurement list for automatic backup.
                    self.driver.send(':MEASure:CLEar')
                    self.driver.send(':MEASure:VPP CHANnel1')
                return
            if self.measurement_channels is not None:
                for channel in self.measurement_channels:
                    self.measurements[channel] = self.driver.get_measurements(channel, activate_missing=True)
                return
            self.configuration = (self.driver.write_extended_setting(*self.write) if self.write
                                  else self.driver.get_scope_actual_configuration())
        except Exception as exc:
            self.error = str(exc)


class ControlThread(QThread):
    """Send a control command once; distinguish send failure from query failure."""

    def __init__(self, driver, action, parent=None):
        super().__init__(parent)
        self.driver, self.action = driver, action
        self.command_sent = False
        self.state = None
        self.trigger_state = None
        self.error = None

    def run(self):
        try:
            with self.driver.io_lock:
                method = {"run": "run", "stop": "stop", "single": "single",
                          "auto_setup": "auto_setup", "clear": "clear"}[self.action]
                getattr(self.driver, method)()
                self.command_sent = True
                self.state = self.driver.get_run_state()
                self.trigger_state = self.driver.query(":TRIG:STATE?")
        except Exception as exc:
            self.error = str(exc)


class RunStateThread(QThread):
    """Read instrument state while idle; never queue behind a waveform transfer."""

    def __init__(self, driver, parent=None):
        super().__init__(parent)
        self.driver = driver
        self.state = None

    def run(self):
        if not self.driver.io_lock.acquire(blocking=False):
            return
        sock = None
        old_timeout = None
        try:
            if not self.driver.is_connected:
                return
            sock = self.driver.sock
            old_timeout = sock.gettimeout()
            sock.settimeout(1.5)
            self.state = self.driver.get_run_state()
        except Exception:
            # Driver records communication failure; the normal reconnect path handles it.
            pass
        finally:
            if sock is not None and self.driver.sock is sock:
                sock.settimeout(old_timeout)
            self.driver.io_lock.release()


class ReadThread(QThread):
    progress = Signal(str)

    def __init__(self, driver, channels, mode, context, configuration, parent=None):
        super().__init__(parent)
        self.driver, self.channels, self.mode = driver, channels, mode
        self.context, self.configuration = context, configuration
        self.wfm = None
        self.measurements = {}
        self.error = None
        self.is_cancelled = False

    def run(self):
        try:
            if self.mode.upper() == "MEMORY":
                _, self.wfm, self.measurements, _ = capture_archive(
                    self.driver, self.channels, base_dir="data/reads", session_name="memory_read",
                    export_options={"capture_screenshot": False}, test_context=self.context,
                    cancel_token=self, stage_callback=self.progress.emit,
                    progress_callback=lambda done, total, speed, elapsed, eta:
                        self.progress.emit(f"接收 {done:,}/{total:,} 字节，{speed:.1f} MB/s，剩余约 {eta:.1f}s"))
            else:
                with self.driver.io_lock:
                    from datetime import datetime
                    folder = os.path.join("data", "reads", datetime.now().strftime("%Y%m%d_%H%M%S_%f_screen"))
                    os.makedirs(folder, exist_ok=True)
                    prior = self.driver.get_run_state()
                    try:
                        self.progress.emit("正在确认停止采集；屏幕记录也可能很大，将分块写入磁盘…")
                        self.driver.stop_and_wait()
                        _, self.wfm = self.driver.read_waveform_to_file(
                            self.channels, os.path.join(folder, "waveform.wfm"), mode=self.mode,
                            cancel_token=self,
                            progress_callback=lambda done, total, speed, elapsed, eta:
                                self.progress.emit(f"接收 {done:,}/{total:,} 字节，{speed:.1f} MB/s，剩余约 {eta:.1f}s"))
                        self.wfm.capture_metadata.update({"mode": self.mode,
                            "hardware_configuration": self.configuration,
                            "configuration_source": "last_ui_sync", "test_context": self.context,
                            "run_state_before_read": prior})
                        for channel in self.wfm.channels:
                            self.measurements[channel] = self.driver.get_measurements(channel)
                        self.wfm.capture_metadata["measurement_raw_responses"] = getattr(self.driver, "last_measurement_responses", {}) if isinstance(getattr(self.driver, "last_measurement_responses", {}), dict) else {}
                        ScopeStorage.save_json(self.wfm.capture_metadata, os.path.join(folder, "metadata.json"))
                        ScopeStorage.save_json(self.measurements, os.path.join(folder, "measurements.json"))
                    finally:
                        if str(prior).strip().upper() == "RUN" and self.driver.is_connected:
                            self.driver.run()
        except Exception as error:
            self.error = str(error)
            logger.exception("波形读取失败（mode=%s, channels=%s）", self.mode, self.channels)


class LiveStreamThread(QThread):
    """网线高速实时流后台工作线程 (非阻塞 GUI, 稳定 20~30 FPS)"""
    waveform_ready = Signal(object, object)
    fps_updated = Signal(float, float)  # (fps, latency_ms)
    error_occurred = Signal(str)

    def __init__(self, driver, channels=[1, 2], interval_ms=50, mode="SCREen"):
        super().__init__()
        self.driver = driver
        self.channels = channels
        self.interval_ms = interval_ms
        self.mode = mode
        self._running = True
        self.frame_consumed = threading.Event()
        self.frame_consumed.set()

    def run(self):
        frame_count = 0
        last_stat_time = time.time()

        while self._running:
            if not self.driver or not self.driver.is_connected:
                break

            t0 = time.time()
            try:
                # 1. 读取实时波形
                wfm = self.driver.read_waveform(channels=self.channels, mode=self.mode)
                
                # 2. 读取硬件级精确测量值
                meas = {}
                for ch in self.channels:
                    meas[ch] = self.driver.get_measurements(ch)

                t1 = time.time()
                latency_ms = (t1 - t0) * 1000.0
                frame_count += 1
                now = time.time()

                if now - last_stat_time >= 1.0:
                    fps = frame_count / (now - last_stat_time)
                    self.fps_updated.emit(fps, latency_ms)
                    frame_count = 0
                    last_stat_time = now

                self.frame_consumed.clear()
                self.waveform_ready.emit(wfm, meas)
                # Never queue full waveform objects faster than the GUI can
                # display them (especially during a drag or an open dialog).
                while self._running and not self.frame_consumed.wait(0.05):
                    pass

                # 精确控制帧间隔
                sleep_sec = max(0.005, (self.interval_ms / 1000.0) - (t1 - t0))
                time.sleep(sleep_sec)

            except Exception as e:
                self.error_occurred.emit(str(e))
                time.sleep(0.3)

    def stop(self):
        self._running = False
        self.frame_consumed.set()
        self.wait(1500)


DARK_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #0f1016;
    color: #e2e8f0;
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
}
QGroupBox {
    border: 1px solid #2d2f42;
    border-radius: 6px;
    margin-top: 10px;
    font-weight: bold;
    color: #94a3b8;
    font-size: 11px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: #1a1b26;
    border: 1px solid #32364c;
    border-radius: 4px;
    padding: 4px 6px;
    color: #f8fafc;
    selection-background-color: #3b82f6;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid #6366f1;
}
QPushButton {
    background-color: #272a3d;
    border: 1px solid #3b3f5c;
    border-radius: 4px;
    padding: 5px 10px;
    color: #f1f5f9;
}
QPushButton:hover {
    background-color: #353952;
}
QPushButton:pressed {
    background-color: #1e2030;
}
QTabWidget::pane {
    border: 1px solid #2d2f42;
    background-color: #14151f;
}
QTabBar::tab {
    background-color: #1a1b26;
    border: 1px solid #2d2f42;
    padding: 6px 14px;
    margin-right: 2px;
    color: #94a3b8;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}
QTabBar::tab:selected {
    background-color: #25283b;
    color: #ffffff;
    font-weight: bold;
    border-bottom: 2px solid #6366f1;
}
QStatusBar {
    background-color: #14151f;
    border-top: 1px solid #2d2f42;
    color: #94a3b8;
}
"""


class MainWindow(QMainWindow):
    """上位机主界面"""

    def __init__(self):
        super().__init__()
        configure_ui_font(QApplication.instance())
        self.setWindowTitle("致远 ZDS3000/4000 Plus 示波器上位机")
        self.resize(1360, 850)
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setContentsMargins(2, 2, 2, 2)
        check_icon = (Path(__file__).parent / "assets" / "check.svg").as_posix()
        self.setStyleSheet(DARK_STYLESHEET + """
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 18px; height: 18px; border: 2px solid #94a7c1; border-radius: 4px; background: #202b3c; }
QCheckBox::indicator:hover { border-color: #d8e8ff; background: #33445a; }
QCheckBox::indicator:checked { background: #31d7c0; border-color: #96fff0; image: url("__CHECK_ICON__"); }
QCheckBox::indicator:disabled { border-color: #617084; background: #333d4a; }
""".replace("__CHECK_ICON__", check_icon))

        self.driver = None
        self.current_wfm = None
        self.last_measurements = {}
        self.last_loop_data = None
        self.last_screenshot = None
        self.api_thread = None
        self.api_running = False
        self.live_thread = None
        self.connect_thread = None
        self._close_after_connect = False
        self.control_thread = None
        self._close_after_control = False
        self.run_state_thread = None
        self.settings_thread = None
        self._close_after_settings = False
        self._close_after_record = False
        self.last_configuration = {}
        self.read_thread = None
        self._close_after_read = False
        self.screenshot_thread = None
        self._close_after_screenshot = False
        self._closing = False
        self._auto_reconnect = False
        self._pending_refresh = None
        self._silent_connection = False
        self._retry_delay = 5000
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.timeout.connect(self._retry_connection)

        self._setup_ui()
        self._setup_menus()
        self._setup_connections()
        self.event_timestamps = {}
        self.timestamp_label = QLabel('')
        self.status_bar.addPermanentWidget(self.timestamp_label)
        self._last_saved_wfm = None
        self.btn_open_wfm_folder = QPushButton('打开保存文件夹')
        self.btn_open_wfm_folder.setToolTip('打开最近一次成功保存 WFM 文件的文件夹')
        self.btn_open_wfm_folder.clicked.connect(self._open_wfm_folder)
        self.status_bar.addPermanentWidget(self.btn_open_wfm_folder)
        self.btn_open_wfm_folder.hide()
        # Read only the driver's cached connection flag; no network polling here.
        self.connection_state_timer = QTimer(self)
        self.connection_state_timer.setInterval(250)
        self.connection_state_timer.timeout.connect(self._sync_connection_status)
        self.connection_state_timer.start()
        self.run_state_timer = QTimer(self)
        self.run_state_timer.setInterval(1500)
        self.run_state_timer.timeout.connect(self._poll_run_state)
        self.run_state_timer.start()

        self.status_bar.showMessage("系统就绪，支持通过网线 (TCP/IP Port 5025) 直连 ZDS 示波器")

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(QColor("#07090d"))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.setPen(QColor("#303640"))
        painter.drawRect(self.rect().adjusted(1, 1, -2, -2))

    def _setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(6)

        # 1. 顶部连接与状态工具条
        conn_bar = QWidget()
        conn_bar.setFixedHeight(30)
        conn_layout = QHBoxLayout(conn_bar)
        conn_layout.setContentsMargins(0, 0, 0, 0)
        conn_layout.setSpacing(8)

        connection_form = QWidget()
        connection_form_layout = QFormLayout(connection_form)
        self.txt_ip = QLineEdit("192.168.1.100")
        self.txt_ip.setFixedWidth(130)
        connection_form_layout.addRow("示波器 IP", self.txt_ip)

        self.spin_port = QSpinBox()
        self.spin_port.setRange(1, 65535)
        self.spin_port.setValue(5025)
        self.spin_port.setFixedWidth(70)
        connection_form_layout.addRow("端口", self.spin_port)

        self.btn_connect = QPushButton("连接")
        self.btn_connect.setStyleSheet("background-color: #2563eb; color: white; font-weight: bold; padding: 5px 12px;")
        self.btn_connect.clicked.connect(self._toggle_connection)
        connection_form_layout.addRow(self.btn_connect)
        self.btn_sync_settings = QPushButton("同步当前设置")
        self.btn_sync_settings.setEnabled(False)
        self.btn_sync_settings.clicked.connect(self._sync_current_settings)
        self.btn_sync_settings.hide()

        self.lbl_status_badge = QLabel("未连接")
        self.lbl_status_badge.setStyleSheet("background-color: #ef4444; color: white; border-radius: 3px; padding: 3px 8px; font-size: 11px;")
        connection_form_layout.addRow(self.lbl_status_badge)

        self.lbl_device_info = QLabel("设备: ---")
        self.lbl_device_info.setMaximumWidth(320)
        self.lbl_device_info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_device_info.setStyleSheet("color: #cbd5e1; font-family: Consolas; font-size: 11px;")
        connection_form_layout.addRow(self.lbl_device_info)
        self.device_menu_button = QPushButton("设备")
        self.device_menu = QMenu(self)
        connection_action = QWidgetAction(self.device_menu)
        connection_action.setDefaultWidget(connection_form)
        self.device_menu.addAction(connection_action)
        self.device_menu_button.setMenu(self.device_menu)
        conn_layout.addWidget(self.device_menu_button)

        conn_layout.addStretch()

        # 本地控制服务快捷开关
        self.btn_toggle_api = QPushButton("启动本地控制 API (Port 8000)")
        self.btn_toggle_api.setStyleSheet("background-color: #0f766e; color: white; font-size: 11px; padding: 4px 8px;")
        self.btn_toggle_api.clicked.connect(self._toggle_api_server)

        main_layout.addWidget(conn_bar)

        # 2. 中间主工作区分割器 (左侧绘图+底栏，右侧控制面板)
        splitter = QSplitter(Qt.Horizontal)

        # 左侧容器
        left_container = QWidget()
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        # 波形绘图控件
        self.plot_widget = ScopePlotWidget()
        self.waveform_splitter = ToolSplitter()
        self.waveform_splitter.addWidget(self.plot_widget)
        self.waveform_splitter.setCollapsible(0, False)
        self.waveform_splitter.toggle_requested.connect(self._toggle_tool_pane)
        self.waveform_splitter.splitterMoved.connect(self._remember_tool_size)
        self._last_tool_height = 280
        left_layout.addWidget(self.waveform_splitter, 1)

        # 底部多功能选项卡 (实时测量、环路Bode、数据、截图)
        self.bottom_tabs = QTabWidget()

        # Tab 1: 实时测量
        self.measure_dock = MeasureDockWidget()
        self.measure_dock.refresh_requested.connect(self._query_measurements)
        self.bottom_tabs.addTab(self.measure_dock, "📊 实时物理量测量")

        # Tab 2: 环路 Bode
        self.bode_dock = BodeDockWidget()
        self.bottom_tabs.addTab(self.bode_dock, "📈 电源环路与 Bode 分析")

        # Tab 3: 数据化导出与 FFT 谐波分析
        self.data_export_dock = DataExportDockWidget()
        self.data_export_dock.hide()
        self.hardware_fft = HardwareFFTPanel()
        self.hardware_fft.calculate.clicked.connect(lambda: self._request_hardware_fft(True))
        self.hardware_fft.read_screen.clicked.connect(lambda: self._request_hardware_fft(False))
        self.bottom_tabs.addTab(self.hardware_fft, "仪器 FFT")
        self.math_panel = MathPanel()
        self.decode_panel = DecodePanel()
        self.math_panel.requested.connect(self._request_instrument_analysis)
        self.decode_panel.requested.connect(self._request_instrument_analysis)
        self.math_panel.screen_requested.connect(self._view_math_screen)
        self.bottom_tabs.addTab(self.math_panel, 'Math 运算')
        self.bottom_tabs.addTab(self.decode_panel, 'Decode 解码')
        self.advanced_settings = AdvancedSettings()
        self.advanced_settings.write_requested.connect(self._write_advanced_setting)
        self.settings_dialog = SettingsPopup(self)
        self.settings_dialog.resize(490, 540)
        self.settings_dialog.layout().addWidget(self.advanced_settings)
        self.record_workbench = RecordWorkbench()
        self.record_workbench.record_loaded.connect(self._on_project_loaded)
        self.record_workbench.interval_selected.connect(self.plot_widget.show_interval)
        self.record_workbench.busy_changed.connect(self._on_record_busy)
        self.record_workbench.work_finished.connect(self._on_record_finished)
        self.record_workbench.progress_changed.connect(self._on_record_progress)
        self.record_workbench.state_provider = self._project_state
        self.record_workbench.can_start = self._can_record_task
        self.data_export_dock.save_csv_requested.connect(self._save_csv_file)
        self.record_tab = QScrollArea()
        self.record_tab.setWidgetResizable(True)
        self.record_tab.setWidget(self.record_workbench)
        self.record_tab.setParent(self)
        self.record_tab.hide()

        # Tab 4: 屏幕截图原生预览
        self.tab_screenshot = ScreenshotPanel()
        self.tab_screenshot.capture_requested.connect(self._capture_screen_from_scope)
        self.lbl_sc_preview = self.tab_screenshot.preview
        self.bottom_tabs.addTab(self.tab_screenshot, "📷 示波器原生截图预览")

        # The lower tool pane is optional, resizable and fully collapsible.
        self.tool_pane = QWidget()
        self.tool_pane.setMinimumHeight(0)
        self.tool_pane.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        tool_layout = QVBoxLayout(self.tool_pane)
        tool_layout.setContentsMargins(0, 0, 0, 0)
        tool_layout.setSpacing(3)
        tool_header = QHBoxLayout()
        self.tool_pane_title = QLabel("测量")
        self.tool_pane_title.setStyleSheet("font-weight: bold; color: #a4b8e5;")
        self.collapse_tool_button = QPushButton("收起 ↓")
        self.collapse_tool_button.clicked.connect(self._collapse_tool_pane)
        tool_header.addWidget(self.tool_pane_title)
        tool_header.addStretch()
        tool_header.addWidget(self.collapse_tool_button)
        tool_layout.addLayout(tool_header)
        self.bottom_tabs.tabBar().hide()
        self.bottom_tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.bottom_tabs.setMinimumHeight(0)
        tool_layout.addWidget(self.bottom_tabs, 1)
        self.waveform_splitter.addWidget(self.tool_pane)
        self.waveform_splitter.setStretchFactor(0, 1)
        self.waveform_splitter.setStretchFactor(1, 0)
        self.waveform_splitter.setSizes([1000, 0])
        splitter.addWidget(left_container)

        # 右侧示波器控制面板
        self.control_panel = ControlPanel()
        conn_layout.removeWidget(self.device_menu_button)
        self.control_panel.layout().insertWidget(0, self.device_menu_button)
        self.ui_settings = QSettings('ZLG_Scope_Team', 'ZLG_ZDS_Scope_UI')
        for ch, widgets in self.control_panel.ch_widgets.items():
            name = self.ui_settings.value(f'channels/{ch}/name', str(ch))
            widgets['name_edit'].setText(name)
            self.plot_widget.set_channel_name(ch, name)
        self.control_panel.channel_name_changed.connect(self._rename_channel)
        self.control_panel.channel_center_changed.connect(self.plot_widget.set_channel_centered)
        self.plot_widget.channel_settings_requested.connect(self._open_channel_settings)
        self.control_panel.setFixedWidth(228)
        conn_layout.addWidget(self.control_panel.btn_read_wave)
        self.window_chrome = WindowChrome(self, conn_bar)
        conn_layout.addWidget(self.window_chrome.controls)
        self.control_panel.btn_export.clicked.connect(self._save_wfm_file)
        self.control_panel.btn_export.setToolTip('直接保存当前原始波形为 WFM 文件，记住上次目录，不创建记录子文件夹。')
        self.control_panel.layout().setSpacing(10)
        functions = QGroupBox("功能")
        function_layout = QGridLayout(functions)
        function_layout.setContentsMargins(6, 8, 6, 8)
        self.function_buttons = {}
        features = [("Math", lambda: self._open_tool(self.math_panel, "Math 仪器运算")),
                    ("Decode", lambda: self._open_tool(self.decode_panel, "Decode 仪器事件表")),
                    ("FFT", lambda: self._request_hardware_fft(True)),
                    ("电源环路", lambda: self._open_tool(self.bode_dock, "电源环路 / Bode")),
                    ("测量", self._toggle_measurements),
                    ("数据导出", self._show_file_tools),
                    ("采集设置", lambda: self._show_parameter_settings((":ACQUIRE",), "采集设置"))]
        for index, (title, callback) in enumerate(features):
            button = QPushButton(title)
            button.setMinimumHeight(36)
            button.clicked.connect(callback)
            function_layout.addWidget(button, index // 2, index % 2)
            self.function_buttons[title] = button
        self.function_buttons['测量'].setToolTip('展开并读取硬件测量；再次点击收起，恢复你指定的原始项目：仅 CH1 Vpp。普通波形刷新不会增加测量项。')
        self.function_buttons['数据导出'].setToolTip('打开数据导出面板：WFM、全量 CSV、完整记录包及离线工程。')
        self.control_panel.layout().addWidget(functions)
        self.control_panel.layout().addStretch(1)
        trigger_group = QGroupBox("触发")
        trigger_layout = QVBoxLayout(trigger_group)
        trigger = self.plot_widget.bottom_bar.card_trigger
        self.plot_widget.bottom_bar.layout().removeWidget(trigger)
        trigger_layout.addWidget(trigger)
        trigger.setToolTip("单击打开触发设置")
        trigger.setCursor(Qt.PointingHandCursor)
        trigger.clicked.connect(lambda: self._show_parameter_settings((":TRIG",), "触发设置"))
        self.trigger_settings_button = QPushButton("触发设置")
        self.trigger_settings_button.clicked.connect(lambda: self._show_parameter_settings((":TRIG",), "触发设置"))
        trigger_layout.addWidget(self.trigger_settings_button)
        self.control_panel.layout().addWidget(trigger_group)
        splitter.addWidget(self.control_panel)
        self.plot_widget.bottom_bar.channel_cursor_selected.connect(self._open_channel_settings)
        self.plot_widget.bottom_bar.card_timebase.clicked.connect(self.control_panel.show_timebase_settings)
        self.control_panel.layout().setContentsMargins(2, 2, 2, 2)
        self.channel_advanced_button = QPushButton("探头、阻抗与反相…")
        self.channel_advanced_button.clicked.connect(lambda: self._show_parameter_settings(
            (f":CHANNEL{self.control_panel.ch_tabs.currentIndex() + 1}:",), "通道输入与探头设置"))
        self.control_panel.channel_dialog.layout().addWidget(self.channel_advanced_button)

        splitter.setStretchFactor(0, 8)
        splitter.setStretchFactor(1, 2)
        main_layout.addWidget(splitter, 1)

        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.record_task_label = QLabel()
        self.record_task_progress = QProgressBar()
        self.record_task_progress.setFixedWidth(130)
        self.record_task_cancel = QPushButton("取消离线任务")
        self.record_task_cancel.clicked.connect(self.record_workbench._cancel_analysis)
        for item in (self.record_task_label, self.record_task_progress, self.record_task_cancel):
            self.status_bar.addPermanentWidget(item)
            item.hide()

    def _setup_menus(self):
        menubar = self.menuBar()

        # 文件菜单
        file_menu = menubar.addMenu("文件 (&File)")
        act_save_wfm = file_menu.addAction("保存波形为 .wfm (原生二进制)")
        act_save_wfm.triggered.connect(self._save_wfm_file)

        act_save_csv = file_menu.addAction("保存波形为 .csv（派生物理量及原始码）")
        act_save_csv.triggered.connect(self._save_csv_file)

        act_open_wfm = file_menu.addAction("打开已有 .wfm 波形文件...")
        act_open_wfm.triggered.connect(self._open_wfm_file)
        file_menu.addAction("保存完整离线工程...").triggered.connect(self.record_workbench._save_project)
        file_menu.addAction("打开完整离线工程...").triggered.connect(self.record_workbench._load_project)

        file_menu.addSeparator()
        act_snapshot = file_menu.addAction("导出完整测试快照包 (Save Snapshot)")
        act_snapshot.triggered.connect(self._save_snapshot)

        file_menu.addSeparator()
        act_exit = file_menu.addAction("退出 (&Exit)")
        act_exit.triggered.connect(self.close)

        # 工具菜单
        tools_menu = menubar.addMenu("工具 (&Tools)")

        act_open_dir = tools_menu.addAction("打开测试归档数据文件夹 (Captures)")
        act_open_dir.triggered.connect(self._open_captures_dir)

        # 帮助菜单
        help_menu = menubar.addMenu("帮助 (&Help)")
        act_about = help_menu.addAction("关于致远示波器上位机")
        act_about.triggered.connect(self._show_about)
        menubar.hide()

    def _open_tool(self, widget, title, recalculate=False):
        self.bottom_tabs.setCurrentWidget(widget)
        self.tool_pane_title.setText(title)
        if self.waveform_splitter.sizes()[1] < 40:
            total = max(1, sum(self.waveform_splitter.sizes()))
            height = min(self._last_tool_height, max(150, int(total * .45)))
            self.waveform_splitter.setSizes([max(250, total - height), height])
        if recalculate and self.current_wfm:
            self._on_data_recalculate()

    def _remember_tool_size(self, position, index):
        sizes = self.waveform_splitter.sizes()
        if len(sizes) > 1 and sizes[1] > 80:
            self._last_tool_height = sizes[1]

    def _collapse_tool_pane(self):
        self._remember_tool_size(0, 1)
        self.waveform_splitter.setSizes([max(1, sum(self.waveform_splitter.sizes())), 0])
        if getattr(self, '_measurement_display_changed', False):
            self._restore_measurements()

    def _toggle_measurements(self):
        if self.bottom_tabs.currentWidget() is self.measure_dock and self.waveform_splitter.sizes()[1] > 0:
            self._collapse_tool_pane()
        else:
            self._open_tool(self.measure_dock, '硬件测量（收起后恢复 CH1 Vpp）')
            if self.driver and self.driver.is_connected:
                self._query_measurements()

    def _restore_measurements(self):
        if not getattr(self, '_measurement_display_changed', False):
            return
        if self.settings_thread is not None:
            self._restore_measurements_pending = True
            return
        if not self.driver or not self.driver.is_connected:
            self.status_bar.showMessage('设备已断开，未能恢复 CH1 Vpp；请连接后收起测量重试。')
            return
        self._restore_measurements_pending = False
        self.control_panel.setEnabled(False)
        self.btn_connect.setEnabled(False)
        self.settings_thread = SettingsThread(self.driver, self, restore_measurements=True)
        self.settings_thread.finished.connect(self._on_settings_finished)
        self.settings_thread.start()

    def _toggle_tool_pane(self):
        if self.waveform_splitter.sizes()[1] > 0:
            self._collapse_tool_pane()
        else:
            self._open_tool(self.bottom_tabs.currentWidget(), self.tool_pane_title.text())

    def _show_parameter_settings(self, prefixes, title):
        self.control_panel.channel_dialog.hide()
        self.advanced_settings.set_filter(prefixes)
        self.settings_dialog.setWindowTitle(title)
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()
        if self.driver and self.driver.is_connected and not any((self.settings_thread, self.control_thread, self.read_thread, self.screenshot_thread)) and not (self.live_thread and self.live_thread.isRunning()):
            self._sync_current_settings()

    def _open_channel_settings(self, channel):
        checkbox = self.control_panel.ch_widgets[channel]['cb_center']
        checkbox.blockSignals(True)
        checkbox.setChecked(channel in self.plot_widget._center_overrides)
        checkbox.blockSignals(False)
        self.control_panel.show_channel_settings(channel)

    def _rename_channel(self, channel, name):
        self.ui_settings.setValue(f'channels/{channel}/name', name)
        self.plot_widget.set_channel_name(channel, name)

    def _view_math_screen(self):
        self._open_tool(self.tab_screenshot, 'Math 仪器画面')
        self._capture_screen_from_scope()

    def _request_instrument_analysis(self, action, options):
        if not self._check_connected() or not self._can_record_task():
            return
        panel = self.decode_panel if action == 'decode' else self.math_panel
        panel.message.setText('正在读取仪器解码事件…' if action == 'decode' else '正在执行仪器 Math 操作…')
        self.math_panel.set_busy(True)
        self.decode_panel.set_busy(True)
        self.control_panel.setEnabled(False)
        self.btn_connect.setEnabled(False)
        self.settings_thread = InstrumentAnalysisThread(self.driver, action, options, self)
        self.settings_thread.finished.connect(self._on_instrument_analysis_finished)
        self.settings_thread.start()

    def _on_instrument_analysis_finished(self):
        worker = self.settings_thread
        if worker is None:
            return
        self.settings_thread = None
        self.math_panel.set_busy(False)
        self.decode_panel.set_busy(False)
        self.control_panel.setEnabled(True)
        self.btn_connect.setEnabled(True)
        panel = self.decode_panel if worker.action == 'decode' else self.math_panel
        if worker.error:
            panel.message.setText('操作失败：' + worker.error + '；请检查仪器支持情况和当前设置。')
        elif worker.action == 'decode':
            self.decode_panel.show_events(worker.result)
        else:
            self.math_panel.message.setText('仪器当前 Math 模式：' + str(worker.result) + '。可点击“查看仪器画面”获取结果截图。')
        self._sync_connection_status()
        worker.deleteLater()
        if self._close_after_settings:
            QTimer.singleShot(0, self.close)

    def _request_hardware_fft(self, calculate=True):
        self._open_tool(self.hardware_fft, '仪器 FFT（原生画面）')
        if not self._check_connected() or not self._can_record_task():
            return
        try:
            fmt, directory, prefix = self.hardware_fft.images.options()
        except ValueError as error:
            self.hardware_fft.message.setText(str(error))
            return
        self.hardware_fft.message.setText('正在调用仪器 FFT 并读取屏幕；保留仪器当前窗函数与幅度单位…' if calculate else '正在直接读取仪器当前画面，不查询或更改 FFT 参数…')
        self.hardware_fft.set_busy(True)
        self.control_panel.setEnabled(False)
        self.btn_connect.setEnabled(False)
        self.record_workbench.setEnabled(False)
        self.screenshot_thread = HardwareFFTThread(self.driver, directory, self,
                                                   self.hardware_fft.source.currentData(), calculate,
                                                   fmt=fmt, prefix=prefix, invert=self.hardware_fft.images.invert.isChecked())
        self.screenshot_thread.finished.connect(self._on_screenshot_finished)
        self.screenshot_thread.start()

    def _show_file_tools(self):
        if not hasattr(self, "file_tools"):
            self.file_tools = QWidget()
            layout = QGridLayout(self.file_tools)
            actions = [("导出完整记录包", self._save_snapshot), ("导出全量 CSV", self._save_csv_file),
                       ("保存原始 WFM", self._save_wfm_file), ("打开 WFM", self._open_wfm_file),
                       ("保存离线工程", self.record_workbench._save_project),
                       ("打开离线工程", self.record_workbench._load_project),
                       ("打开记录目录", self._open_captures_dir)]
            for index, (text, callback) in enumerate(actions):
                button = QPushButton(text)
                descriptions = {
                    '导出完整记录包': '重新读取已启用通道的完整存储记录。包含原始 WFM、测量值、设置快照、元数据、完整性报告与文件校验清单；截图、CSV 和分析结果按导出选项及实际获取情况保存。不是只保存当前显示的抽样曲线。',
                    '导出全量 CSV': '导出当前记录的全部采样点，包含时间、各通道物理值和原始码；不重新采集。',
                    '保存原始 WFM': '保存当前记录的原始波形及仪器头信息，体积通常比 CSV 小，可再次在本软件打开；不重新采集。',
                    '打开 WFM': '从磁盘读取已有原始波形，在未连接示波器时也可查看，不向设备写入设置。',
                    '保存离线工程': '保存当前原始记录和工程状态，包括配置、测量、光标及分析结果，便于以后恢复查看。',
                    '打开离线工程': '校验并恢复保存的记录和工程状态，不修改示波器设置。',
                    '打开记录目录': '在资源管理器中打开采集记录的保存目录。',
                }
                button.setToolTip(descriptions[text])
                button.setMinimumHeight(48)
                button.clicked.connect(callback)
                layout.addWidget(button, index // 2, index % 2)
            self.bottom_tabs.addTab(self.file_tools, "数据导出")
        self._open_tool(self.file_tools, "数据与工程")

    def _show_more_tools(self):
        if not hasattr(self, "more_tools"):
            self.more_tools = QWidget()
            layout = QVBoxLayout(self.more_tools)
            layout.addWidget(self.btn_toggle_api)
            about = QPushButton("关于示波器上位机")
            about.clicked.connect(self._show_about)
            layout.addWidget(about)
            layout.addStretch()
            self.bottom_tabs.addTab(self.more_tools, "更多")
        self._open_tool(self.more_tools, "更多功能")

    def _setup_connections(self):
        # 控制面板信号
        self.control_panel.run_action_requested.connect(self._on_run_action)
        self.control_panel.read_waveform_requested.connect(self._read_waveform_from_scope)
        self.control_panel.capture_screen_requested.connect(self._capture_screen_from_scope)
        self.control_panel.continuous_mode_changed.connect(self._on_continuous_mode_changed)
        self.control_panel.timebase_changed.connect(self._on_timebase_changed)
        self.control_panel.channel_changed.connect(self._on_channel_changed)
        self.control_panel.memory_depth_changed.connect(self._on_memory_depth_changed)

        # Bode 分析信号
        self.bode_dock.query_loop_requested.connect(self._query_loop_data_from_scope)

        # 数字化导出信号
        self.data_export_dock.recalculate_requested.connect(self._on_data_recalculate)

    # --------------------------------------------------------------------------
    # 连接管理
    # --------------------------------------------------------------------------
    def _toggle_connection(self):
        if self.screenshot_thread is not None:
            self.status_bar.showMessage("正在截图，完成后可断开连接。")
            return
        if self.read_thread is not None:
            self.read_thread.is_cancelled = True
            self.btn_connect.setEnabled(False)
            self.btn_connect.setText("正在取消读取…")
            return
        if self.control_thread is not None or self.settings_thread is not None:
            return
        if self.connect_thread is not None:
            self.connect_thread.cancel()
            self.btn_connect.setEnabled(False)
            self.btn_connect.setText("正在取消…")
            self.status_bar.showMessage("正在取消连接，等待当前网络请求结束…")
            return
        if self.driver and self.driver.is_connected:
            self._disconnect_scope()
        else:
            self._connect_scope()

    def _connect_scope(self, silent=False):
        if self.connect_thread is not None:
            return
        self._silent_connection = silent
        host = self.txt_ip.text().strip()
        port = self.spin_port.value()
        if not host:
            self.status_bar.showMessage('请输入示波器 IP 地址。')
            return
        self.txt_ip.setEnabled(False)
        self.spin_port.setEnabled(False)
        self.control_panel.setEnabled(False)
        self.btn_toggle_api.setEnabled(False)
        self.btn_sync_settings.setEnabled(False)
        self.btn_connect.setText("连接")
        self.btn_connect.setEnabled(False)
        self.lbl_status_badge.setText("连接中…")
        self.lbl_status_badge.setStyleSheet("background-color: #d97706; color: white; padding: 3px 8px;")
        self.status_bar.showMessage(f"正在连接 {host}:{port} 并读取仪器当前设置（不写入初始化值），可取消…")
        self.connect_thread = ConnectThread(host, port, self)
        self.connect_thread.finished.connect(self._on_connect_finished)
        self.connect_thread.start()

    @Slot()
    def _on_connect_finished(self):
        worker = self.connect_thread
        if worker is None:
            return
        self.connect_thread = None
        self.txt_ip.setEnabled(True)
        self.spin_port.setEnabled(True)
        self.control_panel.setEnabled(True)
        self.btn_toggle_api.setEnabled(True)
        self.btn_connect.setEnabled(True)
        cancelled = worker.cancelled or self._close_after_connect
        if worker.driver is not None and not cancelled:
            self._auto_reconnect = True
            self._retry_delay = 5000
            self.reconnect_timer.stop()
            self.driver = worker.driver
            self._apply_scope_settings(worker.configuration)
            idn = self.driver.device_info
            api_server_state.driver = self.driver
            api_server_state.last_wfm = None
            api_server_state.last_loop = None
            # Reconnection must not discard the last captured record.
            self.lbl_status_badge.setText("已连接")
            self.lbl_status_badge.setStyleSheet("background-color: #10b981; color: white; border-radius: 3px; padding: 3px 8px; font-size: 11px;")
            self.lbl_device_info.setText(f"设备: {idn}")
            self.btn_connect.setText("断开")
            self.btn_connect.setStyleSheet("background-color: #dc2626; color: white; font-weight: bold; padding: 5px 12px;")
            self.btn_sync_settings.setEnabled(True)

            self.status_bar.showMessage(f"已连接并同步设备当前设置: {idn}。未向设备写入初始化设置。")
        else:
            if worker.driver is not None:
                worker.driver.disconnect()
            self.driver = None
            api_server_state.driver = None
            self.lbl_status_badge.setText("未连接")
            self.lbl_status_badge.setStyleSheet("background-color: #ef4444; color: white; padding: 3px 8px;")
            self.btn_connect.setText("连接")
            self.status_bar.showMessage("已取消连接" if cancelled else f"连接失败: {worker.error}")
            if not cancelled and not self._silent_connection:
                QMessageBox.critical(self, "连接失败", f"无法连接至示波器 ({worker.host}:{worker.port}):\n{worker.error}\n\n"
                                     "请关闭其他示波器客户端和网络测试窗口后重试。\n"
                                     "若持续超时，请重启示波器，再检查网口 IP 和远程控制设置。")
        worker.deleteLater()
        if cancelled:
            self._auto_reconnect = False
            self._pending_refresh = None
            self.reconnect_timer.stop()
        elif self.driver and self.driver.is_connected:
            if self._pending_refresh is not None:
                mode, self._pending_refresh = self._pending_refresh, None
                QTimer.singleShot(0, lambda: self._read_waveform_from_scope(mode) if not self._closing else None)
        elif self._auto_reconnect:
            self._schedule_reconnect()
        if self._close_after_connect:
            QTimer.singleShot(0, self.close)

    def _disconnect_scope(self):
        self._auto_reconnect = False
        self._pending_refresh = None
        self.reconnect_timer.stop()
        if self.live_thread:
            self.live_thread.stop()
            self.live_thread = None
        self.control_panel.cb_live.setChecked(False)

        if self.driver:
            self.driver.disconnect()
            self.driver = None

        api_server_state.driver = None
        self.btn_sync_settings.setEnabled(False)
        self.lbl_status_badge.setText("未连接")
        self.lbl_status_badge.setStyleSheet("background-color: #ef4444; color: white; border-radius: 3px; padding: 3px 8px; font-size: 11px;")
        self.lbl_device_info.setText("设备: ---")
        self.btn_connect.setText("连接")
        self.btn_connect.setStyleSheet("background-color: #2563eb; color: white; font-weight: bold; padding: 5px 12px;")
        self.advanced_settings.connected = False
        self.advanced_settings.apply.setEnabled(False)
        self.status_bar.showMessage("示波器已断开连接")

    def _apply_scope_settings(self, config):
        self.last_configuration = config
        self.control_panel.apply_scope_configuration(config)
        self.advanced_settings.set_configuration(config, bool(self.driver and self.driver.is_connected))
        for channel in range(1, 5):
            units = config.get("extended_settings", {}).get(f":CHANnel{channel}:UNITs", "VOLTage")
            self.measure_dock.set_channel_unit(channel, "A" if units.upper() == "AMPERE" else "V")
        for channel, values in config.get("channels", {}).items():
            if "enabled" in values:
                self.plot_widget.set_channel_visible(int(channel), values["enabled"])
        self.plot_widget.update_scope_config(config)

    def _sync_current_settings(self):
        if not self._check_connected():
            return
        if self.live_thread is not None and self.live_thread.isRunning():
            self.status_bar.showMessage("请先关闭实时流，再同步当前设置。")
            return
        self.control_panel.setEnabled(False)
        self.btn_connect.setEnabled(False)
        self.btn_sync_settings.setEnabled(False)
        self.btn_sync_settings.setText("正在同步…")
        self.status_bar.showMessage("正在后台读取仪器当前设置，不改变采集状态或设备参数…")
        self.settings_thread = SettingsThread(self.driver, self)
        self.settings_thread.finished.connect(self._on_settings_finished)
        self.settings_thread.start()

    def _write_advanced_setting(self, command, value):
        if not self._check_connected():
            return
        if self.live_thread is not None and self.live_thread.isRunning():
            self.status_bar.showMessage("请先关闭实时流，再修改设备设置。")
            return
        self.control_panel.setEnabled(False)
        self.btn_connect.setEnabled(False)
        self.btn_sync_settings.setEnabled(False)
        self.advanced_settings.apply.setEnabled(False)
        self.status_bar.showMessage("正在更新设置并读取设备确认…")
        self.settings_thread = SettingsThread(self.driver, self, write=(command, value))
        self.settings_thread.finished.connect(self._on_settings_finished)
        self.settings_thread.start()

    @Slot()
    def _on_settings_finished(self):
        worker = self.settings_thread
        if worker is None:
            return
        self.settings_thread = None
        self.control_panel.setEnabled(True)
        self.btn_connect.setEnabled(True)
        self.btn_sync_settings.setText("同步当前设置")
        self.btn_sync_settings.setEnabled(bool(self.driver and self.driver.is_connected))
        self.advanced_settings.apply.setEnabled(bool(self.driver and self.driver.is_connected))
        self.measure_dock.refresh_button.setEnabled(True)
        if worker.restore_measurements:
            if worker.error:
                self.status_bar.showMessage(f'恢复 CH1 Vpp 失败：{worker.error}；可再次展开并收起测量重试。')
            else:
                self._measurement_display_changed = False
                self.status_bar.showMessage('已恢复示波器测量显示：仅 CH1 Vpp。')
            worker.deleteLater()
            if self._close_after_settings:
                QTimer.singleShot(0, self.close)
            return
        if worker.measurement_channels is not None:
            if worker.error:
                self.measure_dock.message.setText(f"测量读取失败：{worker.error}")
                self._sync_connection_status()
            else:
                for channel, values in worker.measurements.items():
                    self.last_measurements[channel] = values
                    self.measure_dock.update_measurements(channel, values)
                count = sum(value is not None for values in worker.measurements.values() for value in values.values())
                self.measure_dock.message.setText(f"已更新 {len(worker.measurements)} 个通道，{count} 项有效测量。无效项可稍后重试。")
                if self.current_wfm:
                    self.current_wfm.capture_metadata["measurement_raw_responses"] = self.driver.last_measurement_responses
                    self.data_export_dock.update_data_view(self.current_wfm, self.last_measurements, self.last_loop_data)
            worker.deleteLater()
            if getattr(self, '_restore_measurements_pending', False) or self._close_after_settings:
                self._restore_measurements()
                return
            if self._close_after_settings:
                QTimer.singleShot(0, self.close)
            return
        if not self._close_after_settings:
            if worker.error:
                self._sync_connection_status()
                operation = "设置写入/读回失败，请重新同步核验" if worker.write else "同步设置失败，保留上次显示值（未写入）"
                self.status_bar.showMessage(f"{operation}: {worker.error}")
            elif worker.driver is self.driver and self.driver.is_connected:
                self._apply_scope_settings(worker.configuration)
                self.status_bar.showMessage("已同步仪器当前设置，后续更改只发送被修改的项目。")
        worker.deleteLater()
        if self._close_after_settings:
            QTimer.singleShot(0, self.close)

    # --------------------------------------------------------------------------
    # 控制与读取
    # --------------------------------------------------------------------------
    def _on_run_action(self, action: str):
        if not self._check_connected():
            return
        if action not in {"run", "stop", "single", "auto_setup", "clear"}:
            return
        self.control_panel.setEnabled(False)
        self.btn_connect.setEnabled(False)
        self.status_bar.showMessage(f"正在执行 {action.upper()}，等待设备状态响应…")
        self.control_thread = ControlThread(self.driver, action, self)
        self.control_thread.finished.connect(self._on_control_finished)
        self.control_thread.start()

    @Slot()
    def _on_control_finished(self):
        worker = self.control_thread
        if worker is None:
            return
        self.control_thread = None
        self.control_panel.setEnabled(True)
        self.btn_connect.setEnabled(True)
        self._sync_connection_status()
        self._apply_run_state(worker.state)
        if worker.error:
            stage = "命令已发送，但状态查询失败" if worker.command_sent else "命令发送失败"
            self.status_bar.showMessage(f"{worker.action.upper()}：{stage}: {worker.error}。请勿重复触发，先检查仪器状态。")
        else:
            self.control_panel.set_run_state_visual(worker.state)
            self.plot_widget.update_run_state(worker.state)
            trigger = "等待触发" if str(worker.trigger_state).upper() == "IFTRIG" else str(worker.trigger_state)
            self.status_bar.showMessage(f"执行动作: {worker.action.upper()}，运行状态: {worker.state}；触发状态: {trigger}")
        worker.deleteLater()
        if self._close_after_control:
            QTimer.singleShot(0, self.close)

    def _read_waveform_from_scope(self, mode: str = "SCREen"):
        if self.read_thread is not None:
            self.read_thread.is_cancelled = True
            self.control_panel.btn_read_wave.setEnabled(False)
            self.status_bar.showMessage("正在取消波形刷新…")
            return
        if self.connect_thread is not None:
            self._pending_refresh = mode
            return
        if not self.driver or not self.driver.is_connected:
            self._pending_refresh = mode
            self._auto_reconnect = True
            self.plot_widget.clear_for_refresh()
            self.plot_widget.show_read_status('正在连接示波器，连接成功后自动读取…')
            self._retry_connection()
            return
        if not self._check_connected():
            return
        try:
            channels = [ch for ch, widgets in self.control_panel.ch_widgets.items()
                        if widgets["cb_enable"].isChecked()]
            if not channels:
                raise ValueError("请至少开启一个通道")
            context = self.record_workbench.context()
        except ValueError as error:
            self.status_bar.showMessage(str(error))
            return
        if self.live_thread is not None and self.live_thread.isRunning():
            self.status_bar.showMessage("请先关闭实时流，再执行单次读取。")
            return
        self.control_panel.setEnabled(False)
        self.record_workbench.setEnabled(False)
        self.btn_sync_settings.setEnabled(False)
        self.advanced_settings.apply.setEnabled(False)
        self.btn_connect.setEnabled(False)
        self.control_panel.btn_read_wave.setText("取消刷新")
        self.control_panel.btn_read_wave.setEnabled(True)
        self.status_bar.showMessage(f"正在后台读取 {mode} 数据…")
        self.plot_widget.clear_for_refresh()
        self.read_thread = ReadThread(self.driver, channels, mode, context, self.last_configuration, self)
        self.read_thread.progress.connect(self.status_bar.showMessage)
        self.read_thread.progress.connect(self.plot_widget.show_read_status)
        self.read_thread.finished.connect(self._on_read_finished)
        self.read_thread.start()

    @Slot()
    def _on_read_finished(self):
        worker = self.read_thread
        if worker is None:
            return
        self.read_thread = None
        self.control_panel.btn_read_wave.setText("刷新波形")
        self.control_panel.setEnabled(True)
        self.record_workbench.setEnabled(True)
        self.btn_connect.setEnabled(True)
        connected = bool(self.driver and self.driver.is_connected)
        self.btn_connect.setText("断开" if connected else "连接")
        self.btn_sync_settings.setEnabled(connected)
        self.advanced_settings.apply.setEnabled(connected)
        if worker.wfm is not None and not self._close_after_read:
            self.current_wfm = worker.wfm
            self.last_measurements = worker.measurements
            api_server_state.last_wfm = worker.wfm
            self.record_workbench.set_record(worker.wfm)
            self.plot_widget.update_waveform(worker.wfm)
            self.plot_widget.auto_fit()
            self._stamp_event('读取完成')
            for channel, measured in worker.measurements.items():
                if channel not in worker.wfm.channels:
                    continue
                self.measure_dock.set_channel_unit(channel, "A" if worker.wfm.channels[channel].info.probe_type == 1 else "V")
                self.measure_dock.update_measurements(channel, measured)
            self.data_export_dock.update_data_view(worker.wfm, worker.measurements)
        self._sync_connection_status()
        if worker.error:
            self.status_bar.showMessage(f"{'波形已接收，但后续处理失败' if worker.wfm else '读取未完成'}: {worker.error}")
        else:
            self.status_bar.showMessage("读取完成。MEMOry 记录保存在 data/reads；显示抽样，原始文件保留全部返回字节。" if worker.mode.upper() == "MEMORY"
                                        else "屏幕范围记录读取完成，原始文件已保存到 data/reads；显示抽样，不代表完整存储记录。")
        if worker.wfm is None:
            self.plot_widget.show_read_status('刷新已取消' if worker.is_cancelled else '读取失败，请重试')
            if not worker.is_cancelled and self._auto_reconnect and not (self.driver and self.driver.is_connected):
                self._pending_refresh = worker.mode
                self.plot_widget.show_read_status('连接中断，正在等待自动重连…')
                self._schedule_reconnect()
        worker.deleteLater()
        if self._close_after_read:
            QTimer.singleShot(0, self.close)

    def _query_measurements(self):
        if not self._check_connected():
            return
        if self.live_thread and self.live_thread.isRunning():
            self.measure_dock.message.setText("自动刷新正在更新测量；可关闭自动刷新后单独读取。")
            return
        channels = [ch for ch, widgets in self.control_panel.ch_widgets.items() if widgets["cb_enable"].isChecked()]
        if not channels:
            self.measure_dock.message.setText("请先开启需要测量的通道。")
            return
        self.measure_dock.refresh_button.setEnabled(False)
        self.measure_dock.message.setText("正在启用缺失的测量项并等待读数…")
        self.control_panel.setEnabled(False)
        self.btn_connect.setEnabled(False)
        self.btn_sync_settings.setEnabled(False)
        self.advanced_settings.apply.setEnabled(False)
        self.settings_thread = SettingsThread(self.driver, self, measurement_channels=channels)
        self._measurement_display_changed = True
        self.settings_thread.finished.connect(self._on_settings_finished)
        self.settings_thread.start()

    def _capture_screen_from_scope(self):
        if not self._check_connected():
            return
        if self.live_thread is not None and self.live_thread.isRunning():
            self.status_bar.showMessage("请先关闭实时流，再获取并保存截图。")
            return
        try:
            fmt, directory, prefix = self.tab_screenshot.options()
        except ValueError as error:
            self.status_bar.showMessage(str(error))
            return
        self._open_tool(self.tab_screenshot, "示波器截图")
        self.tab_screenshot.set_busy(True)
        self.tab_screenshot.message.setText("正在后台接收仪器屏幕并自动保存，完成后可复制或另存为…")
        self.control_panel.setEnabled(False)
        self.btn_connect.setEnabled(False)
        self.btn_sync_settings.setEnabled(False)
        self.advanced_settings.apply.setEnabled(False)
        self.record_workbench.setEnabled(False)
        self.status_bar.showMessage("正在后台截图…")
        self.screenshot_thread = ScreenshotThread(self.driver, fmt, directory, prefix, self,
                                                  invert=self.tab_screenshot.invert.isChecked())
        self.screenshot_thread.finished.connect(self._on_screenshot_finished)
        self.screenshot_thread.start()

    @Slot()
    def _on_screenshot_finished(self):
        worker = self.screenshot_thread
        if worker is None:
            return
        self.screenshot_thread = None
        self.control_panel.setEnabled(True)
        self.record_workbench.setEnabled(True)
        self.btn_connect.setEnabled(True)
        connected = bool(self.driver and self.driver.is_connected)
        self.btn_sync_settings.setEnabled(connected)
        self.advanced_settings.apply.setEnabled(connected)
        self.tab_screenshot.set_busy(False)
        if isinstance(worker, HardwareFFTThread):
            self.hardware_fft.set_busy(False)
            if worker.payload is not None:
                self.hardware_fft.images.set_image(worker.payload, worker.path, worker.save_error,
                    auto_copy=not self._close_after_screenshot, inverted_path=worker.inverted_path)
                self.hardware_fft.message.setText('已收到仪器原生画面（不是频谱数组）。可双击预览、复制、反色或另存为。若 FFT 尚未显示完成，可稍后点击“读取当前 FFT 画面”。' + (f' 提示：{worker.error}' if worker.error else ''))
            else:
                self.hardware_fft.message.setText(f'仪器 FFT 读取失败：{worker.error}；未使用本地 FFT 替代。')
        elif worker.payload is not None:
            self.last_screenshot = worker.payload
            self.tab_screenshot.set_image(worker.payload, worker.path, worker.save_error,
                                          auto_copy=not self._close_after_screenshot,
                                          inverted_path=worker.inverted_path)
            self.status_bar.showMessage("截图已接收，但自动保存失败，请另存为。" if worker.save_error
                                        else f"截图已自动保存：{worker.path}")
        else:
            message = f"截图失败：{worker.error}（上次有效图片仍保留）"
            self.tab_screenshot.message.setText(message)
            self.status_bar.showMessage(message)
        self._sync_connection_status()
        worker.deleteLater()
        if self._close_after_screenshot:
            QTimer.singleShot(0, self.close)

    def _query_loop_data_from_scope(self):
        if not self._check_connected():
            return
        try:
            loop_data = self.driver.get_loop_analysis()
            self.last_loop_data = loop_data
            api_server_state.last_loop = loop_data
            self.bode_dock.update_loop_data(loop_data)
            self.status_bar.showMessage(f"环路 Bode 数据读取完成: PM={loop_data.get('pm')}, GM={loop_data.get('gm')}")
        except Exception as e:
            self.status_bar.showMessage(f"读取环路数据失败: {e}")

    def _on_continuous_mode_changed(self, enabled: bool, interval_ms: int, mode: str):
        if enabled:
            if not self._check_connected():
                self.control_panel.cb_live.setChecked(False)
                return

            if self.live_thread and self.live_thread.isRunning():
                self.live_thread.stop()

            self.live_thread = LiveStreamThread(
                driver=self.driver,
                channels=[ch for ch, widgets in self.control_panel.ch_widgets.items()
                          if widgets["cb_enable"].isChecked()],
                interval_ms=interval_ms,
                mode=mode
            )
            self.live_thread.waveform_ready.connect(self._on_live_waveform_ready)
            self.live_thread.fps_updated.connect(self._on_fps_updated)
            self.live_thread.error_occurred.connect(self._on_live_error)
            self.live_thread.start()
            self.status_bar.showMessage(f"【网线实时流已启动】 模式: {mode}, 目标帧间隔: {interval_ms} ms")
        else:
            if self.live_thread:
                self.live_thread.stop()
                self.live_thread = None
            self.status_bar.showMessage("网线实时流已停止")

    def _on_live_waveform_ready(self, wfm, meas):
        worker = self.sender()
        try:
            self.current_wfm = wfm
            self.record_workbench.set_record(wfm)
            api_server_state.last_wfm = wfm
            self.plot_widget.update_waveform(wfm, reset_cursors=False)
            for ch, m in meas.items():
                self.last_measurements[ch] = m
                self.measure_dock.update_measurements(ch, m)
            # FFT/text are regenerated on explicit analysis/export actions.
            # Keep the latest inputs without rebuilding tables each live frame.
            self.data_export_dock.current_wfm = wfm
            self.data_export_dock.current_measurements = self.last_measurements
            self.data_export_dock.current_loop_data = self.last_loop_data
        finally:
            if isinstance(worker, LiveStreamThread):
                worker.frame_consumed.set()

    def _on_fps_updated(self, fps: float, latency_ms: float):
        self.status_bar.showMessage(
            f"【网线实时读取中】 帧率: {fps:.1f} FPS | 传输延迟: {latency_ms:.1f} ms | 通道: CH1, CH2"
        )

    def _on_live_error(self, err_msg: str):
        self._sync_connection_status()
        self.status_bar.showMessage(f"网线实时流异常: {err_msg}")

    def _on_timebase_changed(self, scale: float, offset: float):
        if self.driver and self.driver.is_connected:
            try:
                self.driver.set_timebase(scale=scale, offset=offset)
            except Exception:
                pass

    def _on_channel_changed(self, ch: int, cfg: dict):
        if self.driver and self.driver.is_connected:
            try:
                self.driver.set_channel(
                    ch=ch,
                    display=cfg.get("display"),
                    scale=cfg.get("scale"),
                    offset=cfg.get("offset"),
                    coupling=cfg.get("coupling"),
                    bw_limit_20m=cfg.get("bw_limit")
                )
            except Exception:
                pass
        if "display" in cfg:
            self.plot_widget.set_channel_visible(ch, cfg["display"])

    def _on_memory_depth_changed(self, depth: str):
        if self.driver and self.driver.is_connected:
            try:
                self.driver.set_memory_depth(depth)
                self.status_bar.showMessage(f"已设置示波器存储深度为: {depth}")
            except Exception as e:
                self.status_bar.showMessage(f"设置存储深度失败: {e}")

    # --------------------------------------------------------------------------
    # 数字化数据与 FFT 谐波重算
    # --------------------------------------------------------------------------
    def _on_data_recalculate(self, ch=1, downsample=200):
        if not self.current_wfm and self.driver and self.driver.is_connected:
            self._read_waveform_from_scope()
        self.data_export_dock.update_data_view(
            wfm=self.current_wfm,
            measurements=self.last_measurements,
            loop_data=self.last_loop_data
        )
        self.status_bar.showMessage("已重新生成数字化文本报表与 FFT 谐波分解数据")

    # --------------------------------------------------------------------------
    # 存储与文件保存
    # --------------------------------------------------------------------------
    def _save_wfm_file(self):
        if not self.current_wfm:
            QMessageBox.information(self, "提示", "当前无波形数据，请先读取波形。")
            return
        directory = self.ui_settings.value('export/wfm_directory', str(Path('data').resolve()))
        filename = 'waveform_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.wfm'
        path, _ = QFileDialog.getSaveFileName(self, "保存为原生 WFM 二进制文件", str(Path(directory) / filename), "WFM Files (*.wfm)")
        if path:
            self.ui_settings.setValue('export/wfm_directory', str(Path(path).resolve().parent))
            wfm = self.current_wfm
            self.record_workbench._task(lambda worker: ScopeStorage.save_wfm(wfm, path),
                                       self._wfm_saved)

    def _stamp_event(self, name):
        self.event_timestamps[name] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.timestamp_label.setText('  |  '.join(f'{key}：{value}' for key,value in self.event_timestamps.items()))

    def _wfm_saved(self, path):
        self._last_saved_wfm = Path(path).resolve()
        self.btn_open_wfm_folder.setToolTip(str(self._last_saved_wfm.parent))
        self.btn_open_wfm_folder.show()
        self._stamp_event('WFM 已保存')
        self.status_bar.showMessage('WFM 已保存：' + path)

    def _open_wfm_folder(self):
        if self._last_saved_wfm is not None:
            directory = self._last_saved_wfm.parent
            if not directory.is_dir() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
                self.status_bar.showMessage('无法打开保存文件夹：' + str(directory))

    def _schedule_reconnect(self):
        if self._auto_reconnect and not self._closing and not self.reconnect_timer.isActive():
            self.reconnect_timer.start(self._retry_delay)
            self._retry_delay = min(30000, self._retry_delay * 2)

    def _retry_connection(self):
        if not self._auto_reconnect or self._closing:
            return
        if self.driver and self.driver.is_connected:
            return
        if any((self.connect_thread, self.read_thread, self.control_thread, self.settings_thread, self.screenshot_thread)) or (self.live_thread and self.live_thread.isRunning()):
            self._schedule_reconnect()
            return
        self._connect_scope(silent=True)

    def _save_csv_file(self):
        if not self.current_wfm:
            QMessageBox.information(self, "提示", "当前无波形数据，请先读取波形。")
            return
        directory = self.ui_settings.value('export/csv_directory', str(Path('data').resolve()))
        path, _ = QFileDialog.getSaveFileName(self, "保存为 CSV 文件", str(Path(directory) / 'waveform.csv'), "CSV Files (*.csv)")
        if path:
            self.ui_settings.setValue('export/csv_directory', str(Path(path).resolve().parent))
            wfm = self.current_wfm
            self.record_workbench._task(lambda worker: ScopeStorage.save_csv(wfm, path,
                       progress_callback=lambda done, total: worker.progress.emit(done / max(1, total) * 100),
                       cancel_check=lambda: worker.is_cancelled),
                       lambda path: self.status_bar.showMessage("全量 CSV 已保存: " + path),
                       cancellable=True, label="全量 CSV 导出")

    def _open_wfm_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开并离线分析 WFM 文件", "", "WFM Files (*.wfm);;All Files (*.*)")
        if path:
            from ..protocol import parse_wfm_from_file
            self.record_workbench._task(lambda worker: parse_wfm_from_file(path, lazy=True),
                                       lambda wfm: self._on_project_loaded(wfm, {}))

    def _save_snapshot(self):
        """打开数据归档配置对话框，执行容量预估与后台流式下载"""
        if not self._check_connected():
            return
        try:
            test_context = self.record_workbench.context()
        except ValueError as error:
            QMessageBox.warning(self, "工况参数无效", str(error))
            return
        if self.live_thread:
            self.live_thread.stop()
            self.live_thread = None
        self.control_panel.cb_live.setChecked(False)

        channels = [ch for ch, widgets in self.control_panel.ch_widgets.items()
                    if widgets["cb_enable"].isChecked()]
        if not channels:
            QMessageBox.warning(self, "提示", "请先在控制面板中至少勾选一个开启的通道。")
            return

        from ..memory_depth import expected_depth_m
        exp_m = expected_depth_m(channels)
        expected_points = (exp_m * 1_000_000) if exp_m else 256_000_000

        dlg = ExportConfigDialog(
            driver=self.driver,
            active_channels=channels,
            expected_points=expected_points,
            test_context=test_context,
            parent=self
        )
        if dlg.exec() == QDialog.Accepted and dlg.capture_result:
            res = dlg.capture_result
            wfm = res["wfm"]
            meas = res["measurements"]
            self.current_wfm, self.last_measurements = wfm, meas
            self.record_workbench.set_record(wfm)
            self.last_screenshot = res.get("screenshot")
            if self.last_screenshot:
                try:
                    self.tab_screenshot.set_image(self.last_screenshot,
                        os.path.join(res["saved_dir"], "screen.png"), auto_copy=False)
                except ValueError as error:
                    self.tab_screenshot.message.setText(f"归档截图无效：{error}")
            self.plot_widget.update_waveform(wfm)
            self.data_export_dock.update_data_view(wfm, meas)
            self.status_bar.showMessage(f"全量波形已成功归档至: {res['saved_dir']}")


    def _open_captures_dir(self):
        p = os.path.abspath("data/captures")
        ScopeStorage.ensure_dir(p)
        os.startfile(p)

    def _can_record_task(self):
        allowed = (self.connect_thread is None and self.control_thread is None and self.settings_thread is None
                   and self.screenshot_thread is None
                   and self.read_thread is None
                   and not (self.live_thread is not None and self.live_thread.isRunning()))
        if not allowed:
            self.record_workbench.message.setText("请先结束设备操作并关闭实时流，再执行离线任务。")
        return allowed

    def _on_record_busy(self, busy):
        for item in (self.record_task_label, self.record_task_progress, self.record_task_cancel):
            item.setVisible(busy)
        if busy:
            worker = self.record_workbench.worker
            self.record_task_label.setText(getattr(worker, "label", "离线任务"))
            self.record_task_progress.setRange(0, 0)
            self.record_task_cancel.setEnabled(bool(worker and getattr(worker, "cancellable", False)))

    def _on_record_progress(self, value):
        self.record_task_progress.setRange(0, 100)
        self.record_task_progress.setValue(value)

    def _on_record_finished(self):
        if self._close_after_record:
            QTimer.singleShot(0, self.close)

    def _project_state(self):
        return {"configuration": self.current_wfm.capture_metadata.get("hardware_configuration", self.last_configuration)
                if self.current_wfm else self.last_configuration,
                "measurements": self.last_measurements,
                "cursors": {"enabled": self.plot_widget.cb_cursor.isChecked(),
                            "y_coordinates": "divisions",
                            "display_centers": self.plot_widget._center_overrides.copy(),
                            "x1": self.plot_widget.line_x1.value(), "x2": self.plot_widget.line_x2.value(),
                            "y1": self.plot_widget.line_y1.value(), "y2": self.plot_widget.line_y2.value()}}

    def _on_project_loaded(self, wfm, state):
        self.current_wfm = wfm
        wfm.capture_metadata["test_context"] = state.get("test_context", wfm.capture_metadata.get("test_context", {}))
        if state.get("configuration"):
            wfm.capture_metadata["hardware_configuration"] = state["configuration"]
        api_server_state.last_wfm = wfm
        api_server_state.last_loop = None
        self.last_measurements = {int(ch): values for ch, values in state.get("measurements", {}).items()}
        self.last_loop_data = None
        self.plot_widget._center_overrides = {int(ch): float(value) for ch, value in state.get("cursors", {}).get("display_centers", {}).items()}
        self.plot_widget.update_waveform(wfm)
        self.record_workbench.set_record(wfm)
        self.record_workbench.set_context(state.get("test_context", {}))
        if not (self.driver and self.driver.is_connected):
            self._apply_scope_settings(state.get("configuration", {}))
        cursors = state.get("cursors", {})
        self.plot_widget.cb_cursor.setChecked(cursors.get("enabled", False))
        for name in ("x1", "x2", "y1", "y2"):
            if name in cursors:
                value = cursors[name]
                if name.startswith("y") and cursors.get("y_coordinates") != "divisions":
                    value = (value - self.plot_widget._display_centers.get(1, 0)) / self.plot_widget._display_scales.get(1, 1)
                getattr(self.plot_widget, "line_" + name).setValue(value)
        interval = state.get("interval")
        if interval:
            self.record_workbench.start.setValue(interval[0])
            self.record_workbench.end.setValue(interval[1])
        if state.get("analysis_report"):
            self.record_workbench._show_analysis(state["analysis_report"])
        self.data_export_dock.update_data_view(wfm, self.last_measurements)
        self.record_workbench.message.setText("已校验并恢复离线工程：原始记录、配置快照、光标、工况及分析结果；未写入设备。")
        for channel, values in self.last_measurements.items():
            self.measure_dock.update_measurements(channel, values)

    def _show_about(self):
        QMessageBox.about(
            self,
            "关于致远示波器上位机",
            "<b>致远 ZDS3000 / ZDS4000 Plus 示波器专业上位机</b><br>"
            "版本: v1.0.0<br><br>"
            "<b>核心特性:</b><br>"
            "1. 纯 Python 高速以太网 SCPI 通信 (默认端口 5025)，无驱动依赖<br>"
            "2. 60 FPS PyQtGraph 硬件加速波形渲染与双光标测量<br>"
            "3. 官方 WFM 原生二进制文件编解码与 CSV/JSON 导出<br>"
            "4. 硬件级电源环路 Bode 稳定性分析 (PM / GM)<br>"
            "5. 原始记录保存、区间导出与离线工程<br>"
            "6. 本地 RESTful 控制接口"
        )

    # --------------------------------------------------------------------------
    # 后台 API 服务
    # --------------------------------------------------------------------------
    def _toggle_api_server(self):
        if not self.api_running:
            self._start_api_server()
        else:
            self._stop_api_server()

    def _start_api_server(self):
        def run_uvicorn():
            uvicorn.run(fastapi_app, host="127.0.0.1", port=8000, log_level="warning")

        self.api_thread = threading.Thread(target=run_uvicorn, daemon=True)
        self.api_thread.start()
        self.api_running = True

        self.btn_toggle_api.setText("本地控制 API (运行中:8000)")
        self.btn_toggle_api.setStyleSheet("background-color: #047857; color: white; font-weight: bold; font-size: 11px;")
        self.status_bar.showMessage("本地控制 API 服务已启动: http://127.0.0.1:8000/docs")

    def _stop_api_server(self):
        # 提示用户 Uvicorn 作为 daemon 线程常驻
        QMessageBox.information(self, "API 服务提示", "本地控制 API 服务运行在 http://127.0.0.1:8000\n自动化脚本可调用控制和数据接口。")

    def _poll_run_state(self):
        if self._closing or not self.driver or not self.driver.is_connected:
            return
        if any((self.run_state_thread, self.connect_thread, self.control_thread,
                self.settings_thread, self.read_thread, self.screenshot_thread,
                self.live_thread)):
            return
        self.run_state_thread = RunStateThread(self.driver, self)
        self.run_state_thread.finished.connect(self._on_run_state_finished)
        self.run_state_thread.start()

    def _on_run_state_finished(self):
        worker = self.run_state_thread
        if worker is None:
            return
        self.run_state_thread = None
        if not self._closing and worker.driver is self.driver and self.driver.is_connected:
            # A newer explicit command owns the next state update.
            if self.control_thread is None:
                self._apply_run_state(worker.state)
        worker.deleteLater()
        if self._closing:
            QTimer.singleShot(0, self.close)

    def _apply_run_state(self, state):
        if isinstance(state, str) and state.strip().upper() in {"RUN", "STOP", "SINGLE"}:
            self.control_panel.set_run_state_visual(state.strip())
            self.plot_widget.update_run_state(state.strip())
            self.last_configuration["run_state"] = state.strip()

    @Slot()
    def _sync_connection_status(self):
        if self.connect_thread is not None:
            return
        if self.driver is not None and not self.driver.is_connected:
            if self.lbl_status_badge.text() != "连接已断开":
                reason = getattr(self.driver, "last_error", "") or "通信连接已关闭"
                self.lbl_status_badge.setText("连接已断开")
                self.lbl_status_badge.setStyleSheet("background-color: #ef4444; color: white; padding: 3px 8px;")
                self.lbl_device_info.setText("设备: 连接已断开（历史波形仍保留）")
                self.btn_connect.setText("连接")
                self.btn_sync_settings.setEnabled(False)
                self.advanced_settings.connected = False
                self.advanced_settings.apply.setEnabled(False)
                self.btn_connect.setStyleSheet("background-color: #2563eb; color: white; padding: 5px 12px;")
                self.status_bar.showMessage(f"连接已断开: {reason}")
            if api_server_state.driver is self.driver:
                api_server_state.driver = None
            self._schedule_reconnect()

    def _check_connected(self) -> bool:
        self._sync_connection_status()
        if self.control_thread is not None or self.settings_thread is not None or self.read_thread is not None or self.screenshot_thread is not None:
            self.status_bar.showMessage("设备操作正在执行，请等待当前操作结束。")
            return False
        if not self.driver or not self.driver.is_connected:
            reason = getattr(self.driver, "last_error", "") if self.driver else ""
            message = f"通信已断开：{reason}\n请在设备菜单点击【连接】。" if reason else "示波器未连接，请先在设备菜单点击【连接】。"
            QMessageBox.warning(self, "未连接", message)
            return False
        return True

    def closeEvent(self, event):
        self._closing = True
        self._auto_reconnect = False
        self.reconnect_timer.stop()
        self.run_state_timer.stop()
        if self.run_state_thread is not None:
            event.ignore()
            return
        if self.screenshot_thread is not None:
            self._close_after_screenshot = True
            self.status_bar.showMessage("等待当前截图保存结束后关闭…")
            event.ignore()
            return
        if self.read_thread is not None:
            self._close_after_read = True
            self.read_thread.is_cancelled = True
            self.status_bar.showMessage("等待读取结束后关闭…")
            event.ignore()
            return
        if self.record_workbench.worker is not None:
            self._close_after_record = True
            self.record_workbench.worker.is_cancelled = True
            self.status_bar.showMessage("等待离线任务结束后关闭…")
            event.ignore()
            return
        if self.settings_thread is not None:
            self._close_after_settings = True
            self.status_bar.showMessage("正在等待读取设置结束，随后自动关闭…")
            event.ignore()
            return
        if self.control_thread is not None:
            self._close_after_control = True
            self.status_bar.showMessage("正在等待后台控制结束，随后自动关闭…")
            event.ignore()
            return
        if self.connect_thread is not None:
            self._close_after_connect = True
            self.connect_thread.cancel()
            self.btn_connect.setEnabled(False)
            self.status_bar.showMessage("正在结束后台连接，随后自动关闭…")
            event.ignore()
            return
        if self.live_thread:
            self.live_thread.stop()
            self.live_thread = None
        if self.driver:
            self.driver.disconnect()
        event.accept()
