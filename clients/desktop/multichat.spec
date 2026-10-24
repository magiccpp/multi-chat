# PyInstaller spec for MultiChat.exe (build on Windows: see build_exe.ps1).
# One-folder build: faster start-up than one-file and friendlier to antivirus scanners.
import os

from PyInstaller.utils.hooks import collect_submodules

ICON = "installer/multichat.ico" if os.path.exists("installer/multichat.ico") else None  # made by installer/make_icon.py

hidden = collect_submodules("multichat_client") + collect_submodules("multichat_desktop") + [
    "keyring.backends.Windows",
    "pygments.lexers",
    "pygments.formatters.html",
    "pygments.styles.monokai",
    "pygments.styles.default",
]

a = Analysis(
    ["multichat_desktop/__main__.py"],
    pathex=[],
    hiddenimports=hidden,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtQuick3D"],
    noarchive=False,
)
# Small console exe for the Claude Code approval bridge (stdio JSON-RPC needs a console subsystem binary).
b = Analysis(["multichat_desktop/bridge_main.py"], hiddenimports=collect_submodules("multichat_client"), excludes=["PySide6", "tkinter"])
pyz = PYZ(a.pure)
bridge_pyz = PYZ(b.pure)
bridge = EXE(bridge_pyz, b.scripts, [], exclude_binaries=True, name="multichat-approval-bridge", console=True, icon=ICON)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MultiChat",
    console=False,
    icon=ICON,
)
coll = COLLECT(exe, bridge, a.binaries, a.datas, b.binaries, b.datas, strip=False, upx=False, name="MultiChat")
