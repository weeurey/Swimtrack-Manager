import os
import sys
from pathlib import Path

from .app import run_app


def _bootstrap_bundled_binaries() -> None:
    # When frozen by PyInstaller, prepend the bundle and executable directories
    # to PATH so bundled ffmpeg/ffprobe are discoverable via shutil.which.
    if not getattr(sys, "frozen", False):
        return
    candidates: list[str] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(meipass)
    exe_dir = str(Path(sys.executable).resolve().parent)
    candidates.append(exe_dir)
    candidates.append(str(Path(exe_dir) / "_internal"))
    existing = os.environ.get("PATH", "")
    parts = [c for c in candidates if c and os.path.isdir(c)]
    if parts:
        os.environ["PATH"] = os.pathsep.join(parts + ([existing] if existing else []))


def main():
    _bootstrap_bundled_binaries()
    run_app()


if __name__ == "__main__":
    main()
