# PyInstaller spec: build with  .venv\Scripts\pyinstaller PandaLoader.spec
# Produces a single dist\PandaLoader.exe (windowed, no console). The exe unpacks
# its runtime to a temp folder at launch, so one stable path for the firewall rule.
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    datas=[("pandaloader/web/static", "pandaloader/web/static")],
    hiddenimports=collect_submodules("pystray") + ["PIL.Image", "PIL.ImageDraw", "tkinter", "tkinter.simpledialog"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "py_spy", "fitz", "pymupdf"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="PandaLoader",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon="pandaloader/web/static/favicon.ico",
)
