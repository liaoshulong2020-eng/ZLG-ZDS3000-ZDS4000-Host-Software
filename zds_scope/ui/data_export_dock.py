"""
数据化导出与 FFT 谐波分析面板 (Data Export & FFT Harmonics Dock)
将示波器实测波形、硬件测量、FFT 频谱与各次谐波量化为精确纯文本与表格
支持复制和保存结构化测量报表
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QTextEdit, QComboBox, QGroupBox, QSpinBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QApplication, QSplitter, QFileDialog
)
from PySide6.QtGui import QFont, QColor

from ..data_processor import DigitalDataProcessor
from ..storage import ScopeStorage


class DataExportDockWidget(QWidget):
    """数据化与 FFT 谐波导出控件"""

    recalculate_requested = Signal(int, int)  # (channel, downsample_points)
    save_csv_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.last_text_report = ""
        self.last_fft_res = {}
        self.current_wfm = None
        self.current_measurements = {}
        self.current_loop_data = None
        self._setup_ui()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(6)

        # 1. 顶部控制栏
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        top_bar.addWidget(QLabel("分析通道:"))
        self.combo_ch = QComboBox()
        self.combo_ch.addItems(["CH1", "CH2", "CH3", "CH4"])
        self.combo_ch.currentIndexChanged.connect(self._on_params_changed)
        top_bar.addWidget(self.combo_ch)

        top_bar.addWidget(QLabel("时域点数采样:"))
        self.combo_downsample = QComboBox()
        self.combo_downsample.addItem("100 点 (紧凑 Prompt)", 100)
        self.combo_downsample.addItem("200 点 (标准推荐)", 200)
        self.combo_downsample.addItem("500 点 (高精细节)", 500)
        self.combo_downsample.addItem("1000 点 (摘要，非全量)", 1000)
        self.combo_downsample.setCurrentIndex(1)
        self.combo_downsample.currentIndexChanged.connect(self._on_params_changed)
        top_bar.addWidget(self.combo_downsample)

        self.btn_recalc = QPushButton("🔄 重新生成数据化文本 (Generate Data)")
        self.btn_recalc.setStyleSheet("""
            background-color: #2563eb;
            color: white;
            font-weight: bold;
            padding: 5px 12px;
            border-radius: 4px;
        """)
        self.btn_recalc.clicked.connect(self._trigger_recalculate)
        top_bar.addWidget(self.btn_recalc)

        top_bar.addStretch()

        self.lbl_thd_badge = QLabel("基波: --- | THD: ---")
        self.lbl_thd_badge.setStyleSheet("""
            background-color: #1e1e2e;
            border: 1px solid #3b82f6;
            color: #38bdf8;
            font-family: Consolas;
            font-size: 11px;
            font-weight: bold;
            padding: 4px 8px;
            border-radius: 4px;
        """)
        top_bar.addWidget(self.lbl_thd_badge)

        main_layout.addLayout(top_bar)

        # 2. 中间分割区：左侧 FFT 谐波表格，右侧纯文本摘要
        splitter = QSplitter(Qt.Horizontal)

        # 左侧：谐波量化数据表
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        lbl_harm_title = QLabel("FFT 谐波分解量化表 (Harmonics Table):")
        lbl_harm_title.setStyleSheet("font-weight: bold; color: #94a3b8; font-size: 11px;")
        left_layout.addWidget(lbl_harm_title)

        self.table_harmonics = QTableWidget()
        self.table_harmonics.setColumnCount(5)
        self.table_harmonics.setHorizontalHeaderLabels(["次数", "频率 (kHz)", "幅值 (V)", "幅度 (dBV)", "相对基波 (dBc)"])
        self.table_harmonics.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_harmonics.setStyleSheet("""
            QTableWidget {
                background-color: #14151f;
                gridline-color: #262837;
                color: #e2e8f0;
                font-family: Consolas;
                font-size: 11px;
            }
            QHeaderView::section {
                background-color: #1c1e2e;
                color: #94a3b8;
                font-weight: bold;
                border: 1px solid #262837;
                padding: 3px;
            }
        """)
        left_layout.addWidget(self.table_harmonics)
        splitter.addWidget(left_widget)

        # 右侧：数据文本预览区
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        lbl_text_title = QLabel("测量摘要预览（已抽样）；全量数据请使用 文件 → 导出完整测试快照包:")
        lbl_text_title.setStyleSheet("font-weight: bold; color: #94a3b8; font-size: 11px;")
        right_layout.addWidget(lbl_text_title)

        self.txt_preview = QTextEdit()
        self.txt_preview.setReadOnly(True)
        self.txt_preview.setFont(QFont("Consolas", 10))
        self.txt_preview.setStyleSheet("""
            background-color: #0d0e15;
            color: #38bdf8;
            border: 1px solid #272a3d;
            border-radius: 4px;
            padding: 6px;
        """)
        right_layout.addWidget(self.txt_preview)
        splitter.addWidget(right_widget)

        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)
        main_layout.addWidget(splitter)

        # 3. 底部复制与保存操作条
        bottom_bar = QHBoxLayout()
        bottom_bar.setSpacing(8)

        self.btn_copy_all = QPushButton("📋 复制摘要文本 (非全量)")
        self.btn_copy_all.setStyleSheet("""
            background-color: #059669;
            color: white;
            font-weight: bold;
            padding: 6px 14px;
            border-radius: 4px;
        """)
        self.btn_copy_all.clicked.connect(self._copy_all_text)
        bottom_bar.addWidget(self.btn_copy_all)

        self.btn_copy_harmonics_only = QPushButton("📋 仅复制 FFT 谐波数据")
        self.btn_copy_harmonics_only.setStyleSheet("background-color: #1e293b; color: #38bdf8; padding: 6px;")
        self.btn_copy_harmonics_only.clicked.connect(self._copy_harmonics_only)
        bottom_bar.addWidget(self.btn_copy_harmonics_only)


        self.btn_save_csv = QPushButton("💾 导出全量时域 CSV")
        self.btn_save_csv.setStyleSheet("background-color: #1e293b; color: #f1f5f9; padding: 6px;")
        self.btn_save_csv.clicked.connect(self._save_to_csv_file)
        bottom_bar.addWidget(self.btn_save_csv)

        bottom_bar.addStretch()
        main_layout.addLayout(bottom_bar)

    def _on_params_changed(self):
        self._trigger_recalculate()

    def _trigger_recalculate(self):
        ch = self.combo_ch.currentIndex() + 1
        downsample = self.combo_downsample.currentData()
        self.recalculate_requested.emit(ch, downsample)

    def update_data_view(self, wfm, measurements: dict, loop_data: dict = None):
        """用最新波形与测量数据更新数字化视图"""
        self.current_wfm = wfm
        self.current_measurements = measurements
        self.current_loop_data = loop_data

        active_ch = self.combo_ch.currentIndex() + 1
        if wfm and wfm.channels and active_ch not in wfm.channels:
            active_ch = min(wfm.channels)
            self.combo_ch.blockSignals(True)
            self.combo_ch.setCurrentIndex(active_ch - 1)
            self.combo_ch.blockSignals(False)
        downsample = self.combo_downsample.currentData() or 200

        # 生成全量数据化文本
        text_report = DigitalDataProcessor.generate_summary_text(
            wfm=wfm,
            measurements=measurements,
            loop_data=loop_data,
            active_ch=active_ch,
            downsample_points=downsample
        )
        self.last_text_report = text_report
        self.txt_preview.setPlainText(text_report)

    def _copy_all_text(self):
        if not self.last_text_report:
            QMessageBox.information(self, "提示", "暂无可用数据，请先连接示波器并读取波形。")
            return
        QApplication.clipboard().setText(self.last_text_report)
        QMessageBox.information(
            self,
            "复制成功",
            "已将全套数字化数据（硬件测量表、FFT 谐波数据、时域采样点）复制到剪贴板！\n\n"
            "可粘贴到测试记录或文档。摘要中的波形点已抽样，不代替完整记录。"
        )

    def _copy_harmonics_only(self):
        if not self.last_fft_res or not self.last_fft_res.get("harmonics"):
            QMessageBox.information(self, "提示", "暂无 FFT 谐波数据。")
            return

        lines = ["# FFT 谐波量化数据表"]
        lines.append(f"基波频率: {self.last_fft_res.get('fundamental_freq', 0)/1e3:.3f} kHz")
        unit = getattr(self, "amplitude_unit", "V")
        lines.append(f"基波幅值: {self.last_fft_res.get('fundamental_amp', 0):.4f} {unit}")
        lines.append(f"总谐波失真 (THD): {self.last_fft_res.get('thd_pct', 0):.2f} %\n")
        lines.append(f"谐波次数,实际频率(kHz),幅值({unit}),幅度(dB{unit}),相对基波(dBc)")
        for h in self.last_fft_res["harmonics"]:
            lines.append(f"{h['order']},{h['freq_hz']/1e3:.3f},{h['amp_v']:.4f},{h['amp_dbv']:.2f},{h['rel_dbc']:.2f}")

        QApplication.clipboard().setText("\n".join(lines))
        QMessageBox.information(self, "成功", "FFT 谐波数据已复制到剪贴板。")

    def _save_to_csv_file(self):
        self.save_csv_requested.emit()
