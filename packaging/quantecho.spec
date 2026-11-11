# PyInstaller spec for the Windows standalone build (onedir; wrapped into MSIX by build_msix.ps1).
#   .venv\Scripts\pyinstaller packaging\quantecho.spec --noconfirm
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "packaging" / "quantecho_app.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "quantecho" / "assets"), "quantecho/assets"), *collect_data_files("tzdata")],
    hiddenimports=[*collect_submodules("quantecho.providers"), "keyring.backends.Windows", "websocket"],
    # schwab-py is optional (Schwab users run from source); keep the bundle lean
    excludes=["schwab", "numpy", "pandas", "matplotlib", "PIL", "pytest", "IPython",
              "quantecho.trading"],  # options trading is source-only, never in the Store build
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="QuantEcho",
    icon=str(ROOT / "packaging" / "assets" / "quantecho.ico"),
    console=False,          # windowed app
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="QuantEcho")
