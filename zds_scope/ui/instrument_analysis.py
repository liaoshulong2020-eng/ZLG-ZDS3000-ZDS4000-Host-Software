"""Documented instrument Math controls and lossless Decode event retrieval."""
import csv
from pathlib import Path
from PySide6.QtCore import QThread, Signal, QSettings
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QCheckBox, QPushButton, QTableWidget, QTableWidgetItem, QFileDialog, QApplication)

OPERATORS = [('加法 A+B', 'ADD'), ('减法 A−B', 'SUBTract'), ('乘法 A×B', 'MULTiply'),
             ('除法 A/B', 'DIVIsion'), ('微分 A', 'DIFFerential'), ('积分 A', 'INTEgral')]


def parse_events(raw):
    # The manual defines semicolon blocks and comma fields, but no fixed schema.
    return [next(csv.reader([block.replace('，', ',')])) for block in raw.strip().split(';') if block.strip()]


class InstrumentAnalysisThread(QThread):
    def __init__(self, driver, action, options=None, parent=None):
        super().__init__(parent)
        self.driver, self.action, self.options = driver, action, options or {}
        self.result = None
        self.error = None

    def run(self):
        try:
            with self.driver.io_lock:
                if self.action == 'decode':
                    self.result = self.driver.query(':DECOde:PLUG:EVENt?')
                elif self.action == 'math_read':
                    self.result = self.driver.query(':MATH:MODE?')
                elif self.action == 'math_off':
                    self.driver.send(':MATH:MODE OFF')
                    self.result = self.driver.query(':MATH:MODE?')
                elif self.action == 'math_apply':
                    op = self.options['operator']
                    a, b = self.options['a'], self.options['b']
                    if op not in dict(OPERATORS).values() or a not in range(1,5) or b not in range(1,5):
                        raise ValueError('无效运算设置')
                    self.driver.send(f':MATH:BASIc:SA CHANnel{a}')
                    if op not in ('DIFFerential', 'INTEgral'):
                        self.driver.send(f':MATH:BASIc:SB CHANnel{b}')
                    self.driver.send(f':MATH:BASIc:OPERator {op}')
                    self.driver.send(':MATH:BASIc:INVErt ' + ('ON' if self.options['invert'] else 'OFF'))
                    self.driver.send(':MATH:MODE BASIc')
                    self.result = self.driver.query(':MATH:MODE?')
                else:
                    raise ValueError('未知操作')
        except Exception as error:
            self.error = str(error)


class MathPanel(QWidget):
    requested = Signal(str, dict)
    screen_requested = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.operator = QComboBox()
        for title, value in OPERATORS:
            self.operator.addItem(title, value)
        self.a, self.b = QComboBox(), QComboBox()
        for channel in range(1,5):
            self.a.addItem(f'CH{channel}', channel)
            self.b.addItem(f'CH{channel}', channel)
        self.b.setCurrentIndex(1)
        self.invert = QCheckBox('结果反相')
        for label, widget in [('运算', self.operator), ('信源 A', self.a), ('信源 B', self.b)]:
            row.addWidget(QLabel(label)); row.addWidget(widget)
        row.addWidget(self.invert)
        layout.addLayout(row)
        buttons = QHBoxLayout()
        self.actions = []
        for title, action in [('应用到示波器', 'math_apply'), ('读取当前模式', 'math_read'), ('关闭仪器 Math', 'math_off')]:
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, action=action: self.requested.emit(action, self.options()))
            buttons.addWidget(button); self.actions.append(button)
        screen = QPushButton('查看仪器画面')
        screen.clicked.connect(self.screen_requested.emit)
        buttons.addWidget(screen); self.actions.append(screen)
        layout.addLayout(buttons)
        self.message = QLabel('由示波器执行计算。点击“应用”才写入设置；运算结果通过仪器画面查看，可预览、复制、保存。')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        layout.addStretch()
        self.operator.currentIndexChanged.connect(self._update_sources)

    def _update_sources(self):
        self.b.setEnabled(self.operator.currentData() not in ('DIFFerential', 'INTEgral'))

    def options(self):
        return dict(operator=self.operator.currentData(), a=self.a.currentData(), b=self.b.currentData(), invert=self.invert.isChecked())

    def set_busy(self, busy):
        for widget in self.actions + [self.operator, self.a, self.b, self.invert]:
            widget.setEnabled(not busy)
        if not busy:
            self._update_sources()


class DecodePanel(QWidget):
    requested = Signal(str, dict)

    def __init__(self):
        super().__init__()
        self.raw = ''
        self.settings = QSettings('ZLG_Scope_Team', 'ZLG_ZDS_Decode')
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.read = QPushButton('读取仪器解码事件')
        self.copy = QPushButton('复制事件表')
        self.save = QPushButton('导出事件 CSV')
        self.read.clicked.connect(lambda: self.requested.emit('decode', {}))
        self.copy.clicked.connect(lambda: QApplication.clipboard().setText(self.raw))
        self.save.clicked.connect(self.export)
        for button in (self.read, self.copy, self.save):
            row.addWidget(button)
        layout.addLayout(row)
        self.message = QLabel('请先在示波器上设置解码协议、信源及阈值。此处只读取当前事件表，不更改解码配置；字段按仪器原始顺序显示。')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table, 1)
        self.copy.setEnabled(False); self.save.setEnabled(False)

    def show_events(self, raw):
        self.raw = raw
        rows = parse_events(raw)
        cols = max((len(row) for row in rows), default=0)
        self.table.clear()
        self.table.setRowCount(min(len(rows), 2000)); self.table.setColumnCount(cols)
        self.table.setHorizontalHeaderLabels([f'字段 {i+1}' for i in range(cols)])
        for i, row in enumerate(rows[:2000]):
            for j, value in enumerate(row):
                self.table.setItem(i,j,QTableWidgetItem(value))
        self.message.setText(f'已读取 {len(rows)} 个内容块；表格最多预览 2000 块，复制及导出包含全部返回内容。' if rows else '仪器返回空事件表。请检查仪器解码是否开启、是否捕获到有效数据。')
        self.copy.setEnabled(bool(rows)); self.save.setEnabled(bool(rows))

    def set_busy(self, busy):
        self.read.setEnabled(not busy)

    def export(self):
        directory = self.settings.value('directory', str(Path('data').resolve()))
        path, _ = QFileDialog.getSaveFileName(self, '导出解码事件', str(Path(directory)/'decode_events.csv'), 'CSV (*.csv)')
        if path:
            try:
                with open(path, 'w', encoding='utf-8-sig', newline='') as handle:
                    csv.writer(handle).writerows(parse_events(self.raw))
                self.settings.setValue('directory', str(Path(path).parent))
                self.message.setText('事件表已保存：'+path)
            except OSError as error:
                self.message.setText('保存失败：'+str(error))
