"""Load a real Windows UI font, including when Qt uses offscreen rendering."""
import os
from pathlib import Path
from PySide6.QtGui import QFont, QFontDatabase


def configure_ui_font(app):
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename in ("msyh.ttc", "msyhbd.ttc"):
        path = fonts / filename
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    available = set(QFontDatabase.families())
    for family in ("Microsoft YaHei", "Microsoft YaHei UI", "SimSun", "Noto Sans CJK SC"):
        if family in available:
            app.setFont(QFont(family, 9))
            return
