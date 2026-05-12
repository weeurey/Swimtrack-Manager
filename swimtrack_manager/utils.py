from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

INVALID_FILENAME_CHARS = r'<>:"/\\|?*'
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, fallback: str = "Untitled") -> str:
    """Make a filename safe for Windows/Linux and simple MP3 players."""
    cleaned = "".join("-" if c in INVALID_FILENAME_CHARS else c for c in name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    cleaned = cleaned.replace("–", "-").replace("—", "-")
    # Keep filenames conservative for low-cost USB players.
    cleaned = re.sub(r"[^A-Za-z0-9 _.,()\[\]\-]", "", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in RESERVED_NAMES:
        cleaned = f"{cleaned}_"
    return cleaned[:120]


def numbered_name(order: int, title: str, extension: str = ".mp3", width: int = 3) -> str:
    title = sanitize_filename(title)
    if not extension.startswith("."):
        extension = f".{extension}"
    return f"{order:0{width}d} - {title}{extension.lower()}"


def numbered_folder(order: int, folder: str, width: int = 3) -> str:
    return f"{order:0{width}d} - {sanitize_filename(folder or 'Tracks')}"


def which_or_none(binary: str) -> str | None:
    return shutil.which(binary)


def run_command(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def iter_audio_files(root: Path, supported_exts: Iterable[str]) -> Iterable[Path]:
    supported = {ext.lower() for ext in supported_exts}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in supported:
            yield path


def likely_factory_demo(path: Path) -> bool:
    text = " ".join(path.parts).lower()
    patterns = ["test", "testing", "demo", "sample", "music", "musics"]
    return any(p in text for p in patterns)
