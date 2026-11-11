"""
示波器实时测量参数监控面板 (Measurement Dock)
实时展示各通道的 Vpp、Vrms、Vmax、Vmin、频率、占空比、上升时间、过冲等硬件测量指标
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QHBoxLayout, QFrame, QPushButton
)
from PySide6.QtGui import QColor, QFont
from .scope_plot import CHANNEL_COLORS


class MeasureDockWidget(QWidget):
    """测量数据表格显示控件"""
    refresh_requested = Signal()

    MEAS_ITEMS = [
        ("峰峰值 (Vpp)", "vpp", "V"),
        ("有效值 (Vrms)", "vrms", "V"),
        ("平均值 (Vavg)", "vavg", "V"),
        ("最大值 (Vmax)", "vmax", "V"),
        ("最小值 (Vmin)", "vmin", "V"),
        ("频率 (Freq)", "freq", "Hz"),
        ("周期 (Period)", "period", "s"),
        ("占空比 (Duty)", "duty", "%"),
        ("过冲 (Overshoot)", "overshoot", "%"),
        ("上升时间 (Tr)", "rise_time", "s"),
        ("下降时间 (Tf)", "fall_time", "s"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.channel_units = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # 头部说明
        hdr_layout = QHBoxLayout()
        title = QLabel("实时物理量测量 (Hardware Measurements)")
        title.setStyleSheet("font-weight: bold; color: #e2e8f0; font-size: 11px;")
        hdr_layout.addWidget(title)
        hdr_layout.addStretch()
        self.refresh_button = QPushButton("刷新测量")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        hdr_layout.addWidget(self.refresh_button)
        layout.addLayout(hdr_layout)
        self.message = QLabel("读取仪器硬件测量；缺失的测量项会自动启用。无需重新下载波形。")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

        # 测量表格
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setRowCount(len(self.MEAS_ITEMS))
        self.table.setHorizontalHeaderLabels(["CH1", "CH2", "CH3", "CH4"])
        self.table.setVerticalHeaderLabels([label for label, _, _ in self.MEAS_ITEMS])

        # 表头与网格样式
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: #161722;
                gridline-color: #2a2b3d;
                color: #e2e8f0;
                font-family: Consolas;
                font-size: 11px;
                border: 1px solid #2a2b3d;
            }
            QHeaderView::section {
                background-color: #1f202e;
                color: #94a3b8;
                font-weight: bold;
                border: 1px solid #2a2b3d;
                padding: 2px;
            }
        """)

        # 为各通道填充初始单元格
        font = QFont("Consolas", 10)
        for r in range(len(self.MEAS_ITEMS)):
            for c in range(4):
                item = QTableWidgetItem("---")
                item.setTextAlignment(Qt.AlignCenter)
                item.setFont(font)
                item.setForeground(QColor(CHANNEL_COLORS[c + 1]))
                self.table.setItem(r, c, item)

        layout.addWidget(self.table)

    def update_measurements(self, ch: int, meas: dict):
        """更新指定通道的测量数据"""
        col = ch - 1
        if col < 0 or col >= 4:
            return

        for row, (_, key, unit) in enumerate(self.MEAS_ITEMS):
            if unit == "V":
                unit = self.channel_units.get(ch, "V")
            val = meas.get(key)
            text = self._format_value(key, val, unit)
            item = self.table.item(row, col)
            if item:
                item.setText(text)
                item.setToolTip("仪器尚未返回有效值。可点击刷新测量重试；平直信号可能无法测量频率、周期或边沿。" if val is None else "仪器硬件测量值")

    def set_channel_unit(self, channel, unit):
        self.channel_units[channel] = unit
        self.table.horizontalHeaderItem(channel - 1).setText(f"CH{channel} ({unit})")

    def _format_value(self, key: str, val: float, unit: str) -> str:
        if val is None:
            return "无有效测量"
        if val == 0.0 and key in ["freq", "period"]:
            return "---"

        if unit == "Hz":
            if val >= 1e6:
                return f"{val / 1e6:.3f} MHz"
            elif val >= 1e3:
                return f"{val / 1e3:.3f} kHz"
            else:
                return f"{val:.1f} Hz"
        elif unit == "s":
            if val < 1e-6:
                return f"{val * 1e9:.1f} ns"
            elif val < 1e-3:
                return f"{val * 1e6:.2f} us"
            elif val < 1.0:
                return f"{val * 1e3:.2f} ms"
            else:
                return f"{val:.3f} s"
        elif unit == "%":
            return f"{val:.1f} %"
        elif unit in ("V", "A"):
            if abs(val) < 0.1:
                return f"{val * 1000.0:.1f} m{unit}"
            else:
                return f"{val:.3f} {unit}"
        return f"{val:.2f} {unit}"
