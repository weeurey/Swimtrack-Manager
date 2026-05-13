from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import uuid4

SUPPORTED_EXTENSIONS = {".mp3", ".ape", ".flac", ".wav"}
DEFAULT_OUTPUT_EXTENSION = ".mp3"


@dataclass
class Track:
    """A track in the current working playlist."""

    source_path: Path
    title: str
    order: int
    folder: str = ""
    folder_order: int = 1
    duration_seconds: Optional[float] = None
    on_device: bool = False
    remove_from_device: bool = False
    audio_processing: bool = False
    batch_rename: bool = False
    volume_change: bool = False
    id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def extension(self) -> str:
        return self.source_path.suffix.lower()

    @property
    def supported(self) -> bool:
        return self.extension in SUPPORTED_EXTENSIONS

    @property
    def display_duration(self) -> str:
        if self.duration_seconds is None:
            return ""
        total = int(round(self.duration_seconds))
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h:d}:{m:02d}:{s:02d}"
        return f"{m:d}:{s:02d}"


@dataclass
class CueSettings:
    """Audio cue generation settings."""

    enabled: bool = False
    output_format: str = "mp3"

    # Volume-only processing for tracks marked Volume
    volume_enabled: bool = False
    volume_gain_db: float = 0.0
    volume_limiter: bool = True

    # Volume / mixing behaviour
    main_volume_db: float = 0.0
    final_volume_db: float = 0.0
    prevent_mix_attenuation: bool = True
    output_limiter: bool = True

    # Ducking: reduce the base track volume while a voice cue is playing.
    voice_ducking_enabled: bool = False
    voice_ducking_percent: int = 60  # percent reduction of the base track during voice cues (0-100)

    # Beep indicators
    beep_enabled: bool = False
    beep_frequency_hz: int = 880
    beep_duration_ms: int = 350
    beep_volume_db: int = -7
    beep_percentages: list[int] = field(default_factory=lambda: [25, 50, 75])
    beep_every_minutes: int = 0
    beep_at_start: bool = False
    beep_at_end: bool = False
    beep_repeat_count: int = 1
    beep_gap_ms: int = 150

    # Voice intro/indicator options
    voice_title: bool = False
    voice_track_number: bool = False
    voice_folder: bool = False
    voice_progress: bool = False
    voice_progress_percentages: list[int] = field(default_factory=lambda: [50])
    voice_every_minutes: int = 0
    voice_at_start: bool = False
    voice_at_end: bool = False
    voice_custom_intro: str = ""
    voice_custom_outro: str = ""
    voice_indicator_prefix: str = ""
    voice_indicator_suffix: str = ""
    intro_silence_ms: int = 600
    post_intro_silence_ms: int = 600
    speech_rate: int = 165
    speech_volume: float = 1.0
    progress_announcement_style: str = "percentage"  # percentage | elapsed | remaining | elapsed_remaining | timecode


@dataclass
class BatchRenameSettings:
    """Pending batch rename rules applied only during sync."""

    prefix: str = ""
    suffix: str = ""
    search_text: str = ""
    replace_text: str = ""
    case_mode: str = "unchanged"  # unchanged | lower | upper | title
    numbering_mode: str = "none"  # none | prefix | suffix | replace
    start_number: int = 1
    increment: int = 1
    padding: int = 2
    separator: str = " - "
    use_source_filename: bool = False
    remove_original_title: bool = False


@dataclass
class DeviceInfo:
    path: Path
    filesystem: str = "Unknown"
    total_bytes: int = 0
    free_bytes: int = 0
    warning: str = ""

    @property
    def total_display(self) -> str:
        return format_bytes(self.total_bytes)

    @property
    def free_display(self) -> str:
        return format_bytes(self.free_bytes)

    @property
    def is_fat32(self) -> bool:
        fs = self.filesystem.upper()
        return fs in {"FAT32", "VFAT", "MSDOS"}


def format_bytes(value: int) -> str:
    if value <= 0:
        return "Unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"
