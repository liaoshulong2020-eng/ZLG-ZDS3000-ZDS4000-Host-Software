# Rebuild and publish to 2.app/<project>: powershell -File build_exe.ps1
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

project = Path(SPECPATH)
analysis = Analysis(
    [str(project / "run_app.py")],
    pathex=[str(project)],
    binaries=[],
    datas=[(str(project / "zds_scope" / "trigger_queries.json"), "zds_scope"),
           (str(project / "zds_scope" / "ui" / "assets" / "check.svg"), "zds_scope/ui/assets"),
           (str(project / "zds_scope" / "ui" / "assets" / "scope.ico"), "zds_scope/ui/assets")],
    hiddenimports=collect_submodules("uvicorn"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
# Qt uses the unsuffixed Windows ICU API. PATH may contain Poppler/Conda's
# incompatible ICU (version-suffixed exports), which bindepend can miscollect.
# Leave the Windows system DLL to the OS loader, as in the unfrozen program.
analysis.binaries = [item for item in analysis.binaries
                     if Path(item[0]).name.lower() not in {"icuuc.dll", "icudt78.dll"}]
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, analysis.binaries, analysis.datas,
    [], name="ZLG示波器上位机", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, runtime_tmpdir=None, console=False,
    disable_windowed_traceback=False,
    icon=str(project / "zds_scope" / "ui" / "assets" / "scope.ico"),
)
