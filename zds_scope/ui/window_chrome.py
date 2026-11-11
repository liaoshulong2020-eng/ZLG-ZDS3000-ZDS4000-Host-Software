"""Borderless main-window controls and dismissible settings popups."""
from PySide6.QtCore import Qt, QObject, QEvent, QPointF, QRectF
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import QApplication, QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QAbstractButton


class CaptionButton(QAbstractButton):
    """Small vector window controls, independent of glyph/font rendering."""
    def __init__(self, role, window, tooltip):
        super().__init__()
        self.role, self.target_window = role, window
        self.setFixedSize(40, 28)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setFocusPolicy(Qt.TabFocus)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor('#0f1016'))
        if self.underMouse() or self.isDown() or self.hasFocus():
            background = '#c53843' if self.role == 'close' else '#292e39'
            if self.isDown():
                background = '#a52b35' if self.role == 'close' else '#363e4d'
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(background))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 4, 4)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor('#d5dae3'), 1.2))
        x, y = self.width()/2, self.height()/2
        if self.role == 'minimize':
            painter.drawLine(QPointF(x-5, y+3), QPointF(x+5, y+3))
        elif self.role == 'maximize':
            if self.target_window.isMaximized():
                painter.drawPolyline([QPointF(x-2,y-5), QPointF(x+5,y-5), QPointF(x+5,y+2)])
                painter.drawRect(QRectF(x-5,y-2,7,7))
            else:
                painter.drawRect(QRectF(x-4.5,y-4.5,9,9))
        else:
            painter.drawLine(QPointF(x-4,y-4), QPointF(x+4,y+4))
            painter.drawLine(QPointF(x+4,y-4), QPointF(x-4,y+4))


class SettingsPopup(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName('settingsPopup')
        self.setStyleSheet('QDialog#settingsPopup { border: 1px solid #607089; border-radius: 8px; }')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        header = QHBoxLayout()
        self.heading = QLabel()
        self.heading.setStyleSheet('font-weight: bold; color: #e2e8f0;')
        self.windowTitleChanged.connect(self.heading.setText)
        header.addWidget(self.heading, 1)
        close = QPushButton('×'); close.setFixedSize(28, 26)
        close.setToolTip('关闭设置（也可点击外部或按 Esc）')
        close.clicked.connect(self.hide)
        header.addWidget(close)
        layout.addLayout(header)


class WindowChrome(QObject):
    def __init__(self, window, header):
        super().__init__(window)
        self.window, self.header = window, header
        self.controls = QWidget()
        self.controls.setFixedWidth(228)
        layout = QHBoxLayout(self.controls)
        layout.setContentsMargins(0,0,0,0); layout.setSpacing(0); layout.addStretch()
        for role, tip, action in [('minimize','最小化',window.showMinimized),('maximize','最大化 / 还原',self.toggle_maximized),('close','关闭',window.close)]:
            button = CaptionButton(role, window, tip)
            button.clicked.connect(action);layout.addWidget(button)
        header.setToolTip('拖动空白处移动窗口；双击最大化 / 还原')
        QApplication.instance().installEventFilter(self)
        window.setMouseTracking(True);window.centralWidget().setMouseTracking(True)

    def toggle_maximized(self):
        self.window.showNormal() if self.window.isMaximized() else self.window.showMaximized()
        self.controls.update()

    def eventFilter(self, watched, event):
        # QApplication delivers every paint/layout event here. Only mouse
        # presses need the widget/window lookup used for native dragging.
        kind=event.type()
        if kind not in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            return False
        if not isinstance(watched,QWidget) or watched.window() is not self.window:
            return False
        if kind==QEvent.MouseButtonDblClick and watched is self.header and event.button()==Qt.LeftButton:
            self.toggle_maximized();return True
        if kind==QEvent.MouseButtonPress and event.button()==Qt.LeftButton:
            point=self.window.mapFromGlobal(event.globalPosition().toPoint())
            edges=Qt.Edges()
            if not self.window.isMaximized():
                if point.x()<6:edges|=Qt.LeftEdge
                elif point.x()>=self.window.width()-6:edges|=Qt.RightEdge
                if point.y()<6:edges|=Qt.TopEdge
                elif point.y()>=self.window.height()-6:edges|=Qt.BottomEdge
            handle=self.window.windowHandle()
            if handle and edges and handle.startSystemResize(edges):return True
            if handle and watched is self.header and handle.startSystemMove():return True
        return False
