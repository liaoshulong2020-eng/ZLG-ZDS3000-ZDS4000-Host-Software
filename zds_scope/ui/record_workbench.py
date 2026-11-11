import time
from pathlib import Path

from PySide6.QtCore import Signal, QThread, Qt, Slot, QTimer, QSettings
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QLabel,
    QTableWidget, QTableWidgetItem, QPushButton, QFileDialog, QMessageBox, QDoubleSpinBox,
    QCheckBox, QHeaderView, QProgressBar)
from ..records import bounds, export_region, scan_record, save_project, load_project, save_json


class RecordTask(QThread):
    progress = Signal(float)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function
        self.result = None
        self.error = None
        self.is_cancelled = False

    def run(self):
        try:
            self.result = self.function(self)
        except Exception as error:
            self.error = str(error)


class RecordWorkbench(QWidget):
    record_loaded = Signal(object, dict)
    interval_selected = Signal(float, float)
    busy_changed = Signal(bool)
    work_finished = Signal()
    progress_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.wfm = None
        self.worker = None
        self.report = None
        self.state_provider = lambda: {}
        self.can_start = lambda: True
        self.task_watchdog = QTimer(self)
        self.task_watchdog.setInterval(500)
        self.task_watchdog.timeout.connect(self._recover_finished_task)
        layout = QVBoxLayout(self)
        self.summary = QLabel("暂无记录；可读取仪器或打开离线工程。")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        form = QFormLayout()
        self.context_fields = {}
        for name, label in (("topology", "电路拓扑/型号"), ("input", "输入工况"), ("output", "输出工况"),
                            ("load", "负载/运行状态"), ("notes", "探头接地、异常现象与测试备注")):
            field = QLineEdit()
            self.context_fields[name] = field
            form.addRow(label, field)
        layout.addLayout(form)
        self.channels = QTableWidget(4, 7)
        self.channels.setHorizontalHeaderLabels(["通道/单位", "信号含义", "测量位置/探头", "期望下限", "期望上限", "预期频率Hz", "备注"])
        self.channels.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.channels.setMinimumHeight(160)
        self.channels.setMaximumHeight(180)
        for row in range(4):
            for col in range(7):
                self.channels.setItem(row, col, QTableWidgetItem(f"CH{row+1}" if col == 0 else ""))
                if col == 0:
                    self.channels.item(row, col).setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        layout.addWidget(self.channels)
        row = QHBoxLayout()
        self.start, self.end = QDoubleSpinBox(), QDoubleSpinBox()
        for widget in (self.start, self.end):
            widget.setDecimals(12)
            widget.setRange(-1e12, 1e12)
            widget.setSuffix(" s")
        row.addWidget(QLabel("区间起止："))
        row.addWidget(self.start)
        row.addWidget(self.end)
        self.selected_channels = {}
        for ch in range(1, 5):
            check = QCheckBox(f"CH{ch}")
            check.setChecked(True)
            self.selected_channels[ch] = check
            row.addWidget(check)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.actions = []
        for text, callback in (("导出所选区间 CSV", self._export), ("全记录异常定位", self._analyze),
                               ("保存分析报告", self._save_report), ("保存完整工程", self._save_project),
                               ("打开离线工程", self._load_project)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            self.actions.append(button)
            row.addWidget(button)
        layout.addLayout(row)
        self.events = QTableWidget(0, 4)
        self.events.setHorizontalHeaderLabels(["通道", "候选事件", "时间(s)", "原始采样索引"])
        self.events.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.events.cellClicked.connect(self._select_event)
        self.events.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.events)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.cancel_analysis = QPushButton("取消当前分析")
        self.cancel_analysis.setEnabled(False)
        self.cancel_analysis.clicked.connect(self._cancel_analysis)
        layout.addWidget(self.cancel_analysis)
        self.message = QLabel("候选事件仅供复核；峰值等模式的解码和物理量需标定。FFT 摘要不代表全记录分析。")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

    def context(self):
        result = {key: field.text() for key, field in self.context_fields.items()}
        result["channels"] = {}
        for row in range(4):
            values = [self.channels.item(row, col).text().strip() for col in range(1, 7)]
            entry = {"signal": values[0], "location_probe": values[1], "notes": values[5]}
            for key, value in zip(("minimum", "maximum", "frequency_hz"), values[2:5]):
                if value:
                    import math
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError("阈值/频率必须为有限数值")
                    entry[key] = number
            if "minimum" in entry and "maximum" in entry and entry["minimum"] > entry["maximum"]:
                raise ValueError(f"CH{row+1} 期望下限不能大于上限")
            result["channels"][str(row + 1)] = entry
        return result

    def set_context(self, context):
        for key, field in self.context_fields.items():
            field.setText(str(context.get(key, "")))
        for row in range(4):
            entry = context.get("channels", {}).get(str(row + 1), {})
            for col, key in enumerate(("signal", "location_probe", "minimum", "maximum", "frequency_hz", "notes"), 1):
                self.channels.item(row, col).setText(str(entry.get(key, "")))

    def set_record(self, wfm):
        self.wfm = wfm
        self.report = None
        self.events.setRowCount(0)
        limits = [bounds(data) for data in wfm.channels.values()]
        if limits:
            self.start.setValue(min(value[0] for value in limits))
            self.end.setValue(max(value[1] for value in limits))
        units = []
        for ch, checkbox in self.selected_channels.items():
            checkbox.setEnabled(ch in wfm.channels)
            checkbox.setChecked(ch in wfm.channels)
            unit = "A" if ch in wfm.channels and wfm.channels[ch].info.probe_type == 1 else "V"
            self.channels.item(ch-1, 0).setText(f"CH{ch} ({unit})")
            if ch in wfm.channels:
                units.append(f"CH{ch}: {len(wfm.channels[ch].raw_data):,} 原始码/{unit}")
        self.summary.setText("；".join(units) + f"；采样率 {wfm.head.sample_rate:g} Hz；采集模式代码 {wfm.head.acq_mode}")

    def _task(self, function, complete, cancellable=False, label="后台任务"):
        if self.worker is not None or not self.can_start():
            return
        for action in self.actions:
            action.setEnabled(False)
        self.progress.setRange(0, 0)
        self.message.setText(label + "处理中，界面保持响应；可继续操作仪器。")
        self.cancel_analysis.setEnabled(cancellable)
        self.worker = RecordTask(function, self)
        self.worker.complete = complete
        self.worker.label = label
        self.worker.cancellable = cancellable
        self.worker.progress.connect(self._progress)
        # A QObject slot gives Qt an explicit GUI-thread receiver context.
        self.worker.finished.connect(self._on_task_finished, Qt.QueuedConnection)
        self.busy_changed.emit(True)
        self.worker.start()
        self.task_watchdog.start()

    @Slot()
    def _on_task_finished(self):
        if self.worker is not None and not self.worker.isRunning():
            self._finished(self.worker.complete)

    def _recover_finished_task(self):
        if self.worker is not None and self.worker.isFinished() and not self.worker.isRunning():
            self._on_task_finished()

    def _cancel_analysis(self):
        if self.worker:
            self.worker.is_cancelled = True
            self.cancel_analysis.setEnabled(False)
            self.message.setText("正在结束当前任务的数据块…")

    def _progress(self, value):
        self.progress.setRange(0, 100)
        self.progress.setValue(int(value))
        self.progress_changed.emit(int(value))

    def _finished(self, complete):
        worker = self.worker
        if worker is None:
            return
        self.worker = None
        self.task_watchdog.stop()
        self.cancel_analysis.setEnabled(False)
        for action in self.actions:
            action.setEnabled(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if not worker.error else 0)
        self.busy_changed.emit(False)
        if worker.error:
            self.message.setText("操作失败: " + worker.error)
        else:
            try:
                complete(worker.result)
            except Exception as error:
                self.message.setText("结果处理失败: " + str(error))
        worker.deleteLater()
        self.work_finished.emit()

    def _require_record(self):
        if self.wfm is None:
            QMessageBox.information(self, "暂无数据", "请先读取波形或打开离线工程。")
            return False
        return True

    def _export(self):
        if not self._require_record():
            return
        try:
            context = self.context()
        except ValueError as error:
            self.message.setText(str(error))
            return
        directory = QFileDialog.getExistingDirectory(self, "选择区间导出的上级目录")
        if not directory:
            return
        target = str(Path(directory) / ("region_" + str(time.time_ns())))
        channels = [ch for ch, check in self.selected_channels.items() if check.isChecked() and check.isEnabled()]
        wfm, start, end = self.wfm, self.start.value(), self.end.value()
        self._task(lambda worker: export_region(wfm, target, channels, start, end, context),
                   lambda path: self.message.setText("区间数据已导出：" + path))

    def _analyze(self):
        if not self._require_record():
            return
        try:
            context = self.context()
        except ValueError as error:
            self.message.setText(str(error))
            return
        wfm = self.wfm
        self._task(lambda worker: scan_record(wfm, context,
                       progress=lambda done, total: worker.progress.emit(done / max(1, total) * 100),
                       cancelled=lambda: worker.is_cancelled), self._show_analysis, cancellable=True)

    def _show_analysis(self, report):
        self.report = report
        self.events.setRowCount(len(report["events"]))
        for row, event in enumerate(report["events"]):
            for col, value in enumerate((event["channel"], event["kind"], f"{event['time_s']:.12g}", event["sample_index"])):
                self.events.setItem(row, col, QTableWidgetItem(str(value)))
        count = sum(ch["scanned_points"] for ch in report["channels"].values())
        self.message.setText(f"已逐块扫描全部 {count:,} 个原始码，列出 {len(report['events'])} 个候选事件。点击事件可定位波形；候选不等于故障。")

    def _select_event(self, row, col):
        if self.report:
            event = self.report["events"][row]
            data = self.wfm.channels[event["channel"]]
            width = max(100 * data.dt, 1e-9)
            self.start.setValue(event["time_s"] - width)
            self.end.setValue(event["time_s"] + width)
            self.interval_selected.emit(self.start.value(), self.end.value())

    def _save_report(self):
        if self.report is None:
            self.message.setText("请先执行全记录异常定位。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存完整分析报告", "analysis_events.json", "JSON (*.json)")
        if path:
            save_json(path, self.report)
            self.message.setText("报告已保存：" + path)

    def _save_project(self):
        if not self._require_record():
            return
        try:
            state = self.state_provider()
            state["test_context"] = self.context()
            state["interval"] = [self.start.value(), self.end.value()]
            state["analysis_report"] = self.report
        except ValueError as error:
            self.message.setText(str(error))
            return
        settings = QSettings("ZLG_Scope_Team", "ZLG_ZDS_Scope_UI")
        directory = QFileDialog.getExistingDirectory(self, "选择工程保存的上级目录", settings.value("export/project_directory", str(Path("data").resolve())))
        if not directory:
            return
        settings.setValue("export/project_directory", directory)
        path = str(Path(directory) / ("scope_project_" + str(time.time_ns())))
        wfm = self.wfm
        self._task(lambda worker: save_project(wfm, path, state), lambda path: self.message.setText("完整工程已保存：" + path))

    def _load_project(self):
        directory = QFileDialog.getExistingDirectory(self, "选择包含 project.json 的工程目录")
        if directory:
            self._task(lambda worker: load_project(directory), lambda result: self.record_loaded.emit(*result))
