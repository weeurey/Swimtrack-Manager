# SwimTrack Manager

A cross-platform desktop GUI for preparing audio files for simple waterproof bone-conduction headphones that behave like a USB MP3 player.

The app is designed around devices like the Aztine waterproof bone-conduction headphones:

- Bluetooth is not used underwater; files are prepared for MP3/offline mode.
- The device should be formatted as FAT32.
- Supported device formats are MP3, APE, FLAC and WAV.
- Playback order is controlled by numeric filename prefixes.
- Numbered folders can be used to control folder order.

## Features included

- Full desktop GUI using PySide6 / Qt.
- Windows and Linux support.
- Select and load a mounted headphone drive. Workflow tabs stay locked until a device has been loaded successfully.
- View audio tracks on the player.
- Add local audio files to a playlist.
- Remove tracks from the sync set.
- Mark/unmark selected tracks for audio processing; cue generation only happens to marked tracks when **Apply Sync** is clicked.
- Mark/unmark selected tracks for batch rename; rename rules only happen when **Apply Sync** is clicked.
- Mark/unmark selected tracks for volume change; volume boost only happens when **Apply Sync** is clicked.
- Batch rename titles are calculated before audio processing, so spoken title cues use the final renamed title that will be written to the device.
- Drag/reorder selected tracks with up/down buttons.
- Rename track titles.
- Folder management using numbered folder prefixes.
- Sync preview before writing to the device, refreshed automatically after track, folder, preset, cue and device-path changes.
- Sync Preview tab includes a status summary, current-operation label, progress bar, transfer speed readouts and inline live activity log for staging, deleting/replacing and final writing.
- Preview tab shows marked tracks, cue/rename/volume timeline events, temporary rendered preview audio, scrub control and playback speed control.
- Dedicated verbose Log tab with timestamped scan, preview, backup, processing and sync output.
- Save current-session logs to a `.log` or `.txt` file.
- Backup current headphone contents to a ZIP file.
- Remove likely factory/demo test music.
- FAT32 detection/warnings where supported by the operating system.
- Storage/free-space display.
- Audio cue presets for music, podcasts, audiobooks and training.
- Save, load and delete named custom presets that include both Audio Cue settings and Batch Rename settings.
- Audio Volume tab for boosting quiet MP3 tracks, with per-track volume marks and limiter option.
- Beep indicators by percentage, fixed interval, start marker, end marker, repeat pulse count, gap, frequency, duration and volume.
- Spoken track number, folder and/or title intro.
- Spoken progress announcements by percentage markers, fixed minute intervals, start marker, end marker, custom intro/outro text, and custom indicator prefix/suffix.
- Voice wording options: percentage complete, elapsed time, remaining time, or elapsed plus remaining.
- Non-destructive workflow: source files are never modified in place.
- Generated output defaults to MP3.

## Important dependencies

This is a source package, not a compiled installer. Install Python 3.10+ and then install the app dependencies. This version is compatible with Python 3.13 and no longer depends on `pydub`, avoiding the removed `audioop` module issue:

```bash
python -m pip install -r requirements.txt
```

You also need FFmpeg and FFprobe installed and available on your PATH for audio conversion, duration detection and cue insertion.

### Windows FFmpeg

Install FFmpeg and ensure `ffmpeg.exe` and `ffprobe.exe` are on your PATH.

### Linux FFmpeg

On Debian/Ubuntu:

```bash
sudo apt install ffmpeg
```

For speech generation, install a system speech engine. `pyttsx3` can use SAPI5 on Windows and eSpeak on Linux. On Debian/Ubuntu:

```bash
sudo apt install espeak-ng
```

Speech features are optional. Beep-only cue insertion works without speech support.

## Running

From the project folder:

```bash
python -m swimtrack_manager
```

or:

```bash
python run.py
```

## Typical workflow

1. Connect the headphones by USB.
2. Open SwimTrack Manager.
3. Select the mounted headphone drive.
4. Click **Load Device**. Other tabs remain disabled until this succeeds.
5. Add or reorder tracks.
6. Mark any tracks that should receive cue processing using **Mark/Unmark Audio Processing** in the Library tab.
7. Choose an audio cue preset or configure the **Beeps** and **Voice** subtabs manually.
8. Mark any tracks that should use rename rules with **Mark/Unmark Batch Rename**, then configure the **Batch Rename** tab.
9. Mark any quiet tracks with **Mark/Unmark Volume Change**, then configure **Audio Cues → Audio Volume**.
10. Open the **Preview** tab to generate a temporary processed preview, inspect the cue timeline, scrub playback and test different playback speeds.
11. Review the sync preview. It refreshes automatically; use **Refresh Preview** if you want to force a rebuild.
12. Check the current-operation bar, transfer speed readouts and the **Live activity** log in the **Sync Preview** tab while syncing.
13. Check the full **Log** tab if anything looks wrong. The log records what the app scanned, planned and wrote.
14. Click **Apply Sync**.
15. When the completion dialog appears, choose **Eject device** or **Continue editing**. The app does not eject automatically.

## Batch rename

The Batch Rename tab lets you prepare rename rules without immediately changing files.
Select tracks in the Library tab and click **Mark/Unmark Batch Rename**, or use the Batch
Rename tab to mark all active tracks. Rename rules include:

- Prefix and suffix.
- Search and replace.
- Case conversion: unchanged, lower, upper or title case.
- Optional extra sequence number as a prefix, suffix or full title replacement.
- Start number, increment and zero-padding.
- Custom number separator.
- Option to base the rename on the original source filename instead of the current Title column.
- Option to remove the original title completely, generating names only from prefix/suffix/sequence rules.

The headphone playback-order prefix is still added separately during sync. For example,
a batch title of `01 - Warmup` can become `001 - 01 - Warmup.mp3` on the device. If a track is also marked for audio processing, voice title cues use the generated batch title before the audio file is processed, so the spoken name matches the final device name apart from the numeric playback prefix.

## Preview tab

The Preview tab lists tracks marked for any processing action: audio cues, batch rename or volume change. Select a marked track and click **Generate Preview** to create a temporary rendered file using the same settings that sync will use. The tab shows:

- a timeline of planned insertions and processing events,
- voice intro/indicator positions,
- beep positions,
- volume boost events,
- batch rename result,
- playback controls,
- scrub slider,
- playback speed options from 0.5x to 2.0x.

Preview files are temporary and are removed when the app closes.

## Volume change marks

Use **Mark/Unmark Volume Change** in the Library tab to queue volume boosting for selected tracks. The actual boost is controlled in **Audio Cues → Audio Volume** and is only applied during **Apply Sync**. This is separate from cue processing, so you can make a quiet music file louder without adding beeps or speech.

## Custom presets

Use the preset controls in the Audio Cues tab to save named custom presets. A custom preset stores the current Beeps/Voice/Audio Volume/Volume Mix settings and the current Batch Rename rules, including whether original titles should be removed. Select `Custom: Your Name` and click **Apply Preset** to restore it later.

## Audio processing marks

The Audio Cues tab has a master processing switch, but processing only runs on tracks
marked **Audio** in the Library tab. This prevents accidentally re-encoding every file
when you only want cues on podcasts, audiobooks or training tracks. If tracks are marked
for audio processing while the master switch is off, the Sync Preview tab shows a warning.

## Linux device detection

The headphones must be mounted before the app can load them. In the Device tab,
click **Find Drives**. The app searches common Linux mount locations such as:

- `/media/$USER/...`
- `/run/media/$USER/...`
- `/mnt/...`

If the list is empty, open your file manager and click the headphones/USB device
once to mount it, then click **Find Drives** again, then click **Load Device**. You can also check the mount
path manually with:

```bash
lsblk -f
findmnt
```

Paste the mounted directory path into the app's **Path** field, not the raw block
device path such as `/dev/sdb1`.

## Packaging into a desktop executable

PyInstaller can be used to create a standalone app. Build separately on each target OS:

```bash
python -m pip install pyinstaller
pyinstaller SwimTrackManager.spec
```

The output will be created in the `dist/` folder.

## Notes and limitations

- The app does not automatically format devices. This is intentional to avoid accidental data loss.
- Cue insertion can take time because each marked file is decoded and re-encoded with FFmpeg.
- APE/FLAC/WAV files can be imported if FFmpeg can decode them, but processed output is MP3 by default.
- Some low-cost players sort files differently. This app uses conservative numeric prefixes such as `001 - Track Title.mp3`.
- The **Sync Preview** tab now includes a compact **Live activity** log directly under the progress bar, so long FFmpeg/sync tasks show granular activity without switching tabs.
- If the sync preview does not look right, open the full **Log** tab and click **Refresh Preview**. The preview builder logs the operation count and any traceback.
- If speech generation fails, switch to a beep-only preset or install a compatible system speech engine.
- Voice indicators can be slower to generate than beeps because each spoken marker must be synthesized and mixed into the file.
- The audio engine now uses FFmpeg directly rather than pydub, so Python 3.13 does not require `audioop` or `pyaudioop`.

## Project layout

```text
swimtrack_manager/
  __main__.py
  app.py
  audio.py
  device.py
  models.py
  presets.py
  settings.py
  sync.py
  utils.py
run.py
requirements.txt
SwimTrackManager.spec
```

### Volume note

Version note: older builds could make processed MP3s sound quieter because FFmpeg's `amix` filter normalised the original track together with silent/delayed cue tracks. This build disables that mix attenuation by default. Use **Audio Cues → Audio Volume** for marked-track boosts. In **Audio Cues → Volume / Mix** you can also:

- keep or allow mix attenuation,
- boost or reduce the original track gain,
- add final output gain,
- keep a limiter enabled to reduce clipping risk after boosts.

For the Aztine-style swimming headphones, try **Final output gain +3 dB to +6 dB** if the files are still too quiet.
