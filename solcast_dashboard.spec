# Build with: python -m PyInstaller --clean solcast_dashboard.spec
from PyInstaller.utils.hooks import collect_all
from pathlib import Path

root = Path(SPECPATH)
datas = [
    (str(root / "app.py"), "."),
    (str(root / "openmeteo.py"), "."),
]
binaries = []
hiddenimports = [
    "streamlit", "streamlit.web.cli", "openmeteo_requests", "requests_cache", "retry_requests",
    "solcast_data", "amber_data", "sigen_data",
]
for package in ("streamlit", "openmeteo_requests", "requests_cache", "retry_requests"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

a = Analysis(
    [str(root / "launcher.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SolcastDashboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
