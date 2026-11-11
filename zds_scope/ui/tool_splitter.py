"""A persistent, draggable divider between the waveform and optional tools."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import QSplitter, QSplitterHandle


class ToolDivider(QSplitterHandle):
    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self.setCursor(Qt.SplitVCursor)
        self.setToolTip("上下拖动调整波形与工具区域；双击收起 / 展开")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#181c28"))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#7182a4"))
        painter.drawRoundedRect(self.width() // 2 - 24, self.height() // 2 - 2, 48, 4, 2, 2)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.splitter().toggle_requested.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class ToolSplitter(QSplitter):
    toggle_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(Qt.Vertical, parent)
        self.setHandleWidth(10)
        self.setChildrenCollapsible(True)
        self.setOpaqueResize(True)

    def createHandle(self):
        return ToolDivider(self.orientation(), self)
