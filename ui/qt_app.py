from __future__ import annotations

from pathlib import Path
from typing import cast

from PySide6 import QtCore, QtGui, QtWidgets

from domain.project_modes import list_project_modes
from services.errors import AppError

from .contracts import UiAdvancedSettingsViewModel, UiQuickLaunchSettingsViewModel
from .controller import DesktopGuiController, handle_ui_error
from .launch_profiles import list_launch_profile_ids
from .polling import TERMINAL_RUN_STATUSES, plan_poll_refresh
from .presentation import (
    STRICTNESS_LABELS,
    THEME_LABELS,
    get_ui_theme,
    label_for_launch_profile,
    label_for_strictness,
    label_for_theme,
    launch_profile_value_from_label,
    normalize_ui_theme,
    translate_asset_kind,
    translate_paragraph_status,
    translate_provider,
    translate_run_stage,
    translate_run_status,
    translate_session_health,
    yes_no,
)


class DesktopQtApp(QtWidgets.QMainWindow):
    def __init__(self, controller: DesktopGuiController):
        super().__init__()
        self.controller = controller
        self._mode_definitions = list_project_modes()
        self._mode_labels = {
            item.mode_id: item.label for item in self._mode_definitions
        }
        self._mode_ids_by_label = {
            item.label: item.mode_id for item in self._mode_definitions
        }
        self._strictness_ids_by_label = {
            label_for_strictness(value): value for value in STRICTNESS_LABELS
        }
        self._launch_profile_ids_by_label = {
            label_for_launch_profile(value): value
            for value in list_launch_profile_ids()
        }
        self._launch_profile_labels = {
            value: label_for_launch_profile(value)
            for value in list_launch_profile_ids()
        }
        self._theme_ids_by_label = {
            label_for_theme(value): value for value in THEME_LABELS
        }
        self.active_project_id: str | None = None
        self.active_run_id: str | None = None
        self._project_ids_by_row: list[str] = []
        self._history_ids_by_row: list[tuple[str, str]] = []
        self._paragraph_numbers_by_row: list[int] = []
        self._session_buttons: list[QtWidgets.QPushButton] = []
        self._last_paragraph_signature: tuple[tuple[int, str, str, int, bool], ...] = ()
        self._terminal_refresh_signature: tuple[str | None, str | None] | None = None
        self._poll_interval_active_ms = 500
        self._poll_interval_idle_ms = 1600
        self._theme_id = self.controller.get_ui_theme()
        self._current_downloads_root = ""
        self._current_videos_dir = ""
        self._current_images_dir = ""

        self.setWindowTitle("Vid Img Downloader")
        self.resize(1480, 920)
        self._build_ui()
        self._apply_theme(self._theme_id)
        self.refresh()

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(self._poll_interval_idle_ms)
        self._timer.timeout.connect(self._poll_refresh)
        self._timer.start()

    def _build_ui(self) -> None:
        toolbar = self.addToolBar("Главное")
        toolbar.setMovable(False)
        for text, handler in (
            ("Открыть сценарий", self.on_open_script),
            ("Старт", self.on_start_run),
            ("Отменить", self.on_abort_run),
            ("Повторить весь прогон", self.on_rerun_full_run),
            ("Проверить сессию", self.on_check_session),
        ):
            action = toolbar.addAction(text)
            action.triggered.connect(handler)

        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Медиа-станция по абзацам")
        title.setObjectName("WindowTitle")
        subtitle = QtWidgets.QLabel(
            "Темная тема, русский интерфейс и только нужные действия"
        )
        subtitle.setObjectName("WindowSubtitle")
        title_block = QtWidgets.QVBoxLayout()
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        self.theme_combo = QtWidgets.QComboBox()
        self.theme_combo.addItems([label_for_theme(item) for item in THEME_LABELS])
        self.theme_combo.setCurrentText(label_for_theme(self._theme_id))
        self.theme_combo.currentTextChanged.connect(self.on_theme_changed)
        self.status_label = QtWidgets.QLabel("Готово")
        header.addLayout(title_block)
        header.addStretch(1)
        header.addWidget(QtWidgets.QLabel("Тема"))
        header.addWidget(self.theme_combo)
        header.addWidget(self.status_label)
        root.addLayout(header)

        self.main_tabs = QtWidgets.QTabWidget()
        root.addWidget(self.main_tabs, 1)

        self._build_main_tab(self.main_tabs)
        self._build_api_keys_tab(self.main_tabs)
        self._build_session_tab(self.main_tabs)
        self._build_advanced_tab(self.main_tabs)
        self._build_history_tab(self.main_tabs)

    def _build_main_tab(self, tabs: QtWidgets.QTabWidget) -> None:
        tab = QtWidgets.QWidget()
        tabs.addTab(tab, "Главная")
        layout = QtWidgets.QVBoxLayout(tab)

        self.project_name_edit = QtWidgets.QLineEdit()
        self.script_path_edit = QtWidgets.QLineEdit()
        self.output_dir_edit = QtWidgets.QLineEdit()
        self.mode_combo = QtWidgets.QComboBox()
        self.launch_profile_combo = QtWidgets.QComboBox()
        self.script_path_edit.setMinimumWidth(620)
        self.output_dir_edit.setMinimumWidth(620)
        self.mode_combo.setMinimumWidth(420)
        self.launch_profile_combo.setMinimumWidth(220)
        self.mode_combo.addItems([item.label for item in self._mode_definitions])
        self.mode_combo.currentTextChanged.connect(
            lambda _value: self.refresh_preview()
        )
        self.launch_profile_combo.addItems(
            [self._launch_profile_labels[item] for item in list_launch_profile_ids()]
        )
        self.launch_profile_combo.currentTextChanged.connect(
            lambda _value: self._sync_custom_timing_visibility()
        )
        self.mode_combo.setMinimumContentsLength(28)
        self.project_name_edit.setPlaceholderText("Необязательно")

        launch_box = QtWidgets.QGroupBox("Что запускать")
        launch_layout = QtWidgets.QGridLayout(launch_box)
        launch_layout.setColumnStretch(1, 1)
        row_index = 0
        launch_layout.addWidget(QtWidgets.QLabel("Название проекта"), row_index, 0)
        launch_layout.addWidget(self.project_name_edit, row_index, 1)
        row_index += 1
        script_row = QtWidgets.QHBoxLayout()
        script_row.addWidget(self.script_path_edit, 1)
        browse_script = QtWidgets.QPushButton("Обзор")
        browse_script.clicked.connect(self.on_browse_script)
        script_row.addWidget(browse_script)
        launch_layout.addWidget(QtWidgets.QLabel("Файл сценария"), row_index, 0)
        launch_layout.addWidget(_wrap_layout(script_row), row_index, 1)
        row_index += 1
        output_row = QtWidgets.QHBoxLayout()
        output_row.addWidget(self.output_dir_edit, 1)
        browse_output = QtWidgets.QPushButton("Обзор")
        browse_output.clicked.connect(self.on_browse_output_dir)
        output_row.addWidget(browse_output)
        launch_layout.addWidget(QtWidgets.QLabel("Папка вывода"), row_index, 0)
        launch_layout.addWidget(_wrap_layout(output_row), row_index, 1)
        row_index += 1
        launch_layout.addWidget(QtWidgets.QLabel("Режим проекта"), row_index, 0)
        launch_layout.addWidget(self.mode_combo, row_index, 1)
        row_index += 1
        launch_layout.addWidget(QtWidgets.QLabel("Профиль запуска"), row_index, 0)
        launch_layout.addWidget(self.launch_profile_combo, row_index, 1)
        row_index += 1
        self.paragraph_selection_edit = QtWidgets.QLineEdit()
        self.paragraph_selection_edit.setPlaceholderText("Например: 5..end, 8-12")
        self.paragraph_selection_edit.textChanged.connect(
            lambda _value: self.refresh_preview()
        )
        launch_layout.addWidget(QtWidgets.QLabel("Абзацы"), row_index, 0)
        launch_layout.addWidget(self.paragraph_selection_edit, row_index, 1)
        row_index += 1
        count_row = QtWidgets.QHBoxLayout()
        self.supporting_image_spin = _spin_box(0, 12, 1)
        self.supporting_image_spin.valueChanged.connect(
            lambda _value: self.refresh_preview()
        )
        self.fallback_image_spin = _spin_box(0, 12, 1)
        self.fallback_image_spin.valueChanged.connect(
            lambda _value: self.refresh_preview()
        )
        count_row.addWidget(QtWidgets.QLabel("Основные изображения"))
        count_row.addWidget(self.supporting_image_spin)
        count_row.addSpacing(16)
        count_row.addWidget(QtWidgets.QLabel("Резервные изображения"))
        count_row.addWidget(self.fallback_image_spin)
        count_row.addStretch(1)
        launch_layout.addWidget(QtWidgets.QLabel("Media counts"), row_index, 0)
        launch_layout.addWidget(_wrap_layout(count_row), row_index, 1)
        layout.addWidget(launch_box)

        ai_box = QtWidgets.QGroupBox("Gemini control")
        ai_layout = QtWidgets.QVBoxLayout(ai_box)
        self.manual_prompt_edit = QtWidgets.QPlainTextEdit()
        self.manual_prompt_edit.setPlaceholderText(
            "Дополнительный prompt для Gemini. Необязательно."
        )
        self.manual_prompt_edit.setMaximumHeight(96)
        ai_layout.addWidget(self.manual_prompt_edit)
        self.attach_full_script_context_checkbox = QtWidgets.QCheckBox(
            "Прикреплять весь сценарий как контекст"
        )
        self.attach_full_script_context_checkbox.toggled.connect(
            lambda _checked: self.refresh_preview()
        )
        ai_layout.addWidget(self.attach_full_script_context_checkbox)
        enrich_button = QtWidgets.QPushButton("Обновить intent через Gemini")
        enrich_button.clicked.connect(self.on_enrich_project_intents)
        ai_layout.addWidget(enrich_button)
        layout.addWidget(ai_box)

        ready_box = QtWidgets.QGroupBox("Перед запуском")
        ready_layout = QtWidgets.QVBoxLayout(ready_box)
        self.preview_text = QtWidgets.QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setMaximumHeight(140)
        ready_layout.addWidget(self.preview_text)
        layout.addWidget(ready_box)

        self._build_workspace(layout)

    def _build_api_keys_tab(self, tabs: QtWidgets.QTabWidget) -> None:
        tab = QtWidgets.QWidget()
        tabs.addTab(tab, "Ключи API")
        layout = QtWidgets.QVBoxLayout(tab)

        gemini_box = QtWidgets.QGroupBox("Gemini")
        gemini_layout = QtWidgets.QVBoxLayout(gemini_box)
        self.gemini_key_edit = QtWidgets.QLineEdit()
        self.gemini_key_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.gemini_key_edit.setPlaceholderText("Введите Gemini API key")
        gemini_layout.addWidget(self.gemini_key_edit)
        gemini_buttons = QtWidgets.QHBoxLayout()
        store_button = QtWidgets.QPushButton("Сохранить")
        store_button.clicked.connect(self.on_store_gemini_key)
        clear_button = QtWidgets.QPushButton("Удалить")
        clear_button.clicked.connect(self.on_clear_gemini_key)
        gemini_buttons.addWidget(store_button)
        gemini_buttons.addWidget(clear_button)
        gemini_layout.addLayout(gemini_buttons)
        layout.addWidget(gemini_box)

        stock_box = QtWidgets.QGroupBox("Ключи стоков")
        stock_layout = QtWidgets.QFormLayout(stock_box)
        self.stock_key_edits: dict[str, QtWidgets.QLineEdit] = {}
        for provider_id, label in (
            ("pexels", "Pexels API key"),
            ("pixabay", "Pixabay API key"),
        ):
            row = QtWidgets.QHBoxLayout()
            edit = QtWidgets.QLineEdit()
            edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            edit.setPlaceholderText(f"Введите ключ для {label}")
            save_button = QtWidgets.QPushButton("Сохранить")
            save_button.clicked.connect(
                lambda _checked=False, provider_id=provider_id: (
                    self.on_store_provider_key(provider_id)
                )
            )
            clear_button = QtWidgets.QPushButton("Удалить")
            clear_button.clicked.connect(
                lambda _checked=False, provider_id=provider_id: (
                    self.on_clear_provider_key(provider_id)
                )
            )
            row.addWidget(edit, 1)
            row.addWidget(save_button)
            row.addWidget(clear_button)
            self.stock_key_edits[provider_id] = edit
            stock_layout.addRow(label, _wrap_layout(row))
        layout.addWidget(stock_box)
        layout.addStretch(1)

    def _build_advanced_tab(self, tabs: QtWidgets.QTabWidget) -> None:
        tab = QtWidgets.QWidget()
        tabs.addTab(tab, "Эксперт")
        layout = QtWidgets.QVBoxLayout(tab)

        self.action_delay_spin = _spin_box(0, 60000, 900)
        self.launch_timeout_spin = _spin_box(1000, 300000, 45000)
        self.navigation_timeout_spin = _spin_box(1000, 300000, 30000)
        self.download_timeout_spin = QtWidgets.QDoubleSpinBox()
        self.download_timeout_spin.setRange(1.0, 3600.0)
        self.download_timeout_spin.setValue(120.0)
        self.strictness_combo = QtWidgets.QComboBox()
        self.strictness_combo.addItems(
            [label_for_strictness(item) for item in STRICTNESS_LABELS]
        )
        self.strictness_combo.currentTextChanged.connect(
            lambda _value: self.refresh_preview()
        )
        self.free_provider_checks: dict[str, QtWidgets.QCheckBox] = {}
        self.action_delay_spin.valueChanged.connect(
            lambda _value: self.refresh_preview()
        )
        self.launch_timeout_spin.valueChanged.connect(
            lambda _value: self.refresh_preview()
        )
        self.navigation_timeout_spin.valueChanged.connect(
            lambda _value: self.refresh_preview()
        )
        self.download_timeout_spin.valueChanged.connect(
            lambda _value: self.refresh_preview()
        )

        behavior_box = QtWidgets.QGroupBox("Поведение запуска")
        behavior_form = QtWidgets.QFormLayout(behavior_box)
        behavior_form.addRow("Строгость AI-анализа", self.strictness_combo)
        layout.addWidget(behavior_box)

        provider_box = QtWidgets.QGroupBox("Источники бесплатных изображений")
        provider_layout = QtWidgets.QVBoxLayout(provider_box)
        provider_layout.addWidget(
            QtWidgets.QLabel(
                "Используются только в режимах с бесплатными изображениями."
            )
        )
        for provider_id, label in (
            ("pexels", "Pexels"),
            ("pixabay", "Pixabay"),
            ("openverse", "Openverse"),
        ):
            checkbox = QtWidgets.QCheckBox(label)
            checkbox.toggled.connect(lambda _checked, self=self: self.refresh_preview())
            self.free_provider_checks[provider_id] = checkbox
            provider_layout.addWidget(checkbox)
        layout.addWidget(provider_box)

        presets_box = QtWidgets.QGroupBox("Пресеты")
        presets_layout = QtWidgets.QVBoxLayout(presets_box)
        presets_layout.addWidget(
            QtWidgets.QLabel("Сохраняйте наборы настроек для повторного использования.")
        )
        self.preset_name_edit = QtWidgets.QLineEdit()
        self.preset_name_edit.setPlaceholderText("Например: Только видео Storyblocks")
        presets_layout.addWidget(self.preset_name_edit)
        preset_buttons = QtWidgets.QHBoxLayout()
        for text, handler in (
            ("Сохранить", self.on_save_preset),
            ("Загрузить", self.on_load_preset),
            ("Экспорт", self.on_export_preset),
            ("Импорт", self.on_import_preset),
        ):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(handler)
            preset_buttons.addWidget(button)
        presets_layout.addLayout(preset_buttons)
        layout.addWidget(presets_box)

        self.custom_timing_box = QtWidgets.QGroupBox("Custom: задержки и таймауты")
        custom_form = QtWidgets.QFormLayout(self.custom_timing_box)
        custom_form.addRow("Задержка действий, мс", self.action_delay_spin)
        custom_form.addRow("Таймаут запуска, мс", self.launch_timeout_spin)
        custom_form.addRow("Таймаут навигации, мс", self.navigation_timeout_spin)
        custom_form.addRow("Таймаут скачивания, с", self.download_timeout_spin)
        layout.addWidget(self.custom_timing_box)
        layout.addStretch(1)
        self._sync_custom_timing_visibility()

    def _build_session_tab(self, tabs: QtWidgets.QTabWidget) -> None:
        tab = QtWidgets.QWidget()
        tabs.addTab(tab, "Сессия")
        layout = QtWidgets.QVBoxLayout(tab)
        self.session_text = QtWidgets.QPlainTextEdit()
        self.session_text.setReadOnly(True)
        layout.addWidget(self.session_text, 1)
        buttons = QtWidgets.QGridLayout()
        actions = (
            ("Войти в браузере", self.on_prepare_login),
            ("Проверить сессию", self.on_check_session),
            ("Импортировать сессию", self.on_import_session),
            ("Подтвердить вручную", self.on_mark_session_ready),
            ("Снять подтверждение", self.on_clear_session_override),
            ("Выйти", self.on_logout),
            ("Сбросить сессию", self.on_reset_session),
        )
        for index, (text, handler) in enumerate(actions):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(handler)
            self._session_buttons.append(button)
            buttons.addWidget(button, index // 2, index % 2)
        layout.addLayout(buttons)

    def _build_history_tab(self, tabs: QtWidgets.QTabWidget) -> None:
        tab = QtWidgets.QWidget()
        tabs.addTab(tab, "История")
        layout = QtWidgets.QVBoxLayout(tab)
        self.history_list = QtWidgets.QListWidget()
        self.history_list.itemSelectionChanged.connect(self.on_history_selected)
        layout.addWidget(self.history_list)

    def _build_workspace(self, parent_layout: QtWidgets.QVBoxLayout) -> None:
        progress_box = QtWidgets.QGroupBox("Что сейчас происходит")
        progress_layout = QtWidgets.QVBoxLayout(progress_box)
        self.run_summary_label = QtWidgets.QLabel("Нет активного запуска")
        self.run_summary_label.setObjectName("RunSummary")
        self.run_detail_label = QtWidgets.QLabel("Откройте сценарий и нажмите Старт")
        self.run_paths_label = QtWidgets.QLabel("")
        self.run_paths_label.setWordWrap(True)
        self.run_eta_label = QtWidgets.QLabel("")
        self.run_totals_label = QtWidgets.QLabel("")
        self.run_progress = QtWidgets.QProgressBar()
        self.run_progress.setRange(0, 100)
        self.run_progress.setFormat("%p%")
        progress_layout.addWidget(self.run_summary_label)
        progress_layout.addWidget(self.run_detail_label)
        progress_layout.addWidget(self.run_paths_label)
        progress_layout.addWidget(self.run_eta_label)
        progress_layout.addWidget(self.run_totals_label)
        progress_layout.addWidget(self.run_progress)
        progress_actions = QtWidgets.QHBoxLayout()
        self.open_downloads_button = QtWidgets.QPushButton("Открыть папку выгрузки")
        self.open_downloads_button.clicked.connect(self.on_open_downloads_folder)
        progress_actions.addWidget(self.open_downloads_button)
        progress_actions.addStretch(1)
        self.export_logs_button = QtWidgets.QPushButton("Скачать логи")
        self.export_logs_button.clicked.connect(self.on_export_logs)
        progress_actions.addWidget(self.export_logs_button)
        progress_layout.addLayout(progress_actions)
        parent_layout.addWidget(progress_box)

        self.project_list = QtWidgets.QListWidget()
        self.paragraph_list = QtWidgets.QListWidget()
        self.paragraph_list.itemSelectionChanged.connect(self.on_paragraph_selected)
        self.paragraph_text = QtWidgets.QPlainTextEdit()
        self.paragraph_text.setReadOnly(True)
        self.video_queries_text = QtWidgets.QPlainTextEdit()
        self.image_queries_text = QtWidgets.QPlainTextEdit()
        self.downloaded_files_list = QtWidgets.QListWidget()
        self.paragraph_status_value = QtWidgets.QLabel("-")
        self.paragraph_note_value = QtWidgets.QLabel("-")
        self.paragraph_note_value.setWordWrap(True)
        self.downloads_root_value = QtWidgets.QLabel("-")
        self.videos_dir_value = QtWidgets.QLabel("-")
        self.images_dir_value = QtWidgets.QLabel("-")
        for label in (
            self.downloads_root_value,
            self.videos_dir_value,
            self.images_dir_value,
        ):
            label.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            )
            label.setWordWrap(True)

        result_box = QtWidgets.QGroupBox("Результат")
        result_layout = QtWidgets.QVBoxLayout(result_box)
        result_hint = QtWidgets.QLabel(
            "После завершения прогона проверьте реальные файлы в папке downloads."
        )
        result_hint.setWordWrap(True)
        result_layout.addWidget(result_hint)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.paragraph_list.setMinimumWidth(240)
        splitter.addWidget(self.paragraph_list)

        detail_widget = QtWidgets.QWidget()
        detail_layout = QtWidgets.QVBoxLayout(detail_widget)
        detail_form = QtWidgets.QFormLayout()
        detail_form.addRow("Статус", self.paragraph_status_value)
        detail_form.addRow("Короткая заметка", self.paragraph_note_value)
        detail_form.addRow("Папка downloads", self.downloads_root_value)
        detail_form.addRow("Папка videos", self.videos_dir_value)
        detail_form.addRow("Папка images", self.images_dir_value)
        detail_layout.addLayout(detail_form)
        detail_layout.addWidget(QtWidgets.QLabel("Текст абзаца"))
        detail_layout.addWidget(self.paragraph_text, 1)
        detail_layout.addWidget(QtWidgets.QLabel("Сохраненные файлы"))
        detail_layout.addWidget(self.downloaded_files_list, 1)
        splitter.addWidget(detail_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        result_layout.addWidget(splitter, 1)
        parent_layout.addWidget(result_box, 1)

    def _quick_form(self) -> UiQuickLaunchSettingsViewModel:
        selected_mode = self._mode_ids_by_label.get(
            self.mode_combo.currentText(), "sb_video_only"
        )
        provider_ids = [
            provider_id
            for provider_id, checkbox in self.free_provider_checks.items()
            if checkbox.isChecked()
        ]
        return UiQuickLaunchSettingsViewModel(
            project_name=self.project_name_edit.text().strip(),
            script_path=self.script_path_edit.text().strip(),
            output_dir=self.output_dir_edit.text().strip(),
            paragraph_selection_text=self.paragraph_selection_edit.text().strip(),
            selected_paragraphs=[],
            mode_id=selected_mode,
            launch_profile_id=self._launch_profile_ids_by_label.get(
                self.launch_profile_combo.currentText().strip(),
                launch_profile_value_from_label(
                    self.launch_profile_combo.currentText().strip()
                ),
            ),
            strictness=self._strictness_ids_by_label.get(
                self.strictness_combo.currentText().strip(), "balanced"
            ),
            provider_ids=provider_ids,
            supporting_image_limit=self.supporting_image_spin.value(),
            fallback_image_limit=self.fallback_image_spin.value(),
            manual_prompt=self.manual_prompt_edit.toPlainText().strip(),
            attach_full_script_context=self.attach_full_script_context_checkbox.isChecked(),
        )

    def _advanced_form(self) -> UiAdvancedSettingsViewModel:
        return UiAdvancedSettingsViewModel(
            action_delay_ms=self.action_delay_spin.value(),
            launch_timeout_ms=self.launch_timeout_spin.value(),
            navigation_timeout_ms=self.navigation_timeout_spin.value(),
            downloads_timeout_seconds=self.download_timeout_spin.value(),
        )

    def refresh(self, *, preserve_forms: bool = False) -> None:
        state = self.controller.build_state(
            active_project_id=self.active_project_id, active_run_id=self.active_run_id
        )
        self._apply_state(state, preserve_forms=preserve_forms)

    def _poll_refresh(self) -> None:
        next_interval = self._poll_interval_idle_ms
        if self.active_project_id is None and self.active_run_id is None:
            self._terminal_refresh_signature = None
            self._set_poll_interval(next_interval)
            return
        try:
            live_snapshot = self.controller.build_live_snapshot(
                active_project_id=self.active_project_id,
                active_run_id=self.active_run_id,
            )
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            self._set_poll_interval(next_interval)
            return
        run_status = (
            live_snapshot.run_progress.status
            if live_snapshot.run_progress is not None
            else None
        )
        poll_plan = plan_poll_refresh(
            run_id=live_snapshot.active_run_id,
            run_status=run_status,
            previous_terminal_signature=self._terminal_refresh_signature,
            active_interval_ms=self._poll_interval_active_ms,
            idle_interval_ms=self._poll_interval_idle_ms,
        )
        if (
            self.active_project_id is not None
            and live_snapshot.active_run_id is not None
            and run_status is not None
            and run_status not in TERMINAL_RUN_STATUSES
        ):
            self._apply_live_snapshot(live_snapshot)
            try:
                live_state = self.controller.build_live_run_state(
                    active_project_id=self.active_project_id,
                    active_run_id=live_snapshot.active_run_id,
                    selected_paragraph_no=self._current_paragraph_number(),
                    live_snapshot=live_snapshot,
                )
            except Exception as exc:
                self._show_notification(handle_ui_error(exc))
                self._set_poll_interval(next_interval)
                return
            if live_state.paragraph_items:
                self._apply_live_paragraph_items(live_state.paragraph_items)
        if poll_plan.should_heavy_refresh:
            self.refresh(preserve_forms=True)
        self._terminal_refresh_signature = poll_plan.terminal_signature
        next_interval = poll_plan.next_interval_ms
        self._set_poll_interval(next_interval)

    def _set_poll_interval(self, interval_ms: int) -> None:
        if self._timer.interval() != interval_ms:
            self._timer.setInterval(interval_ms)

    def _apply_live_state(self, state) -> None:
        self._apply_live_snapshot(state)
        self._apply_live_paragraph_items(state.paragraph_items)

    def _apply_live_snapshot(self, state) -> None:
        self.active_run_id = state.active_run_id
        self.status_label.setText(state.status_text)
        self._render_run_progress(state.run_progress)
        self._set_session_actions_enabled(self.controller.session_actions_enabled())
        self.export_logs_button.setEnabled(self.active_run_id is not None)

    def _apply_live_paragraph_items(self, paragraph_items) -> None:
        self._fill_paragraph_list(paragraph_items)
        self._render_current_paragraph_detail(paragraph_items)

    def _sync_custom_timing_visibility(self) -> None:
        if not hasattr(self, "custom_timing_box"):
            return
        launch_profile_id = self._launch_profile_ids_by_label.get(
            self.launch_profile_combo.currentText().strip(),
            launch_profile_value_from_label(
                self.launch_profile_combo.currentText().strip()
            ),
        )
        self.custom_timing_box.setVisible(launch_profile_id == "custom")
        self.refresh_preview()

    def refresh_preview(self) -> None:
        if self.active_project_id is None:
            self.preview_text.setPlainText(
                "Сначала откройте сценарий, чтобы увидеть параметры запуска."
            )
            return
        try:
            preview = self.controller.build_run_preview(
                self.active_project_id, self._quick_form(), self._advanced_form()
            )
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self._render_preview(preview)

    def _apply_state(self, state, *, preserve_forms: bool = False) -> None:
        self.active_project_id = state.active_project_id
        self.active_run_id = state.active_run_id
        self.status_label.setText(state.status_text)
        if not preserve_forms:
            if not self.output_dir_edit.text().strip():
                self.output_dir_edit.setText(state.quick_launch.output_dir)
            self.mode_combo.setCurrentText(
                self._mode_labels.get(
                    state.quick_launch.mode_id, self._mode_labels["sb_video_only"]
                )
            )
            self.launch_profile_combo.setCurrentText(
                self._launch_profile_labels.get(
                    state.quick_launch.launch_profile_id,
                    self._launch_profile_labels["normal"],
                )
            )
            self.strictness_combo.setCurrentText(
                label_for_strictness(state.quick_launch.strictness)
            )
            self.paragraph_selection_edit.setText(
                state.quick_launch.paragraph_selection_text
            )
            self.supporting_image_spin.setValue(
                state.quick_launch.supporting_image_limit
            )
            self.fallback_image_spin.setValue(state.quick_launch.fallback_image_limit)
            self.manual_prompt_edit.setPlainText(state.quick_launch.manual_prompt)
            self.attach_full_script_context_checkbox.setChecked(
                state.quick_launch.attach_full_script_context
            )
            selected_free = set(state.quick_launch.provider_ids)
            for provider_id, checkbox in self.free_provider_checks.items():
                checkbox.setChecked(provider_id in selected_free)
            self.action_delay_spin.setValue(state.advanced.action_delay_ms)
            self.launch_timeout_spin.setValue(state.advanced.launch_timeout_ms)
            self.navigation_timeout_spin.setValue(state.advanced.navigation_timeout_ms)
            self.download_timeout_spin.setValue(
                state.advanced.downloads_timeout_seconds
            )
            if not self.gemini_key_edit.text().strip():
                self.gemini_key_edit.setText(self.controller.get_gemini_key() or "")
            for provider_id, edit in self.stock_key_edits.items():
                if not edit.text().strip():
                    edit.setText(
                        self.controller.get_provider_api_key(provider_id) or ""
                    )
            self._sync_custom_timing_visibility()

        self._fill_history_list(state.run_history)
        self._fill_paragraph_list(state.paragraph_items)
        self._render_current_paragraph_detail(state.paragraph_items)
        self._render_session(state.session)
        self._render_preview(state.run_preview)
        self._render_run_progress(state.run_progress)
        self._set_session_actions_enabled(self.controller.session_actions_enabled())
        self.export_logs_button.setEnabled(self.active_run_id is not None)

    def _set_session_actions_enabled(self, enabled: bool) -> None:
        for button in self._session_buttons:
            button.setEnabled(enabled)

    def _fill_project_list(self, projects) -> None:
        self.project_list.blockSignals(True)
        self.project_list.clear()
        self._project_ids_by_row = []
        for project in projects:
            summary = f"{project.name} ({project.paragraphs_total})"
            if project.numbering_issues:
                summary += " - есть проблемы с нумерацией"
            self.project_list.addItem(summary)
            self._project_ids_by_row.append(project.project_id)
        if self.active_project_id in self._project_ids_by_row:
            self.project_list.setCurrentRow(
                self._project_ids_by_row.index(self.active_project_id)
            )
        self.project_list.blockSignals(False)

    def _fill_history_list(self, runs) -> None:
        self.history_list.blockSignals(True)
        self.history_list.clear()
        self._history_ids_by_row = []
        for run in runs:
            self.history_list.addItem(
                f"{run.run_id} | {translate_run_status(run.status)} | {run.created_at}"
            )
            self._history_ids_by_row.append((run.run_id, run.project_id))
        if self.active_run_id is not None:
            for index, (run_id, _project_id) in enumerate(self._history_ids_by_row):
                if run_id == self.active_run_id:
                    self.history_list.setCurrentRow(index)
                    break
        self.history_list.blockSignals(False)

    def _fill_paragraph_list(self, paragraph_items) -> None:
        signature = self._paragraph_signature(paragraph_items)
        selected_paragraph_no = self._current_paragraph_number()
        if signature == self._last_paragraph_signature:
            return
        self.paragraph_list.blockSignals(True)
        self.paragraph_list.clear()
        self._paragraph_numbers_by_row = []
        for item in paragraph_items:
            label = f"P{item.paragraph_no} | {translate_paragraph_status(item.status)}"
            if not item.numbering_valid:
                label += " | проблема нумерации"
            self.paragraph_list.addItem(label)
            self._paragraph_numbers_by_row.append(item.paragraph_no)
        if selected_paragraph_no in self._paragraph_numbers_by_row:
            self.paragraph_list.setCurrentRow(
                self._paragraph_numbers_by_row.index(selected_paragraph_no)
            )
        elif self._paragraph_numbers_by_row:
            self.paragraph_list.setCurrentRow(0)
        self.paragraph_list.blockSignals(False)
        self._last_paragraph_signature = signature

    def _render_preview(self, preview) -> None:
        if preview is None:
            self.preview_text.setPlainText("")
            return
        lines = [
            f"Проект: {preview.project_name}",
            f"Режим: {preview.mode_label}",
            f"Вывод: {preview.output_dir}",
            f"Абзацы: {preview.selected_paragraphs}/{preview.paragraphs_total}",
            f"Провайдеры: {', '.join(translate_provider(item) for item in preview.providers) if preview.providers else 'нет'}",
            "",
            *preview.summary_lines,
        ]
        if preview.warnings:
            lines.extend(
                [
                    "",
                    "Предупреждения:",
                    *[f"- {warning}" for warning in preview.warnings],
                ]
            )
        self.preview_text.setPlainText("\n".join(lines))

    def _render_run_progress(self, progress) -> None:
        if progress is None:
            self.run_summary_label.setText("Нет активного запуска")
            self.run_detail_label.setText("Откройте сценарий и нажмите Старт")
            self.run_paths_label.setText("")
            self.run_eta_label.setText("")
            self.run_totals_label.setText("")
            self.run_progress.setValue(0)
            self._current_downloads_root = ""
            self._current_videos_dir = ""
            self._current_images_dir = ""
            self.open_downloads_button.setEnabled(False)
            return
        self.run_summary_label.setText(
            f"{translate_run_status(progress.status)} · обработано {progress.paragraphs_processed} из {progress.paragraphs_total} абзацев"
        )
        stage = translate_run_stage(progress.current_stage)
        current = (
            f"абзац {progress.current_paragraph_no}"
            if progress.current_paragraph_no is not None
            else "ожидание"
        )
        self.run_detail_label.setText(
            f"{translate_run_status(progress.status)} - {stage} - {current}"
        )
        self._current_downloads_root = progress.downloads_root
        self._current_videos_dir = progress.videos_dir
        self._current_images_dir = progress.images_dir
        self.run_paths_label.setText(
            (
                f"Папка выгрузки: {progress.downloads_root}"
                if progress.downloads_root
                else "После завершения прогона проверьте папку downloads."
            )
        )
        self.run_eta_label.setText(progress.eta_text)
        self.run_totals_label.setText(
            "С файлами: "
            f"{progress.paragraphs_completed} · "
            f"Без файлов: {progress.paragraphs_no_match} · "
            f"Ошибки: {progress.paragraphs_failed} · "
            f"Видео: {progress.downloaded_video_files} · "
            f"Изображения: {progress.downloaded_image_files}"
        )
        self.run_progress.setValue(int(progress.percent_complete))
        self.open_downloads_button.setEnabled(bool(progress.downloads_root))

    def _paragraph_signature(
        self, paragraph_items
    ) -> tuple[tuple[int, str, str, int, bool], ...]:
        return tuple(
            (
                item.paragraph_no,
                item.status,
                item.result_note,
                len(item.downloaded_files),
                item.numbering_valid,
            )
            for item in paragraph_items
        )

    def _render_current_paragraph_detail(self, paragraph_items) -> None:
        if not paragraph_items:
            self._clear_paragraph_detail()
            return
        selected_paragraph_no = self._current_paragraph_number()
        if selected_paragraph_no is not None:
            for item in paragraph_items:
                if item.paragraph_no == selected_paragraph_no:
                    self._render_paragraph_detail(item)
                    return
        self._render_paragraph_detail(paragraph_items[0])

    def _render_session(self, session) -> None:
        lines = [
            f"Состояние: {translate_session_health(session.health)}",
            f"Аккаунт: {session.account or 'вход не выполнен'}",
            f"Браузер готов: {yes_no(session.browser_ready)}",
            f"Окно входа открыто: {yes_no(session.native_login_running)}",
            f"Текущий URL: {session.current_url or '-'}",
        ]
        if session.native_debug_port is not None:
            lines.append(f"Debug port: {session.native_debug_port}")
        if session.imported_source:
            lines.append(f"Импортировано из: {session.imported_source}")
        if session.imported_profile_name:
            lines.append(f"Импортированный профиль: {session.imported_profile_name}")
        if session.imported_at:
            lines.append(f"Дата импорта: {session.imported_at}")
        if session.reason_code:
            lines.append(f"Reason code: {session.reason_code}")
        if session.manual_ready_override:
            lines.append(
                f"Ручное подтверждение: да ({session.manual_ready_override_note or 'подтверждено оператором'})"
            )
        if session.diagnostic_lines:
            lines.extend(["", "Диагностика:", *session.diagnostic_lines])
        if session.manual_prompt:
            lines.extend(["", session.manual_prompt])
        if session.last_error:
            lines.extend(["", f"Последняя ошибка: {session.last_error}"])
        self.session_text.setPlainText("\n".join(lines))

    def _render_paragraph_detail(self, item) -> None:
        detail_lines = [
            f"Абзац #{item.paragraph_no}",
            f"Статус: {translate_paragraph_status(item.status)}",
        ]
        if item.validation_issues:
            detail_lines.extend(
                [
                    "Проблемы валидации:",
                    *[f"- {issue}" for issue in item.validation_issues],
                ]
            )
        detail_lines.extend(["", item.text])
        self.paragraph_text.setPlainText("\n".join(detail_lines))
        self.video_queries_text.setPlainText("\n".join(item.video_queries))
        self.image_queries_text.setPlainText("\n".join(item.image_queries))
        self.paragraph_status_value.setText(translate_paragraph_status(item.status))
        self.paragraph_note_value.setText(item.result_note or "-")
        self.downloads_root_value.setText(self._current_downloads_root or "-")
        self.videos_dir_value.setText(self._current_videos_dir or "-")
        self.images_dir_value.setText(self._current_images_dir or "-")
        self.downloaded_files_list.clear()
        for asset in item.downloaded_files:
            path_suffix = f" | {asset.local_path}" if asset.local_path else ""
            exists_suffix = "" if asset.exists else " | файл не найден"
            self.downloaded_files_list.addItem(
                f"{translate_provider(asset.provider_name)} | {asset.role} | {translate_asset_kind(asset.kind)} | {asset.title}{path_suffix}{exists_suffix}"
            )

    def _clear_paragraph_detail(self) -> None:
        self.paragraph_status_value.setText("-")
        self.paragraph_note_value.setText("-")
        self.downloads_root_value.setText(self._current_downloads_root or "-")
        self.videos_dir_value.setText(self._current_videos_dir or "-")
        self.images_dir_value.setText(self._current_images_dir or "-")
        self.paragraph_text.setPlainText("")
        self.downloaded_files_list.clear()

    def on_browse_script(self) -> None:
        path, _selected = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Открыть сценарий",
            str(Path.cwd()),
            "Документы Word (*.docx);;Все файлы (*)",
        )
        if not path:
            return
        self.script_path_edit.setText(path)
        if not self.project_name_edit.text().strip():
            self.project_name_edit.setText(Path(path).stem.replace("_", " "))

    def on_browse_output_dir(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Выберите папку вывода",
            self.output_dir_edit.text() or str(Path.cwd()),
        )
        if path:
            self.output_dir_edit.setText(path)

    def on_open_script(self) -> None:
        try:
            summary = self.controller.open_script(
                self.script_path_edit.text(),
                project_name=self.project_name_edit.text().strip() or None,
            )
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.active_project_id = summary.project_id
        self.refresh(preserve_forms=True)

    def on_start_run(self) -> None:
        if self.active_project_id is None:
            self.on_open_script()
            if self.active_project_id is None:
                return
        try:
            run_id = self.controller.start_run_async(
                self.active_project_id, self._quick_form(), self._advanced_form()
            )
        except Exception as exc:
            self._apply_storyblocks_parallelism_autofix(exc)
            self._show_notification(handle_ui_error(exc))
            return
        self.active_run_id = run_id
        self.refresh(preserve_forms=True)

    def _apply_storyblocks_parallelism_autofix(self, exc: Exception) -> None:
        if not isinstance(exc, AppError):
            return
        if exc.code != "storyblocks_parallelism_guard":
            return
        self.launch_profile_combo.setCurrentText(self._launch_profile_labels["normal"])

    def on_abort_run(self) -> None:
        if self.active_run_id is None:
            return
        try:
            self.controller.cancel_run(self.active_run_id)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.refresh(preserve_forms=True)

    def on_rerun_full_run(self) -> None:
        if self.active_project_id is None:
            return
        try:
            self.active_run_id = self.controller.rerun_full_run_async(
                self.active_project_id,
                self._quick_form(),
                self._advanced_form(),
            )
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.refresh(preserve_forms=True)

    def on_save_preset(self) -> None:
        name = self.preset_name_edit.text().strip()
        if not name:
            self._show_notification(handle_ui_error(ValueError("Укажите имя пресета.")))
            return
        try:
            self.controller.save_preset(name, self._quick_form(), self._advanced_form())
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.refresh()

    def on_load_preset(self) -> None:
        name = self.preset_name_edit.text().strip()
        if not name:
            self._show_notification(handle_ui_error(ValueError("Укажите имя пресета.")))
            return
        try:
            quick, advanced = self.controller.load_preset(name)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self._apply_quick_form(quick)
        self._apply_advanced_form(advanced)
        self.refresh_preview()

    def on_export_preset(self) -> None:
        name = self.preset_name_edit.text().strip()
        if not name:
            self._show_notification(handle_ui_error(ValueError("Укажите имя пресета.")))
            return
        path, _selected = QtWidgets.QFileDialog.getSaveFileName(
            self, "Экспорт пресета", f"{name}.json", "JSON files (*.json)"
        )
        if not path:
            return
        try:
            self.controller.export_preset(name, path)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        QtWidgets.QMessageBox.information(self, "Пресет", "Пресет экспортирован")

    def on_import_preset(self) -> None:
        path, _selected = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Импорт пресета",
            str(Path.cwd()),
            "JSON files (*.json);;Все файлы (*)",
        )
        if not path:
            return
        try:
            preset = self.controller.import_preset(path)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.preset_name_edit.setText(preset.name)
        self.refresh()

    def on_store_gemini_key(self) -> None:
        self._show_notification(
            self.controller.set_gemini_key(self.gemini_key_edit.text())
        )

    def on_clear_gemini_key(self) -> None:
        self.controller.delete_gemini_key()
        self.gemini_key_edit.clear()
        QtWidgets.QMessageBox.information(self, "Ключ Gemini", "Ключ Gemini удален")

    def on_store_provider_key(self, provider_id: str) -> None:
        edit = self.stock_key_edits.get(provider_id)
        if edit is None:
            return
        self._show_notification(
            self.controller.set_provider_api_key(provider_id, edit.text())
        )

    def on_clear_provider_key(self, provider_id: str) -> None:
        edit = self.stock_key_edits.get(provider_id)
        if edit is not None:
            edit.clear()
        self._show_notification(self.controller.delete_provider_api_key(provider_id))

    def on_prepare_login(self) -> None:
        self._run_session_action(self.controller.prepare_storyblocks_login)

    def on_import_session(self) -> None:
        self._import_existing_session()

    def on_logout(self) -> None:
        self._run_session_action(self.controller.logout_storyblocks)

    def on_check_session(self) -> None:
        self._run_session_action(self.controller.check_storyblocks_session)

    def on_reset_session(self) -> None:
        self._run_session_action(self.controller.reset_storyblocks_session)

    def on_mark_session_ready(self) -> None:
        self._run_session_action(self.controller.mark_storyblocks_session_ready)

    def on_clear_session_override(self) -> None:
        self._run_session_action(self.controller.clear_storyblocks_session_override)

    def on_enrich_project_intents(self) -> None:
        if self.active_project_id is None:
            self.on_open_script()
            if self.active_project_id is None:
                return
        try:
            self.controller.enrich_project_intents_with_ai(
                self.active_project_id,
                self._quick_form(),
                self._advanced_form(),
            )
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.refresh(preserve_forms=True)

    def on_project_selected(self) -> None:
        row = self.project_list.currentRow()
        if row < 0 or row >= len(self._project_ids_by_row):
            return
        self.active_project_id = self._project_ids_by_row[row]
        self.refresh()

    def on_history_selected(self) -> None:
        row = self.history_list.currentRow()
        if row < 0 or row >= len(self._history_ids_by_row):
            return
        self.active_run_id, self.active_project_id = self._history_ids_by_row[row]
        self.main_tabs.setCurrentIndex(0)
        self.refresh()

    def on_export_logs(self) -> None:
        if self.active_run_id is None:
            return
        default_name = f"run-{self.active_run_id}.log"
        path, _selected = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Сохранить журнал запуска",
            default_name,
            "Log files (*.log);;Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        try:
            saved = self.controller.export_run_log(self.active_run_id, path)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        QtWidgets.QMessageBox.information(
            self,
            "Лог запуска",
            f"Лог сохранен: {saved}",
        )

    def on_paragraph_selected(self) -> None:
        if self.active_project_id is None:
            return
        row = self.paragraph_list.currentRow()
        if row < 0 or row >= len(self._paragraph_numbers_by_row):
            return
        paragraph_no = self._paragraph_numbers_by_row[row]
        item = self.controller.build_paragraph_detail(
            self.active_project_id, paragraph_no, self.active_run_id
        )
        if item is not None:
            self._render_paragraph_detail(item)

    def on_open_downloads_folder(self) -> None:
        path = self._current_downloads_root.strip()
        if not path:
            QtWidgets.QMessageBox.information(
                self,
                "Папка выгрузки",
                "Путь к папке выгрузки появится после создания summary для запуска.",
            )
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def on_save_queries(self) -> None:
        if self.active_project_id is None:
            return
        paragraph_no = self._current_paragraph_number()
        if paragraph_no is None:
            return
        try:
            self.controller.update_paragraph_queries(
                self.active_project_id,
                paragraph_no,
                video_queries=self.video_queries_text.toPlainText().splitlines(),
                image_queries=self.image_queries_text.toPlainText().splitlines(),
            )
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.refresh()

    def _current_paragraph_number(self) -> int | None:
        row = self.paragraph_list.currentRow()
        if row < 0 or row >= len(self._paragraph_numbers_by_row):
            return None
        return self._paragraph_numbers_by_row[row]

    def _apply_quick_form(self, quick: UiQuickLaunchSettingsViewModel) -> None:
        self.project_name_edit.setText(quick.project_name)
        self.script_path_edit.setText(quick.script_path)
        self.output_dir_edit.setText(quick.output_dir)
        self.paragraph_selection_edit.setText(quick.paragraph_selection_text)
        self.mode_combo.setCurrentText(
            self._mode_labels.get(quick.mode_id, self._mode_labels["sb_video_only"])
        )
        self.launch_profile_combo.setCurrentText(
            self._launch_profile_labels.get(
                quick.launch_profile_id,
                self._launch_profile_labels["normal"],
            )
        )
        self.strictness_combo.setCurrentText(label_for_strictness(quick.strictness))
        self.supporting_image_spin.setValue(quick.supporting_image_limit)
        self.fallback_image_spin.setValue(quick.fallback_image_limit)
        self.manual_prompt_edit.setPlainText(quick.manual_prompt)
        self.attach_full_script_context_checkbox.setChecked(
            quick.attach_full_script_context
        )
        selected_free = set(quick.provider_ids)
        for provider_id, checkbox in self.free_provider_checks.items():
            checkbox.setChecked(provider_id in selected_free)

    def _apply_advanced_form(self, advanced: UiAdvancedSettingsViewModel) -> None:
        self.action_delay_spin.setValue(advanced.action_delay_ms)
        self.launch_timeout_spin.setValue(advanced.launch_timeout_ms)
        self.navigation_timeout_spin.setValue(advanced.navigation_timeout_ms)
        self.download_timeout_spin.setValue(advanced.downloads_timeout_seconds)
        self._sync_custom_timing_visibility()

    def _run_session_action(self, action) -> None:
        try:
            session = action()
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self._render_session(session)
        self.refresh()

    def _import_existing_session(self, browser_name: str | None = None) -> None:
        try:
            options = self.controller.discover_importable_browser_profiles(browser_name)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return

        selected_path = ""
        selected_browser = browser_name
        if options:
            labels = [item.display_label for item in options]
            label, accepted = QtWidgets.QInputDialog.getItem(
                self,
                "Импорт профиля браузера",
                "Выберите внешний профиль Chrome или Edge для импорта:",
                labels,
                0,
                False,
            )
            if not accepted:
                return
            selected = next(item for item in options if item.display_label == label)
            selected_path = selected.profile_dir
            selected_browser = selected.browser_name
        else:
            caption = "Выберите папку внешнего профиля Chrome/Edge (Default/Profile X)"
            selected_path = QtWidgets.QFileDialog.getExistingDirectory(
                self, caption, str(Path.home())
            )
            if not selected_path:
                return
        self._run_session_action(
            lambda selected_path=selected_path, selected_browser=selected_browser: (
                self.controller.import_storyblocks_session_from_path(
                    selected_path,
                    browser_name=selected_browser,
                )
            )
        )

    def _show_notification(self, notification) -> None:
        self.status_label.setText(notification.message)
        icon = QtWidgets.QMessageBox.Icon.Information
        if notification.severity == "error":
            icon = QtWidgets.QMessageBox.Icon.Critical
        elif notification.severity == "warning":
            icon = QtWidgets.QMessageBox.Icon.Warning
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(notification.title)
        box.setText(notification.message)
        box.setIcon(icon)
        box.exec()

    def on_theme_changed(self, label: str) -> None:
        theme_id = normalize_ui_theme(self._theme_ids_by_label.get(label, "dark"))
        if theme_id == self._theme_id:
            return
        self._theme_id = self.controller.set_ui_theme(theme_id)
        self._apply_theme(self._theme_id)

    def _apply_theme(self, theme_id: str) -> None:
        theme = get_ui_theme(theme_id)
        app = cast(QtWidgets.QApplication | None, QtWidgets.QApplication.instance())
        if app is not None:
            app.setStyle("Fusion")
            palette = QtGui.QPalette()
            palette.setColor(
                QtGui.QPalette.ColorRole.Window, QtGui.QColor(theme.window_bg)
            )
            palette.setColor(
                QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(theme.text)
            )
            palette.setColor(
                QtGui.QPalette.ColorRole.Base, QtGui.QColor(theme.input_bg)
            )
            palette.setColor(
                QtGui.QPalette.ColorRole.AlternateBase,
                QtGui.QColor(theme.surface_alt_bg),
            )
            palette.setColor(
                QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(theme.surface_bg)
            )
            palette.setColor(
                QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(theme.text)
            )
            palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(theme.text))
            palette.setColor(
                QtGui.QPalette.ColorRole.Button, QtGui.QColor(theme.surface_bg)
            )
            palette.setColor(
                QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(theme.text)
            )
            palette.setColor(
                QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(theme.selection)
            )
            palette.setColor(
                QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(theme.text)
            )
            app.setPalette(palette)
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{
                background: {theme.window_bg};
                color: {theme.text};
                font-family: 'Segoe UI';
                font-size: 13px;
            }}
            QLabel#WindowTitle {{
                font-size: 24px;
                font-weight: 700;
                color: {theme.text};
            }}
            QLabel#WindowSubtitle {{
                color: {theme.muted_text};
                font-size: 12px;
            }}
            QLabel#RunSummary {{
                font-size: 22px;
                font-weight: 700;
                color: {theme.text};
            }}
            QToolBar {{
                background: {theme.surface_bg};
                border: 1px solid {theme.border};
                spacing: 6px;
                padding: 8px;
            }}
            QToolButton {{
                background: {theme.surface_alt_bg};
                border: 1px solid {theme.border};
                border-radius: 8px;
                padding: 8px 12px;
            }}
            QToolButton:hover {{
                background: {theme.selection};
            }}
            QLineEdit, QPlainTextEdit, QListWidget, QComboBox, QSpinBox, QDoubleSpinBox {{
                background: {theme.input_bg};
                border: 1px solid {theme.border};
                border-radius: 8px;
                padding: 6px;
                color: {theme.text};
                selection-background-color: {theme.selection};
            }}
            QTabWidget::pane, QGroupBox {{
                background: {theme.surface_bg};
                border: 1px solid {theme.border};
                border-radius: 12px;
                margin-top: 12px;
                padding-top: 8px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 14px;
                padding: 0 6px;
                color: {theme.muted_text};
            }}
            QTabBar::tab {{
                background: {theme.surface_alt_bg};
                border: 1px solid {theme.border};
                padding: 8px 14px;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
                margin-right: 4px;
            }}
            QTabBar::tab:selected {{
                background: {theme.accent};
                color: #ffffff;
                border-color: {theme.accent};
            }}
            QPushButton {{
                background: {theme.surface_alt_bg};
                border: 1px solid {theme.border};
                border-radius: 8px;
                padding: 8px 12px;
                color: {theme.text};
            }}
            QPushButton:hover {{
                background: {theme.selection};
            }}
            QPushButton:pressed {{
                background: {theme.accent_pressed};
                color: #ffffff;
            }}
            QPushButton:disabled {{
                color: {theme.muted_text};
                background: {theme.surface_bg};
            }}
            QProgressBar {{
                border: 1px solid {theme.border};
                border-radius: 8px;
                background: {theme.input_bg};
                text-align: center;
            }}
            QProgressBar::chunk {{
                background: {theme.accent};
                border-radius: 7px;
            }}
            """
        )


def launch_pyside_app(controller: DesktopGuiController) -> None:
    app = QtWidgets.QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QtWidgets.QApplication([])
    window = DesktopQtApp(controller)
    window.show()
    if owns_app:
        app.exec()


def _wrap_layout(layout: QtWidgets.QLayout) -> QtWidgets.QWidget:
    widget = QtWidgets.QWidget()
    widget.setLayout(layout)
    return widget


def _spin_box(minimum: int, maximum: int, value: int) -> QtWidgets.QSpinBox:
    widget = QtWidgets.QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    return widget
