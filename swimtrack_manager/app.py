from __future__ import annotations

import shutil
import subprocess
import tempfile
import traceback
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot, QUrl, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

    HAS_MULTIMEDIA = True
except Exception:  # pragma: no cover - depends on PySide6 multimedia packaging
    QAudioOutput = None
    QMediaPlayer = None
    HAS_MULTIMEDIA = False

from .audio import build_cue_timeline, duration_seconds, format_time, output_extension_for, process_track
from .device import backup_device, discover_candidate_devices, find_likely_factory_tracks, get_device_info, scan_device
from .models import BatchRenameSettings, CueSettings, Track, format_bytes
from .presets import make_preset, preset_names
from .settings import cue_from_dict, cue_to_dict, load_settings, rename_from_dict, rename_to_dict, save_settings
from .sync import (
    SyncPlan,
    SyncStats,
    apply_batch_rename_title,
    apply_sync_plan,
    build_sync_plan,
    dry_run_text,
    planned_destination_for_track,
    sync_progress_total,
    track_requires_processing,
)
from .utils import likely_factory_demo, numbered_folder, sanitize_filename


class ScanWorker(QObject):
    log = Signal(str)
    finished = Signal(object, object)
    error = Signal(str)

    def __init__(self, device_path: Path):
        super().__init__()
        self.device_path = device_path

    @Slot()
    def run(self):
        try:
            self.log.emit("Reading device information...")
            info = get_device_info(self.device_path)
            self.log.emit("Scanning audio files...")
            tracks = scan_device(self.device_path, duration_func=duration_seconds, progress=self.log.emit)
            self.finished.emit(info, tracks)
        except Exception:
            self.error.emit(traceback.format_exc())


class SyncWorker(QObject):
    log = Signal(str)
    progress_update = Signal(int, int, str)
    transfer_update = Signal(float, float, float, int, str)
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, plan: SyncPlan, settings: CueSettings):
        super().__init__()
        self.plan = plan
        self.settings = settings

    @Slot()
    def run(self):
        try:
            stats = apply_sync_plan(
                self.plan,
                self.settings,
                progress=self.log.emit,
                progress_step=self.progress_update.emit,
                transfer_update=self.transfer_update.emit,
            )
            self.finished.emit(stats)
        except Exception:
            self.error.emit(traceback.format_exc())


class BackupWorker(QObject):
    log = Signal(str)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, device_path: Path, zip_path: Path):
        super().__init__()
        self.device_path = device_path
        self.zip_path = zip_path

    @Slot()
    def run(self):
        try:
            result = backup_device(self.device_path, self.zip_path, progress=self.log.emit)
            self.finished.emit(str(result))
        except Exception:
            self.error.emit(traceback.format_exc())


class PreviewWorker(QObject):
    log = Signal(str)
    finished = Signal(str, object, str)
    error = Signal(str)

    def __init__(self, track: Track, settings: CueSettings, track_number: int, output_path: Path):
        super().__init__()
        self.track = track
        self.settings = settings
        self.track_number = track_number
        self.output_path = output_path

    @Slot()
    def run(self):
        try:
            timeline = build_cue_timeline(self.track, self.settings, self.track_number)
            if track_requires_processing(self.settings, self.track):
                self.log.emit(f"Generating playable preview for {self.track.title}...")
                path = process_track(self.track, self.output_path, self.settings, self.track_number, progress=self.log.emit)
            else:
                self.log.emit(f"No audio rendering needed for {self.track.title}; using source audio for preview.")
                path = self.track.source_path
            self.finished.emit(str(path), timeline, self.track.title)
        except Exception:
            self.error.emit(traceback.format_exc())


class MainWindow(QMainWindow):
    C_ORDER = 0
    C_FOLDER_ORDER = 1
    C_FOLDER = 2
    C_TITLE = 3
    C_AUDIO = 4
    C_RENAME = 5
    C_VOLUME = 6
    C_DURATION = 7
    C_FORMAT = 8
    C_STATUS = 9
    C_SOURCE = 10

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SwimTrack Manager")
        self.resize(1320, 820)

        self.device_path: Path | None = None
        self.device_info = None
        self.device_loaded = False
        self.custom_presets: dict[str, dict] = {}
        self.tracks: list[Track] = []
        self.cue_settings = CueSettings()
        self.rename_settings = BatchRenameSettings()
        self._loading_table = False
        self._worker_thread: QThread | None = None
        self._worker: QObject | None = None
        self._inline_log_lines: list[str] = []
        self._inline_log_limit = 800
        self._preview_temp_dir = Path(tempfile.mkdtemp(prefix="swimtrack_preview_"))
        self._preview_current_path: Path | None = None
        self._preview_slider_dragging = False

        self._load_persisted_settings()
        self._build_ui()
        self._connect_actions()
        self.refresh_table()
        self.refresh_rename_preview(log_event=False)
        self.refresh_preview(log_event=False)
        self.refresh_device_candidates(silent=True)

    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)

        self.gate_label = QLabel("Step 1: Load a suitable headphone device to enable the workflow tabs.")
        self.gate_label.setWordWrap(True)
        root.addWidget(self.gate_label)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_device_tab(), "Device")
        self.tabs.addTab(self._build_library_tab(), "Library")
        self.tabs.addTab(self._build_batch_rename_tab(), "Batch Rename")
        self.tabs.addTab(self._build_cues_tab(), "Audio Cues")
        self.tabs.addTab(self._build_processing_preview_tab(), "Preview")
        self.tabs.addTab(self._build_preview_tab(), "Sync Preview")
        self.tabs.addTab(self._build_log_tab(), "Log")
        root.addWidget(self.tabs, 1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready")
        self.set_device_loaded(False)

        file_menu = self.menuBar().addMenu("File")
        action_exit = QAction("Exit", self)
        action_exit.triggered.connect(self.close)
        file_menu.addAction(action_exit)

    def _build_device_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        selector = QGroupBox("Headphone device")
        selector_layout = QGridLayout(selector)
        self.device_path_edit = QLineEdit()
        self.device_path_edit.setPlaceholderText("Select the mounted headphone drive/folder")
        self.browse_device_button = QPushButton("Browse...")
        self.refresh_devices_button = QPushButton("Find Drives")
        self.scan_button = QPushButton("Load Device")
        self.candidate_combo = QComboBox()
        self.candidate_combo.setMinimumWidth(420)
        selector_layout.addWidget(QLabel("Path"), 0, 0)
        selector_layout.addWidget(self.device_path_edit, 0, 1)
        selector_layout.addWidget(self.browse_device_button, 0, 2)
        selector_layout.addWidget(self.refresh_devices_button, 0, 3)
        selector_layout.addWidget(self.scan_button, 0, 4)
        selector_layout.addWidget(QLabel("Detected"), 1, 0)
        selector_layout.addWidget(self.candidate_combo, 1, 1, 1, 4)
        layout.addWidget(selector)

        info = QGroupBox("Device status")
        info_layout = QFormLayout(info)
        self.loaded_label = QLabel("Not loaded")
        self.fs_label = QLabel("Unknown")
        self.storage_label = QLabel("Unknown")
        self.warning_label = QLabel("Select and load a device.")
        self.warning_label.setWordWrap(True)
        info_layout.addRow("Loaded", self.loaded_label)
        info_layout.addRow("Filesystem", self.fs_label)
        info_layout.addRow("Storage", self.storage_label)
        info_layout.addRow("Warning", self.warning_label)
        layout.addWidget(info)

        actions = QGroupBox("Device actions")
        actions_layout = QHBoxLayout(actions)
        self.backup_button = QPushButton("Backup Device to ZIP")
        self.mark_factory_button = QPushButton("Mark Factory/Test Music for Removal")
        self.clear_removals_button = QPushButton("Clear Removal Marks")
        actions_layout.addWidget(self.backup_button)
        actions_layout.addWidget(self.mark_factory_button)
        actions_layout.addWidget(self.clear_removals_button)
        actions_layout.addStretch(1)
        layout.addWidget(actions)

        help_text = QLabel(
            "For the Aztine test headphones: use MP3 mode underwater, keep the drive FAT32, "
            "and use numeric prefixes for ordered playback. This app does not format devices. "
            "On Linux, the headphones must be mounted first. Common paths are /media/$USER, "
            "/run/media/$USER, and /mnt."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        layout.addStretch(1)
        return tab

    def _build_library_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        controls = QHBoxLayout()
        self.add_files_button = QPushButton("Add Audio Files")
        self.toggle_remove_button = QPushButton("Mark/Unmark Remove")
        self.toggle_audio_button = QPushButton("Mark/Unmark Audio Processing")
        self.toggle_batch_button = QPushButton("Mark/Unmark Batch Rename")
        self.toggle_volume_button = QPushButton("Mark/Unmark Volume Change")
        self.up_button = QPushButton("Move Up")
        self.down_button = QPushButton("Move Down")
        self.set_folder_button = QPushButton("Set Folder")
        self.renumber_button = QPushButton("Renumber")
        for widget in [
            self.add_files_button,
            self.toggle_remove_button,
            self.toggle_audio_button,
            self.toggle_batch_button,
            self.toggle_volume_button,
            self.up_button,
            self.down_button,
            self.set_folder_button,
            self.renumber_button,
        ]:
            controls.addWidget(widget)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels(
            ["Order", "Folder #", "Folder", "Title", "Audio", "Rename", "Volume", "Duration", "Format", "Status", "Source"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(self.C_TITLE, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(self.C_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(self.C_SOURCE, QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, 1)

        note = QLabel(
            "Edit Title, Folder, or Folder # directly in the table. Physical file changes are not written until Apply Sync. "
            "Use Mark/Unmark buttons to queue cue processing, batch renaming, volume changes, or removal."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        return tab


    def _build_batch_rename_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        actions = QHBoxLayout()
        self.rename_mark_selected_button = QPushButton("Mark/Unmark Selected Library Tracks")
        self.rename_mark_all_button = QPushButton("Mark All Active Tracks")
        self.rename_clear_all_button = QPushButton("Unmark All Rename Marks")
        self.rename_refresh_preview_button = QPushButton("Refresh Rename Preview")
        for widget in [
            self.rename_mark_selected_button,
            self.rename_mark_all_button,
            self.rename_clear_all_button,
            self.rename_refresh_preview_button,
        ]:
            actions.addWidget(widget)
        actions.addStretch(1)
        layout.addLayout(actions)

        rules = QGroupBox("Batch rename rules applied during sync")
        form = QFormLayout(rules)
        self.rename_prefix_edit = QLineEdit()
        self.rename_suffix_edit = QLineEdit()
        self.rename_search_edit = QLineEdit()
        self.rename_replace_edit = QLineEdit()
        self.rename_case_combo = QComboBox()
        self.rename_case_combo.addItems(["unchanged", "lower", "upper", "title"])
        self.rename_numbering_combo = QComboBox()
        self.rename_numbering_combo.addItems(["none", "prefix", "suffix", "replace"])
        self.rename_start_spin = QSpinBox()
        self.rename_start_spin.setRange(0, 999999)
        self.rename_start_spin.setValue(1)
        self.rename_increment_spin = QSpinBox()
        self.rename_increment_spin.setRange(1, 9999)
        self.rename_increment_spin.setValue(1)
        self.rename_padding_spin = QSpinBox()
        self.rename_padding_spin.setRange(1, 8)
        self.rename_padding_spin.setValue(2)
        self.rename_separator_edit = QLineEdit(" - ")
        self.rename_use_source_check = QCheckBox("Use original source filename as base instead of current Title column")
        self.rename_remove_original_check = QCheckBox("Remove the original title completely")

        form.addRow("Prefix", self.rename_prefix_edit)
        form.addRow("Suffix", self.rename_suffix_edit)
        form.addRow("Search text", self.rename_search_edit)
        form.addRow("Replace with", self.rename_replace_edit)
        form.addRow("Case transform", self.rename_case_combo)
        form.addRow("Extra sequence number", self.rename_numbering_combo)
        form.addRow("Start number", self.rename_start_spin)
        form.addRow("Increment", self.rename_increment_spin)
        form.addRow("Number padding", self.rename_padding_spin)
        form.addRow("Number separator", self.rename_separator_edit)
        form.addRow("Base title", self.rename_use_source_check)
        form.addRow("Original title", self.rename_remove_original_check)
        layout.addWidget(rules)

        self.rename_preview_text = QTextEdit()
        self.rename_preview_text.setReadOnly(True)
        self.rename_preview_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.rename_preview_text, 1)

        note = QLabel(
            "Batch rename marks and rules only affect the sync plan. The Library title column is not permanently changed here. "
            "The headphone playback-order prefix is still added separately, so a rename preview title may become a final file like '001 - 01 - Warmup.mp3'. "
            "Enable 'Remove the original title completely' to generate names only from prefix/suffix/sequence rules."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.populate_rename_controls()
        return tab

    def _build_cues_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        preset_box = QGroupBox("Preset")
        preset_layout = QHBoxLayout(preset_box)
        self.preset_combo = QComboBox()
        self.apply_preset_button = QPushButton("Apply Preset")
        self.custom_preset_name_edit = QLineEdit()
        self.custom_preset_name_edit.setPlaceholderText("Custom preset name")
        self.save_preset_button = QPushButton("Save Custom Preset")
        self.delete_preset_button = QPushButton("Delete Custom Preset")
        preset_layout.addWidget(QLabel("Preset"))
        preset_layout.addWidget(self.preset_combo)
        preset_layout.addWidget(self.apply_preset_button)
        preset_layout.addWidget(QLabel("Name"))
        preset_layout.addWidget(self.custom_preset_name_edit)
        preset_layout.addWidget(self.save_preset_button)
        preset_layout.addWidget(self.delete_preset_button)
        preset_layout.addStretch(1)
        layout.addWidget(preset_box)

        general = QGroupBox("Processing master switch")
        general_layout = QFormLayout(general)
        self.process_enabled_check = QCheckBox("Enable audio processing for tracks marked 'Audio'")
        self.output_format_combo = QComboBox()
        self.output_format_combo.addItems(["mp3", "wav", "flac"])
        general_layout.addRow("Processing", self.process_enabled_check)
        general_layout.addRow("Output format", self.output_format_combo)
        layout.addWidget(general)

        cue_tabs = QTabWidget()
        cue_tabs.addTab(self._build_beep_settings_tab(), "Beeps")
        cue_tabs.addTab(self._build_voice_settings_tab(), "Voice")
        cue_tabs.addTab(self._build_audio_volume_tab(), "Audio Volume")
        cue_tabs.addTab(self._build_volume_settings_tab(), "Volume / Mix")
        layout.addWidget(cue_tabs, 1)

        explanation = QLabel(
            "Cue settings are only applied to tracks marked for Audio Processing. Beep and voice options are separated, "
            "but they can be combined. For underwater use, short beep cues are often clearer than speech."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.populate_cue_controls()
        self.refresh_preset_combo()
        return tab

    def _build_beep_settings_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.beep_enabled_check = QCheckBox("Add beep cues")
        self.percentages_edit = QLineEdit("25,50,75")
        self.every_minutes_spin = QSpinBox()
        self.every_minutes_spin.setRange(0, 120)
        self.every_minutes_spin.setSuffix(" min")
        self.beep_start_check = QCheckBox("Beep at start")
        self.beep_end_check = QCheckBox("Beep near end")
        self.beep_frequency_spin = QSpinBox()
        self.beep_frequency_spin.setRange(100, 4000)
        self.beep_frequency_spin.setSuffix(" Hz")
        self.beep_frequency_spin.setValue(880)
        self.beep_duration_spin = QSpinBox()
        self.beep_duration_spin.setRange(50, 5000)
        self.beep_duration_spin.setSuffix(" ms")
        self.beep_duration_spin.setValue(350)
        self.beep_volume_spin = QSpinBox()
        self.beep_volume_spin.setRange(-60, 12)
        self.beep_volume_spin.setSuffix(" dB")
        self.beep_volume_spin.setValue(-7)
        self.beep_repeat_spin = QSpinBox()
        self.beep_repeat_spin.setRange(1, 10)
        self.beep_gap_spin = QSpinBox()
        self.beep_gap_spin.setRange(0, 5000)
        self.beep_gap_spin.setSuffix(" ms")
        form.addRow("Beeps", self.beep_enabled_check)
        form.addRow("Percentage markers", self.percentages_edit)
        form.addRow("Repeat every", self.every_minutes_spin)
        form.addRow("Start marker", self.beep_start_check)
        form.addRow("End marker", self.beep_end_check)
        form.addRow("Frequency", self.beep_frequency_spin)
        form.addRow("Pulse duration", self.beep_duration_spin)
        form.addRow("Volume", self.beep_volume_spin)
        form.addRow("Pulses per marker", self.beep_repeat_spin)
        form.addRow("Gap between pulses", self.beep_gap_spin)
        return tab

    def _build_voice_settings_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.voice_title_check = QCheckBox("Speak track title before track")
        self.voice_track_check = QCheckBox("Speak track number before track")
        self.voice_folder_check = QCheckBox("Speak folder name before track")
        self.voice_progress_check = QCheckBox("Add spoken indicator cues")
        self.progress_percentages_edit = QLineEdit("50")
        self.voice_every_minutes_spin = QSpinBox()
        self.voice_every_minutes_spin.setRange(0, 120)
        self.voice_every_minutes_spin.setSuffix(" min")
        self.voice_start_check = QCheckBox("Speak progress at start")
        self.voice_end_check = QCheckBox("Speak progress near end")
        self.progress_style_combo = QComboBox()
        self.progress_style_combo.addItems(["percentage", "elapsed", "remaining", "elapsed_remaining", "timecode"])
        self.voice_custom_intro_edit = QLineEdit()
        self.voice_custom_outro_edit = QLineEdit()
        self.voice_indicator_prefix_edit = QLineEdit()
        self.voice_indicator_suffix_edit = QLineEdit()
        self.speech_rate_spin = QSpinBox()
        self.speech_rate_spin.setRange(80, 260)
        self.speech_rate_spin.setValue(165)
        self.speech_volume_spin = QDoubleSpinBox()
        self.speech_volume_spin.setRange(0.0, 1.0)
        self.speech_volume_spin.setSingleStep(0.05)
        self.speech_volume_spin.setDecimals(2)
        self.speech_volume_spin.setValue(1.0)
        self.intro_silence_spin = QSpinBox()
        self.intro_silence_spin.setRange(0, 10000)
        self.intro_silence_spin.setSuffix(" ms")
        self.post_intro_silence_spin = QSpinBox()
        self.post_intro_silence_spin.setRange(0, 10000)
        self.post_intro_silence_spin.setSuffix(" ms")
        form.addRow("Voice title", self.voice_title_check)
        form.addRow("Voice track number", self.voice_track_check)
        form.addRow("Voice folder", self.voice_folder_check)
        form.addRow("Voice indicators", self.voice_progress_check)
        form.addRow("Percentage markers", self.progress_percentages_edit)
        form.addRow("Repeat every", self.voice_every_minutes_spin)
        form.addRow("Start marker", self.voice_start_check)
        form.addRow("End marker", self.voice_end_check)
        form.addRow("Progress wording", self.progress_style_combo)
        form.addRow("Custom intro text", self.voice_custom_intro_edit)
        form.addRow("Custom outro text", self.voice_custom_outro_edit)
        form.addRow("Indicator prefix", self.voice_indicator_prefix_edit)
        form.addRow("Indicator suffix", self.voice_indicator_suffix_edit)
        form.addRow("Speech rate", self.speech_rate_spin)
        form.addRow("Speech volume", self.speech_volume_spin)
        form.addRow("Silence before intro", self.intro_silence_spin)
        form.addRow("Silence after intro", self.post_intro_silence_spin)
        return tab


    def _build_audio_volume_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.volume_enabled_check = QCheckBox("Enable volume changes for tracks marked Volume")
        self.volume_gain_spin = QDoubleSpinBox()
        self.volume_gain_spin.setRange(-24.0, 24.0)
        self.volume_gain_spin.setSingleStep(0.5)
        self.volume_gain_spin.setDecimals(1)
        self.volume_gain_spin.setSuffix(" dB")
        self.volume_gain_spin.setValue(3.0)
        self.volume_limiter_check = QCheckBox("Use limiter after volume boost")
        self.volume_limiter_check.setChecked(True)
        note = QLabel(
            "Use this tab when the headphone output is too quiet. Mark tracks with 'Mark/Unmark Volume Change' in the Library, "
            "set a boost here, then Apply Sync. +3 dB is a safe first test; +6 dB is noticeably louder but may need the limiter."
        )
        note.setWordWrap(True)
        form.addRow("Volume processing", self.volume_enabled_check)
        form.addRow("Track volume boost", self.volume_gain_spin)
        form.addRow("Limiter", self.volume_limiter_check)
        form.addRow("Guidance", note)
        return tab

    def _build_volume_settings_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.main_volume_spin = QDoubleSpinBox()
        self.main_volume_spin.setRange(-24.0, 24.0)
        self.main_volume_spin.setSingleStep(0.5)
        self.main_volume_spin.setDecimals(1)
        self.main_volume_spin.setSuffix(" dB")
        self.main_volume_spin.setValue(0.0)
        self.final_volume_spin = QDoubleSpinBox()
        self.final_volume_spin.setRange(-24.0, 24.0)
        self.final_volume_spin.setSingleStep(0.5)
        self.final_volume_spin.setDecimals(1)
        self.final_volume_spin.setSuffix(" dB")
        self.final_volume_spin.setValue(0.0)
        self.prevent_mix_attenuation_check = QCheckBox("Keep original track volume when mixing cues")
        self.prevent_mix_attenuation_check.setChecked(True)
        self.output_limiter_check = QCheckBox("Use limiter to prevent clipping after boosts")
        self.output_limiter_check.setChecked(True)
        note = QLabel(
            "Earlier builds used FFmpeg amix's default normalisation, which could make processed tracks quieter when cues were mixed in. "
            "This option disables that attenuation by default. Use Final output gain if your headphones still need a louder file."
        )
        note.setWordWrap(True)
        form.addRow("Original track gain", self.main_volume_spin)
        form.addRow("Final output gain", self.final_volume_spin)
        form.addRow("Mix behaviour", self.prevent_mix_attenuation_check)
        form.addRow("Safety limiter", self.output_limiter_check)
        form.addRow("Note", note)
        return tab


    def _build_processing_preview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        chooser = QGroupBox("Track preview")
        chooser_layout = QGridLayout(chooser)
        self.preview_track_combo = QComboBox()
        self.preview_track_combo.setMinimumWidth(520)
        self.generate_track_preview_button = QPushButton("Generate Preview")
        self.preview_status_label = QLabel("Select a marked track, then generate a preview.")
        self.preview_status_label.setWordWrap(True)
        chooser_layout.addWidget(QLabel("Marked track"), 0, 0)
        chooser_layout.addWidget(self.preview_track_combo, 0, 1)
        chooser_layout.addWidget(self.generate_track_preview_button, 0, 2)
        chooser_layout.addWidget(self.preview_status_label, 1, 0, 1, 3)
        layout.addWidget(chooser)

        playback = QGroupBox("Playback")
        playback_layout = QGridLayout(playback)
        self.preview_play_button = QPushButton("Play")
        self.preview_pause_button = QPushButton("Pause")
        self.preview_stop_button = QPushButton("Stop")
        self.preview_scrub_slider = QSlider(Qt.Orientation.Horizontal)
        self.preview_scrub_slider.setRange(0, 0)
        self.preview_time_label = QLabel("0:00 / 0:00")
        self.preview_speed_combo = QComboBox()
        self.preview_speed_combo.addItems(["0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "2.0x"])
        self.preview_speed_combo.setCurrentText("1.0x")
        playback_layout.addWidget(self.preview_play_button, 0, 0)
        playback_layout.addWidget(self.preview_pause_button, 0, 1)
        playback_layout.addWidget(self.preview_stop_button, 0, 2)
        playback_layout.addWidget(QLabel("Speed"), 0, 3)
        playback_layout.addWidget(self.preview_speed_combo, 0, 4)
        playback_layout.addWidget(self.preview_scrub_slider, 1, 0, 1, 4)
        playback_layout.addWidget(self.preview_time_label, 1, 4)
        layout.addWidget(playback)

        self.preview_timeline_table = QTableWidget(0, 3)
        self.preview_timeline_table.setHorizontalHeaderLabels(["Time", "Type", "Detail"])
        self.preview_timeline_table.verticalHeader().setVisible(False)
        self.preview_timeline_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.preview_timeline_table.setAlternatingRowColors(True)
        layout.addWidget(self.preview_timeline_table, 1)

        note = QLabel(
            "The preview list includes tracks marked for audio processing, batch rename, or volume change. "
            "Generate Preview renders a temporary file using the same settings that Apply Sync will use, then shows cue/rename/volume events on the timeline."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        if HAS_MULTIMEDIA:
            self.preview_audio_output = QAudioOutput(self)
            self.preview_player = QMediaPlayer(self)
            self.preview_player.setAudioOutput(self.preview_audio_output)
            self.preview_player.positionChanged.connect(self.preview_position_changed)
            self.preview_player.durationChanged.connect(self.preview_duration_changed)
            self.preview_audio_output.setVolume(0.9)
        else:
            self.preview_audio_output = None
            self.preview_player = None
            for button in [self.preview_play_button, self.preview_pause_button, self.preview_stop_button]:
                button.setEnabled(False)
            self.preview_status_label.setText("Qt Multimedia is not available in this PySide6 install; timeline generation still works, but playback is disabled.")
        return tab

    def _build_preview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        controls = QHBoxLayout()
        self.refresh_preview_button = QPushButton("Refresh Preview")
        self.apply_sync_button = QPushButton("Apply Sync")
        self.clear_activity_button = QPushButton("Clear Activity")
        controls.addWidget(self.refresh_preview_button)
        controls.addWidget(self.apply_sync_button)
        controls.addWidget(self.clear_activity_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        summary_box = QGroupBox("Status summary")
        summary_layout = QGridLayout(summary_box)
        self.status_device_label = QLabel("Device: not selected")
        self.status_tracks_label = QLabel("Tracks: 0")
        self.status_marks_label = QLabel("Pending marks: none")
        self.status_plan_label = QLabel("Plan: not built")
        for label in [self.status_device_label, self.status_tracks_label, self.status_marks_label, self.status_plan_label]:
            label.setWordWrap(True)
        summary_layout.addWidget(self.status_device_label, 0, 0)
        summary_layout.addWidget(self.status_tracks_label, 0, 1)
        summary_layout.addWidget(self.status_marks_label, 1, 0)
        summary_layout.addWidget(self.status_plan_label, 1, 1)
        layout.addWidget(summary_box)

        progress_box = QGroupBox("Current operation")
        progress_layout = QVBoxLayout(progress_box)
        self.operation_label = QLabel("Idle")
        self.operation_label.setWordWrap(True)
        self.operation_progress = QProgressBar()
        self.operation_progress.setRange(0, 1)
        self.operation_progress.setValue(0)
        progress_layout.addWidget(self.operation_label)
        progress_layout.addWidget(self.operation_progress)
        speed_grid = QGridLayout()
        self.transfer_current_label = QLabel("Current speed: 0 B/s")
        self.transfer_average_label = QLabel("Average speed: 0 B/s")
        self.transfer_peak_label = QLabel("Top speed: 0 B/s")
        self.transfer_bytes_label = QLabel("Transferred: 0 B")
        speed_grid.addWidget(self.transfer_current_label, 0, 0)
        speed_grid.addWidget(self.transfer_average_label, 0, 1)
        speed_grid.addWidget(self.transfer_peak_label, 1, 0)
        speed_grid.addWidget(self.transfer_bytes_label, 1, 1)
        progress_layout.addLayout(speed_grid)
        progress_layout.addWidget(QLabel("Live activity"))
        self.sync_activity_text = QTextEdit()
        self.sync_activity_text.setReadOnly(True)
        self.sync_activity_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.sync_activity_text.setMinimumHeight(190)
        self.sync_activity_text.setMaximumHeight(300)
        self.sync_activity_text.setPlaceholderText("Detailed sync activity will appear here during preview refreshes and sync operations.")
        progress_layout.addWidget(self.sync_activity_text)
        layout.addWidget(progress_box)

        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.preview_text, 1)
        return tab

    def _build_log_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        controls = QHBoxLayout()
        self.clear_log_button = QPushButton("Clear Log")
        self.save_log_button = QPushButton("Save Log...")
        controls.addWidget(self.clear_log_button)
        controls.addWidget(self.save_log_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.log_text, 1)

        note = QLabel(
            "Verbose logs are kept for this session only unless you save them. "
            "They include scan, preview, import, backup, marking, rename, processing, and sync actions."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        return tab

    def _connect_actions(self):
        self.device_path_edit.textChanged.connect(self.device_path_text_changed)
        self.browse_device_button.clicked.connect(self.browse_device)
        self.refresh_devices_button.clicked.connect(lambda: self.refresh_device_candidates(silent=False))
        self.candidate_combo.currentIndexChanged.connect(self.candidate_device_changed)
        self.scan_button.clicked.connect(self.scan_device)
        self.backup_button.clicked.connect(self.backup_device_clicked)
        self.mark_factory_button.clicked.connect(self.mark_factory_music)
        self.clear_removals_button.clicked.connect(self.clear_removal_marks)
        self.add_files_button.clicked.connect(self.add_audio_files)
        self.toggle_remove_button.clicked.connect(lambda: self.toggle_selected_flag("remove_from_device", "removal"))
        self.toggle_audio_button.clicked.connect(lambda: self.toggle_selected_flag("audio_processing", "audio processing"))
        self.toggle_batch_button.clicked.connect(lambda: self.toggle_selected_flag("batch_rename", "batch rename"))
        self.toggle_volume_button.clicked.connect(lambda: self.toggle_selected_flag("volume_change", "volume change"))
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        self.set_folder_button.clicked.connect(self.set_folder_for_selected)
        self.renumber_button.clicked.connect(self.renumber_tracks)
        self.table.itemChanged.connect(self.table_item_changed)

        self.rename_mark_selected_button.clicked.connect(lambda: self.toggle_selected_flag("batch_rename", "batch rename"))
        self.rename_mark_all_button.clicked.connect(self.mark_all_batch_rename)
        self.rename_clear_all_button.clicked.connect(self.clear_all_batch_rename)
        self.rename_refresh_preview_button.clicked.connect(lambda: self.refresh_rename_preview("manual rename refresh"))

        self.apply_preset_button.clicked.connect(self.apply_preset)
        self.save_preset_button.clicked.connect(self.save_custom_preset)
        self.delete_preset_button.clicked.connect(self.delete_custom_preset)
        self.preset_combo.currentTextChanged.connect(self.preset_selection_changed)
        self.refresh_preview_button.clicked.connect(lambda: self.refresh_preview("manual refresh"))
        self.apply_sync_button.clicked.connect(self.apply_sync)
        self.clear_activity_button.clicked.connect(self.clear_inline_activity)
        self.generate_track_preview_button.clicked.connect(self.generate_processing_preview)
        self.preview_track_combo.currentIndexChanged.connect(lambda *_args: self.preview_track_selection_changed())
        self.preview_play_button.clicked.connect(self.preview_play)
        self.preview_pause_button.clicked.connect(self.preview_pause)
        self.preview_stop_button.clicked.connect(self.preview_stop)
        self.preview_speed_combo.currentTextChanged.connect(self.preview_speed_changed)
        self.preview_scrub_slider.sliderPressed.connect(self.preview_slider_pressed)
        self.preview_scrub_slider.sliderReleased.connect(self.preview_slider_released)
        self.clear_log_button.clicked.connect(self.clear_log)
        self.save_log_button.clicked.connect(self.save_log)

        for widget in self._rename_widgets():
            if isinstance(widget, QCheckBox):
                widget.stateChanged.connect(lambda *_args: self.update_rename_settings_from_ui("rename setting changed"))
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(lambda *_args: self.update_rename_settings_from_ui("rename setting changed"))
            elif isinstance(widget, QSpinBox):
                widget.valueChanged.connect(lambda *_args: self.update_rename_settings_from_ui("rename setting changed"))
            elif isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(lambda *_args: self.update_rename_settings_from_ui("rename setting changed"))

        for widget in self._cue_widgets():
            if isinstance(widget, QCheckBox):
                widget.stateChanged.connect(lambda *_args: self.update_cue_settings_from_ui("cue setting changed"))
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(lambda *_args: self.update_cue_settings_from_ui("cue setting changed"))
            elif isinstance(widget, QSpinBox):
                widget.valueChanged.connect(lambda *_args: self.update_cue_settings_from_ui("cue setting changed"))
            elif isinstance(widget, QDoubleSpinBox):
                widget.valueChanged.connect(lambda *_args: self.update_cue_settings_from_ui("cue setting changed"))
            elif isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(lambda *_args: self.update_cue_settings_from_ui("cue setting changed"))

    def _cue_widgets(self) -> list[QWidget]:
        return [
            self.process_enabled_check,
            self.output_format_combo,
            self.volume_enabled_check,
            self.volume_gain_spin,
            self.volume_limiter_check,
            self.beep_enabled_check,
            self.percentages_edit,
            self.every_minutes_spin,
            self.beep_start_check,
            self.beep_end_check,
            self.beep_frequency_spin,
            self.beep_duration_spin,
            self.beep_volume_spin,
            self.beep_repeat_spin,
            self.beep_gap_spin,
            self.main_volume_spin,
            self.final_volume_spin,
            self.prevent_mix_attenuation_check,
            self.output_limiter_check,
            self.voice_title_check,
            self.voice_track_check,
            self.voice_folder_check,
            self.voice_progress_check,
            self.progress_percentages_edit,
            self.voice_every_minutes_spin,
            self.voice_start_check,
            self.voice_end_check,
            self.progress_style_combo,
            self.voice_custom_intro_edit,
            self.voice_custom_outro_edit,
            self.voice_indicator_prefix_edit,
            self.voice_indicator_suffix_edit,
            self.speech_rate_spin,
            self.speech_volume_spin,
            self.intro_silence_spin,
            self.post_intro_silence_spin,
        ]

    def _rename_widgets(self) -> list[QWidget]:
        return [
            self.rename_prefix_edit,
            self.rename_suffix_edit,
            self.rename_search_edit,
            self.rename_replace_edit,
            self.rename_case_combo,
            self.rename_numbering_combo,
            self.rename_start_spin,
            self.rename_increment_spin,
            self.rename_padding_spin,
            self.rename_separator_edit,
            self.rename_use_source_check,
            self.rename_remove_original_check,
        ]

    def _load_persisted_settings(self):
        data = load_settings()
        device = data.get("last_device")
        if device:
            self.device_path = Path(device)
        if isinstance(data.get("cue_settings"), dict):
            self.cue_settings = cue_from_dict(data["cue_settings"])
        if isinstance(data.get("rename_settings"), dict):
            self.rename_settings = rename_from_dict(data["rename_settings"])
        if isinstance(data.get("custom_presets"), dict):
            self.custom_presets = data["custom_presets"]

    def _save_persisted_settings(self):
        save_settings(
            {
                "last_device": str(self.device_path) if self.device_path else "",
                "cue_settings": cue_to_dict(self.cue_settings),
                "rename_settings": rename_to_dict(self.rename_settings),
                "custom_presets": self.custom_presets,
            }
        )

    def set_device_loaded(self, loaded: bool):
        """Gate all workflow tabs until the user has explicitly loaded a device."""
        self.device_loaded = loaded
        if hasattr(self, "tabs"):
            for index in range(self.tabs.count()):
                self.tabs.setTabEnabled(index, index == 0 or loaded)
            if not loaded and self.tabs.currentIndex() != 0:
                self.tabs.setCurrentIndex(0)
        if hasattr(self, "gate_label"):
            if loaded:
                device_text = str(self.device_path) if self.device_path else "loaded device"
                self.gate_label.setText(f"Device loaded: {device_text}. Workflow tabs are enabled. Review pending marks, preview, then Apply Sync.")
            else:
                self.gate_label.setText("Step 1: Load a suitable headphone device to enable the workflow tabs.")
        if hasattr(self, "loaded_label"):
            self.loaded_label.setText("Loaded" if loaded else "Not loaded")

    def refresh_preset_combo(self, select: str | None = None):
        if not hasattr(self, "preset_combo"):
            return
        current = select or self.preset_combo.currentText()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for name in preset_names():
            self.preset_combo.addItem(name)
        for name in sorted(self.custom_presets):
            self.preset_combo.addItem(f"Custom: {name}")
        if current:
            idx = self.preset_combo.findText(current)
            if idx >= 0:
                self.preset_combo.setCurrentIndex(idx)
        self.preset_combo.blockSignals(False)
        self.preset_selection_changed(self.preset_combo.currentText())

    def preset_selection_changed(self, text: str):
        if not hasattr(self, "custom_preset_name_edit"):
            return
        if text.startswith("Custom: "):
            self.custom_preset_name_edit.setText(text.split(": ", 1)[1])

    def save_custom_preset(self):
        self.update_cue_settings_from_ui("custom preset save")
        self.update_rename_settings_from_ui_no_refresh()
        name = self.custom_preset_name_edit.text().strip()
        if not name and self.preset_combo.currentText().startswith("Custom: "):
            name = self.preset_combo.currentText().split(": ", 1)[1].strip()
        if not name:
            QMessageBox.warning(self, "Save preset", "Enter a custom preset name first.")
            return
        name = sanitize_filename(name, fallback="Custom Preset")
        self.custom_presets[name] = {
            "cue_settings": cue_to_dict(self.cue_settings),
            "rename_settings": rename_to_dict(self.rename_settings),
        }
        self._save_persisted_settings()
        self.refresh_preset_combo(select=f"Custom: {name}")
        self.log(f"Saved custom preset: {name}")

    def delete_custom_preset(self):
        text = self.preset_combo.currentText()
        name = self.custom_preset_name_edit.text().strip()
        if text.startswith("Custom: "):
            name = text.split(": ", 1)[1]
        if not name or name not in self.custom_presets:
            QMessageBox.information(self, "Delete preset", "Select an existing custom preset to delete.")
            return
        confirm = QMessageBox.question(
            self,
            "Delete preset",
            f"Delete custom preset '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        del self.custom_presets[name]
        self._save_persisted_settings()
        self.refresh_preset_combo()
        self.log(f"Deleted custom preset: {name}")

    def log(self, message: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.log_text.append(line)
        self.log_text.ensureCursorVisible()
        self.append_inline_activity(line)
        self.statusBar().showMessage(message, 7000)

    def append_inline_activity(self, line: str):
        if not hasattr(self, "sync_activity_text"):
            return
        self._inline_log_lines.append(line)
        if len(self._inline_log_lines) > self._inline_log_limit:
            self._inline_log_lines = self._inline_log_lines[-self._inline_log_limit :]
        self.sync_activity_text.setPlainText("\n".join(self._inline_log_lines))
        self.sync_activity_text.ensureCursorVisible()

    def clear_inline_activity(self):
        self._inline_log_lines.clear()
        if hasattr(self, "sync_activity_text"):
            self.sync_activity_text.clear()
        self.log("Live activity log cleared.")

    def clear_log(self):
        self.log_text.clear()
        self.log("Log cleared.")

    def save_log(self):
        default = str(Path.home() / "swimtrack_manager.log")
        path, _ = QFileDialog.getSaveFileName(self, "Save log", default, "Log files (*.log *.txt);;All files (*)")
        if not path:
            return
        Path(path).write_text(self.log_text.toPlainText(), encoding="utf-8")
        self.log(f"Saved log to {path}")

    def device_path_text_changed(self, text: str):
        text = text.strip()
        self.device_path = Path(text) if text else None
        self.set_device_loaded(False)
        self.refresh_preview("device path changed", log_event=False)

    def browse_device(self):
        start = str(self.device_path or Path.home())
        path = QFileDialog.getExistingDirectory(self, "Select mounted headphone drive", start)
        if path:
            self.device_path = Path(path)
            self.device_path_edit.setText(str(self.device_path))
            self._save_persisted_settings()
            self.log(f"Selected device path: {self.device_path}")
            self.refresh_preview("device path selected")

    def refresh_device_candidates(self, silent: bool = False):
        previous_path = self.device_path_edit.text().strip()
        self.candidate_combo.blockSignals(True)
        self.candidate_combo.clear()

        candidates = discover_candidate_devices()
        if not candidates:
            self.candidate_combo.addItem("No mounted removable/audio drives detected", "")
            self.candidate_combo.blockSignals(False)
            if not silent:
                self.log(
                    "No mounted headphone drive was detected. On Linux, mount the device in your file manager "
                    "or check `lsblk -f`; then paste the mount path such as /media/$USER/DEVICE into Path."
                )
            return

        selected_index = 0
        for index, info in enumerate(candidates):
            display = f"{info.path} — {info.filesystem} — {info.free_display} free of {info.total_display}"
            self.candidate_combo.addItem(display, str(info.path))
            if previous_path and Path(previous_path) == info.path:
                selected_index = index

        self.candidate_combo.setCurrentIndex(selected_index)
        self.candidate_combo.blockSignals(False)
        selected_path = self.candidate_combo.currentData()
        if selected_path and not previous_path:
            self.device_path = Path(selected_path)
            self.device_path_edit.setText(selected_path)
        if not silent:
            self.log(f"Found {len(candidates)} candidate drive(s).")
            for info in candidates:
                self.log(f"Candidate: {info.path} | filesystem={info.filesystem} | free={info.free_display} | total={info.total_display}")

    def candidate_device_changed(self, _index: int):
        path_text = self.candidate_combo.currentData()
        if path_text:
            self.device_path = Path(path_text)
            self.device_path_edit.setText(path_text)
            self._save_persisted_settings()
            self.log(f"Selected detected drive: {path_text}")
            self.refresh_preview("detected drive selected")

    def scan_device(self):
        path_text = self.device_path_edit.text().strip()
        path = Path(path_text) if path_text else self.device_path
        if not path:
            self.log("Load blocked: no device path selected.")
            QMessageBox.warning(self, "Device path", "Select a valid mounted headphone folder/drive first.")
            return
        if not path.exists() or not path.is_dir():
            self.log(f"Load blocked: path is not a directory: {path}")
            QMessageBox.warning(self, "Device path", "Select a valid mounted headphone folder/drive first.")
            return
        self.device_path = path
        self.log(f"Loading device: {path}")
        self.run_threaded(ScanWorker(path), finished_slot=self.scan_finished)

    def scan_finished(self, info, tracks):
        self.device_info = info
        self.tracks = tracks
        self.fs_label.setText(info.filesystem)
        self.storage_label.setText(f"{info.free_display} free of {info.total_display}")
        self.warning_label.setText(info.warning or "Looks OK. FAT32-compatible filesystem detected.")
        suitable = info.is_fat32 or info.filesystem == "Unknown"
        self.refresh_table()
        if suitable:
            self.loaded_label.setText("Loaded")
            self.set_device_loaded(True)
            self.log(f"Loaded device with {len(tracks)} supported audio file(s).")
            self.refresh_preview("device load completed")
        else:
            self.set_device_loaded(False)
            self.loaded_label.setText("Unsuitable filesystem")
            self.gate_label.setText("Device found but not enabled: filesystem is not suitable for the Aztine test headphones. Reformat as FAT32 outside this app, then Load Device again.")
            self.log(f"Device load blocked: filesystem {info.filesystem} is not suitable for the Aztine test headphones. Reformat as FAT32 outside this app.")
            QMessageBox.warning(
                self,
                "Unsuitable device filesystem",
                f"The selected device appears to use {info.filesystem}. The Aztine test headphones should be FAT32/VFAT/MSDOS. Reformat as FAT32 outside this app, then load the device again.",
            )
        self._save_persisted_settings()

    def backup_device_clicked(self):
        if not self.device_loaded or not self.device_path:
            QMessageBox.warning(self, "Backup", "Load a suitable device first.")
            return
        default = str(Path.home() / "swimtrack_headphones_backup.zip")
        path, _ = QFileDialog.getSaveFileName(self, "Save device backup", default, "ZIP files (*.zip)")
        if not path:
            return
        self.run_threaded(BackupWorker(self.device_path, Path(path)), finished_slot=self.backup_finished)

    def backup_finished(self, zip_path: str):
        self.log(f"Backup created: {zip_path}")
        QMessageBox.information(self, "Backup complete", f"Backup created:\n{zip_path}")

    def add_audio_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Add audio files",
            str(Path.home()),
            "Audio files (*.mp3 *.ape *.flac *.wav);;All files (*)",
        )
        if not files:
            return
        next_order = len([t for t in self.tracks if not t.remove_from_device]) + 1
        for file_name in files:
            path = Path(file_name)
            title = sanitize_filename(path.stem)
            self.log(f"Import queued: {path}")
            self.tracks.append(
                Track(
                    source_path=path,
                    title=title,
                    order=next_order,
                    folder="",
                    folder_order=1,
                    duration_seconds=duration_seconds(path),
                    on_device=False,
                )
            )
            next_order += 1
        self.renumber_tracks()
        self.log(f"Added {len(files)} file(s).")

    def selected_track_indices(self) -> list[int]:
        rows = sorted({idx.row() for idx in self.table.selectionModel().selectedRows()})
        indices: list[int] = []
        for row in rows:
            item = self.table.item(row, self.C_ORDER)
            if item is None:
                continue
            track_id = item.data(Qt.ItemDataRole.UserRole)
            for idx, track in enumerate(self.tracks):
                if track.id == track_id:
                    indices.append(idx)
                    break
        return indices

    def selected_track_ids(self) -> list[str]:
        ids: list[str] = []
        for idx in self.selected_track_indices():
            ids.append(self.tracks[idx].id)
        return ids

    def _mark_selected_flag(self, attr: str, value: bool, label: str):
        selected = self.selected_track_indices()
        if not selected:
            self.log(f"No selected tracks to {label.lower()}.")
            QMessageBox.information(self, label, "Select one or more tracks in the Library tab first.")
            return
        for idx in selected:
            setattr(self.tracks[idx], attr, value)
            self.log(f"{label}: {self.tracks[idx].title}")
        self.refresh_table(select_ids=[self.tracks[idx].id for idx in selected])
        self.refresh_preview(f"{label.lower()} changed")

    def toggle_selected_flag(self, attr: str, label: str):
        selected = self.selected_track_indices()
        if not selected:
            self.log(f"No selected tracks to toggle {label} mark.")
            QMessageBox.information(self, "Mark/Unmark", "Select one or more tracks in the Library tab first.")
            return
        selected_tracks = [self.tracks[idx] for idx in selected]
        new_value = not all(bool(getattr(track, attr)) for track in selected_tracks)
        for track in selected_tracks:
            setattr(track, attr, new_value)
            self.log(f"{'Marked' if new_value else 'Unmarked'} {label}: {track.title}")
        self.refresh_table(select_ids=[track.id for track in selected_tracks])
        self.refresh_preview(f"{label} marks toggled")

    def mark_selected_remove(self):
        self._mark_selected_flag("remove_from_device", True, "Marked for removal")

    def restore_selected(self):
        selected = self.selected_track_indices()
        if not selected:
            self.log("No selected tracks to restore.")
            QMessageBox.information(self, "Restore", "Select one or more tracks in the Library tab first.")
            return
        for idx in selected:
            self.tracks[idx].remove_from_device = False
            self.log(f"Restored track: {self.tracks[idx].title}")
        self.refresh_table(select_ids=[self.tracks[idx].id for idx in selected])
        self.refresh_preview("track removal marks changed")

    def mark_selected_audio_processing(self):
        self._mark_selected_flag("audio_processing", True, "Marked for audio processing")

    def clear_selected_audio_processing(self):
        self._mark_selected_flag("audio_processing", False, "Cleared audio processing mark")

    def mark_selected_batch_rename(self):
        self._mark_selected_flag("batch_rename", True, "Marked for batch rename")

    def clear_selected_batch_rename(self):
        self._mark_selected_flag("batch_rename", False, "Cleared batch rename mark")

    def mark_all_batch_rename(self):
        count = 0
        for track in self.tracks:
            if not track.remove_from_device:
                track.batch_rename = True
                count += 1
        self.log(f"Marked all active tracks for batch rename: {count}.")
        self.refresh_table()
        self.refresh_preview("all batch rename marks changed")

    def clear_all_batch_rename(self):
        for track in self.tracks:
            track.batch_rename = False
        self.log("Cleared all batch rename marks.")
        self.refresh_table()
        self.refresh_preview("all batch rename marks cleared")

    def move_selected(self, direction: int):
        selected = self.selected_track_indices()
        selected_ids = self.selected_track_ids()
        if not selected:
            return
        if direction < 0:
            for idx in selected:
                if idx > 0:
                    self.tracks[idx - 1], self.tracks[idx] = self.tracks[idx], self.tracks[idx - 1]
        else:
            for idx in reversed(selected):
                if idx < len(self.tracks) - 1:
                    self.tracks[idx + 1], self.tracks[idx] = self.tracks[idx], self.tracks[idx + 1]
        self.log(f"Moved {len(selected)} selected track(s) {'up' if direction < 0 else 'down'}.")
        self.renumber_tracks(select_ids=selected_ids)

    def set_folder_for_selected(self):
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Set Folder")
        dialog.setText("Use the table's Folder and Folder # columns to set folders directly.")
        dialog.setInformativeText("Tip: set Folder to e.g. 'Audiobook' and Folder # to 1, then click Renumber.")
        dialog.exec()

    def renumber_tracks(self, select_ids: list[str] | None = None):
        active = [t for t in self.tracks if not t.remove_from_device]
        for order, track in enumerate(active, start=1):
            track.order = order
        self.refresh_table(select_ids=select_ids)
        self.log(f"Renumbered {len(active)} active track(s).")
        self.refresh_preview("tracks renumbered")

    def mark_factory_music(self):
        candidates = find_likely_factory_tracks(self.tracks)
        for track in candidates:
            track.remove_from_device = True
        self.refresh_table()
        self.refresh_preview("factory/test tracks marked")
        self.log(f"Marked {len(candidates)} likely factory/test track(s) for removal.")

    def clear_removal_marks(self):
        for track in self.tracks:
            track.remove_from_device = False
        self.refresh_table()
        self.log("Cleared all removal marks.")
        self.refresh_preview("removal marks cleared")

    def refresh_table(self, select_ids: list[str] | None = None):
        self._loading_table = True
        try:
            self.table.setRowCount(0)
            for row, track in enumerate(self.tracks):
                self.table.insertRow(row)
                values = [
                    str(track.order),
                    str(track.folder_order),
                    track.folder,
                    track.title,
                    "Yes" if track.audio_processing else "",
                    "Yes" if track.batch_rename else "",
                    "Yes" if getattr(track, "volume_change", False) else "",
                    track.display_duration,
                    track.extension.lstrip(".").upper(),
                    self.track_status(track),
                    str(track.source_path),
                ]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if col in {self.C_AUDIO, self.C_RENAME, self.C_VOLUME, self.C_DURATION, self.C_FORMAT, self.C_STATUS, self.C_SOURCE}:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if col == self.C_ORDER:
                        item.setData(Qt.ItemDataRole.UserRole, track.id)
                    if track.remove_from_device:
                        item.setForeground(Qt.GlobalColor.darkRed)
                    elif not track.supported:
                        item.setForeground(Qt.GlobalColor.darkYellow)
                    elif track.audio_processing or track.batch_rename or getattr(track, "volume_change", False):
                        item.setForeground(Qt.GlobalColor.darkBlue)
                    self.table.setItem(row, col, item)
            if select_ids:
                for row in range(self.table.rowCount()):
                    item = self.table.item(row, self.C_ORDER)
                    if item and item.data(Qt.ItemDataRole.UserRole) in select_ids:
                        self.table.selectRow(row)
        finally:
            self._loading_table = False
        if hasattr(self, "preview_track_combo"):
            self.refresh_processing_preview_tracks()

    def track_status(self, track: Track) -> str:
        if track.remove_from_device:
            return "Marked for removal"
        if not track.supported:
            return "Unsupported"
        parts = []
        if likely_factory_demo(track.source_path) and track.on_device:
            parts.append("Possible factory/test")
        else:
            parts.append("On device" if track.on_device else "New")
        if track.audio_processing:
            parts.append("Audio processing pending")
        if track.batch_rename:
            parts.append("Batch rename pending")
        if getattr(track, "volume_change", False):
            parts.append("Volume change pending")
        return "; ".join(parts)

    def table_item_changed(self, item: QTableWidgetItem):
        if self._loading_table:
            return
        row = item.row()
        id_item = self.table.item(row, self.C_ORDER)
        if not id_item:
            return
        track_id = id_item.data(Qt.ItemDataRole.UserRole)
        track = next((t for t in self.tracks if t.id == track_id), None)
        if not track:
            return
        old_title = track.title
        try:
            if item.column() == self.C_ORDER:
                track.order = max(1, int(item.text()))
                self.tracks.sort(key=lambda t: (t.remove_from_device, t.folder_order, t.folder.lower(), t.order, t.title.lower()))
                self.log(f"Edited order for '{track.title}' to {track.order}.")
                self.refresh_table(select_ids=[track.id])
            elif item.column() == self.C_FOLDER_ORDER:
                track.folder_order = max(1, int(item.text()))
                self.log(f"Edited folder order for '{track.title}' to {track.folder_order}.")
            elif item.column() == self.C_FOLDER:
                track.folder = sanitize_filename(item.text(), fallback="")
                self.log(f"Edited folder for '{track.title}' to '{track.folder}'.")
            elif item.column() == self.C_TITLE:
                track.title = sanitize_filename(item.text())
                self.log(f"Edited title: '{old_title}' → '{track.title}'.")
        except ValueError:
            self.log(f"Invalid table edit ignored at row {row + 1}, column {item.column() + 1}.")
            self.refresh_table()
            return
        self.refresh_preview("track table edited")

    def populate_cue_controls(self):
        s = self.cue_settings
        self.process_enabled_check.setChecked(s.enabled)
        self.output_format_combo.setCurrentText(s.output_format)
        self.volume_enabled_check.setChecked(bool(getattr(s, "volume_enabled", False)))
        self.volume_gain_spin.setValue(float(getattr(s, "volume_gain_db", 0.0)))
        self.volume_limiter_check.setChecked(bool(getattr(s, "volume_limiter", True)))
        self.main_volume_spin.setValue(float(getattr(s, "main_volume_db", 0.0)))
        self.final_volume_spin.setValue(float(getattr(s, "final_volume_db", 0.0)))
        self.prevent_mix_attenuation_check.setChecked(bool(getattr(s, "prevent_mix_attenuation", True)))
        self.output_limiter_check.setChecked(bool(getattr(s, "output_limiter", True)))
        self.beep_enabled_check.setChecked(s.beep_enabled)
        self.percentages_edit.setText(",".join(str(x) for x in s.beep_percentages))
        self.every_minutes_spin.setValue(s.beep_every_minutes)
        self.beep_start_check.setChecked(s.beep_at_start)
        self.beep_end_check.setChecked(s.beep_at_end)
        self.beep_frequency_spin.setValue(s.beep_frequency_hz)
        self.beep_duration_spin.setValue(s.beep_duration_ms)
        self.beep_volume_spin.setValue(s.beep_volume_db)
        self.beep_repeat_spin.setValue(s.beep_repeat_count)
        self.beep_gap_spin.setValue(s.beep_gap_ms)
        self.voice_title_check.setChecked(s.voice_title)
        self.voice_track_check.setChecked(s.voice_track_number)
        self.voice_folder_check.setChecked(s.voice_folder)
        self.voice_progress_check.setChecked(s.voice_progress)
        self.progress_percentages_edit.setText(",".join(str(x) for x in s.voice_progress_percentages))
        self.voice_every_minutes_spin.setValue(s.voice_every_minutes)
        self.voice_start_check.setChecked(s.voice_at_start)
        self.voice_end_check.setChecked(s.voice_at_end)
        self.progress_style_combo.setCurrentText(s.progress_announcement_style)
        self.voice_custom_intro_edit.setText(s.voice_custom_intro)
        self.voice_custom_outro_edit.setText(s.voice_custom_outro)
        self.voice_indicator_prefix_edit.setText(s.voice_indicator_prefix)
        self.voice_indicator_suffix_edit.setText(s.voice_indicator_suffix)
        self.speech_rate_spin.setValue(s.speech_rate)
        self.speech_volume_spin.setValue(s.speech_volume)
        self.intro_silence_spin.setValue(s.intro_silence_ms)
        self.post_intro_silence_spin.setValue(s.post_intro_silence_ms)

    def populate_rename_controls(self):
        r = self.rename_settings
        self.rename_prefix_edit.setText(r.prefix)
        self.rename_suffix_edit.setText(r.suffix)
        self.rename_search_edit.setText(r.search_text)
        self.rename_replace_edit.setText(r.replace_text)
        self.rename_case_combo.setCurrentText(r.case_mode)
        self.rename_numbering_combo.setCurrentText(r.numbering_mode)
        self.rename_start_spin.setValue(r.start_number)
        self.rename_increment_spin.setValue(r.increment)
        self.rename_padding_spin.setValue(r.padding)
        self.rename_separator_edit.setText(r.separator)
        self.rename_use_source_check.setChecked(r.use_source_filename)
        self.rename_remove_original_check.setChecked(r.remove_original_title)

    def parse_percentages(self, text: str) -> list[int]:
        values: list[int] = []
        for part in text.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                continue
            if 0 < value < 100:
                values.append(value)
        return sorted(set(values))

    def update_cue_settings_from_ui(self, reason: str = "cue settings changed"):
        if not hasattr(self, "process_enabled_check"):
            return
        s = self.cue_settings
        s.enabled = self.process_enabled_check.isChecked()
        s.output_format = self.output_format_combo.currentText()
        s.volume_enabled = self.volume_enabled_check.isChecked()
        s.volume_gain_db = self.volume_gain_spin.value()
        s.volume_limiter = self.volume_limiter_check.isChecked()
        s.main_volume_db = self.main_volume_spin.value()
        s.final_volume_db = self.final_volume_spin.value()
        s.prevent_mix_attenuation = self.prevent_mix_attenuation_check.isChecked()
        s.output_limiter = self.output_limiter_check.isChecked()
        s.beep_enabled = self.beep_enabled_check.isChecked()
        s.beep_percentages = self.parse_percentages(self.percentages_edit.text())
        s.beep_every_minutes = self.every_minutes_spin.value()
        s.beep_at_start = self.beep_start_check.isChecked()
        s.beep_at_end = self.beep_end_check.isChecked()
        s.beep_frequency_hz = self.beep_frequency_spin.value()
        s.beep_duration_ms = self.beep_duration_spin.value()
        s.beep_volume_db = self.beep_volume_spin.value()
        s.beep_repeat_count = self.beep_repeat_spin.value()
        s.beep_gap_ms = self.beep_gap_spin.value()
        s.voice_title = self.voice_title_check.isChecked()
        s.voice_track_number = self.voice_track_check.isChecked()
        s.voice_folder = self.voice_folder_check.isChecked()
        s.voice_progress = self.voice_progress_check.isChecked()
        s.voice_progress_percentages = self.parse_percentages(self.progress_percentages_edit.text())
        s.voice_every_minutes = self.voice_every_minutes_spin.value()
        s.voice_at_start = self.voice_start_check.isChecked()
        s.voice_at_end = self.voice_end_check.isChecked()
        s.progress_announcement_style = self.progress_style_combo.currentText()
        s.voice_custom_intro = self.voice_custom_intro_edit.text()
        s.voice_custom_outro = self.voice_custom_outro_edit.text()
        s.voice_indicator_prefix = self.voice_indicator_prefix_edit.text()
        s.voice_indicator_suffix = self.voice_indicator_suffix_edit.text()
        s.speech_rate = self.speech_rate_spin.value()
        s.speech_volume = self.speech_volume_spin.value()
        s.intro_silence_ms = self.intro_silence_spin.value()
        s.post_intro_silence_ms = self.post_intro_silence_spin.value()
        self._save_persisted_settings()
        self.refresh_preview(reason, log_event=False)

    def update_rename_settings_from_ui(self, reason: str = "rename settings changed"):
        if not hasattr(self, "rename_prefix_edit"):
            return
        r = self.rename_settings
        r.prefix = self.rename_prefix_edit.text()
        r.suffix = self.rename_suffix_edit.text()
        r.search_text = self.rename_search_edit.text()
        r.replace_text = self.rename_replace_edit.text()
        r.case_mode = self.rename_case_combo.currentText()
        r.numbering_mode = self.rename_numbering_combo.currentText()
        r.start_number = self.rename_start_spin.value()
        r.increment = self.rename_increment_spin.value()
        r.padding = self.rename_padding_spin.value()
        r.separator = self.rename_separator_edit.text()
        r.use_source_filename = self.rename_use_source_check.isChecked()
        r.remove_original_title = self.rename_remove_original_check.isChecked()
        self._save_persisted_settings()
        self.refresh_rename_preview(reason, log_event=False)
        self.refresh_preview(reason, log_event=False)

    def apply_preset(self):
        selected = self.preset_combo.currentText()
        if selected.startswith("Custom: "):
            name = selected.split(": ", 1)[1]
            data = self.custom_presets.get(name)
            if not isinstance(data, dict):
                QMessageBox.warning(self, "Preset", "That custom preset could not be loaded.")
                return
            self.cue_settings = cue_from_dict(data.get("cue_settings", {}))
            self.rename_settings = rename_from_dict(data.get("rename_settings", {}))
            self.populate_cue_controls()
            self.populate_rename_controls()
            self._save_persisted_settings()
            self.refresh_rename_preview("custom preset applied", log_event=False)
            self.refresh_preview("custom preset applied")
            self.log(f"Applied custom preset: {name}")
            return

        self.cue_settings = make_preset(selected)
        self.populate_cue_controls()
        self._save_persisted_settings()
        self.refresh_preview("preset applied")
        self.log(f"Applied preset: {selected}")

    def active_tracks_sorted(self) -> list[Track]:
        return sorted(
            [t for t in self.tracks if not t.remove_from_device],
            key=lambda t: (t.folder_order if t.folder.strip() else 0, t.folder.lower(), t.order, t.title.lower()),
        )

    def refresh_rename_preview(self, reason: str = "rename preview refresh", log_event: bool = True):
        if not hasattr(self, "rename_preview_text"):
            return
        self.update_rename_settings_from_ui_no_refresh()
        marked = [t for t in self.active_tracks_sorted() if t.batch_rename]
        if not marked:
            self.rename_preview_text.setPlainText("No tracks are marked for batch rename. Select tracks in the Library tab, then click 'Mark for Batch Rename'.")
            if log_event:
                self.log(f"Rename preview updated ({reason}): no marked tracks.")
            return
        lines = [f"Batch rename preview: {len(marked)} marked active track(s).", ""]
        for seq, track in enumerate(marked, start=1):
            new_title = apply_batch_rename_title(track, self.rename_settings, seq)
            lines.append(f"{seq:03d}. {track.title}  →  {new_title}")
        self.rename_preview_text.setPlainText("\n".join(lines))
        if log_event:
            self.log(f"Rename preview updated ({reason}): {len(marked)} marked track(s).")

    def update_rename_settings_from_ui_no_refresh(self):
        if not hasattr(self, "rename_prefix_edit"):
            return
        r = self.rename_settings
        r.prefix = self.rename_prefix_edit.text()
        r.suffix = self.rename_suffix_edit.text()
        r.search_text = self.rename_search_edit.text()
        r.replace_text = self.rename_replace_edit.text()
        r.case_mode = self.rename_case_combo.currentText()
        r.numbering_mode = self.rename_numbering_combo.currentText()
        r.start_number = self.rename_start_spin.value()
        r.increment = self.rename_increment_spin.value()
        r.padding = self.rename_padding_spin.value()
        r.separator = self.rename_separator_edit.text()
        r.use_source_filename = self.rename_use_source_check.isChecked()
        r.remove_original_title = self.rename_remove_original_check.isChecked()

    def build_plan_or_warn(self) -> SyncPlan | None:
        if not self.device_path:
            path_text = self.device_path_edit.text().strip()
            if path_text:
                self.device_path = Path(path_text)
        if not self.device_loaded or not self.device_path or not self.device_path.exists():
            self.log("Sync plan blocked: no loaded device selected.")
            QMessageBox.warning(self, "Sync", "Load a suitable device first.")
            return None
        self.update_cue_settings_from_ui("sync requested")
        self.update_rename_settings_from_ui_no_refresh()
        plan = build_sync_plan(self.device_path, self.tracks, self.cue_settings, self.rename_settings)
        self.log(f"Built sync plan: {plan.summary} ({len(plan.operations)} operation(s)).")
        return plan

    def refresh_status_summary(self, plan: SyncPlan | None = None):
        if not hasattr(self, "status_device_label"):
            return
        device = str(self.device_path) if self.device_path else "not selected"
        if self.device_info:
            device += f" | {self.device_info.filesystem} | {self.device_info.free_display} free"
        active = len([t for t in self.tracks if not t.remove_from_device])
        remove = len([t for t in self.tracks if t.remove_from_device])
        audio = len([t for t in self.tracks if t.audio_processing and not t.remove_from_device])
        rename = len([t for t in self.tracks if t.batch_rename and not t.remove_from_device])
        volume = len([t for t in self.tracks if getattr(t, "volume_change", False) and not t.remove_from_device])
        unsupported = len([t for t in self.tracks if not t.supported])
        self.status_device_label.setText(f"Device: {device}")
        self.status_tracks_label.setText(f"Tracks: {len(self.tracks)} total | {active} active | {remove} removal | {unsupported} unsupported")
        self.status_marks_label.setText(f"Pending marks: {audio} audio processing | {rename} batch rename | {volume} volume change | {remove} remove")
        self.status_plan_label.setText(f"Plan: {plan.summary if plan else 'not built'}")

    def refresh_preview(self, reason: str = "preview refresh", log_event: bool = True):
        if not hasattr(self, "preview_text"):
            return
        path_text = self.device_path_edit.text().strip() if hasattr(self, "device_path_edit") else ""
        if path_text:
            self.device_path = Path(path_text)
        self.refresh_rename_preview(log_event=False)
        if not self.device_path:
            self.preview_text.setPlainText("Select a headphone device to see the sync preview.")
            if hasattr(self, "operation_label"):
                self.operation_label.setText("Preview idle: select a headphone device")
                self.operation_progress.setRange(0, 1)
                self.operation_progress.setValue(0)
            self.refresh_status_summary(None)
            if log_event:
                self.log(f"Preview not built ({reason}): no device path selected.")
            return
        try:
            plan = build_sync_plan(self.device_path, self.tracks, self.cue_settings, self.rename_settings)
            self.preview_text.setPlainText(self.preview_text_with_details(plan))
            if hasattr(self, "operation_label"):
                self.operation_label.setText(f"Preview ready: {plan.summary}")
                self.operation_progress.setRange(0, max(1, sync_progress_total(plan)))
                self.operation_progress.setValue(0)
            self.refresh_status_summary(plan)
            if log_event:
                self.log(f"Preview updated ({reason}): {plan.summary} ({len(plan.operations)} operation(s)).")
        except Exception:
            text = traceback.format_exc()
            self.preview_text.setPlainText(text)
            self.refresh_status_summary(None)
            self.log(f"Preview failed ({reason}): {text[-1000:]}")

    def preview_text_with_details(self, plan: SyncPlan) -> str:
        lines = [dry_run_text(plan), "", "Status details:"]
        active = self.active_tracks_sorted()
        rename_seq = 0
        per_folder_counter: dict[str, int] = {}
        root = plan.device_path
        for track in active:
            folder_key = "__root__" if not track.folder.strip() else numbered_folder(track.folder_order, track.folder)
            per_folder_counter[folder_key] = per_folder_counter.get(folder_key, 0) + 1
            if track.batch_rename:
                rename_seq += 1
            dest = planned_destination_for_track(
                root,
                track,
                per_folder_counter[folder_key],
                self.cue_settings,
                self.rename_settings,
                rename_seq,
            )
            flags = []
            if track.audio_processing:
                flags.append("audio")
            if track.batch_rename:
                flags.append("rename")
            if getattr(track, "volume_change", False):
                flags.append("volume")
            if not flags:
                flags.append("standard")
            try:
                rel = dest.relative_to(root)
            except Exception:
                rel = dest
            lines.append(f"- {track.title}: {', '.join(flags)} → {rel}")
        if self.cue_settings.enabled or self.cue_settings.volume_enabled:
            lines.append("")
            lines.append(
                "Volume: marked-track boost "
                f"{float(getattr(self.cue_settings, 'volume_gain_db', 0.0)):+.1f} dB "
                f"({'enabled' if getattr(self.cue_settings, 'volume_enabled', False) else 'disabled'}); "
                "mix original gain "
                f"{float(getattr(self.cue_settings, 'main_volume_db', 0.0)):+.1f} dB; "
                f"final gain {float(getattr(self.cue_settings, 'final_volume_db', 0.0)):+.1f} dB; "
                f"mix attenuation {'prevented' if getattr(self.cue_settings, 'prevent_mix_attenuation', True) else 'allowed'}; "
                f"limiter {'on' if getattr(self.cue_settings, 'output_limiter', True) else 'off'}."
            )
        if self.cue_settings.enabled and not any(t.audio_processing for t in active):
            lines.append("")
            lines.append("Warning: audio processing is enabled, but no active tracks are marked for Audio Processing.")
        if any(t.audio_processing for t in active) and not self.cue_settings.enabled:
            lines.append("")
            lines.append("Warning: tracks are marked for Audio Processing, but the audio processing master switch is off.")
        if any(getattr(t, "volume_change", False) for t in active) and not self.cue_settings.volume_enabled:
            lines.append("")
            lines.append("Warning: tracks are marked for Volume Change, but Audio Cues → Audio Volume is disabled.")
        if any(t.batch_rename for t in active):
            lines.append("Batch rename will be applied during sync; Library titles are unchanged until the device is rescanned.")
        return "\n".join(lines)


    def marked_processing_tracks(self) -> list[Track]:
        return [
            track
            for track in self.active_tracks_sorted()
            if track.audio_processing or track.batch_rename or getattr(track, "volume_change", False)
        ]

    def refresh_processing_preview_tracks(self):
        if not hasattr(self, "preview_track_combo"):
            return
        current_id = self.preview_track_combo.currentData()
        self.preview_track_combo.blockSignals(True)
        self.preview_track_combo.clear()
        marked = self.marked_processing_tracks()
        if not marked:
            self.preview_track_combo.addItem("No tracks are marked for preview", "")
            self.preview_status_label.setText("Mark tracks for Audio Processing, Batch Rename, or Volume Change to preview them here.")
        else:
            for track in marked:
                flags = []
                if track.audio_processing:
                    flags.append("audio")
                if track.batch_rename:
                    flags.append("rename")
                if getattr(track, "volume_change", False):
                    flags.append("volume")
                self.preview_track_combo.addItem(f"{track.order:03d} - {track.title} ({', '.join(flags)})", track.id)
            idx = self.preview_track_combo.findData(current_id)
            if idx >= 0:
                self.preview_track_combo.setCurrentIndex(idx)
        self.preview_track_combo.blockSignals(False)

    def preview_track_selection_changed(self):
        track_id = self.preview_track_combo.currentData() if hasattr(self, "preview_track_combo") else ""
        track = next((t for t in self.tracks if t.id == track_id), None)
        if not track:
            self.preview_timeline_table.setRowCount(0)
            return
        effective, track_number = self.preview_effective_track(track)
        self.populate_preview_timeline(build_cue_timeline(effective, self.cue_settings, track_number))
        self.preview_status_label.setText("Timeline shown from current settings. Click Generate Preview for playable rendered audio.")

    def preview_effective_track(self, selected: Track) -> tuple[Track, int]:
        per_folder_counter: dict[str, int] = {}
        rename_sequence = 0
        for track in self.active_tracks_sorted():
            folder_key = "__root__" if not track.folder.strip() else numbered_folder(track.folder_order, track.folder)
            per_folder_counter[folder_key] = per_folder_counter.get(folder_key, 0) + 1
            file_order = per_folder_counter[folder_key]
            if track.batch_rename:
                rename_sequence += 1
            final_title = track.title
            if track.batch_rename:
                final_title = apply_batch_rename_title(track, self.rename_settings, rename_sequence)
            if track.id == selected.id:
                return replace(track, title=final_title), file_order
        return selected, selected.order

    def generate_processing_preview(self):
        track_id = self.preview_track_combo.currentData() if hasattr(self, "preview_track_combo") else ""
        track = next((t for t in self.tracks if t.id == track_id), None)
        if not track:
            QMessageBox.information(self, "Preview", "No marked track is selected for preview.")
            return
        self.update_cue_settings_from_ui("preview requested")
        self.update_rename_settings_from_ui_no_refresh()
        effective, track_number = self.preview_effective_track(track)
        ext = output_extension_for(self.cue_settings, effective.source_path.suffix, track_requires_processing(self.cue_settings, effective))
        output_path = self._preview_temp_dir / f"preview_{effective.id}{ext}"
        self.preview_status_label.setText(f"Generating preview for {effective.title}...")
        self.log(f"Preview generation requested for {effective.title}; playback/order number {track_number}.")
        started = self.run_threaded(PreviewWorker(effective, self.cue_settings, track_number, output_path), finished_slot=self.preview_generation_finished)
        self.generate_track_preview_button.setEnabled(not bool(started))

    def preview_generation_finished(self, path_text: str, timeline, title: str):
        self.generate_track_preview_button.setEnabled(True)
        self._preview_current_path = Path(path_text)
        self.populate_preview_timeline(timeline)
        if self.preview_player is not None:
            self.preview_player.setSource(QUrl.fromLocalFile(path_text))
            self.preview_status_label.setText(f"Preview ready for {title}. Use Play, scrub, and speed controls to review it.")
        else:
            self.preview_status_label.setText(f"Preview timeline ready for {title}. Playback unavailable because Qt Multimedia is missing.")
        self.log(f"Preview ready: {path_text}")

    def populate_preview_timeline(self, timeline):
        if not hasattr(self, "preview_timeline_table"):
            return
        self.preview_timeline_table.setRowCount(0)
        for row, event in enumerate(timeline or []):
            self.preview_timeline_table.insertRow(row)
            time_ms = int(event.get("time_ms", 0))
            values = [format_time(time_ms), str(event.get("type", "")), str(event.get("detail", ""))]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.preview_timeline_table.setItem(row, col, item)

    def preview_play(self):
        if self.preview_player is None:
            QMessageBox.information(self, "Preview playback", "Qt Multimedia is not available in this PySide6 install.")
            return
        if not self._preview_current_path:
            QMessageBox.information(self, "Preview playback", "Generate a preview first.")
            return
        self.preview_player.play()

    def preview_pause(self):
        if self.preview_player is not None:
            self.preview_player.pause()

    def preview_stop(self):
        if self.preview_player is not None:
            self.preview_player.stop()

    def preview_speed_changed(self, text: str):
        if self.preview_player is None:
            return
        try:
            rate = float(text.lower().replace("x", ""))
        except ValueError:
            rate = 1.0
        self.preview_player.setPlaybackRate(rate)
        self.log(f"Preview playback speed set to {rate:g}x.")

    def preview_position_changed(self, position: int):
        if self._preview_slider_dragging:
            return
        self.preview_scrub_slider.setValue(position)
        duration = self.preview_scrub_slider.maximum()
        self.preview_time_label.setText(f"{format_time(position)} / {format_time(duration)}")

    def preview_duration_changed(self, duration: int):
        self.preview_scrub_slider.setRange(0, max(0, duration))
        self.preview_time_label.setText(f"0:00 / {format_time(duration)}")

    def preview_slider_pressed(self):
        self._preview_slider_dragging = True

    def preview_slider_released(self):
        self._preview_slider_dragging = False
        if self.preview_player is not None:
            self.preview_player.setPosition(self.preview_scrub_slider.value())

    def apply_sync(self):
        plan = self.build_plan_or_warn()
        if plan is None:
            return
        if not plan.operations:
            self.log("Apply Sync requested: no changes to apply.")
            QMessageBox.information(self, "Sync", "No changes to apply.")
            return
        confirm = QMessageBox.question(
            self,
            "Apply Sync",
            f"Apply these changes to the selected device?\n\n{plan.summary}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            self.log("Apply Sync cancelled by user.")
            return
        total_steps = sync_progress_total(plan)
        self.operation_progress.setRange(0, total_steps)
        self.operation_progress.setValue(0)
        self.operation_label.setText(f"Starting sync: {plan.summary}")
        self.apply_sync_button.setEnabled(False)
        self.refresh_preview_button.setEnabled(False)
        self.clear_activity_button.setEnabled(False)
        self.log(f"Starting sync: {plan.summary}.")
        self.log(
            f"Sync marks: {len([t for t in self.tracks if t.audio_processing and not t.remove_from_device])} audio, "
            f"{len([t for t in self.tracks if t.batch_rename and not t.remove_from_device])} rename, "
            f"{len([t for t in self.tracks if getattr(t, 'volume_change', False) and not t.remove_from_device])} volume, "
            f"{len([t for t in self.tracks if t.remove_from_device])} remove."
        )
        for op in plan.operations:
            self.log(f"Planned: {op.description}")
        self.sync_transfer_updated(0.0, 0.0, 0.0, 0, "Starting sync")
        started = self.run_threaded(SyncWorker(plan, self.cue_settings), finished_slot=self.sync_finished)
        if not started:
            self.apply_sync_button.setEnabled(True)
            self.refresh_preview_button.setEnabled(True)
            self.clear_activity_button.setEnabled(True)

    def _rate_text(self, bps: float) -> str:
        return f"{format_bytes(int(max(0, bps)))}/s"

    def sync_finished(self, stats: SyncStats | None = None):
        self.operation_label.setText("Sync complete. Device has not been ejected.")
        self.operation_progress.setValue(self.operation_progress.maximum())
        self.apply_sync_button.setEnabled(True)
        self.refresh_preview_button.setEnabled(True)
        self.clear_activity_button.setEnabled(True)
        stats = stats or SyncStats()
        summary = (
            "Sync complete.\n\n"
            "The device has not been ejected.\n\n"
            f"Transferred: {format_bytes(stats.bytes_transferred)}\n"
            f"Average transfer speed: {self._rate_text(stats.average_bps)}\n"
            f"Top transfer speed: {self._rate_text(stats.peak_bps)}\n"
            f"Elapsed time: {stats.elapsed_seconds:.1f}s\n\n"
            "Would you like to eject the device now, or continue editing the drive?"
        )
        self.log(
            f"Sync complete. Transferred {format_bytes(stats.bytes_transferred)}; "
            f"average {self._rate_text(stats.average_bps)}; top {self._rate_text(stats.peak_bps)}. Device not ejected."
        )
        msg = QMessageBox(self)
        msg.setWindowTitle("Sync complete")
        msg.setText(summary)
        eject_button = msg.addButton("Eject device", QMessageBox.ButtonRole.AcceptRole)
        continue_button = msg.addButton("Continue editing", QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(continue_button)
        msg.exec()
        if msg.clickedButton() == eject_button:
            self.eject_device_clicked()
        elif self.device_path:
            QTimer.singleShot(250, self.scan_device)

    def eject_device_clicked(self):
        if not self.device_path:
            QMessageBox.information(self, "Eject device", "No device is currently loaded.")
            return
        if shutil.which("gio"):
            try:
                result = subprocess.run(["gio", "mount", "-u", str(self.device_path)], capture_output=True, text=True, timeout=20)
                if result.returncode == 0:
                    self.log(f"Ejected/unmounted device with gio: {self.device_path}")
                    self.set_device_loaded(False)
                    QMessageBox.information(self, "Eject device", "Device ejected/unmounted successfully.")
                    return
                self.log(f"gio eject failed: {(result.stderr or result.stdout or '').strip()}")
            except Exception as exc:
                self.log(f"gio eject failed: {exc}")
        QMessageBox.information(
            self,
            "Eject device",
            "Automatic eject is not available on this system. Use your file manager or OS eject/safely-remove option before unplugging.",
        )

    def run_threaded(self, worker: QObject, finished_slot):
        try:
            busy = bool(self._worker_thread and self._worker_thread.isRunning())
        except RuntimeError:
            busy = False
        if busy:
            self.log("Operation blocked: another operation is already running.")
            QMessageBox.warning(self, "Busy", "Another operation is already running.")
            return False
        thread = QThread(self)
        self._worker_thread = thread
        self._worker = worker
        worker.moveToThread(thread)
        worker.log.connect(self.log)  # type: ignore[attr-defined]
        if hasattr(worker, "progress_update"):
            worker.progress_update.connect(self.sync_progress_updated)  # type: ignore[attr-defined]
        if hasattr(worker, "transfer_update"):
            worker.transfer_update.connect(self.sync_transfer_updated)  # type: ignore[attr-defined]
        worker.error.connect(self.worker_error)  # type: ignore[attr-defined]
        worker.finished.connect(finished_slot)  # type: ignore[attr-defined]
        worker.finished.connect(lambda *_args: thread.quit())  # type: ignore[attr-defined]
        worker.error.connect(thread.quit)  # type: ignore[attr-defined]
        thread.started.connect(worker.run)  # type: ignore[attr-defined]
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self.worker_cleanup)
        self.log(f"Started background operation: {worker.__class__.__name__}.")
        thread.start()
        return True

    def worker_cleanup(self):
        self.log("Background operation finished.")
        self._worker_thread = None
        self._worker = None

    def sync_progress_updated(self, current: int, total: int, message: str):
        if total <= 0:
            total = 1
        self.operation_progress.setRange(0, total)
        safe_current = max(0, min(current, total))
        self.operation_progress.setValue(safe_current)
        self.operation_label.setText(message)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.append_inline_activity(f"[{timestamp}] Progress {safe_current}/{total}: {message}")

    def sync_transfer_updated(self, current_bps: float, average_bps: float, peak_bps: float, bytes_done: int, message: str):
        if not hasattr(self, "transfer_current_label"):
            return
        self.transfer_current_label.setText(f"Current speed: {self._rate_text(current_bps)}")
        self.transfer_average_label.setText(f"Average speed: {self._rate_text(average_bps)}")
        self.transfer_peak_label.setText(f"Top speed: {self._rate_text(peak_bps)}")
        self.transfer_bytes_label.setText(f"Transferred: {format_bytes(bytes_done)}")
        self.statusBar().showMessage(f"{message} | avg {self._rate_text(average_bps)} | top {self._rate_text(peak_bps)}", 7000)

    def worker_error(self, text: str):
        if hasattr(self, "operation_label"):
            self.operation_label.setText("Operation failed. See Log tab for details.")
            self.apply_sync_button.setEnabled(True)
            self.refresh_preview_button.setEnabled(True)
            if hasattr(self, "clear_activity_button"):
                self.clear_activity_button.setEnabled(True)
            if hasattr(self, "generate_track_preview_button"):
                self.generate_track_preview_button.setEnabled(True)
        self.log(text)
        QMessageBox.critical(self, "Operation failed", text[-4000:])

    def closeEvent(self, event):
        self._save_persisted_settings()
        try:
            if self.preview_player is not None:
                self.preview_player.stop()
        except Exception:
            pass
        try:
            shutil.rmtree(self._preview_temp_dir, ignore_errors=True)
        except Exception:
            pass
        super().closeEvent(event)


def run_app():
    import sys

    app = QApplication(sys.argv)
    app.setApplicationName("SwimTrack Manager")
    app.setOrganizationName("SwimTrack")
    window = MainWindow()
    if window.device_path:
        window.device_path_edit.setText(str(window.device_path))
    window.show()
    sys.exit(app.exec())
