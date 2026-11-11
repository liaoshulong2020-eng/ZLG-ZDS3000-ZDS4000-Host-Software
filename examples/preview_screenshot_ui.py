"""Offscreen visual check using a real screenshot already saved on disk."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication
from zds_scope.ui.main_window import MainWindow

source, output = map(Path, sys.argv[1:3])
output.parent.mkdir(parents=True, exist_ok=True)
app = QApplication([])
QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
app.setFont(QFont("Microsoft YaHei", 9))
window = MainWindow()
window.tab_screenshot.settings = QSettings(str(output.parent / "preview.ini"), QSettings.IniFormat)
window.tab_screenshot.set_image(source.read_bytes(), str(source.resolve()), auto_copy=False)
window.tab_screenshot.invert.setChecked(True)
window.bottom_tabs.setCurrentWidget(window.tab_screenshot)
window.resize(1360, 900)
window.show()
app.processEvents()
window.grab().save(str(output))
window.close()
print(output)
