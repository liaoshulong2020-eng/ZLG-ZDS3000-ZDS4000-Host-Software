"""
基于 PyQtGraph 的高性能示波器波形显示控件与专业仪器状态/光标底栏
使用轻量曲线绘制、标尺网格、共享光标及 ZDS 原生风格状态栏。
"""

import numpy as np
import pyqtgraph as pg
from ..display_envelope import DisplayEnvelope
from PySide6.QtCore import Qt, Signal, QPointF, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
    QPushButton, QFrame, QComboBox, QSizePolicy
)
from PySide6.QtGui import QColor, QPen, QFont, QPainter


# 通道标准配色方案
CHANNEL_COLORS = {
    1: "#ffe600",  # CH1: 亮黄 (Yellow)
    2: "#00ff00",  # CH2: 绿色 (ZDS native)
    3: "#3b82f6",  # CH3: 蓝色 (ZDS native)
    4: "#ff00ff",  # CH4: 品红 (ZDS native)
}


def format_time(t: float) -> str:
    """格式化时间为 s, ms, us, ns"""
    abs_t = abs(t)
    sign = "-" if t < 0 else ""
    if abs_t == 0.0:
        return "0.00s"
    if abs_t < 1e-6:
        return f"{sign}{abs_t * 1e9:.2f}ns"
    elif abs_t < 1e-3:
        return f"{sign}{abs_t * 1e6:.2f}us"
    elif abs_t < 1.0:
        return f"{sign}{abs_t * 1e3:.2f}ms"
    else:
        return f"{sign}{abs_t:.3f}s"


def format_freq(f: float) -> str:
    """格式化频率"""
    if f <= 0:
        return "---"
    if f >= 1e6:
        return f"{f / 1e6:.3f}MHz"
    elif f >= 1e3:
        return f"{f / 1e3:.2f}kHz"
    else:
        return f"{f:.1f}Hz"


def format_voltage(v: float, unit: str = "V") -> str:
    """格式化物理量（电压/电流）"""
    abs_v = abs(v)
    sign = "-" if v < 0 else ""
    if abs_v == 0.0:
        return f"0.00{unit}"
    if abs_v < 0.1:
        return f"{sign}{abs_v * 1000.0:.2f}m{unit}"
    else:
        return f"{sign}{abs_v:.3f}{unit}"


def format_sample_rate(rate: float) -> str:
    """格式化采样率"""
    try:
        r = float(rate)
        if r >= 1e9:
            return f"{r / 1e9:.2f}GSa/s"
        elif r >= 1e6:
            return f"{r / 1e6:.1f}MSa/s"
        elif r >= 1e3:
            return f"{r / 1e3:.1f}kSa/s"
        return f"{r:.0f}Sa/s"
    except Exception:
        return str(rate) if rate else "---"


def format_memory_depth(depth_val) -> str:
    """格式化存储深度"""
    try:
        pts = float(depth_val)
        if pts >= 1e6:
            return f"{int(pts / 1e6)}Mpts"
        elif pts >= 1e3:
            return f"{int(pts / 1e3)}kpts"
        return f"{int(pts)}pts"
    except Exception:
        return str(depth_val) if depth_val else "---"


class ChannelStatusCard(QFrame):
    """
    单个通道底栏状态卡片 (如 CH1 500mV/div Pos -1.50V, 独立 YA/YB/ΔY, 耦合/阻抗/滤波/探头)
    """
    clicked = Signal(int)

    def __init__(self, ch_id: int, parent=None):
        super().__init__(parent)
        self.ch_id = ch_id
        self.is_active_cursor = (ch_id == 1)
        self.enabled = (ch_id == 1)
        self.unit = "V"
        self._setup_ui()

    def _setup_ui(self):
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"点击打开 CH{self.ch_id} 通道设置")

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(4, 2, 4, 2)
        main_layout.setSpacing(4)

        color = CHANNEL_COLORS[self.ch_id]

        # 左侧属性列 (通道号角标 + 耦合 + 滤波 + 探头 + 阻抗)
        left_col = QVBoxLayout()
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(1)

        self.lbl_num = QLabel(str(self.ch_id))
        self.lbl_num.setAlignment(Qt.AlignCenter)
        self.lbl_num.setFixedSize(20, 18)
        self.lbl_num.setFont(QFont("Consolas", 9, QFont.Bold))
        left_col.addWidget(self.lbl_num)

        attr_style = "font-family: Consolas; font-size: 12px; color: #94a3b8;"
        self.lbl_coupling = QLabel("DC")
        self.lbl_coupling.setStyleSheet(attr_style)
        left_col.addWidget(self.lbl_coupling)

        self.lbl_bw = QLabel("BwL")
        self.lbl_bw.setStyleSheet("font-family: Consolas; font-size: 12px; color: #eab308;")
        left_col.addWidget(self.lbl_bw)

        self.lbl_probe = QLabel("1:1")
        self.lbl_probe.setStyleSheet(attr_style)
        left_col.addWidget(self.lbl_probe)

        self.lbl_term = QLabel("1MΩ")
        self.lbl_term.setStyleSheet(attr_style)
        left_col.addWidget(self.lbl_term)

        main_layout.addLayout(left_col)

        # 右侧参数列 (垂直档位、偏置、独立 YA、YB、ΔY)
        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(1)

        self.lbl_scale = QLabel("500mV/div")
        self.lbl_scale.setFont(QFont("Consolas", 9, QFont.Bold))
        self.lbl_scale.setStyleSheet(f"color: {color}; font-size: 12px;")
        right_col.addWidget(self.lbl_scale)

        self.lbl_pos = QLabel("Pos -1.50V")
        self.lbl_pos.setStyleSheet("font-family: Consolas; font-size: 12px; color: #cbd5e1;")
        right_col.addWidget(self.lbl_pos)

        cursor_style = "font-family: Consolas; font-size: 12px; color: #fb923c;"
        self.lbl_ya = QLabel("YA  +3.020V")
        self.lbl_ya.setStyleSheet(cursor_style)
        right_col.addWidget(self.lbl_ya)

        self.lbl_yb = QLabel("YB  -20.00mV")
        self.lbl_yb.setStyleSheet(cursor_style)
        right_col.addWidget(self.lbl_yb)

        self.lbl_dy = QLabel("ΔY  -3.040V")
        self.lbl_dy.setStyleSheet(f"font-family: Consolas; font-size: 12px; font-weight: bold; color: {color};")
        right_col.addWidget(self.lbl_dy)

        main_layout.addLayout(right_col)
        self._update_style()

    def _update_style(self):
        color = CHANNEL_COLORS[self.ch_id]
        if self.enabled:
            border_color = color if self.is_active_cursor else "#2a2c3d"
            bg_color = "#181a24"
            self.setStyleSheet(f"""
                ChannelStatusCard {{
                    background-color: {bg_color};
                    border: 1px solid {border_color};
                    border-radius: 3px;
                }}
            """)
            self.lbl_num.setStyleSheet(f"background-color: {color}; color: #000; border-radius: 2px;")
            self.lbl_coupling.setVisible(True)
            self.lbl_bw.setVisible(True)
            self.lbl_probe.setVisible(True)
            self.lbl_term.setVisible(True)
        else:
            self.setStyleSheet("""
                ChannelStatusCard {{
                    background-color: #12131a;
                    border: 1px solid #1f212d;
                    border-radius: 3px;
                }}
            """)
            self.lbl_num.setStyleSheet("background-color: #334155; color: #94a3b8; border-radius: 2px;")
            self.lbl_scale.setText("--")
            self.lbl_scale.setStyleSheet("color: #64748b; font-size: 12px;")
            self.lbl_pos.setText("Closed")
            self.lbl_pos.setStyleSheet("font-family: Consolas; font-size: 12px; color: #475569;")
            self.lbl_ya.setText("YA  ---")
            self.lbl_yb.setText("YB  ---")
            self.lbl_dy.setText("ΔY  ---")
            self.lbl_coupling.setVisible(False)
            self.lbl_bw.setVisible(False)
            self.lbl_probe.setVisible(False)
            self.lbl_term.setVisible(False)

    def set_channel_info(self, enabled: bool, scale_v: float = 0.5, offset_v: float = 0.0,
                         coupling: str = "DC", bw_limit: bool = True, probe: float = 1.0,
                         term: str = "1M", unit: str = "V"):
        self.enabled = enabled
        self.unit = unit
        color = CHANNEL_COLORS[self.ch_id]
        if enabled:
            scale_str = format_voltage(scale_v, unit) + "/div"
            pos_str = f"Pos {format_voltage(offset_v, unit)}"
            self.lbl_scale.setText(scale_str)
            self.lbl_scale.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: bold;")
            self.lbl_pos.setText(pos_str)
            self.lbl_pos.setStyleSheet("font-family: Consolas; font-size: 12px; color: #cbd5e1;")
            self.lbl_coupling.setText(str(coupling).upper())
            self.lbl_bw.setText("BwL" if bw_limit else "Full")
            self.lbl_bw.setStyleSheet("font-family: Consolas; font-size: 12px; color: " + ("#eab308" if bw_limit else "#64748b") + ";")
            probe_str = f"{int(probe)}:1" if probe >= 1 else f"1:{int(1/probe)}"
            self.lbl_probe.setText(probe_str)
            self.lbl_term.setText("1MΩ" if "1M" in str(term).upper() else "50Ω")
        self._update_style()

    def set_cursors(self, ya: float, yb: float, dy: float):
        if not self.enabled:
            self.lbl_ya.setText("YA  ---")
            self.lbl_yb.setText("YB  ---")
            self.lbl_dy.setText("ΔY  ---")
            return
        self.lbl_ya.setText(f"YA {format_voltage(ya, self.unit)}")
        self.lbl_yb.setText(f"YB {format_voltage(yb, self.unit)}")
        self.lbl_dy.setText(f"ΔY {format_voltage(dy, self.unit)}")

    def set_active_cursor(self, is_active: bool):
        self.is_active_cursor = is_active
        self._update_style()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.ch_id)
        super().mousePressEvent(event)


class CursorXCard(QFrame):
    """
    光标 X 轴时间与频率测量卡片 (XA, XB, ΔX, 1/ΔX)
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("""
            CursorXCard {
                background-color: #161822;
                border: 1px solid #282a3a;
                border-radius: 3px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(1)

        self.lbl_xa = QLabel("XA  -600.00us")
        self.lbl_xa.setStyleSheet("font-family: Consolas; font-size: 12px; color: #f97316;")
        layout.addWidget(self.lbl_xa)

        self.lbl_xb = QLabel("XB   600.00us")
        self.lbl_xb.setStyleSheet("font-family: Consolas; font-size: 12px; color: #f97316;")
        layout.addWidget(self.lbl_xb)

        self.lbl_dx = QLabel("ΔX   1.2000ms")
        self.lbl_dx.setStyleSheet("font-family: Consolas; font-size: 12px; font-weight: bold; color: #38bdf8;")
        layout.addWidget(self.lbl_dx)

        self.lbl_idx = QLabel("1/ΔX 833.3Hz")
        self.lbl_idx.setStyleSheet("font-family: Consolas; font-size: 12px; color: #4ade80;")
        layout.addWidget(self.lbl_idx)

    def set_x_cursors(self, xa: float, xb: float, dx: float, freq: float):
        self.lbl_xa.setText(f"XA  {format_time(xa)}")
        self.lbl_xb.setText(f"XB  {format_time(xb)}")
        self.lbl_dx.setText(f"ΔX  {format_time(dx)}")
        self.lbl_idx.setText(f"1/ΔX {format_freq(freq)}")


class TriggerCard(QFrame):
    """
    触发状态与参数卡片 (STOP/RUN, 源通道, 扫查模式, 触发电平, 边沿)
    """
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("""
            TriggerCard {
                background-color: #161822;
                border: 1px solid #282a3a;
                border-radius: 3px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(1)

        # 运行状态 (STOP / RUN)
        self.lbl_state = QLabel("STOP")
        self.lbl_state.setAlignment(Qt.AlignCenter)
        self.lbl_state.setFont(QFont("Consolas", 9, QFont.Bold))
        self.lbl_state.setStyleSheet("background-color: #dc2626; color: white; border-radius: 2px; padding: 1px 4px;")
        layout.addWidget(self.lbl_state)

        self.lbl_src_mode = QLabel("CH1 Auto")
        self.lbl_src_mode.setStyleSheet("font-family: Consolas; font-size: 12px; color: #e2e8f0;")
        layout.addWidget(self.lbl_src_mode)

        self.lbl_level = QLabel("T: 1.50V")
        self.lbl_level.setStyleSheet("font-family: Consolas; font-size: 12px; color: #fbbf24;")
        layout.addWidget(self.lbl_level)

        self.lbl_slope = QLabel("Edge ↑")
        self.lbl_slope.setStyleSheet("font-family: Consolas; font-size: 12px; color: #94a3b8;")
        layout.addWidget(self.lbl_slope)

    def set_trigger_info(self, state: str = "STOP", source: str = "1", sweep: str = "Auto",
                         level: str = "1.50V", slope: str = "POS"):
        state_clean = str(state).strip().upper()
        if "RUN" in state_clean or "TRIG" in state_clean:
            self.lbl_state.setText("RUN")
            self.lbl_state.setStyleSheet("background-color: #16a34a; color: white; border-radius: 2px; padding: 1px 4px;")
        elif "SINGLE" in state_clean:
            self.lbl_state.setText("SINGLE")
            self.lbl_state.setStyleSheet("background-color: #f97316; color: white; border-radius: 2px; padding: 1px 4px;")
        else:
            self.lbl_state.setText("STOP")
            self.lbl_state.setStyleSheet("background-color: #dc2626; color: white; border-radius: 2px; padding: 1px 4px;")

        src_num = "1"
        for s in ("1", "2", "3", "4"):
            if s in str(source):
                src_num = s
                break
        self.lbl_src_mode.setText(f"CH{src_num} {sweep.capitalize() if sweep else 'Auto'}")
        self.lbl_level.setText(f"T: {level if 'V' in str(level) else str(level) + 'V'}")
        arrow = "↑" if "POS" in str(slope).upper() else "↓"
        self.lbl_slope.setText(f"Edge {arrow}")


class TimebaseAcqCard(QFrame):
    """
    时基、存储深度与采样率卡片 (时基档位, X-Pos, 采集时长, 存储深度 Mpts, 采样率 GSa/s, 模式 Peak/Normal)
    """
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("""
            TimebaseAcqCard {
                background-color: #161822;
                border: 1px solid #282a3a;
                border-radius: 3px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(1)

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        self.lbl_tb = QLabel("200 us/div")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("单击设置水平时基与偏移")
        self.lbl_tb.setFont(QFont("Consolas", 9, QFont.Bold))
        self.lbl_tb.setStyleSheet("color: #ffffff; font-size: 12px;")
        row1.addWidget(self.lbl_tb)

        self.lbl_xpos = QLabel("X-Pos: 0.00s")
        self.lbl_xpos.setStyleSheet("font-family: Consolas; font-size: 12px; color: #f59e0b;")
        row1.addWidget(self.lbl_xpos)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        self.lbl_duration = QLabel("128ms")
        self.lbl_duration.setStyleSheet("font-family: Consolas; font-size: 12px; color: #eab308;")
        row2.addWidget(self.lbl_duration)

        self.lbl_mdepth = QLabel("512Mpts")
        self.lbl_mdepth.setFont(QFont("Consolas", 9, QFont.Bold))
        self.lbl_mdepth.setStyleSheet("color: #38bdf8; font-size: 12px;")
        row2.addWidget(self.lbl_mdepth)
        layout.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setContentsMargins(0, 0, 0, 0)
        self.lbl_acq = QLabel("Peak")
        self.lbl_acq.setStyleSheet("font-family: Consolas; font-size: 12px; color: #94a3b8;")
        row3.addWidget(self.lbl_acq)

        self.lbl_srate = QLabel("4.00GSa/s")
        self.lbl_srate.setFont(QFont("Consolas", 8, QFont.Bold))
        self.lbl_srate.setStyleSheet("color: #10b981;")
        row3.addWidget(self.lbl_srate)
        layout.addLayout(row3)

    def set_timebase_acq(self, scale_s: float = 0.0002, offset_s: float = 0.0,
                         mdepth="512Mpts", srate="4.00GSa/s", acq_mode="Peak", duration_s: float = None):
        self.lbl_tb.setText(format_time(scale_s) + "/div")
        self.lbl_xpos.setText(f"X-Pos {format_time(offset_s)}")
        self.lbl_mdepth.setText(format_memory_depth(mdepth))
        self.lbl_srate.setText(format_sample_rate(srate))
        self.lbl_acq.setText(str(acq_mode).capitalize())
        if duration_s is not None and duration_s > 0:
            self.lbl_duration.setText(format_time(duration_s))
        else:
            try:
                pts = float(mdepth)
                sr = float(srate)
                if sr > 0:
                    self.lbl_duration.setText(format_time(pts / sr))
            except Exception:
                self.lbl_duration.setText(format_time(scale_s * 14))


class ScopeBottomBar(QFrame):
    """
    完整示波器经典底层状态与多通道独立光标信息栏 (高度还原 ZDS 示波器底层原生工具条)
    """
    channel_cursor_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(100)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("""
            ScopeBottomBar {
                background-color: #13141c;
                border-top: 1px solid #282a3c;
                border-bottom: 1px solid #282a3c;
            }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        # CH1 ~ CH4 独立状态卡片
        self.ch_cards = {}
        for ch in [1, 2, 3, 4]:
            card = ChannelStatusCard(ch)
            card.clicked.connect(self._on_channel_clicked)
            layout.addWidget(card, 3)
            self.ch_cards[ch] = card

        # 光标 X 轴时间测量卡片
        self.card_cursor_x = CursorXCard()
        layout.addWidget(self.card_cursor_x, 3)

        # 触发状态卡片
        self.card_trigger = TriggerCard()
        layout.addWidget(self.card_trigger, 2)

        # 时基与采集卡片 (存储深度、采样率、时基)
        self.card_timebase = TimebaseAcqCard()
        layout.addWidget(self.card_timebase, 3)

    def _on_channel_clicked(self, ch: int):
        self.channel_cursor_selected.emit(ch)

    def set_active_y_channel(self, ch: int):
        for cid, card in self.ch_cards.items():
            card.set_active_cursor(cid == ch)

    def update_cursor_x(self, xa: float, xb: float, dx: float, freq: float):
        self.card_cursor_x.set_x_cursors(xa, xb, dx, freq)

    def update_channel_cursor(self, ch: int, ya: float, yb: float, dy: float):
        if ch in self.ch_cards:
            self.ch_cards[ch].set_cursors(ya, yb, dy)

    def update_run_state(self, state: str):
        self.card_trigger.lbl_state.setText(state.upper())
        if "RUN" in state.upper() or "TRIG" in state.upper():
            self.card_trigger.lbl_state.setStyleSheet("background-color: #16a34a; color: white; border-radius: 2px; padding: 1px 4px;")
        elif "SINGLE" in state.upper():
            self.card_trigger.lbl_state.setStyleSheet("background-color: #f97316; color: white; border-radius: 2px; padding: 1px 4px;")
        else:
            self.card_trigger.lbl_state.setStyleSheet("background-color: #dc2626; color: white; border-radius: 2px; padding: 1px 4px;")

    def update_from_config(self, config: dict):
        if not config:
            return
        # 1. 运行状态
        if "run_state" in config:
            self.update_run_state(config["run_state"])

        # 2. 通道设置
        channels_cfg = config.get("channels", {})
        ext = config.get("extended_settings", {})
        for ch, card in self.ch_cards.items():
            ccfg = channels_cfg.get(ch, channels_cfg.get(str(ch), {}))
            enabled = ccfg.get("enabled", False)
            scale = ccfg.get("scale", 0.5)
            offset = ccfg.get("offset", 0.0)
            coupling = ccfg.get("coupling", "DC")
            bw_limit = ccfg.get("bw_limit", True)
            probe = ccfg.get("probe", 1.0)
            term = ext.get(f":CHANnel{ch}:TERMination", "1M")
            unit = "A" if str(ext.get(f":CHANnel{ch}:UNITs", "")).upper() == "AMPERE" else "V"
            card.set_channel_info(enabled, scale, offset, coupling, bw_limit, probe, term, unit)

        # 3. 触发设置
        trig_src = ext.get(":TRIGger:EDGE:SOURce", "CHANnel1")
        trig_sweep = ext.get(":TRIGger:SWEep", "AUTO")
        trig_level = ext.get(":TRIGger:EDGE:LEVel", "1.5")
        trig_slope = ext.get(":TRIGger:EDGE:SLOPe", "POSitive")
        self.card_trigger.set_trigger_info(
            state=config.get("run_state", "STOP"),
            source=trig_src,
            sweep=trig_sweep,
            level=trig_level,
            slope=trig_slope
        )

        # 4. 时基与采集
        tb_scale = config.get("timebase_scale_s", 0.0002)
        tb_offset = config.get("timebase_offset_s", 0.0)
        mdepth = config.get("mdepth_raw", "512Mpts")
        srate = config.get("sample_rate_hz", 4e9)
        acq_mode = ext.get(":ACQuire:TYPE", "Peak")
        self.card_timebase.set_timebase_acq(tb_scale, tb_offset, mdepth, srate, acq_mode)

    def update_from_waveform(self, wfm):
        if not wfm:
            return
        head = getattr(wfm, "head", None)
        if head:
            acq_modes = {0: "Normal", 1: "Peak", 2: "Average", 3: "HRes"}
            acq_str = acq_modes.get(getattr(head, "acq_mode", 1), "Peak")
            pts = 0
            if getattr(wfm, "channels", None):
                first_ch = next(iter(wfm.channels.values()), None)
                if first_ch and hasattr(first_ch, "raw_data"):
                    pts = len(first_ch.raw_data)
            dur = (pts / head.sample_rate) if (head.sample_rate and pts > 0) else None
            self.card_timebase.set_timebase_acq(
                scale_s=head.hrzt_division,
                offset_s=head.hrzt_offset,
                mdepth=pts or "512Mpts",
                srate=head.sample_rate,
                acq_mode=acq_str,
                duration_s=dur
            )
            trig_src = f"CH{head.trig_source + 1}"
            self.card_trigger.set_trigger_info(source=trig_src)

        # 遍历通道
        for ch, card in self.ch_cards.items():
            if ch in wfm.channels:
                ch_obj = wfm.channels[ch]
                info = getattr(ch_obj, "info", None)
                if info:
                    unit = "A" if getattr(info, "probe_type", 0) == 1 else "V"
                    card.set_channel_info(
                        enabled=True,
                        scale_v=info.vert_division,
                        offset_v=info.vert_offset,
                        coupling="DC" if getattr(info, "couple_mode", 0) == 0 else "AC",
                        bw_limit=bool(getattr(info, "bw_limit", 1)),
                        probe=getattr(info, "probe_att", 1.0),
                        unit=unit
                    )
            else:
                card.enabled = False
                card._update_style()


class ChannelRail(QWidget):
    def __init__(self):
        super().__init__()
        self.baselines = []

    def paintEvent(self, event):
        painter = QPainter(self)
        for color, label_y, baseline_y in self.baselines:
            painter.setPen(QPen(QColor(color), 1))
            painter.drawLine(QPointF(56,label_y), QPointF(72,baseline_y))
            painter.drawLine(QPointF(72,baseline_y), QPointF(77,baseline_y))
            painter.drawLine(QPointF(77,baseline_y), QPointF(73,baseline_y-3))
            painter.drawLine(QPointF(77,baseline_y), QPointF(73,baseline_y+3))


class ScopePlotWidget(QWidget):
    """示波器主波形绘图与独立光标组件"""

    cursor_changed = Signal(dict)
    channel_settings_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.channel_curves = {}
        self.current_wfm = None
        self.active_y_channel = 1
        self._display_scales = {}
        self._display_centers = {}
        self._center_overrides = {}
        self._display_samples = {}
        self._envelopes = {}
        self._setup_ui()
        self._view_timer = QTimer(self)
        self._view_timer.setSingleShot(True)
        self._view_timer.setInterval(40)
        self._view_timer.timeout.connect(self._refresh_visible_samples)
        self.plot_item.vb.sigXRangeChanged.connect(lambda *_: self._view_timer.start())

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # 配置 pyqtgraph 全局暗黑示波器风格
        # Dense traces must use Qt's thin-line fast path. Antialiased wide
        # paths can stall the GUI when thousands of segments overlap.
        pg.setConfigOptions(antialias=False)
        self.plot_widget = pg.PlotWidget(background="#12131a")
        self.read_status = QLabel(self.plot_widget)
        self.read_status.setAlignment(Qt.AlignCenter)
        self.read_status.setWordWrap(True)
        self.read_status.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.read_status.setStyleSheet('background: #1c2433; color: #e2e8f0; border: 1px solid #607089; padding: 12px; font-size: 15px;')
        self.read_status.hide()
        self.plot_item = self.plot_widget.getPlotItem()

        # 显示网格
        self.plot_item.showGrid(x=True, y=True, alpha=0.35)
        self.plot_item.setLabel('bottom', "时间 (Time)", units='s')
        self.plot_item.setLabel('left', "电压 (Voltage)", units='V')

        # 样式微调
        axis_font = QFont("Consolas", 9)
        self.plot_item.getAxis('bottom').setStyle(tickFont=axis_font)
        self.plot_item.getAxis('left').setStyle(tickFont=axis_font)
        self.plot_item.getAxis('bottom').setTextPen('#888899')
        self.plot_item.getAxis('left').setTextPen('#888899')

        # 添加图例
        self.channel_names = {ch: str(ch) for ch in CHANNEL_COLORS}

        # 初始化通道波形曲线
        for ch_id, color_hex in CHANNEL_COLORS.items():
            pen = pg.mkPen(color=color_hex, width=1)
            curve = self.plot_item.plot(pen=pen, name=f"CH{ch_id}", antialias=False)
            curve.setClipToView(True)
            curve.setDownsampling(auto=False)
            curve.setVisible(ch_id in [1, 2])
            self.channel_curves[ch_id] = curve

        plot_row = QHBoxLayout()
        plot_row.setSpacing(0)
        self.channel_rail = ChannelRail()
        self.channel_rail.setFixedWidth(78)
        self.channel_markers = {}
        for ch, color in CHANNEL_COLORS.items():
            marker = QPushButton(str(ch), self.channel_rail)
            marker.setFixedSize(56, 24)
            marker.setStyleSheet(f'color: {color}; border: 0; background: #12131a; text-align: right; padding: 0;')
            marker.clicked.connect(lambda checked=False, channel=ch: self.channel_settings_requested.emit(channel))
            self.channel_markers[ch] = marker
        plot_row.addWidget(self.channel_rail)
        plot_row.addWidget(self.plot_widget, 1)
        layout.addLayout(plot_row, stretch=1)
        self.plot_item.vb.sigRangeChanged.connect(self._update_channel_markers)

        # 专业示波器原生底部状态栏 (含独立通道 Y 光标、时基、采样率、存储深度)
        self.bottom_bar = ScopeBottomBar(self)

        # 辅助控制条 (自适应缩放、光标开关与当前活动 Y 光标选择)
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setContentsMargins(6, 2, 6, 2)

        self.btn_auto_fit = QPushButton("全屏自适应 (Auto Fit)")
        self.btn_auto_fit.setStyleSheet("padding: 2px 8px; font-size: 11px;")
        self.btn_auto_fit.clicked.connect(self.auto_fit)
        toolbar_layout.addWidget(self.btn_auto_fit)
        self.btn_center = QPushButton("各通道居中")
        self.btn_center.setToolTip("按各通道当前档位，将可见波形分别移到中央；仅调整本地显示")
        self.btn_center.clicked.connect(self.center_channels)
        self.btn_center.hide()
        self.btn_restore_position = QPushButton("还原位置")
        self.btn_restore_position.setToolTip("恢复采集记录中的各通道垂直位置")
        self.btn_restore_position.clicked.connect(self.restore_channel_positions)
        self.btn_restore_position.hide()

        self.cb_cursor = QCheckBox("开启双光标 (Cursors)")
        self.cb_cursor.setStyleSheet("font-size: 11px; color: #38bdf8; font-weight: bold;")
        self.cb_cursor.stateChanged.connect(self._user_toggle_cursors)
        toolbar_layout.addWidget(self.cb_cursor)

        self.lbl_cursor_info = QLabel("ΔX: --- | 1/ΔX: --- | ΔY: ---")
        self.lbl_cursor_info.setStyleSheet("font-family: Consolas; font-size: 12px; color: #b8c6d9; padding-left: 10px;")
        toolbar_layout.addStretch()
        toolbar_layout.addWidget(self.lbl_cursor_info)
        layout.addLayout(toolbar_layout)
        layout.addWidget(self.bottom_bar)

        # 初始化光标线
        self._init_cursors()

    def _init_cursors(self):
        """初始化 X1/X2 以及各通道独立的 YA/YB 光标"""
        pen_x = pg.mkPen(color="#ff9900", width=1.2, style=Qt.DashLine)

        self.line_x1 = pg.InfiniteLine(pos=-0.0006, angle=90, movable=True, pen=pen_x, label="XA: {value:.2e}s")
        self.line_x2 = pg.InfiniteLine(pos=0.0006, angle=90, movable=True, pen=pen_x, label="XB: {value:.2e}s")
        self.line_x1.setVisible(False)
        self.line_x2.setVisible(False)
        self.line_x1.sigPositionChanged.connect(self._on_x_cursor_moved)
        self.line_x2.sigPositionChanged.connect(self._on_x_cursor_moved)
        self.plot_item.addItem(self.line_x1, ignoreBounds=True)
        self.plot_item.addItem(self.line_x2, ignoreBounds=True)

        # One shared pair of horizontal cursors updates every channel card.
        pen_a = pg.mkPen(color="#e2e8f0", width=1.2, style=Qt.DashLine)
        pen_b = pg.mkPen(color="#94a3b8", width=1.2, style=Qt.DotLine)
        self.line_y1 = pg.InfiniteLine(pos=3.0, angle=0, movable=True, pen=pen_a, label="YA: {value:.3g}")
        self.line_y2 = pg.InfiniteLine(pos=0.0, angle=0, movable=True, pen=pen_b, label="YB: {value:.3g}")
        self.channel_cursors = {ch: {"ya": self.line_y1, "yb": self.line_y2} for ch in (1, 2, 3, 4)}
        for line in (self.line_y1, self.line_y2):
            line.setVisible(False)
            line.sigPositionChanged.connect(self._on_y_cursor_moved)
            self.plot_item.addItem(line, ignoreBounds=True)

    def _user_toggle_cursors(self, state):
        if state == Qt.Checked or state == 2:
            left, right = self.plot_item.viewRange()[0]
            middle = (left + right) / 2
            spacing = (right - left) * .1
            self.line_x1.setValue(middle - spacing)
            self.line_x2.setValue(middle + spacing)
        self._toggle_cursors(state)

    def _toggle_cursors(self, state):
        visible = state == Qt.Checked or state == 2
        for line in (self.line_x1, self.line_x2, self.line_y1, self.line_y2):
            line.setVisible(visible)
        if visible:
            self._on_x_cursor_moved()
            self._on_y_cursor_moved()
        else:
            self.lbl_cursor_info.setText("ΔX: --- | 1/ΔX: --- | ΔY: ---")

    def _on_x_cursor_moved(self):
        if not self.cb_cursor.isChecked():
            return
        xa = self.line_x1.value()
        xb = self.line_x2.value()
        dx = abs(xb - xa)
        freq = (1.0 / dx) if dx > 1e-12 else 0.0

        self.bottom_bar.update_cursor_x(xa, xb, dx, freq)
        self._emit_cursor_change()

    def _on_y_cursor_moved(self, *_):
        ya, yb = self.line_y1.value(), self.line_y2.value()
        for channel in (1, 2, 3, 4):
            scale = self._display_scales.get(channel, 1.0)
            center = self._display_centers.get(channel, 0.0)
            self.bottom_bar.update_channel_cursor(channel, ya * scale + center, yb * scale + center, (ya - yb) * scale)
        self._emit_cursor_change()

    def _emit_cursor_change(self):
        dx = abs(self.line_x2.value() - self.line_x1.value())
        freq = (1.0 / dx) if dx > 1e-12 else 0.0
        cur_y = self.channel_cursors.get(self.active_y_channel, self.channel_cursors[1])
        dy = abs(cur_y["ya"].value() - cur_y["yb"].value())

        self.lbl_cursor_info.setText(
            f"ΔX: {format_time(dx)} | 1/ΔX: {format_freq(freq)} | ΔY: {dy:.3g} 格（各通道数值见底栏）"
        )

        self.cursor_changed.emit({
            "dx": dx,
            "freq": freq,
            "dy": dy,
            "x1": self.line_x1.value(),
            "x2": self.line_x2.value(),
            "y1": cur_y["ya"].value(),
            "y2": cur_y["yb"].value(),
            "active_channel": self.active_y_channel,
        })

    def update_scope_config(self, config: dict):
        """同步示波器硬件设置到经典底栏"""
        self.bottom_bar.update_from_config(config)
        self._on_y_cursor_moved()

    def update_run_state(self, state: str):
        """同步运行状态"""
        self.bottom_bar.update_run_state(state)

    def update_waveform(self, wfm, reset_cursors=True):
        """刷新通道波形数据与底栏状态"""
        first_waveform = self.current_wfm is None
        self.current_wfm = wfm
        self._envelopes = {ch: DisplayEnvelope(data) for ch, data in wfm.channels.items()}
        self.read_status.hide()
        for ch_id in [1, 2, 3, 4]:
            if ch_id in wfm.channels:
                ch_data = wfm.channels[ch_id]
                scale = abs(float(ch_data.info.vert_division))
                self._display_scales[ch_id] = scale if np.isfinite(scale) and scale > 0 else 1.0
                sign = -1 if ch_data.info.reversed else 1
                self._display_centers[ch_id] = self._center_overrides.get(ch_id, -sign * ch_data.info.vert_offset)
                self._display_samples[ch_id] = self._envelopes[ch_id].samples(pixels=max(100, self.plot_widget.width()))
                self._draw_channel(ch_id)
                self.channel_curves[ch_id].setVisible(True)
            else:
                self.channel_curves[ch_id].clear()
                self._display_samples.pop(ch_id, None)
        self.plot_item.setLabel('left', "垂直位置（格；各通道档位见底栏）", units='')
        self.plot_item.getAxis('left').setTickSpacing(1, .2)
        if first_waveform:
            self.plot_item.setYRange(-4, 4, padding=0)

        # 同步更新底栏
        self.bottom_bar.update_from_waveform(wfm)
        self._on_y_cursor_moved()
        self._toggle_cursors(self.cb_cursor.isChecked() * 2)

        # 根据波形自适应初始化光标位置 (避免飞出波形区域)
        if reset_cursors and self.active_y_channel in wfm.channels:
            data = wfm.channels[self.active_y_channel]
            from ..records import blocks
            v_arr = next(blocks(data, 0, min(1000, len(data.raw_data))))[2] if len(data.raw_data) else np.array([])
            if len(v_arr) > 0:
                v_max = float(np.max(v_arr[:1000]))
                v_min = float(np.min(v_arr[:1000]))
                center = self._display_centers[self.active_y_channel]
                scale = self._display_scales[self.active_y_channel]
                self.channel_cursors[self.active_y_channel]["ya"].setValue((v_max - center) / scale)
                self.channel_cursors[self.active_y_channel]["yb"].setValue((v_min - center) / scale)
                self._on_y_cursor_moved(self.active_y_channel)

    def _draw_channel(self, channel):
        times, values = self._display_samples[channel]
        self.channel_curves[channel].setData(times, (values - self._display_centers[channel]) / self._display_scales[channel])
        self._update_channel_markers()

    def _refresh_visible_samples(self):
        if self.current_wfm is None:
            return
        left, right = self.plot_item.viewRange()[0]
        for channel, envelope in self._envelopes.items():
            self._display_samples[channel] = envelope.samples(left, right, max(100, self.plot_widget.width()))
            self._draw_channel(channel)

    def set_channel_name(self, channel, name):
        self.channel_names[channel] = name.strip() or str(channel)
        self._update_channel_markers()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._update_channel_markers)
        if hasattr(self, '_view_timer'):
            self._view_timer.start()

    def clear_for_refresh(self):
        # Only clear the display. The owning window retains the last record
        # for saving; never delete the user's captured files on a refresh.
        self.current_wfm = None
        self._envelopes.clear()
        self._view_timer.stop()
        self._display_samples.clear()
        for curve in self.channel_curves.values():
            curve.clear()
        for line in (self.line_x1, self.line_x2, self.line_y1, self.line_y2):
            line.hide()
        self._update_channel_markers()
        self.show_read_status('正在读取新波形…')

    def show_read_status(self, message):
        self.read_status.setText(str(message))
        self.read_status.show()
        self._position_read_status()

    def _position_read_status(self):
        width = max(120, min(560, self.plot_widget.width()-40))
        self.read_status.setGeometry(max(0,(self.plot_widget.width()-width)//2), max(0,(self.plot_widget.height()-100)//2), width, 100)
        self.read_status.raise_()

    def _update_channel_markers(self, *_):
        if not hasattr(self, 'channel_markers'):
            return
        self._position_read_status()
        low, high = self.plot_item.viewRange()[1]
        placed = []
        self.channel_rail.baselines = []
        for ch, marker in self.channel_markers.items():
            visible = ch in self._display_samples and self.channel_curves[ch].isVisible()
            marker.setVisible(visible)
            if not visible:
                continue
            zero = -self._display_centers[ch] / self._display_scales[ch]
            edge = min(high, max(low, zero))
            y = int(self.plot_item.vb.mapViewToScene(QPointF(0, edge)).y()) - 12
            y = max(0, min(max(0, self.channel_rail.height()-24), y))
            baseline_y = y + 12
            while any(abs(y-p) < 24 for p in placed) and y >= 24:
                y -= 24
            placed.append(y)
            marker.move(0, y)
            arrow = ' v' if zero < low else ' ^' if zero > high else ''
            marker.setText(f'{self.channel_names[ch]}{arrow}')
            marker.setToolTip(f'CH{ch}：{self.channel_names[ch]}，0 基准位于 {zero:.3g} 格。点击修改显示名称或通道设置。')
            self.channel_rail.baselines.append((CHANNEL_COLORS[ch], y+12, baseline_y))
        self.channel_rail.update()

    def set_channel_centered(self, channel, enabled):
        if channel not in self._display_samples:
            return
        if enabled:
            self.center_channels(channel)
        else:
            self._center_overrides.pop(channel, None)
            info = self.current_wfm.channels[channel].info
            self._display_centers[channel] = info.vert_offset if info.reversed else -info.vert_offset
            self._draw_channel(channel)
            self._on_y_cursor_moved()

    def center_channels(self, selected=None):
        left, right = self.plot_item.viewRange()[0]
        for channel, (times, values) in self._display_samples.items():
            if selected is not None and selected is not False and channel != selected:
                continue
            visible = values[(times >= left) & (times <= right)]
            finite = visible[np.isfinite(visible)]
            if not finite.size:
                continue
            center = float((finite.min() + finite.max()) / 2)
            self._center_overrides[channel] = center
            self._display_centers[channel] = center
            self._draw_channel(channel)
        self.plot_item.setYRange(-4, 4, padding=0)
        self._on_y_cursor_moved()

    def restore_channel_positions(self):
        self._center_overrides.clear()
        if self.current_wfm:
            for channel, data in self.current_wfm.channels.items():
                sign = -1 if data.info.reversed else 1
                self._display_centers[channel] = -sign * data.info.vert_offset
                self._draw_channel(channel)
            self.plot_item.setYRange(-4, 4, padding=0)
            self._on_y_cursor_moved()

    def show_interval(self, start, end):
        from ..records import interval_indices, blocks
        if self.current_wfm is None:
            return
        for channel, data in self.current_wfm.channels.items():
            a, b = interval_indices(data, start, end)
            if b > a and b - a <= 100000:
                _, times, values, _ = next(blocks(data, a, b, size=100000))
                self._display_samples[channel] = (times, values)
                self._draw_channel(channel)
        self.plot_item.setXRange(start, end, padding=0)

    def set_channel_visible(self, ch_id: int, visible: bool):
        if ch_id in self.channel_curves:
            self.channel_curves[ch_id].setVisible(visible)
        if ch_id in self.bottom_bar.ch_cards:
            self.bottom_bar.ch_cards[ch_id].enabled = visible
            self.bottom_bar.ch_cards[ch_id]._update_style()
        self._update_channel_markers()

    def auto_fit(self):
        """自适应波形范围"""
        if not self.current_wfm:
            return
        # Restore the full display samples after interval viewing, and set an
        # explicit X range: clip-to-view can leave autoRange with no points.
        for channel, data in self.current_wfm.channels.items():
            self._display_samples[channel] = self._envelopes[channel].samples(pixels=max(100, self.plot_widget.width()))
            self._draw_channel(channel)
        times = [t for ch, (t,v) in self._display_samples.items() if len(t) and self.channel_curves[ch].isVisible()]
        if times:
            self.plot_item.setXRange(min(float(t[0]) for t in times), max(float(t[-1]) for t in times), padding=.02)
            # Keep instrument positions; local centering is an explicit channel option.
            extents = [float(np.max(np.abs(curve.yData[np.isfinite(curve.yData)]))) for curve in self.channel_curves.values()
                       if curve.isVisible() and curve.yData is not None and np.any(np.isfinite(curve.yData))]
            extent = max(4.0, max(extents, default=0) * 1.05)
            self.plot_item.setYRange(-extent, extent, padding=0)
