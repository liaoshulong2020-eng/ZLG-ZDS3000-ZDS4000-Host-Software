"""
电源环路分析 (Loop Test / Bode 图) 监控组件
展示环路响应曲线 (增益与相位对数频响)、相位裕度 PM、增益裕度 GM 及稳定性诊断
"""

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QFileDialog, QMessageBox
)
from PySide6.QtGui import QFont

from ..storage import ScopeStorage


class BodeDockWidget(QWidget):
    """电源环路 Bode 分析显示面板"""

    query_loop_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.last_loop_data = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # 顶部指标卡片 (PM & GM)
        cards_layout = QHBoxLayout()

        # PM 卡片
        self.card_pm = QFrame()
        self.card_pm.setStyleSheet("background-color: #1e1e2d; border: 1px solid #374151; border-radius: 6px; padding: 4px;")
        pm_vbox = QVBoxLayout(self.card_pm)
        pm_vbox.setContentsMargins(6, 4, 6, 4)
        pm_vbox.setSpacing(2)
        lbl_pm_title = QLabel("相位裕度 (Phase Margin)")
        lbl_pm_title.setStyleSheet("color: #94a3b8; font-size: 10px;")
        self.lbl_pm_val = QLabel("---")
        self.lbl_pm_val.setStyleSheet("color: #10b981; font-size: 16px; font-weight: bold; font-family: Consolas;")
        pm_vbox.addWidget(lbl_pm_title)
        pm_vbox.addWidget(self.lbl_pm_val)
        cards_layout.addWidget(self.card_pm)

        # GM 卡片
        self.card_gm = QFrame()
        self.card_gm.setStyleSheet("background-color: #1e1e2d; border: 1px solid #374151; border-radius: 6px; padding: 4px;")
        gm_vbox = QVBoxLayout(self.card_gm)
        gm_vbox.setContentsMargins(6, 4, 6, 4)
        gm_vbox.setSpacing(2)
        lbl_gm_title = QLabel("增益裕度 (Gain Margin)")
        lbl_gm_title.setStyleSheet("color: #94a3b8; font-size: 10px;")
        self.lbl_gm_val = QLabel("---")
        self.lbl_gm_val.setStyleSheet("color: #38bdf8; font-size: 16px; font-weight: bold; font-family: Consolas;")
        gm_vbox.addWidget(lbl_gm_title)
        gm_vbox.addWidget(self.lbl_gm_val)
        cards_layout.addWidget(self.card_gm)

        # 控制按钮
        btn_vbox = QVBoxLayout()
        self.btn_refresh = QPushButton("读取环路数据 (Query Loop)")
        self.btn_refresh.setStyleSheet("background-color: #2563eb; color: white; padding: 5px; font-weight: bold; font-size: 11px;")
        self.btn_refresh.clicked.connect(self.query_loop_requested.emit)

        self.btn_export = QPushButton("导出环路 CSV")
        self.btn_export.setStyleSheet("background-color: #374151; color: white; padding: 4px; font-size: 11px;")
        self.btn_export.clicked.connect(self._export_csv)

        btn_vbox.addWidget(self.btn_refresh)
        btn_vbox.addWidget(self.btn_export)
        cards_layout.addLayout(btn_vbox)

        layout.addLayout(cards_layout)

        # 双 Y 轴 Bode 绘图区域 (左轴 Gain dB, 右轴 Phase deg)
        self.plot_widget = pg.PlotWidget(background="#12131a")
        self.plot_item = self.plot_widget.getPlotItem()
        self.plot_item.setLogMode(x=True, y=False)
        self.plot_item.showGrid(x=True, y=True, alpha=0.3)
        self.plot_item.setLabel('bottom', "频率 (Frequency)", units='Hz')
        self.plot_item.setLabel('left', "增益 (Gain)", units='dB')

        # 0dB 基准线
        zero_line = pg.InfiniteLine(pos=0.0, angle=0, pen=pg.mkPen("#64748b", width=1, style=Qt.DashLine))
        self.plot_item.addItem(zero_line)

        # 增益曲线
        self.curve_gain = self.plot_item.plot(
            pen=pg.mkPen("#38bdf8", width=2),
            name="增益 (Gain dB)"
        )

        # 创建右侧相位坐标轴
        self.phase_view = pg.ViewBox()
        self.plot_item.scene().addItem(self.phase_view)
        self.plot_item.getAxis('right').linkToView(self.phase_view)
        self.phase_view.setXLink(self.plot_item)
        self.plot_item.showAxis('right')
        self.plot_item.setLabel('right', "相位 (Phase)", units='°')

        # 相位曲线
        self.curve_phase = pg.PlotDataItem(
            pen=pg.mkPen("#f43f5e", width=2, style=Qt.DashLine),
            name="相位 (Phase °)"
        )
        self.phase_view.addItem(self.curve_phase)

        def update_views():
            self.phase_view.setGeometry(self.plot_item.vb.sceneBoundingRect())
            self.phase_view.linkedViewChanged(self.plot_item.vb, self.phase_view.XAxis)

        self.plot_item.vb.sigResized.connect(update_views)

        layout.addWidget(self.plot_widget)

    def update_loop_data(self, loop_data: dict):
        """刷新 Bode 图与 PM/GM 指标"""
        self.last_loop_data = loop_data
        pm = loop_data.get("pm", "---")
        gm = loop_data.get("gm", "---")

        self.lbl_pm_val.setText(pm)
        self.lbl_gm_val.setText(gm)

        # 评定颜色
        try:
            pm_num = float(pm.replace("°", "").strip())
            if 45.0 <= pm_num <= 65.0:
                self.lbl_pm_val.setStyleSheet("color: #10b981; font-size: 16px; font-weight: bold; font-family: Consolas;")
            elif pm_num < 45.0:
                self.lbl_pm_val.setStyleSheet("color: #ef4444; font-size: 16px; font-weight: bold; font-family: Consolas;")
            else:
                self.lbl_pm_val.setStyleSheet("color: #f59e0b; font-size: 16px; font-weight: bold; font-family: Consolas;")
        except Exception:
            pass

        # 解析频响表并绘制
        table = loop_data.get("table", [])
        if not table:
            return

        freqs = []
        gains = []
        phases = []

        for row in table:
            f = row.get("freq_hz")
            g = row.get("gain_db")
            p = row.get("phase_deg")

            # 兼容字符串解析
            if f is None and "freq_str" in row:
                s = row["freq_str"].lower()
                if "k" in s:
                    f = float(s.replace("khz", "")) * 1e3
                elif "m" in s:
                    f = float(s.replace("mhz", "")) * 1e6
                else:
                    f = float(s.replace("hz", ""))

            if g is None and "gain_str" in row:
                g = float(row["gain_str"].replace("db", "").replace("k", "000"))

            if p is None and "phase_str" in row:
                p = float(row["phase_str"].replace("°", "").replace("deg", ""))

            if f is not None and g is not None and p is not None and f > 0:
                freqs.append(f)
                gains.append(g)
                phases.append(p)

        if freqs:
            freqs_arr = np.array(freqs)
            gains_arr = np.array(gains)
            phases_arr = np.array(phases)

            self.curve_gain.setData(freqs_arr, gains_arr)
            self.curve_phase.setData(freqs_arr, phases_arr)
            self.plot_item.enableAutoRange()
            self.phase_view.enableAutoRange()

    def _export_csv(self):
        if not self.last_loop_data or not self.last_loop_data.get("table"):
            QMessageBox.information(self, "提示", "暂无环路数据可导出，请先读取环路数据。")
            return

        path, _ = QFileDialog.getSaveFileName(self, "保存环路测试 CSV", "bode_loop.csv", "CSV Files (*.csv)")
        if path:
            ScopeStorage.save_loop_csv(self.last_loop_data, path)
            QMessageBox.information(self, "成功", f"环路数据已导出至:\n{path}")
