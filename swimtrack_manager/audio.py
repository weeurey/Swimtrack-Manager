from __future__ import annotations

import json
from dataclasses import replace
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from .models import CueSettings, Track
from .utils import which_or_none


FFMPEG_AUDIO_FORMAT = "sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"


def require_ffmpeg() -> None:
    if not which_or_none("ffmpeg"):
        raise RuntimeError("FFmpeg was not found on PATH. Install FFmpeg to process audio cues or convert files.")


def _run_ffmpeg(args: list[str], error_message: str, progress: Callable[[str], None] | None = None) -> None:
    """Run FFmpeg and raise a compact but useful error if it fails."""
    if progress:
        progress("FFmpeg command started: " + " ".join(_compact_arg(a) for a in args[:8]) + (" ..." if len(args) > 8 else ""))
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        if progress and stderr:
            progress(stderr[-2000:])
        raise RuntimeError(f"{error_message}: {stderr[-2000:] if stderr else 'unknown FFmpeg error'}")
    if progress:
        progress("FFmpeg command completed successfully.")


def _compact_arg(arg: str) -> str:
    text = str(arg)
    if len(text) > 72:
        return text[:30] + "…" + text[-30:]
    return text


def duration_seconds(path: Path) -> float | None:
    """Return audio duration using mutagen first, then ffprobe."""
    path = Path(path)
    try:
        from mutagen import File

        audio = File(path)
        if audio is not None and audio.info is not None and getattr(audio.info, "length", None):
            return float(audio.info.length)
    except Exception:
        pass

    ffprobe = which_or_none("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        data = json.loads(proc.stdout or "{}")
        duration = data.get("format", {}).get("duration")
        return float(duration) if duration else None
    except Exception:
        return None


def process_track(
    track: Track,
    destination: Path,
    settings: CueSettings,
    track_number: int,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Create a processed output file with optional intro speech, beeps, and spoken indicators."""
    require_ffmpeg()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    original_ms = int(round((duration_seconds(track.source_path) or 0) * 1000))
    cue_enabled = bool(settings.enabled and getattr(track, "audio_processing", False))
    volume_enabled = bool(getattr(settings, "volume_enabled", False) and getattr(track, "volume_change", False))
    if volume_enabled:
        volume_gain = float(getattr(settings, "volume_gain_db", 0.0) or 0.0)
        settings = replace(
            settings,
            main_volume_db=float(getattr(settings, "main_volume_db", 0.0) or 0.0) + volume_gain,
            output_limiter=bool(getattr(settings, "output_limiter", True) or getattr(settings, "volume_limiter", True)),
        )
    if progress:
        progress(f"Audio processing started for track {track_number}: {track.title}")
        progress(f"Duration detected: {format_time(original_ms) if original_ms else 'unknown'}")
        progress(f"Cue processing: {'on' if cue_enabled else 'off'}; volume change: {'on' if volume_enabled else 'off'}")
        if volume_enabled:
            progress(f"Volume gain requested: {float(getattr(settings, 'volume_gain_db', 0.0) or 0.0):+.1f} dB")
    if original_ms <= 0 and progress:
        progress(f"Could not determine duration for {track.source_path.name}; skipping timed cue placement.")

    with tempfile.TemporaryDirectory(prefix="swimtrack_") as tmp_dir:
        tmp = Path(tmp_dir)
        base_path = track.source_path
        intro_offset_ms = 0

        intro_text_parts: list[str] = []
        if cue_enabled and settings.voice_custom_intro.strip():
            intro_text_parts.append(settings.voice_custom_intro.strip())
        if cue_enabled and settings.voice_track_number:
            intro_text_parts.append(f"Track {track_number}")
        if cue_enabled and settings.voice_folder and track.folder.strip():
            intro_text_parts.append(f"Folder {track.folder}")
        if cue_enabled and settings.voice_title:
            intro_text_parts.append(track.title)

        if intro_text_parts:
            text = ". ".join(intro_text_parts) + "."
            if progress:
                progress(f"Generating intro speech: {text}")
            speech_path = synthesize_speech(text, tmp / "intro.wav", settings)
            intro_path = tmp / "intro_with_silence.wav"
            _make_intro_file(
                speech_path,
                intro_path,
                pre_silence_ms=max(settings.intro_silence_ms, 0),
                post_silence_ms=max(settings.post_intro_silence_ms, 0),
                progress=progress,
            )
            intro_duration = duration_seconds(intro_path) or 0
            intro_offset_ms = int(round(intro_duration * 1000))
            base_path = tmp / "base_with_intro.wav"
            if progress:
                progress(f"Prepending intro speech to {track.source_path.name}")
            _concat_intro_and_source(intro_path, track.source_path, base_path, progress=progress)

        cue_inputs: list[tuple[Path, int, str, bool]] = []

        if cue_enabled and settings.beep_enabled and original_ms > 0:
            beep_path = tmp / "beep.wav"
            _make_beep_file(beep_path, settings, progress=progress)
            for position_ms in beep_positions(original_ms, settings):
                label = f"beep at {format_time(position_ms)}"
                if progress:
                    progress(f"Queueing {label} in {track.title}")
                cue_inputs.append((beep_path, intro_offset_ms + position_ms, label, False))

        if cue_enabled and settings.voice_progress and original_ms > 0:
            voice_markers = voice_indicator_markers(original_ms, settings)
            for marker_index, (position_ms, text) in enumerate(voice_markers, start=1):
                speech_path = tmp / f"voice_indicator_{marker_index}_{int(time.time() * 1000)}.wav"
                try:
                    if progress:
                        progress(f"Generating voice indicator at {format_time(position_ms)}: {text}")
                    synthesize_speech(text, speech_path, settings)
                    cue_inputs.append((speech_path, intro_offset_ms + position_ms, text, True))
                except Exception as exc:
                    if progress:
                        progress(f"Speech indicator failed at {format_time(position_ms)}: {exc}")

        if cue_enabled and settings.voice_custom_outro.strip() and original_ms > 0:
            speech_path = tmp / f"voice_custom_outro_{int(time.time() * 1000)}.wav"
            try:
                text = settings.voice_custom_outro.strip()
                # Keep the outro just before the track end so FFmpeg's duration=first does not trim it entirely.
                position_ms = max(0, original_ms - 1500)
                if progress:
                    progress(f"Generating custom outro near end: {text}")
                synthesize_speech(text, speech_path, settings)
                cue_inputs.append((speech_path, intro_offset_ms + position_ms, text, True))
            except Exception as exc:
                if progress:
                    progress(f"Custom outro speech failed: {exc}")

        if cue_inputs:
            if progress:
                progress(f"Mixing {len(cue_inputs)} audio cue(s) into {destination.name}")
            _mix_cues_into_base(base_path, cue_inputs, destination, settings, progress=progress)
        else:
            if progress:
                progress(f"No overlay cues to mix; exporting {destination.name}")
            _export_audio(base_path, destination, settings, progress=progress)

    if progress:
        progress(f"Finished processed output: {destination.name}")
    return destination


def _make_intro_file(
    speech_path: Path,
    destination: Path,
    pre_silence_ms: int,
    post_silence_ms: int,
    progress: Callable[[str], None] | None = None,
) -> None:
    pre = max(pre_silence_ms, 0) / 1000
    post = max(post_silence_ms, 0) / 1000
    filter_complex = (
        f"anullsrc=r=44100:cl=stereo,atrim=duration={pre:.3f}[pre];"
        f"[0:a]aformat={FFMPEG_AUDIO_FORMAT}[speech];"
        f"anullsrc=r=44100:cl=stereo,atrim=duration={post:.3f}[post];"
        "[pre][speech][post]concat=n=3:v=0:a=1[out]"
    )
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(speech_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            str(destination),
        ],
        "Could not create intro speech audio",
        progress=progress,
    )


def _concat_intro_and_source(
    intro_path: Path,
    source_path: Path,
    destination: Path,
    progress: Callable[[str], None] | None = None,
) -> None:
    filter_complex = (
        f"[0:a]aformat={FFMPEG_AUDIO_FORMAT}[intro];"
        f"[1:a]aformat={FFMPEG_AUDIO_FORMAT}[main];"
        "[intro][main]concat=n=2:v=0:a=1[out]"
    )
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(intro_path),
            "-i",
            str(source_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            str(destination),
        ],
        "Could not prepend intro speech",
        progress=progress,
    )


def _make_beep_file(
    destination: Path,
    settings: CueSettings,
    progress: Callable[[str], None] | None = None,
) -> None:
    duration = max(settings.beep_duration_ms, 1) / 1000
    frequency = max(settings.beep_frequency_hz, 20)
    gain = int(settings.beep_volume_db)
    repeat = max(1, min(settings.beep_repeat_count, 10))
    gap = max(settings.beep_gap_ms, 0) / 1000

    if progress:
        progress(f"Creating beep cue: {repeat} pulse(s), {frequency} Hz, {duration:.3f}s each, {gain} dB.")

    if repeat == 1:
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:duration={duration:.3f}:sample_rate=44100",
                "-filter:a",
                f"volume={gain}dB,aformat={FFMPEG_AUDIO_FORMAT}",
                str(destination),
            ],
            "Could not create beep cue",
            progress=progress,
        )
        return

    inputs: list[str] = []
    input_index = 0
    for pulse in range(repeat):
        inputs.extend(["-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={duration:.3f}:sample_rate=44100"])
        input_index += 1
        if pulse < repeat - 1 and gap > 0:
            inputs.extend(["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={gap:.3f}"])
            input_index += 1

    prepped = []
    for idx in range(input_index):
        prepped.append(f"[{idx}:a]aformat={FFMPEG_AUDIO_FORMAT}[a{idx}]")
    concat_inputs = "".join(f"[a{idx}]" for idx in range(input_index))
    filter_complex = ";".join(prepped + [f"{concat_inputs}concat=n={input_index}:v=0:a=1,volume={gain}dB[out]"])
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            *inputs,
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            str(destination),
        ],
        "Could not create beep cue",
        progress=progress,
    )


def _mix_cues_into_base(
    base_path: Path,
    cue_inputs: list[tuple[Path, int, str, bool]],
    destination: Path,
    settings: CueSettings,
    progress: Callable[[str], None] | None = None,
) -> None:
    inputs: list[str] = ["-i", str(base_path)]
    for entry in cue_inputs:
        cue_path = entry[0]
        inputs.extend(["-i", str(cue_path)])

    main_gain = float(getattr(settings, "main_volume_db", 0.0) or 0.0)
    final_gain = float(getattr(settings, "final_volume_db", 0.0) or 0.0)
    prevent_attenuation = bool(getattr(settings, "prevent_mix_attenuation", True))
    limiter_enabled = bool(getattr(settings, "output_limiter", True))
    ducking_enabled = bool(getattr(settings, "voice_ducking_enabled", False))
    ducking_percent = max(0, min(100, int(getattr(settings, "voice_ducking_percent", 0) or 0)))

    voice_intervals: list[tuple[float, float]] = []
    if ducking_enabled and ducking_percent > 0:
        for cue_path, delay_ms, _label, is_voice in cue_inputs:
            if not is_voice:
                continue
            cue_dur = duration_seconds(cue_path) or 0.0
            if cue_dur <= 0:
                continue
            start_s = max(0, int(delay_ms)) / 1000.0
            voice_intervals.append((start_s, start_s + cue_dur))

    base_filter = f"[0:a]aformat={FFMPEG_AUDIO_FORMAT},volume={main_gain:.2f}dB"
    if voice_intervals:
        duck_factor = max(0.0, (100 - ducking_percent) / 100.0)
        for start_s, end_s in voice_intervals:
            base_filter += (
                f",volume=enable='between(t,{start_s:.3f},{end_s:.3f})'"
                f":volume={duck_factor:.3f}"
            )
        if progress:
            progress(
                f"Ducking base track by {ducking_percent}% during "
                f"{len(voice_intervals)} voice cue interval(s) (factor {duck_factor:.2f})."
            )
    filters = [base_filter + "[base]"]
    mix_labels = ["[base]"]
    if progress:
        progress(
            "Mixing volume settings: "
            f"main gain {main_gain:+.1f} dB, final gain {final_gain:+.1f} dB, "
            f"amix normalize={'off' if prevent_attenuation else 'on'}, "
            f"limiter={'on' if limiter_enabled else 'off'}, "
            f"ducking={'on' if ducking_enabled else 'off'}"
            f"{f' ({ducking_percent}%)' if ducking_enabled else ''}."
        )
    for input_index, (_cue_path, delay_ms, label_text, _is_voice) in enumerate(cue_inputs, start=1):
        delay = max(0, int(delay_ms))
        label = f"cue{input_index}"
        if progress:
            progress(f"Mix cue {input_index}: delay={delay} ms; label={label_text}")
        filters.append(f"[{input_index}:a]aformat={FFMPEG_AUDIO_FORMAT},adelay={delay}|{delay}[{label}]")
        mix_labels.append(f"[{label}]")

    normalize_flag = 0 if prevent_attenuation else 1
    post_filters = [f"amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize={normalize_flag}"]
    if abs(final_gain) > 0.001:
        post_filters.append(f"volume={final_gain:.2f}dB")
    if limiter_enabled:
        post_filters.append("alimiter=limit=0.98")
    filters.append("".join(mix_labels) + ",".join(post_filters) + "[out]")
    codec_args = _output_codec_args(settings)
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            *codec_args,
            str(destination),
        ],
        "Could not mix audio cues",
        progress=progress,
    )


def _export_audio(
    source_path: Path,
    destination: Path,
    settings: CueSettings,
    progress: Callable[[str], None] | None = None,
) -> None:
    if source_path.resolve() == destination.resolve():
        return
    main_gain = float(getattr(settings, "main_volume_db", 0.0) or 0.0)
    final_gain = float(getattr(settings, "final_volume_db", 0.0) or 0.0)
    limiter_enabled = bool(getattr(settings, "output_limiter", True))
    codec_args = _output_codec_args(settings)
    filters = [f"aformat={FFMPEG_AUDIO_FORMAT}"]
    if abs(main_gain) > 0.001:
        filters.append(f"volume={main_gain:.2f}dB")
    if abs(final_gain) > 0.001:
        filters.append(f"volume={final_gain:.2f}dB")
    if limiter_enabled:
        filters.append("alimiter=limit=0.98")
    if progress:
        progress(
            "Export volume settings: "
            f"main gain {main_gain:+.1f} dB, final gain {final_gain:+.1f} dB, "
            f"limiter={'on' if limiter_enabled else 'off'}."
        )
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-filter:a",
            ",".join(filters),
            *codec_args,
            str(destination),
        ],
        "Could not export processed audio",
        progress=progress,
    )


def _output_codec_args(settings: CueSettings) -> list[str]:
    fmt = (settings.output_format or "mp3").lower().lstrip(".")
    if fmt == "mp3":
        return ["-codec:a", "libmp3lame", "-b:a", "192k"]
    if fmt == "wav":
        return ["-codec:a", "pcm_s16le"]
    if fmt == "flac":
        return ["-codec:a", "flac"]
    return ["-codec:a", "libmp3lame", "-b:a", "192k"]


def cue_positions(original_ms: int, percentages: list[int], every_minutes: int) -> list[int]:
    positions: set[int] = set()
    for pct in percentages:
        if 0 < pct < 100:
            positions.add(int(original_ms * pct / 100))
    if every_minutes and every_minutes > 0:
        interval = int(every_minutes * 60 * 1000)
        pos = interval
        while pos < original_ms:
            positions.add(pos)
            pos += interval
    return sorted(positions)


def beep_positions(original_ms: int, settings: CueSettings) -> list[int]:
    positions = set(cue_positions(original_ms, settings.beep_percentages, settings.beep_every_minutes))
    if settings.beep_at_start:
        positions.add(0)
    if settings.beep_at_end:
        estimated_beep_ms = max(1, settings.beep_repeat_count) * max(1, settings.beep_duration_ms) + max(0, settings.beep_repeat_count - 1) * max(0, settings.beep_gap_ms)
        positions.add(max(0, original_ms - estimated_beep_ms - 500))
    return sorted(positions)


def voice_indicator_markers(original_ms: int, settings: CueSettings) -> list[tuple[int, str]]:
    positions = set(cue_positions(original_ms, settings.voice_progress_percentages, settings.voice_every_minutes))
    if settings.voice_at_start:
        positions.add(0)
    if settings.voice_at_end:
        positions.add(max(0, original_ms - 1500))
    markers: list[tuple[int, str]] = []
    for position_ms in sorted(positions):
        text = progress_text_for_position(position_ms, original_ms, settings)
        if settings.voice_indicator_prefix.strip():
            text = f"{settings.voice_indicator_prefix.strip()} {text}"
        if settings.voice_indicator_suffix.strip():
            text = f"{text} {settings.voice_indicator_suffix.strip()}"
        markers.append((position_ms, text))
    return markers


def progress_text(pct: int, original_ms: int, settings: CueSettings) -> str:
    return progress_text_for_position(int(original_ms * pct / 100), original_ms, settings)


def progress_text_for_position(position_ms: int, original_ms: int, settings: CueSettings) -> str:
    pct = int(round((position_ms / max(original_ms, 1)) * 100))
    elapsed = format_spoken_duration(position_ms)
    remaining = format_spoken_duration(max(original_ms - position_ms, 0))
    style = (settings.progress_announcement_style or "percentage").lower()
    if style == "remaining":
        return f"About {remaining} remaining."
    if style == "elapsed":
        return f"{elapsed} elapsed."
    if style in {"elapsed_remaining", "elapsed and remaining", "elapsed + remaining"}:
        return f"{elapsed} elapsed. About {remaining} remaining."
    if style == "timecode":
        return f"{format_time(position_ms)} of {format_time(original_ms)}."
    return f"{pct} percent complete."


def format_time(ms: int) -> str:
    total = max(0, int(round(ms / 1000)))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:d}:{seconds:02d}"


def format_spoken_duration(ms: int) -> str:
    total_seconds = max(0, int(round(ms / 1000)))
    if total_seconds < 60:
        return f"{total_seconds} seconds"
    minutes = int(round(total_seconds / 60))
    if minutes < 60:
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    hours, rem_minutes = divmod(minutes, 60)
    if rem_minutes == 0:
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit}"
    hour_unit = "hour" if hours == 1 else "hours"
    minute_unit = "minute" if rem_minutes == 1 else "minutes"
    return f"{hours} {hour_unit} {rem_minutes} {minute_unit}"


def build_cue_timeline(track: Track, settings: CueSettings, track_number: int) -> list[dict[str, object]]:
    """Return a lightweight timeline of planned processing events for the Preview tab."""
    original_ms = int(round((duration_seconds(track.source_path) or 0) * 1000))
    cue_enabled = bool(settings.enabled and getattr(track, "audio_processing", False))
    volume_enabled = bool(getattr(settings, "volume_enabled", False) and getattr(track, "volume_change", False))
    events: list[dict[str, object]] = []

    if getattr(track, "batch_rename", False):
        events.append({"time_ms": 0, "type": "Rename", "detail": f"Final title: {track.title}"})
    if getattr(track, "volume_change", False):
        if volume_enabled:
            events.append({
                "time_ms": 0,
                "type": "Volume",
                "detail": f"Apply gain {float(getattr(settings, 'volume_gain_db', 0.0) or 0.0):+.1f} dB to whole track",
            })
        else:
            events.append({
                "time_ms": 0,
                "type": "Volume disabled",
                "detail": "Track is marked for volume change, but Audio Volume processing is disabled",
            })
    if not cue_enabled:
        return events

    intro_parts: list[str] = []
    if settings.voice_custom_intro.strip():
        intro_parts.append(settings.voice_custom_intro.strip())
    if settings.voice_track_number:
        intro_parts.append(f"Track {track_number}")
    if settings.voice_folder and track.folder.strip():
        intro_parts.append(f"Folder {track.folder}")
    if settings.voice_title:
        intro_parts.append(track.title)
    if intro_parts:
        events.append({"time_ms": 0, "type": "Voice intro", "detail": ". ".join(intro_parts)})

    if original_ms <= 0:
        return events

    if settings.beep_enabled:
        for position_ms in beep_positions(original_ms, settings):
            events.append({
                "time_ms": position_ms,
                "type": "Beep",
                "detail": f"{settings.beep_repeat_count} pulse(s), {settings.beep_frequency_hz} Hz",
            })
    if settings.voice_progress:
        for position_ms, text in voice_indicator_markers(original_ms, settings):
            events.append({"time_ms": position_ms, "type": "Voice indicator", "detail": text})
    if settings.voice_custom_outro.strip():
        events.append({
            "time_ms": max(0, original_ms - 1500),
            "type": "Voice outro",
            "detail": settings.voice_custom_outro.strip(),
        })
    return sorted(events, key=lambda item: int(item.get("time_ms", 0)))


def synthesize_speech(text: str, destination: Path, settings: CueSettings) -> Path:
    """Generate speech as a local audio file using pyttsx3."""
    try:
        import pyttsx3
    except Exception as exc:
        raise RuntimeError("pyttsx3 is not installed. Install it or use beep-only cues.") from exc

    destination = Path(destination)
    engine = pyttsx3.init()
    try:
        engine.setProperty("rate", int(settings.speech_rate))
        engine.setProperty("volume", float(settings.speech_volume))
    except Exception:
        pass
    engine.save_to_file(text, str(destination))
    engine.runAndWait()
    engine.stop()

    # Some speech engines return before the file is flushed.
    for _ in range(30):
        if destination.exists() and destination.stat().st_size > 128:
            break
        time.sleep(0.1)
    if not destination.exists() or destination.stat().st_size <= 128:
        raise RuntimeError("Speech file was not created. Check your system TTS engine.")
    return destination


def copy_or_convert_track(
    track: Track,
    destination: Path,
    force_mp3: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not force_mp3 and track.source_path.suffix.lower() == destination.suffix.lower():
        if progress:
            progress(f"Copying {track.source_path.name} to {destination.name}")
        shutil.copy2(track.source_path, destination)
        return destination

    require_ffmpeg()
    if progress:
        progress(f"Converting {track.source_path.name} to {destination.name}")
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(track.source_path),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(destination),
        ],
        "FFmpeg conversion failed",
        progress=progress,
    )
    if progress:
        progress(f"Finished conversion: {destination.name}")
    return destination


def output_extension_for(settings: CueSettings, source_ext: str, should_process: bool = True) -> str:
    if settings.enabled and should_process:
        return f".{(settings.output_format or 'mp3').lower().lstrip('.')}"
    return source_ext.lower() if source_ext else ".mp3"
