# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project overview

SwimTrack Manager is a cross-platform PySide6/Qt desktop GUI for preparing
audio files for waterproof bone-conduction headphones (e.g. Aztine) that
expose themselves as USB MP3 players. The app stages and writes audio to a
mounted device, optionally inserting beep/voice cues, applying batch renames,
and boosting per-track volume. All processing is non-destructive — source
files are never modified in place; output is rendered through FFmpeg.

User-facing documentation lives in `README.md`. Read it first for feature
behavior, the typical workflow, and platform notes (FFmpeg, espeak-ng, FAT32,
Linux mount detection).

## Running and packaging

```bash
python -m pip install -r requirements.txt   # PySide6, mutagen, pyttsx3
python -m swimtrack_manager                 # or: python run.py
pyinstaller SwimTrackManager.spec           # standalone build (per-OS)
```

External runtime dependencies that are NOT pip-installable:

- `ffmpeg` and `ffprobe` on PATH (required for decoding, duration, cue mix).
- A system speech engine for voice cues: SAPI5 on Windows, `espeak-ng` on
  Linux. Speech is optional; beep-only cues work without it.

Python 3.10+ is required; the codebase is 3.13-compatible and intentionally
avoids `pydub`/`audioop` by driving FFmpeg directly.

## Source layout

```
run.py                        thin entry point -> swimtrack_manager.__main__:main
SwimTrackManager.spec         PyInstaller spec (collects pyttsx3 submodules)
swimtrack_manager/
  __main__.py    main()        QApplication bootstrap
  app.py         ~2k lines     all Qt UI: tabs, dialogs, signal wiring,
                               Library/Audio Cues/Batch Rename/Preview/
                               Sync Preview/Log tabs, preset controls
  audio.py                     FFmpeg-driven decode, cue/voice mixing,
                               volume boost, limiter, MP3 render
  device.py                    drive discovery, mount detection (Linux
                               /media, /run/media, /mnt), FAT32 checks,
                               free-space readout
  sync.py                      staging, plan/preview building, copy/
                               replace/delete, progress + speed reporting
  models.py                    track / folder / plan dataclasses
  presets.py                   built-in + custom preset load/save/delete
  settings.py                  on-disk user settings
  utils.py                     small shared helpers
```

`app.py` is the integration point. When changing cross-cutting behavior
(marks, preview refresh, preset wiring), expect to touch `app.py` plus one
of `audio.py` / `sync.py` / `presets.py`.

## Architectural conventions

- **Three independent "mark" flags per track**: audio processing, batch
  rename, and volume change. Each runs only on its marked subset, only on
  **Apply Sync**. Preserve this separation — don't fold them together.
- **Batch rename runs before audio processing** so voice title cues speak
  the final renamed title (minus the numeric playback prefix added at sync
  time). Don't reorder this.
- **Numeric filename prefixes** (e.g. `001 - Title.mp3`) drive playback order
  on the device. Numbered folder prefixes drive folder order. The device is
  assumed FAT32.
- **Output is MP3** by default, even when the source is APE/FLAC/WAV.
- **Sync Preview auto-refreshes** on track/folder/preset/cue/device-path
  changes. There is also a manual **Refresh Preview**. New state that
  affects the plan must trigger a refresh.
- **Verbose Log tab** is the canonical debug surface — scan, preview,
  backup, processing and sync all log there with timestamps. When adding
  new operations, log start/end and any tracebacks rather than swallowing.
- **FFmpeg mix attenuation is disabled by default** (older builds quietened
  tracks via `amix` against silent cue tracks). The Volume/Mix subtab
  exposes attenuation, original-track gain, final output gain, and a
  limiter. Don't reintroduce silent mix legs into `amix`.

## Working in this repo

- Prefer editing existing modules over adding new ones; `app.py` already
  centralizes UI wiring.
- Keep the workflow non-destructive: never write back to source paths.
- Shell out to FFmpeg/FFprobe via the existing helpers in `audio.py` rather
  than adding a new subprocess pattern.
- There is currently no test suite, linter config, or CI. Smoke-test by
  running `python -m swimtrack_manager` and exercising the affected tab.
  UI changes cannot be verified from the CLI alone — say so explicitly
  rather than claiming a UI fix is "tested".
- Development branch convention for this setup task:
  `claude/setup-repo-docs-P84mz`.
