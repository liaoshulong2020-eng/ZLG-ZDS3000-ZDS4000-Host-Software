"""
致远 ZDS 示波器专业上位机启动入口
"""

import sys
import os
from pathlib import Path


def main():
    # A double-click or shortcut can supply an unrelated working directory.
    # Keep exported files alongside the executable, outside its temporary bundle.
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)
        if sys.stdout is None or sys.stderr is None:
            logs = Path("data/logs")
            logs.mkdir(parents=True, exist_ok=True)
            stream = (logs / "application.log").open("a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stdout or stream
            sys.stderr = sys.stderr or stream
    if "--self-test-report" in sys.argv:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtWidgets import QApplication
    from zds_scope.ui.main_window import MainWindow
    app = QApplication(sys.argv)
    from PySide6.QtGui import QIcon
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "zds_scope/ui/assets/scope.ico")))
    app.setApplicationName("ZLG_ZDS_Scope_Upper")
    app.setOrganizationName("ZLG_Scope_Team")

    window = MainWindow()
    if "--self-test-report" in sys.argv:
        import json
        from zds_scope.instrument_settings import TRIGGER_QUERIES
        from PySide6.QtCore import qVersion
        report = Path(sys.argv[sys.argv.index("--self-test-report") + 1])
        window.show()
        app.processEvents()
        result = {"success": True, "frozen": bool(getattr(sys, "frozen", False)),
                  "qt_version": qVersion(), "window_title": window.windowTitle(),
                  "icon_loaded": not window.windowIcon().isNull(),
                  "tabs": [window.bottom_tabs.tabText(i) for i in range(window.bottom_tabs.count())],
                  "trigger_modes": sorted(TRIGGER_QUERIES), "working_directory": os.getcwd()}
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        window.close()
        return
    if "--screenshots" in sys.argv:
        window._open_tool(window.tab_screenshot, "示波器截图")
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    main()
