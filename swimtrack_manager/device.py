from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Callable

from .models import DeviceInfo, SUPPORTED_EXTENSIONS, Track
from .utils import iter_audio_files, likely_factory_demo


COMMON_LINUX_DEVICE_FS = {"vfat", "msdos", "exfat", "fuseblk", "ntfs", "ntfs3"}
SPECIAL_LINUX_FS = {
    "autofs",
    "binfmt_misc",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "efivarfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "nsfs",
    "overlay",
    "proc",
    "pstore",
    "rpc_pipefs",
    "securityfs",
    "selinuxfs",
    "squashfs",
    "sysfs",
    "tmpfs",
    "tracefs",
}


def get_device_info(path: Path) -> DeviceInfo:
    path = Path(path).resolve()
    total = free = 0
    try:
        usage = shutil.disk_usage(path)
        total = usage.total
        free = usage.free
    except Exception:
        pass

    filesystem = detect_filesystem(path)
    warning = ""
    if filesystem and filesystem.upper() not in {"FAT32", "VFAT", "MSDOS"}:
        warning = (
            f"Filesystem appears to be {filesystem}. The Aztine notes recommend FAT32. "
            "Do not use NTFS for this headphone model."
        )
    elif filesystem == "Unknown":
        warning = "Could not confirm filesystem. For the Aztine test headphones, use FAT32."

    return DeviceInfo(path=path, filesystem=filesystem, total_bytes=total, free_bytes=free, warning=warning)


def discover_candidate_devices() -> list[DeviceInfo]:
    """Return likely USB/audio-player mount points.

    Linux desktops mount small USB devices in different places depending on the
    distro and file manager: /media/$USER, /run/media/$USER, or sometimes /mnt.
    The UI can scan these mounted paths directly instead of relying only on the
    folder picker sidebar. This only returns mounted paths, never raw /dev nodes.
    """
    system = platform.system().lower()
    if system == "linux":
        return _discover_candidate_devices_linux()
    if system == "windows":
        return _discover_candidate_devices_windows()
    return []


def _discover_candidate_devices_windows() -> list[DeviceInfo]:
    candidates: list[DeviceInfo] = []
    try:
        drives_mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return candidates

    for i in range(26):
        if not drives_mask & (1 << i):
            continue
        root = Path(f"{chr(65 + i)}:/")
        try:
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(root))
        except Exception:
            drive_type = 0
        # DRIVE_REMOVABLE=2, DRIVE_FIXED=3. Some USB audio players report as fixed.
        if drive_type in {2, 3}:
            try:
                info = get_device_info(root)
                if 0 < info.total_bytes <= 256 * 1024**3:
                    candidates.append(info)
            except Exception:
                continue
    return candidates


def _discover_candidate_devices_linux() -> list[DeviceInfo]:
    mounts: list[tuple[str, Path, str]] = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 3:
                    continue
                source, raw_mount_point, fs_type = parts[:3]
                mount_point = Path(raw_mount_point.replace("\\040", " "))
                fs_lower = fs_type.lower()
                if fs_lower in SPECIAL_LINUX_FS:
                    continue
                if mount_point == Path("/"):
                    continue
                if not _looks_like_external_mount(source, mount_point, fs_lower):
                    continue
                mounts.append((source, mount_point, fs_type))
    except Exception:
        return []

    candidates: list[DeviceInfo] = []
    seen: set[Path] = set()
    for _source, mount_point, _fs_type in mounts:
        try:
            resolved = mount_point.resolve()
        except Exception:
            resolved = mount_point
        if resolved in seen or not resolved.exists() or not resolved.is_dir():
            continue
        seen.add(resolved)
        try:
            info = get_device_info(resolved)
            # The test headphones are 16 GB. Keep the upper bound wide enough
            # for SD cards and larger variants, but avoid listing the main disk.
            if 0 < info.total_bytes <= 256 * 1024**3:
                candidates.append(info)
        except Exception:
            continue

    candidates.sort(key=lambda item: str(item.path).lower())
    return candidates


def _looks_like_external_mount(source: str, mount_point: Path, fs_lower: str) -> bool:
    mount_text = str(mount_point)
    if mount_text.startswith(("/media/", "/run/media/", "/mnt/")):
        return True
    if fs_lower in COMMON_LINUX_DEVICE_FS:
        return True
    if source.startswith(("/dev/sd", "/dev/mmcblk", "/dev/disk/by-uuid/", "/dev/disk/by-label/")):
        return True
    return False


def detect_filesystem(path: Path) -> str:
    system = platform.system().lower()
    try:
        if system == "windows":
            return _detect_filesystem_windows(path)
        if system == "linux":
            return _detect_filesystem_linux(path)
    except Exception:
        return "Unknown"
    return "Unknown"


def _detect_filesystem_windows(path: Path) -> str:
    root = Path(path).anchor or str(path)
    volume_name = ctypes.create_unicode_buffer(261)
    fs_name = ctypes.create_unicode_buffer(261)
    serial_number = ctypes.c_ulong()
    max_component_length = ctypes.c_ulong()
    file_system_flags = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root),
        volume_name,
        ctypes.sizeof(volume_name),
        ctypes.byref(serial_number),
        ctypes.byref(max_component_length),
        ctypes.byref(file_system_flags),
        fs_name,
        ctypes.sizeof(fs_name),
    )
    return fs_name.value or "Unknown" if ok else "Unknown"


def _detect_filesystem_linux(path: Path) -> str:
    path = path.resolve()
    best_mount = None
    best_fs = "Unknown"
    with open("/proc/mounts", "r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) < 3:
                continue
            mount_point = Path(parts[1].replace("\\040", " ")).resolve()
            try:
                path.relative_to(mount_point)
            except ValueError:
                continue
            if best_mount is None or len(str(mount_point)) > len(str(best_mount)):
                best_mount = mount_point
                best_fs = parts[2]
    if best_fs != "Unknown":
        return "VFAT" if best_fs.lower() == "vfat" else best_fs.upper()

    # Fallback: df + lsblk, if available.
    proc = subprocess.run(["df", "--output=source", str(path)], capture_output=True, text=True, check=False)
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) >= 2:
        device = lines[-1]
        proc2 = subprocess.run(["lsblk", "-no", "FSTYPE", device], capture_output=True, text=True, check=False)
        fs = proc2.stdout.strip()
        if fs:
            return "VFAT" if fs.lower() == "vfat" else fs.upper()
    return "Unknown"


def scan_device(
    path: Path,
    duration_func: Callable[[Path], float | None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[Track]:
    root = Path(path)
    tracks: list[Track] = []
    files = sorted(iter_audio_files(root, SUPPORTED_EXTENSIONS), key=lambda p: str(p.relative_to(root)).lower())
    if progress:
        progress(f"Found {len(files)} supported audio file(s) under {root}.")
    for idx, file_path in enumerate(files, start=1):
        rel = file_path.relative_to(root)
        if progress:
            progress(f"Reading track {idx}/{len(files)}: {rel}")
        folder = "" if len(rel.parts) <= 1 else rel.parts[0]
        order, title = parse_numbered_title(file_path.stem, idx)
        folder_order, folder_title = parse_numbered_title(folder, 1) if folder else (1, "")
        duration = duration_func(file_path) if duration_func else None
        tracks.append(
            Track(
                source_path=file_path,
                title=title,
                order=order,
                folder=folder_title,
                folder_order=folder_order,
                duration_seconds=duration,
                on_device=True,
            )
        )
    # Normalize display order after scan.
    tracks.sort(key=lambda t: (t.folder_order, t.folder.lower(), t.order, t.title.lower()))
    for i, track in enumerate(tracks, start=1):
        track.order = i
    if progress:
        progress("Finished scanning and normalising track order.")
    return tracks


def parse_numbered_title(stem: str, default_order: int) -> tuple[int, str]:
    import re

    if not stem:
        return default_order, "Untitled"
    match = re.match(r"^\s*(\d{1,5})\s*[-_. )]*(.+)$", stem)
    if match:
        try:
            return int(match.group(1)), match.group(2).strip() or stem
        except ValueError:
            pass
    return default_order, stem.strip()


def backup_device(path: Path, output_zip: Path, progress: Callable[[str], None] | None = None) -> Path:
    path = Path(path).resolve()
    output_zip = Path(output_zip)
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in path.rglob("*"):
            if file.is_file():
                if progress:
                    progress(f"Backing up {file.name}")
                zf.write(file, file.relative_to(path))
    return output_zip


def find_likely_factory_tracks(tracks: list[Track]) -> list[Track]:
    return [track for track in tracks if likely_factory_demo(track.source_path)]
