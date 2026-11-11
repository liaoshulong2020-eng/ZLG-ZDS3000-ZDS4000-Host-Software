"""
波形数据归档与按需导出配置对话框
支持磁盘容量预估、格式自选、后台多线程下载、实时速度与 ETA 显示、以及取消操作
"""

import os
import time
from typing import Dict, Any, List, Optional
from PySide6.QtCore import Qt, QThread, Signal, QSettings
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
    QLineEdit, QPushButton, QProgressBar, QGroupBox, QFileDialog,
    QMessageBox, QFrame
)

from ..storage import ScopeStorage
from ..capture import capture_archive
from ..driver import ScopeTransferCancelled, ScopeTransferInterrupted


class CaptureWorker(QThread):
    """后台异步波形采集与流式写入工作线程"""
    progress_signal = Signal(object, object, float, float, float, str)
    finished_signal = Signal(dict)
    failed_signal = Signal(str, str)     # (error_message, partial_path)
    cancelled_signal = Signal(str)      # (partial_path)

    def __init__(self, driver, channels: List[int], session_name: str,
                 base_dir: str, export_options: dict, parent=None, test_context=None):
        super().__init__(parent)
        self.driver = driver
        self.channels = channels
        self.session_name = session_name
        self.base_dir = base_dir
        self.export_options = export_options
        self.is_cancelled = False
        self.test_context = test_context or {}

    def cancel(self):
        """用户触发取消操作"""
        self.is_cancelled = True

    def run(self):
        t0 = time.time()

        def on_progress(recvd, total, speed, elapsed, eta):
            self.progress_signal.emit(recvd, total, speed, elapsed, eta, "正在通过网线流式接收波形数据...")

        try:
            self.progress_signal.emit(0, 1, 0.0, 0.0, 0.0, "正在连接示波器并核对硬件配置与存储深度...")
            saved_dir, wfm, meas, screenshot = capture_archive(
                driver=self.driver,
                channels=self.channels,
                session_name=self.session_name,
                base_dir=self.base_dir,
                export_options=self.export_options,
                progress_callback=on_progress,
                cancel_token=self
                , test_context=self.test_context,
                stage_callback=lambda stage: self.progress_signal.emit(0, 1, 0., 0., 0., stage)
            )
            result = {
                "saved_dir": saved_dir,
                "wfm": wfm,
                "measurements": meas,
                "screenshot": screenshot,
                "duration_s": time.time() - t0
            }
            self.finished_signal.emit(result)

        except ScopeTransferCancelled as error:
            self.cancelled_signal.emit(str(error))

        except Exception as exc:
            self.failed_signal.emit(str(exc), self.base_dir)


class ExportConfigDialog(QDialog):
    """导出归档选项与容量预估交互对话框"""

    def __init__(self, driver, active_channels: List[int], expected_points: int = 512_000_000, parent=None, test_context=None):
        super().__init__(parent)
        self.driver = driver
        self.active_channels = active_channels or [1, 2]
        self.expected_points = expected_points or 256_000_000
        self.worker: Optional[CaptureWorker] = None
        self.capture_result: Optional[dict] = None
        self.test_context = test_context or {}

        self.setWindowTitle("波形数据归档与按需导出配置 (Export & Storage Manager)")
        self.resize(620, 600)
        self._setup_ui()
        self._update_space_estimate()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # 1. 会话名称与路径
        path_group = QGroupBox("归档会话与存储路径")
        path_layout = QVBoxLayout(path_group)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("会话命名 (Session):"))
        default_name = f"zds_run_{time.strftime('%Y%m%d_%H%M%S')}"
        self.txt_session = QLineEdit(default_name)
        name_row.addWidget(self.txt_session)
        path_layout.addLayout(name_row)

        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("存储目录 (Target Dir):"))
        self.path_settings = QSettings("ZLG_Scope_Team", "ZLG_ZDS_Scope_UI")
        self.txt_dir = QLineEdit(self.path_settings.value("export/capture_directory", os.path.abspath("data/captures")))
        btn_browse = QPushButton("浏览...")
        btn_browse.clicked.connect(self._browse_dir)
        dir_row.addWidget(self.txt_dir)
        dir_row.addWidget(btn_browse)
        path_layout.addLayout(dir_row)

        layout.addWidget(path_group)

        # 2. 导出格式勾选（默认仅原始数据，杜绝重复浪费）
        fmt_group = QGroupBox("导出内容与格式选择")
        fmt_layout = QVBoxLayout(fmt_group)
        fmt_layout.setSpacing(6)

        self.cb_raw_wfm = QCheckBox("原生二进制 WFM (.wfm) — [推荐] 零损耗保存全部原始码，体积极小")
        self.cb_raw_wfm.setChecked(True)
        self.cb_raw_wfm.setEnabled(False)  # 默认必选
        fmt_layout.addWidget(self.cb_raw_wfm)

        self.cb_meta = QCheckBox("元数据、完整性核对与硬件参数报告 (.json/.md)")
        self.cb_meta.setChecked(True)
        self.cb_meta.setEnabled(False)  # 默认必选
        fmt_layout.addWidget(self.cb_meta)

        self.cb_screen = QCheckBox("示波器原生屏幕截图 (.png)")
        self.cb_screen.setChecked(True)
        fmt_layout.addWidget(self.cb_screen)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #374151;")
        fmt_layout.addWidget(sep)

        lbl_opt_tip = QLabel("以下格式按需生成（⚠️ 大存储深度如 256M/512M 时将产生数十 GB 文本）：")
        lbl_opt_tip.setStyleSheet("color: #f59e0b; font-size: 11px; font-weight: bold;")
        fmt_layout.addWidget(lbl_opt_tip)

        self.cb_full_csv = QCheckBox("导出全量时间与校准电压总表 (waveform.csv)")
        self.cb_full_csv.setChecked(False)
        self.cb_full_csv.toggled.connect(self._update_space_estimate)
        fmt_layout.addWidget(self.cb_full_csv)

        self.cb_ch_csv = QCheckBox("导出各通道独立数据表 (CH1_full.csv, CH2_full.csv...)")
        self.cb_ch_csv.setChecked(False)
        self.cb_ch_csv.toggled.connect(self._update_space_estimate)
        fmt_layout.addWidget(self.cb_ch_csv)

        self.cb_npz = QCheckBox("导出 NumPy 压缩数组归档 (waveform_full.npz)")
        self.cb_npz.setChecked(False)
        self.cb_npz.toggled.connect(self._update_space_estimate)
        fmt_layout.addWidget(self.cb_npz)

        self.cb_zip = QCheckBox("打包为完整 ZIP 压缩包 (.zip)")
        self.cb_zip.setChecked(False)
        self.cb_zip.toggled.connect(self._update_space_estimate)
        fmt_layout.addWidget(self.cb_zip)
        self.cb_analyze = QCheckBox("归档时逐块扫描全记录异常候选（额外耗时，可在离线工作台稍后执行）")
        fmt_layout.addWidget(self.cb_analyze)
        self.cb_wait_trigger = QCheckBox("先等待当前触发完成再下载（60秒超时；不重新发送 SINGLE）")
        fmt_layout.addWidget(self.cb_wait_trigger)

        layout.addWidget(fmt_group)

        # 3. 磁盘容量预估状态栏
        self.space_box = QGroupBox("磁盘空间预估 (Disk Space Estimation)")
        space_layout = QVBoxLayout(self.space_box)

        self.lbl_estimate = QLabel("正在计算容量预估...")
        self.lbl_estimate.setStyleSheet("font-family: Consolas; font-size: 11px;")
        space_layout.addWidget(self.lbl_estimate)

        self.lbl_space_status = QLabel("")
        self.lbl_space_status.setWordWrap(True)
        self.lbl_space_status.setStyleSheet("font-size: 11px; font-weight: bold;")
        space_layout.addWidget(self.lbl_space_status)

        layout.addWidget(self.space_box)

        # 4. 传输进度与速度面板 (进行中显示)
        self.progress_box = QGroupBox("以太网实时传输进度 (Live Streaming Progress)")
        self.progress_box.setVisible(False)
        prog_layout = QVBoxLayout(self.progress_box)

        self.lbl_stage = QLabel("准备启动传输...")
        self.lbl_stage.setStyleSheet("color: #38bdf8; font-weight: bold;")
        prog_layout.addWidget(self.lbl_stage)

        self.pbar = QProgressBar()
        self.pbar.setRange(0, 100)
        self.pbar.setValue(0)
        self.pbar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #3b82f6;
                border-radius: 4px;
                text-align: center;
                background-color: #1e1e2e;
                color: white;
                height: 20px;
            }
            QProgressBar::chunk {
                background-color: #2563eb;
            }
        """)
        prog_layout.addWidget(self.pbar)

        self.lbl_perf = QLabel("传输速度: 0.0 MB/s | 已用时间: 0.0s | 预计剩余: 0.0s")
        self.lbl_perf.setStyleSheet("font-family: Consolas; font-size: 11px; color: #94a3b8;")
        prog_layout.addWidget(self.lbl_perf)

        layout.addWidget(self.progress_box)

        # 5. 底部操作按钮
        btn_bar = QHBoxLayout()
        btn_bar.addStretch()

        self.btn_cancel = QPushButton("取消 (Cancel)")
        self.btn_cancel.setStyleSheet("background-color: #374151; color: white; padding: 6px 14px;")
        self.btn_cancel.clicked.connect(self._on_cancel_clicked)
        btn_bar.addWidget(self.btn_cancel)

        self.btn_start = QPushButton("🚀 开始下载归档 (Start Capture)")
        self.btn_start.setStyleSheet("""
            background-color: #059669;
            color: white;
            font-weight: bold;
            padding: 6px 20px;
            border-radius: 4px;
        """)
        self.btn_start.clicked.connect(self._start_capture)
        btn_bar.addWidget(self.btn_start)

        layout.addLayout(btn_bar)

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择归档存储路径", self.txt_dir.text())
        if d:
            self.txt_dir.setText(d)
            self.path_settings.setValue("export/capture_directory", d)
            self._update_space_estimate()

    def _get_current_options(self) -> dict:
        return {
            "export_full_csv": self.cb_full_csv.isChecked(),
            "export_channel_csv": self.cb_ch_csv.isChecked(),
            "export_npz": self.cb_npz.isChecked(),
            "create_zip": self.cb_zip.isChecked(),
            "capture_screenshot": self.cb_screen.isChecked(),
            "analyze_events": self.cb_analyze.isChecked()
            , "wait_for_trigger": self.cb_wait_trigger.isChecked()
        }

    def _update_space_estimate(self):
        opts = self._get_current_options()
        target_dir = self.txt_dir.text()
        n_ch = len(self.active_channels)
        pts = self.expected_points

        est = ScopeStorage.estimate_storage_requirements(n_ch, pts, opts, target_dir)

        details = " | ".join(f"{k}: {v}" for k, v in est["breakdown"].items())
        self.lbl_estimate.setText(
            f"目标通道: {n_ch} 个 | 预期每通道点数: {pts/1e6:.1f} Mpts\n"
            f"细项预估: {details}\n"
            f"预计总占用: {est['estimated_total_str']} | 磁盘可用: {est['free_str']}"
        )

        if est["has_sufficient_space"]:
            self.lbl_space_status.setText("✅ 目标磁盘空间充足，可以安全开始归档。")
            self.lbl_space_status.setStyleSheet("color: #10b981; font-weight: bold;")
            self.btn_start.setEnabled(True)
        else:
            self.lbl_space_status.setText(f"❌ 警告: {est['warning']}！请取消非必要的大体积 CSV 选项。")
            self.lbl_space_status.setStyleSheet("color: #ef4444; font-weight: bold;")
            self.btn_start.setEnabled(False)

    def _start_capture(self):
        self.btn_start.setEnabled(False)
        self.cb_full_csv.setEnabled(False)
        self.cb_wait_trigger.setEnabled(False)
        self.cb_analyze.setEnabled(False)
        self.cb_screen.setEnabled(False)
        self.txt_session.setEnabled(False)
        self.path_settings.setValue("export/capture_directory", self.txt_dir.text().strip())
        self.txt_dir.setEnabled(False)
        self.cb_ch_csv.setEnabled(False)
        self.cb_npz.setEnabled(False)
        self.cb_zip.setEnabled(False)
        self.progress_box.setVisible(True)
        self.btn_cancel.setText("⏹ 取消下载 (Cancel)")
        self.btn_cancel.setStyleSheet("background-color: #dc2626; color: white; font-weight: bold; padding: 6px 14px;")

        self.worker = CaptureWorker(
            driver=self.driver,
            channels=self.active_channels,
            session_name=self.txt_session.text().strip() or "capture",
            base_dir=self.txt_dir.text().strip() or "data/captures",
            export_options=self._get_current_options(),
            parent=self
            , test_context=self.test_context
        )
        self.worker.progress_signal.connect(self._on_worker_progress)
        self.worker.finished_signal.connect(self._on_worker_finished)
        self.worker.failed_signal.connect(self._on_worker_failed)
        self.worker.cancelled_signal.connect(self._on_worker_cancelled)
        self.worker.start()

    def _on_worker_progress(self, recvd: int, total: int, speed: float, elapsed: float, eta: float, stage: str):
        pct = int(min(100, (recvd / total * 100))) if total > 0 else 0
        self.pbar.setValue(pct)
        recvd_mb = recvd / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        self.lbl_stage.setText(f"{stage} {recvd_mb:.1f} MB / {total_mb:.1f} MB ({pct}%)")
        self.lbl_perf.setText(
            f"传输速度: {speed:.1f} MB/s | 已用时间: {elapsed:.1f}s | 预计剩余: {eta:.1f}s"
        )

    def _on_worker_finished(self, result: dict):
        self.capture_result = result
        wfm = result["wfm"]
        saved_dir = result["saved_dir"]
        dur = result.get("duration_s", 0.0)

        meta = wfm.capture_metadata
        integrity = meta.get("integrity_verification", {})
        status = integrity.get("integrity_status", "PASS")
        desc = integrity.get("description", "完整性核对通过")

        QMessageBox.information(
            self,
            "归档成功",
            f"示波器返回的原始记录已归档。\n\n"
            f"- 存储路径: {saved_dir}\n"
            f"- 耗时: {dur:.1f} 秒\n"
            f"- 完整性核对: [{status}] {desc}\n\n"
            f"数据已按所选规范安全落盘，并生成了数据校验与硬件对比报告。"
        )
        self.accept()

    def _on_worker_failed(self, error_msg: str, partial_dir: str):
        QMessageBox.critical(
            self,
            "传输异常中断",
            f"以太网波形读取或保存过程中发生异常：\n{error_msg}\n\n"
            f"如已开始写入原始数据，可根据错误路径查找 .incomplete 文件。\n"
            f"配置查询/等待触发阶段失败时，可能尚未生成数据文件。\n"
            f"请检查仪器状态和网络连接；断线后需重新连接。"
        )
        self.reject()

    def _on_worker_cancelled(self, partial_dir: str):
        QMessageBox.warning(
            self,
            "用户已取消",
            f"波形传输已由用户主动取消。\n\n"
            f"详情：{partial_dir}\n"
            f"若已开始接收，可按错误路径查找 .cancelled 或已保存的原始记录。"
        )
        self.reject()

    def _on_cancel_clicked(self):
        if self.worker and self.worker.isRunning():
            self.lbl_stage.setText("正在取消传输并复位底层连接...")
            self.worker.cancel()
        else:
            self.reject()
