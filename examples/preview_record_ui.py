"""Render the real saved record in an isolated offscreen Qt process."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase, QFont
from zds_scope.ui.main_window import MainWindow
from zds_scope.protocol import parse_wfm_from_file

folder = Path(sys.argv[1])
app = QApplication([])
QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
app.setFont(QFont("Microsoft YaHei", 9))
window = MainWindow()
wfm = parse_wfm_from_file(str(folder / "waveform.wfm"), lazy=True)
configuration = json.loads((folder / "configuration_before.json").read_text(encoding="utf-8"))
window._on_project_loaded(wfm, {"configuration": configuration})
window.resize(1500, 1000)
window.show()
for tab, filename in ((window.advanced_settings, "settings_ui.png"), (window.record_tab, "record_ui.png")):
    window.bottom_tabs.setCurrentWidget(tab)
    app.processEvents()
    window.grab().save(str(folder / filename))
assert all(data._time_array is None and data._voltage_array is None for data in wfm.channels.values())
window.close()
wfm.close()
print("UI rendering passed; full 256M floating-point arrays were not created.")
