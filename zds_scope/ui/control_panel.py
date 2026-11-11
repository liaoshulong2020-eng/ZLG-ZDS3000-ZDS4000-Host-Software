"""
示波器控制面板组件
包含运行控制 (RUN/STOP/SINGLE/AUTO)、水平时基调节与 4 通道垂直灵敏度/偏置配置
"""

import math
from contextlib import ExitStack
from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
    QPushButton, QGroupBox, QComboBox, QDoubleSpinBox, QTabWidget,
    QSpinBox, QDialog, QLineEdit
)
from .scope_plot import CHANNEL_COLORS
from .window_chrome import SettingsPopup
from ..memory_depth import expected_depth_m


class ControlPanel(QWidget):
    """示波器主右侧控制面板"""

    # 信号定义
    run_action_requested = Signal(str)                 # 'run', 'stop', 'single', 'auto_setup', 'clear'
    read_waveform_requested = Signal(str)              # 单次波形读取 (mode)
    capture_screen_requested = Signal()               # 单次截图
    continuous_mode_changed = Signal(bool, int, str)   # (enabled, interval_ms, mode)
    timebase_changed = Signal(object, object)         # None means unchanged
    channel_changed = Signal(int, dict)               # (ch_id, {display, scale, offset, coupling, bw_limit})
    channel_name_changed = Signal(int, str)
    channel_center_changed = Signal(int, bool)
    memory_depth_changed = Signal(str)                # '512000000', '256000000', 'AUTO', etc.

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(8)

        # 1. 运行与触发控制群组
        run_group = QGroupBox("采集与运行控制 (Run & Acquisition)")
        self.run_group = run_group
        run_group_layout = QVBoxLayout(run_group)
        run_group_layout.setSpacing(6)

        btn_row1 = QHBoxLayout()
        self.btn_run = QPushButton("RUN (运行)")
        self.btn_run.setStyleSheet("background-color: #059669; color: white; font-weight: bold; padding: 6px;")
        self.btn_run.clicked.connect(lambda: self.run_action_requested.emit("run"))

        self.btn_stop = QPushButton("STOP (停止)")
        self.btn_stop.setStyleSheet("background-color: #dc2626; color: white; font-weight: bold; padding: 6px;")
        self.btn_stop.clicked.connect(lambda: self.run_action_requested.emit("stop"))

        btn_row1.addWidget(self.btn_run)
        btn_row1.addWidget(self.btn_stop)
        run_group_layout.addLayout(btn_row1)

        btn_row2 = QHBoxLayout()
        self.btn_single = QPushButton("SINGLE (单次)")
        self.btn_single.setStyleSheet("background-color: #d97706; color: white; font-weight: bold; padding: 6px;")
        self.btn_single.clicked.connect(lambda: self.run_action_requested.emit("single"))

        self.btn_auto = QPushButton("AUTO (自动设置)")
        self.btn_auto.setStyleSheet("background-color: #2563eb; color: white; font-weight: bold; padding: 6px;")
        self.btn_auto.clicked.connect(lambda: self.run_action_requested.emit("auto_setup"))

        btn_row2.addWidget(self.btn_single)
        btn_row2.addWidget(self.btn_auto)
        run_group_layout.addLayout(btn_row2)

        # 采集模式与动作
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("数据源:"))
        self.combo_wave_mode = QComboBox()
        self.combo_wave_mode.addItem("屏幕范围", "SCREen")
        self.combo_wave_mode.addItem("完整存储", "MEMOry")
        mode_row.addWidget(self.combo_wave_mode)
        run_group_layout.addLayout(mode_row)

        act_row = QHBoxLayout()
        self.btn_read_wave = QPushButton("读取波形 (Read)")
        self.btn_read_wave.setStyleSheet("background-color: #374151; color: white; font-weight: bold; padding: 6px;")
        self.btn_read_wave.clicked.connect(lambda: self.read_waveform_requested.emit(self.combo_wave_mode.currentData()))

        self.btn_capture = QPushButton("截图并保存")
        self.btn_capture.setStyleSheet("background-color: #4b5563; color: white; padding: 6px;")
        self.btn_capture.clicked.connect(self.capture_screen_requested.emit)

        act_row.addWidget(self.btn_capture)
        self.btn_export = QPushButton("导出 WFM")
        self.btn_export.setStyleSheet("background-color: #4b5563; color: white; padding: 6px;")
        act_row.addWidget(self.btn_export)
        run_group_layout.addLayout(act_row)

        # 持续刷新勾选框
        live_row = QHBoxLayout()
        self.cb_live = QCheckBox("网线实时流 (Live)")
        self.cb_live.setStyleSheet("color: #4ade80; font-weight: bold;")
        self.cb_live.toggled.connect(self._on_live_toggled)
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(20, 5000)
        self.spin_interval.setValue(50)  # 默认 50ms 刷新周期 (20 FPS)
        self.spin_interval.setSuffix(" ms")
        self.spin_interval.valueChanged.connect(self._on_live_toggled)
        live_row.addWidget(self.cb_live)
        live_row.addWidget(self.spin_interval)
        self.cb_live.hide()
        self.spin_interval.hide()

        main_layout.addWidget(run_group)

        # 2. 水平时基 (Horizontal Timebase)
        tb_group = QGroupBox("水平时基 (Horizontal Timebase)")
        self.timebase_group = tb_group
        tb_layout = QVBoxLayout(tb_group)
        tb_layout.setSpacing(6)

        scale_row = QHBoxLayout()
        scale_row.addWidget(QLabel("水平档位:"))
        self.combo_tb_scale = QComboBox()
        self.tb_scales = [
            ("10 ns", 10e-9), ("20 ns", 20e-9), ("50 ns", 50e-9),
            ("100 ns", 100e-9), ("200 ns", 200e-9), ("500 ns", 500e-9),
            ("1 us", 1e-6), ("2 us", 2e-6), ("5 us", 5e-6),
            ("10 us", 10e-6), ("20 us", 20e-6), ("50 us", 50e-6),
            ("100 us", 100e-6), ("200 us", 200e-6), ("500 us", 500e-6),
            ("1 ms", 1e-3), ("2 ms", 2e-3), ("5 ms", 5e-3),
            ("10 ms", 10e-3), ("20 ms", 20e-3), ("50 ms", 50e-3),
            ("100 ms", 100e-3), ("200 ms", 200e-3), ("500 ms", 500e-3),
            ("1 s", 1.0), ("2 s", 2.0), ("5 s", 5.0)
        ]
        for label, val in self.tb_scales:
            self.combo_tb_scale.addItem(label, val)
        self.combo_tb_scale.setCurrentIndex(6)  # 默认 1 us
        self.combo_tb_scale.currentIndexChanged.connect(lambda: self._on_timebase_changed("scale"))
        scale_row.addWidget(self.combo_tb_scale)
        tb_layout.addLayout(scale_row)

        offset_row = QHBoxLayout()
        offset_row.addWidget(QLabel("水平偏移:"))
        self.spin_tb_offset = QDoubleSpinBox()
        self.spin_tb_offset.setRange(-10.0, 10.0)
        self.spin_tb_offset.setValue(0.0)
        self.spin_tb_offset.setDecimals(12)
        self.spin_tb_offset.setKeyboardTracking(False)
        self.spin_tb_offset.setSuffix(" s")
        self.spin_tb_offset.valueChanged.connect(lambda: self._on_timebase_changed("offset"))
        offset_row.addWidget(self.spin_tb_offset)
        tb_layout.addLayout(offset_row)

        self.timebase_dialog = SettingsPopup(self)
        self.timebase_dialog.setWindowTitle("水平时基")
        self.timebase_dialog.resize(340, 190)
        self.timebase_dialog.layout().addWidget(tb_group)

        # 3. 存储深度与 Bank 架构 (Memory Depth & Interleaving)
        mdep_group = QGroupBox("存储深度与 Bank 架构")
        self.memory_group = mdep_group
        mdep_group.setParent(self)
        mdep_layout = QVBoxLayout(mdep_group)
        mdep_layout.setSpacing(4)

        self.lbl_mdep_badge = QLabel("🚀 独立 Bank: 最大 512 Mpts (CH1/CH3 模式)")
        self.lbl_mdep_badge.setStyleSheet("""
            background-color: #064e3b;
            color: #34d399;
            font-size: 11px;
            font-weight: bold;
            padding: 4px;
            border-radius: 3px;
        """)
        self.lbl_mdep_badge.setWordWrap(True)
        mdep_layout.addWidget(self.lbl_mdep_badge)

        mdep_row = QHBoxLayout()
        mdep_row.addWidget(QLabel("存储深度:"))
        self.combo_mdep = QComboBox()
        self.combo_mdep.addItem("AUTO (自动)", "AUTO")
        self.combo_mdep.addItem("512 Mpts (仅CH13或CH24)", "512000000")
        self.combo_mdep.addItem("256 Mpts", "256000000")
        self.combo_mdep.addItem("128 Mpts", "128000000")
        self.combo_mdep.addItem("56 Mpts", "56000000")
        self.combo_mdep.addItem("28 Mpts", "28000000")
        self.combo_mdep.addItem("14 Mpts", "14000000")
        self.combo_mdep.addItem("1.4 Mpts", "1400000")
        self.combo_mdep.addItem("140 kpts", "140000")
        self.combo_mdep.currentIndexChanged.connect(self._on_mdep_changed)
        mdep_row.addWidget(self.combo_mdep)
        mdep_layout.addLayout(mdep_row)
        self.lbl_actual_acquisition = QLabel("设备采集参数：尚未同步")
        self.lbl_actual_acquisition.setWordWrap(True)
        mdep_layout.addWidget(self.lbl_actual_acquisition)

        lbl_mdep_tip = QLabel("已确认：13/24 为512M；12/1234/234/34 为256M。其他组合待确认。")
        lbl_mdep_tip.setStyleSheet("color: #64748b; font-size: 10px;")
        lbl_mdep_tip.setWordWrap(True)
        mdep_layout.addWidget(lbl_mdep_tip)

        mdep_group.hide()

        # 4. 通道垂直设置 (Channel Tabs CH1 ~ CH4)
        ch_group = QGroupBox("模拟通道控制 (Vertical Channels)")
        self.channel_group = ch_group
        ch_layout = QVBoxLayout(ch_group)
        ch_layout.setContentsMargins(2, 6, 2, 4)

        self.ch_tabs = QTabWidget()
        self.ch_widgets = {}

        for ch in [1, 2, 3, 4]:
            tab = self._create_channel_tab(ch)
            self.ch_widgets[ch] = tab
            self.ch_tabs.addTab(tab["widget"], f"CH{ch}")
            # 设置标签颜色指示
            self.ch_tabs.tabBar().setTabTextColor(ch - 1, CHANNEL_COLORS[ch])

        ch_layout.addWidget(self.ch_tabs)
        self.lbl_depth_rule = QLabel()
        self.lbl_depth_rule.setWordWrap(True)
        ch_layout.addWidget(self.lbl_depth_rule)
        for widgets in self.ch_widgets.values():
            widgets["cb_enable"].toggled.connect(self._update_depth_hint)
        self._update_depth_hint()
        self.lbl_depth_rule.hide()
        self.channel_dialog = SettingsPopup(self)
        self.channel_dialog.setWindowTitle("通道设置")
        self.channel_dialog.resize(360, 400)
        ch_group.setTitle("通道参数")
        self.ch_tabs.currentChanged.connect(lambda index: self.channel_dialog.setWindowTitle(f"CH{index + 1} 通道设置"))
        self.channel_dialog.layout().addWidget(ch_group)
        run_group.setTitle("采集")
        for button, text in ((self.btn_run, "RUN"), (self.btn_stop, "STOP"),
                             (self.btn_single, "SINGLE"), (self.btn_auto, "AUTO"),
                             (self.btn_read_wave, "刷新波形"), (self.btn_capture, "截图保存")):
            button.setText(text)
        self.cb_live.setText("自动刷新")
        self.cb_live.setToolTip("连续读取并更新波形；右侧为目标刷新间隔，实际速度取决于数据量和网络。大记录请使用单次刷新波形。")

    def show_channel_settings(self, channel):
        self.ch_tabs.setCurrentIndex(channel - 1)
        self.channel_dialog.setWindowTitle(f"CH{channel} 通道设置")
        self.channel_dialog.show()
        self.channel_dialog.raise_()
        self.channel_dialog.activateWindow()

    def show_timebase_settings(self):
        self.timebase_dialog.show()
        self.timebase_dialog.raise_()
        self.timebase_dialog.activateWindow()

    def setEnabled(self, enabled):
        super().setEnabled(enabled)
        self.btn_read_wave.setEnabled(enabled)
        for name in ("channel_dialog", "timebase_dialog"):
            dialog = getattr(self, name, None)
            if dialog is not None:
                dialog.setEnabled(enabled)

    def _create_channel_tab(self, ch: int) -> dict:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # 使能复选框
        cb_enable = QCheckBox(f"开启通道 CH{ch}")
        cb_enable.setChecked(ch in [1, 2])
        color = CHANNEL_COLORS[ch]
        cb_enable.setStyleSheet(f"font-weight: bold; color: {color}; font-size: 12px;")
        enable_row = QHBoxLayout()
        enable_row.addWidget(cb_enable)
        cb_center = QCheckBox('本地居中')
        cb_center.setToolTip('将此通道当前可见波形移到屏幕中央；取消后恢复采集位置，不写入示波器。')
        cb_center.toggled.connect(lambda enabled: self.channel_center_changed.emit(ch, enabled))
        enable_row.addWidget(cb_center)
        layout.addLayout(enable_row)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel('显示名称：'))
        name_edit = QLineEdit(str(ch))
        name_edit.setMaxLength(16)
        name_edit.setToolTip('仅修改上位机标签，例如 Vbus；不会改变通道编号或仪器设置。')
        name_edit.editingFinished.connect(lambda: self.channel_name_changed.emit(ch, name_edit.text().strip() or str(ch)))
        name_row.addWidget(name_edit)
        layout.addLayout(name_row)

        # 垂直档位
        scale_row = QHBoxLayout()
        scale_row.addWidget(QLabel("垂直档位:"))
        combo_scale = QComboBox()
        scales = [
            ("2 mV/div", 0.002), ("5 mV/div", 0.005), ("10 mV/div", 0.01),
            ("20 mV/div", 0.02), ("50 mV/div", 0.05), ("100 mV/div", 0.1),
            ("200 mV/div", 0.2), ("500 mV/div", 0.5), ("1 V/div", 1.0),
            ("2 V/div", 2.0), ("5 V/div", 5.0), ("10 V/div", 10.0),
            ("20 V/div", 20.0), ("50 V/div", 50.0)
        ]
        for lbl, v in scales:
            combo_scale.addItem(lbl, v)

        # 默认档位
        if ch == 1:
            combo_scale.setCurrentIndex(10)  # 5 V/div
        elif ch == 2:
            combo_scale.setCurrentIndex(4)   # 50 mV/div
        else:
            combo_scale.setCurrentIndex(8)   # 1 V/div
        scale_row.addWidget(combo_scale)
        layout.addLayout(scale_row)

        # 垂直偏移
        offset_row = QHBoxLayout()
        offset_row.addWidget(QLabel("垂直偏置:"))
        spin_offset = QDoubleSpinBox()
        spin_offset.setRange(-100.0, 100.0)
        spin_offset.setValue(-12.0 if ch == 1 else (-5.0 if ch == 2 else 0.0))
        spin_offset.setDecimals(12)
        spin_offset.setKeyboardTracking(False)
        spin_offset.setSuffix(" V")
        offset_row.addWidget(spin_offset)
        layout.addLayout(offset_row)

        # 耦合模式
        coup_row = QHBoxLayout()
        coup_row.addWidget(QLabel("输入耦合:"))
        combo_coup = QComboBox()
        combo_coup.addItems(["DC (直流)", "AC (交流)", "GND (接地)"])
        coup_row.addWidget(combo_coup)
        layout.addLayout(coup_row)

        # 带宽限制
        bw_row = QHBoxLayout()
        cb_bw = QCheckBox("20MHz 带宽限制 (滤波)")
        bw_row.addWidget(cb_bw)
        layout.addLayout(bw_row)

        # 绑定修改事件
        cb_enable.toggled.connect(lambda value: self.channel_changed.emit(ch, {"display": value}))
        combo_scale.currentIndexChanged.connect(lambda: self.channel_changed.emit(ch, {"scale": combo_scale.currentData()}))
        spin_offset.valueChanged.connect(lambda value: self.channel_changed.emit(ch, {"offset": value}))
        combo_coup.currentIndexChanged.connect(lambda: self.channel_changed.emit(ch, {"coupling": combo_coup.currentText().split()[0]}))
        cb_bw.toggled.connect(lambda value: self.channel_changed.emit(ch, {"bw_limit": value}))
        lbl_probe = QLabel("探头倍率：尚未同步（在仪器上设置）")
        layout.addWidget(lbl_probe)

        layout.addStretch()

        return {
            "widget": w,
            "cb_enable": cb_enable,
            "cb_center": cb_center,
            "name_edit": name_edit,
            "combo_scale": combo_scale,
            "spin_offset": spin_offset,
            "combo_coup": combo_coup,
            "cb_bw": cb_bw,
            "lbl_probe": lbl_probe,
        }

    @staticmethod
    def _select_numeric(combo, value, unit):
        value = float(value)
        for i in range(combo.count()):
            candidate = combo.itemData(i)
            if isinstance(candidate, (float, int)) and math.isclose(float(candidate), value, rel_tol=1e-12, abs_tol=1e-18):
                combo.setCurrentIndex(i)
                return
        combo.addItem(f"{value:.12g} {unit}（设备当前值）", value)
        combo.setCurrentIndex(combo.count() - 1)

    def apply_scope_configuration(self, config):
        """Refresh widgets with all outgoing and child signals blocked."""
        with ExitStack() as blockers:
            blockers.enter_context(QSignalBlocker(self))
            for widget in self.findChildren(QWidget):
                blockers.enter_context(QSignalBlocker(widget))
            if config.get("timebase_scale_s") is not None:
                self._select_numeric(self.combo_tb_scale, config["timebase_scale_s"], "s/div")
            if config.get("timebase_offset_s") is not None:
                value = float(config["timebase_offset_s"])
                self.spin_tb_offset.setRange(min(self.spin_tb_offset.minimum(), value), max(self.spin_tb_offset.maximum(), value))
                self.spin_tb_offset.setValue(value)
            if config.get("mdepth_raw") is not None:
                depth = str(config["mdepth_raw"]).strip().upper()
                index = self.combo_mdep.findData(depth)
                if index < 0:
                    self.combo_mdep.addItem(f"{depth}（设备当前值）", depth)
                    index = self.combo_mdep.count() - 1
                self.combo_mdep.setCurrentIndex(index)
            for channel, values in config.get("channels", {}).items():
                widgets = self.ch_widgets.get(int(channel))
                if widgets is None:
                    continue
                if "enabled" in values:
                    widgets["cb_enable"].setChecked(values["enabled"])
                if "scale" in values:
                    self._select_numeric(widgets["combo_scale"], values["scale"], "V/div")
                if "offset" in values:
                    value = float(values["offset"])
                    spin = widgets["spin_offset"]
                    spin.setRange(min(spin.minimum(), value), max(spin.maximum(), value))
                    spin.setValue(value)
                if "coupling" in values:
                    coupling = values["coupling"].upper()
                    for i in range(widgets["combo_coup"].count()):
                        if widgets["combo_coup"].itemText(i).split()[0] == coupling:
                            widgets["combo_coup"].setCurrentIndex(i)
                            break
                if "bw_limit" in values:
                    widgets["cb_bw"].setChecked(values["bw_limit"])
                if "probe" in values:
                    units = config.get("extended_settings", {}).get(f":CHANnel{channel}:UNITs", "VOLTage")
                    suffix = " V/A" if units.upper() == "AMPERE" else "×"
                    widgets["lbl_probe"].setText(f"探头比例：{values['probe']:g}{suffix}（仪器当前值）")
            self._update_depth_hint()
            self.lbl_actual_acquisition.setText(f"设备深度：{config.get('mdepth_raw', '未知')} 点；采样率：{config.get('sample_rate_hz', '未知')} Hz")
            if config.get("run_state"):
                self.set_run_state_visual(config["run_state"])

    def _update_depth_hint(self):
        channels = [ch for ch, widgets in self.ch_widgets.items()
                    if widgets["cb_enable"].isChecked()]
        depth = expected_depth_m(channels)
        ch_str = "+".join(str(c) for c in sorted(channels)) if channels else "无"
        if depth == 512:
            badge_text = f"🚀 512M 极速存储深度 (CH{ch_str} 独立 Bank)"
            badge_style = "background-color: #064e3b; color: #34d399; font-size: 11px; font-weight: bold; padding: 4px; border-radius: 3px;"
            rule_text = f"通道组合 CH{ch_str}：✅ 硬件存储深度 512 Mpts (非冲突 Bank)"
        elif depth == 256:
            badge_text = f"💾 256M 存储深度 (CH{ch_str} 共享 Bank)"
            badge_style = "background-color: #1e3a8a; color: #93c5fd; font-size: 11px; font-weight: bold; padding: 4px; border-radius: 3px;"
            rule_text = f"通道组合 CH{ch_str}：✅ 硬件存储深度 256 Mpts (共享 Bank 模式)"
        else:
            badge_text = f"🔍 待确认深度 (CH{ch_str})"
            badge_style = "background-color: #334155; color: #cbd5e1; font-size: 11px; font-weight: bold; padding: 4px; border-radius: 3px;"
            rule_text = f"通道组合 CH{ch_str}：存储深度待确认（需与设备实际启用通道一致）"

        if hasattr(self, "lbl_depth_rule"):
            self.lbl_depth_rule.setText(rule_text)
            self.lbl_depth_rule.setStyleSheet("color: #94a3b8; font-size: 11px; padding-top: 3px;")
        if hasattr(self, "lbl_mdep_badge"):
            self.lbl_mdep_badge.setText(badge_text)
            self.lbl_mdep_badge.setStyleSheet(badge_style)

    def _on_mdep_changed(self):
        self.memory_depth_changed.emit(self.combo_mdep.currentData())

    def _on_live_toggled(self):
        mode = self.combo_wave_mode.currentData() if hasattr(self, 'combo_wave_mode') else "SCREen"
        self.continuous_mode_changed.emit(self.cb_live.isChecked(), self.spin_interval.value(), mode)

    def _on_timebase_changed(self, field="scale"):
        scale = self.combo_tb_scale.currentData() if field == "scale" else None
        offset = self.spin_tb_offset.value() if field == "offset" else None
        self.timebase_changed.emit(scale, offset)

    def set_run_state_visual(self, state_str: str):
        """更新按钮视觉状态高亮"""
        state_low = state_str.lower()
        if "run" in state_low:
            self.btn_run.setStyleSheet("background-color: #10b981; color: white; font-weight: bold; border: 2px solid #34d399;")
            self.btn_stop.setStyleSheet("background-color: #374151; color: #9ca3af;")
        else:
            self.btn_run.setStyleSheet("background-color: #374151; color: #9ca3af;")
            self.btn_stop.setStyleSheet("background-color: #ef4444; color: white; font-weight: bold; border: 2px solid #f87171;")
