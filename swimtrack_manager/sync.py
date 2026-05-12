from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .audio import copy_or_convert_track, output_extension_for, process_track
from .models import BatchRenameSettings, CueSettings, Track, format_bytes
from .utils import numbered_folder, numbered_name, sanitize_filename


@dataclass
class SyncOperation:
    action: str
    source: Path | None
    destination: Path | None
    description: str
    track: Track | None = None
    final_title: str | None = None
    playback_order: int | None = None


@dataclass
class SyncStats:
    bytes_transferred: int = 0
    elapsed_seconds: float = 0.0
    average_bps: float = 0.0
    peak_bps: float = 0.0


@dataclass
class SyncPlan:
    device_path: Path
    operations: list[SyncOperation]

    @property
    def summary(self) -> str:
        if not self.operations:
            return "No changes planned."
        counts: dict[str, int] = {}
        for op in self.operations:
            counts[op.action] = counts.get(op.action, 0) + 1
        labels = {
            "copy": "copy/rename",
            "process": "audio process",
            "delete": "delete",
            "delete_old": "replace/delete old",
        }
        return ", ".join(f"{count} {labels.get(action, action)}" for action, count in sorted(counts.items()))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def track_requires_processing(settings: CueSettings, track: Track) -> bool:
    """True when sync must create a processed audio file rather than a direct copy."""
    cue_processing = bool(getattr(settings, "enabled", False) and getattr(track, "audio_processing", False))
    volume_processing = bool(getattr(settings, "volume_enabled", False) and getattr(track, "volume_change", False))
    return cue_processing or volume_processing


def _format_rate(bytes_per_second: float) -> str:
    return f"{format_bytes(int(max(0, bytes_per_second)))}/s"


def apply_batch_rename_title(track: Track, settings: BatchRenameSettings, sequence_index: int) -> str:
    """Return the title that will be used for a track marked for batch rename.

    The physical file rename is still applied later by numbered_name(), so this title is
    only the clean title portion after the required playback-order prefix.
    """
    base = "" if settings.remove_original_title else (track.source_path.stem if settings.use_source_filename else track.title)
    base = sanitize_filename(base, fallback="")

    if settings.search_text and base:
        base = base.replace(settings.search_text, settings.replace_text)

    case_mode = (settings.case_mode or "unchanged").lower()
    if case_mode == "lower":
        base = base.lower()
    elif case_mode == "upper":
        base = base.upper()
    elif case_mode == "title":
        base = base.title()

    number = settings.start_number + (max(sequence_index, 1) - 1) * max(settings.increment, 1)
    token = str(number).zfill(max(settings.padding, 1))
    sep = settings.separator
    mode = (settings.numbering_mode or "none").lower()

    if mode == "replace":
        renamed = token
    elif mode == "prefix":
        renamed = f"{token}{sep}{base}" if base else token
    elif mode == "suffix":
        renamed = f"{base}{sep}{token}" if base else token
    else:
        renamed = base

    renamed = f"{settings.prefix}{renamed}{settings.suffix}"
    fallback = token if settings.remove_original_title else "Untitled"
    return sanitize_filename(renamed, fallback=fallback)


def planned_destination_for_track(
    root: Path,
    track: Track,
    file_order: int,
    settings: CueSettings,
    rename_settings: BatchRenameSettings | None,
    rename_sequence: int,
) -> Path:
    ext = output_extension_for(settings, track.source_path.suffix, track_requires_processing(settings, track))
    folder_path = root
    if track.folder.strip():
        folder_name = numbered_folder(track.folder_order, track.folder)
        folder_path = root / folder_name
    final_title = track.title
    if track.batch_rename and rename_settings is not None:
        final_title = apply_batch_rename_title(track, rename_settings, rename_sequence)
    file_name = numbered_name(file_order, final_title, ext)
    return folder_path / file_name


def build_sync_plan(
    device_path: Path,
    tracks: list[Track],
    settings: CueSettings,
    rename_settings: BatchRenameSettings | None = None,
) -> SyncPlan:
    root = Path(device_path).resolve()
    operations: list[SyncOperation] = []

    active_tracks = sorted(
        [t for t in tracks if not t.remove_from_device],
        key=lambda t: (t.folder_order if t.folder.strip() else 0, t.folder.lower(), t.order, t.title.lower()),
    )
    per_folder_counter: dict[str, int] = {}
    rename_sequence = 0

    for track in active_tracks:
        folder_key = "__root__"
        if track.folder.strip():
            folder_key = numbered_folder(track.folder_order, track.folder)
        per_folder_counter[folder_key] = per_folder_counter.get(folder_key, 0) + 1
        file_order = per_folder_counter[folder_key]
        if track.batch_rename:
            rename_sequence += 1
        final_title = track.title
        if track.batch_rename and rename_settings is not None:
            # Apply the pending rename title first, before audio processing is planned.
            # This means spoken title cues read the exact name that will be written to the device.
            final_title = apply_batch_rename_title(track, rename_settings, rename_sequence)
        effective_track = replace(track, title=final_title)

        destination = planned_destination_for_track(
            root,
            track,
            file_order,
            settings,
            rename_settings,
            rename_sequence,
        )

        source_resolved = track.source_path.resolve()
        destination_resolved = destination.resolve()
        same_path = source_resolved == destination_resolved
        should_process = track_requires_processing(settings, track)

        if same_path and not should_process:
            continue

        action = "process" if should_process else "copy"
        rel_dest = destination.relative_to(root)
        extra = []
        if track.batch_rename:
            extra.append("batch rename")
        if settings.enabled and track.audio_processing:
            extra.append("audio processing")
        if settings.volume_enabled and getattr(track, "volume_change", False):
            extra.append("volume change")
        suffix = f" ({', '.join(extra)})" if extra else ""
        operations.append(
            SyncOperation(
                action=action,
                source=track.source_path,
                destination=destination,
                description=f"{action.title()} {track.source_path.name} → {rel_dest}{suffix}",
                track=effective_track,
                final_title=final_title,
                playback_order=file_order,
            )
        )

        # Existing files on the device must be removed after the staged replacement is ready.
        if track.on_device and _is_within(track.source_path, root):
            if should_process or not same_path:
                operations.append(
                    SyncOperation(
                        action="delete_old",
                        source=track.source_path,
                        destination=None,
                        description=f"Remove old copy {track.source_path.relative_to(root)}",
                        track=effective_track,
                        final_title=final_title,
                        playback_order=file_order,
                    )
                )

    for track in tracks:
        if track.remove_from_device and track.on_device:
            operations.append(
                SyncOperation(
                    action="delete",
                    source=track.source_path,
                    destination=None,
                    description=f"Delete {track.source_path.relative_to(root) if _is_within(track.source_path, root) else track.source_path.name}",
                    track=track,
                )
            )
    return SyncPlan(root, operations)


def sync_progress_total(plan: SyncPlan) -> int:
    """Return the number of major sync steps used by the progress bar."""
    write_ops = [o for o in plan.operations if o.action in {"copy", "process"}]
    delete_ops = [o for o in plan.operations if o.action in {"delete", "delete_old"}]
    unique_deletes: set[Path] = set()
    for op in delete_ops:
        if op.source:
            unique_deletes.add(op.source.resolve())
    # stage writes + deletes + final writes + cleanup/completion step
    return max(1, len(write_ops) + len(unique_deletes) + len(write_ops) + 1)


def apply_sync_plan(
    plan: SyncPlan,
    settings: CueSettings,
    progress: Callable[[str], None] | None = None,
    progress_step: Callable[[int, int, str], None] | None = None,
    transfer_update: Callable[[float, float, float, int, str], None] | None = None,
) -> SyncStats:
    root = plan.device_path
    write_ops = [o for o in plan.operations if o.action in {"copy", "process"}]
    delete_ops = [o for o in plan.operations if o.action in {"delete", "delete_old"}]

    tmp_root = root / f".swimtrack_tmp_{int(time.time())}"
    staged: list[tuple[SyncOperation, Path]] = []
    total_steps = sync_progress_total(plan)
    completed_steps = 0
    sync_start = time.monotonic()
    bytes_transferred = 0
    peak_bps = 0.0

    def mark_step(message: str):
        nonlocal completed_steps
        completed_steps = min(completed_steps + 1, total_steps)
        if progress_step:
            progress_step(completed_steps, total_steps, message)

    def record_transfer(delta_bytes: int, elapsed: float, message: str):
        nonlocal bytes_transferred, peak_bps
        delta_bytes = max(0, int(delta_bytes))
        bytes_transferred += delta_bytes
        instant_bps = delta_bytes / max(elapsed, 0.001)
        peak_bps = max(peak_bps, instant_bps)
        total_elapsed = max(time.monotonic() - sync_start, 0.001)
        average_bps = bytes_transferred / total_elapsed
        if progress:
            progress(
                f"Transfer update: {message}; +{format_bytes(delta_bytes)}; "
                f"current {_format_rate(instant_bps)}, average {_format_rate(average_bps)}, peak {_format_rate(peak_bps)}."
            )
        if transfer_update:
            transfer_update(instant_bps, average_bps, peak_bps, bytes_transferred, message)

    try:
        if progress_step:
            progress_step(0, total_steps, "Preparing sync")
        if transfer_update:
            transfer_update(0.0, 0.0, 0.0, 0, "Preparing sync")
        if progress:
            progress(f"Sync started: {len(write_ops)} write operation(s), {len(delete_ops)} delete operation(s).")
            progress("All pending marks are applied only now: removal, batch rename, volume change, and audio processing.")
        if write_ops:
            tmp_root.mkdir(parents=True, exist_ok=False)
            if progress:
                progress(f"Created temporary staging folder: {tmp_root.name}")

        # Stage all new files first. This protects against order-swap overwrites.
        for idx, op in enumerate(write_ops, start=1):
            if not op.track or not op.source or not op.destination:
                continue
            rel_dest = op.destination.relative_to(root)
            tmp_dest = tmp_root / rel_dest
            tmp_dest.parent.mkdir(parents=True, exist_ok=True)
            message = f"Staging {idx}/{len(write_ops)}: {rel_dest}"
            if progress:
                progress(message)
                progress(f"Source: {op.source}")
                progress(f"Action: {op.action}; destination after sync: {rel_dest}")
            if progress_step:
                progress_step(completed_steps, total_steps, message)
            file_start = time.monotonic()
            if op.action == "process":
                spoken_track_number = op.playback_order or idx
                if progress and op.final_title:
                    progress(f"Audio cue title prepared after rename: {op.final_title}")
                    progress(f"Voice track number will use playback order: {spoken_track_number}")
                process_track(op.track, tmp_dest, settings, spoken_track_number, progress=progress)
            else:
                force_mp3 = tmp_dest.suffix.lower() == ".mp3" and op.track.source_path.suffix.lower() != ".mp3"
                copy_or_convert_track(op.track, tmp_dest, force_mp3=force_mp3, progress=progress)
            staged.append((op, tmp_dest))
            staged_bytes = tmp_dest.stat().st_size if tmp_dest.exists() else 0
            record_transfer(staged_bytes, time.monotonic() - file_start, f"Staged {rel_dest}")
            mark_step(f"Staged {idx}/{len(write_ops)}: {rel_dest}")

        # Once staging succeeds, remove old files and user-requested deletions.
        seen_deletes: set[Path] = set()
        if progress and delete_ops:
            progress("Removing old/requested files after staging succeeded.")
        for op in delete_ops:
            if not op.source:
                continue
            src = op.source.resolve()
            if src in seen_deletes:
                if progress:
                    progress(f"Skipping duplicate delete request: {op.source}")
                continue
            seen_deletes.add(src)
            if op.source.exists():
                if progress:
                    progress(op.description)
                if progress_step:
                    progress_step(completed_steps, total_steps, op.description)
                op.source.unlink()
                mark_step(f"Deleted: {op.source.name}")
            elif progress:
                progress(f"Delete skipped because file no longer exists: {op.source}")

        # Move staged files to final destinations, replacing existing paths if needed.
        if progress and staged:
            progress("Moving staged files to final device paths.")
        for op, tmp_dest in staged:
            if not op.destination:
                continue
            final_dest = op.destination
            final_dest.parent.mkdir(parents=True, exist_ok=True)
            if final_dest.exists():
                if progress:
                    progress(f"Replacing existing final file: {final_dest.relative_to(root)}")
                final_dest.unlink()
            message = f"Writing {final_dest.relative_to(root)}"
            if progress:
                progress(message)
            if progress_step:
                progress_step(completed_steps, total_steps, message)
            shutil.move(str(tmp_dest), str(final_dest))
            mark_step(f"Wrote {final_dest.relative_to(root)}")
        if progress:
            progress("Sync file operations completed.")
        mark_step("Sync file operations completed")
    finally:
        if tmp_root.exists():
            shutil.rmtree(tmp_root, ignore_errors=True)
            if progress:
                progress("Cleaned up temporary staging folder.")
        removed_dirs = _remove_empty_dirs(root)
        if progress and removed_dirs:
            progress(f"Removed {removed_dirs} empty folder(s).")

    elapsed = max(time.monotonic() - sync_start, 0.001)
    average_bps = bytes_transferred / elapsed
    stats = SyncStats(bytes_transferred=bytes_transferred, elapsed_seconds=elapsed, average_bps=average_bps, peak_bps=peak_bps)
    if transfer_update:
        transfer_update(0.0, average_bps, peak_bps, bytes_transferred, "Sync complete")
    if progress:
        progress(
            f"Sync transfer summary: {format_bytes(bytes_transferred)} in {elapsed:.1f}s; "
            f"average {_format_rate(average_bps)}, peak {_format_rate(peak_bps)}."
        )
    return stats

def _remove_empty_dirs(root: Path) -> int:
    # Avoid deleting the root itself; remove only empty numbered subfolders left by reordering/deleting.
    removed = 0
    for path in sorted([p for p in root.rglob("*") if p.is_dir()], key=lambda p: len(p.parts), reverse=True):
        if path.name.startswith(".swimtrack_tmp_"):
            continue
        try:
            path.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


def dry_run_text(plan: SyncPlan) -> str:
    lines = [f"Sync preview: {plan.summary}", ""]
    if not plan.operations:
        return "No changes planned."
    for op in plan.operations:
        lines.append(f"- {op.description}")
    return "\n".join(lines)
