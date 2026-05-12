# PyInstaller spec for SwimTrack Manager.
# Build on each target platform separately:
#   pyinstaller SwimTrackManager.spec

import os

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("pyttsx3")


def _collect_bundle_binaries():
    bundle_dir = os.environ.get("FFMPEG_BUNDLE_DIR")
    if not bundle_dir or not os.path.isdir(bundle_dir):
        return []
    found = []
    for entry in os.listdir(bundle_dir):
        full = os.path.join(bundle_dir, entry)
        if os.path.isfile(full):
            found.append((full, "."))
    return found


extra_binaries = _collect_bundle_binaries()

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=extra_binaries,
    datas=[],
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
    [],
    exclude_binaries=True,
    name="SwimTrackManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SwimTrackManager",
)
