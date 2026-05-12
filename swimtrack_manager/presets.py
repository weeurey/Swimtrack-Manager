from __future__ import annotations

from .models import CueSettings


def preset_names() -> list[str]:
    return ["Minimal", "Music swim", "Podcast", "Audiobook", "Training"]


def make_preset(name: str) -> CueSettings:
    name = name.lower().strip()
    if name == "music swim":
        return CueSettings(
            enabled=False,
            beep_enabled=False,
            voice_title=False,
            voice_track_number=False,
        )
    if name == "podcast":
        return CueSettings(
            enabled=True,
            beep_enabled=True,
            beep_percentages=[25, 50, 75],
            beep_every_minutes=0,
            beep_repeat_count=1,
            voice_title=True,
            voice_track_number=True,
            voice_progress=True,
            voice_progress_percentages=[50],
            voice_every_minutes=0,
            progress_announcement_style="percentage",
        )
    if name == "audiobook":
        return CueSettings(
            enabled=True,
            beep_enabled=True,
            beep_percentages=[],
            beep_every_minutes=10,
            beep_repeat_count=2,
            beep_gap_ms=180,
            voice_title=True,
            voice_track_number=True,
            voice_folder=True,
            voice_progress=True,
            voice_progress_percentages=[25, 50, 75],
            voice_every_minutes=10,
            voice_at_end=True,
            progress_announcement_style="elapsed_remaining",
        )
    if name == "training":
        return CueSettings(
            enabled=True,
            beep_enabled=True,
            beep_frequency_hz=1000,
            beep_duration_ms=500,
            beep_volume_db=-5,
            beep_percentages=[],
            beep_every_minutes=5,
            beep_at_start=True,
            beep_at_end=True,
            beep_repeat_count=3,
            beep_gap_ms=150,
            voice_title=False,
            voice_track_number=True,
            voice_progress=True,
            voice_progress_percentages=[],
            voice_every_minutes=5,
            progress_announcement_style="elapsed",
        )
    return CueSettings(enabled=False)
