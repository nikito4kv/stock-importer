from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from domain.project_modes import list_project_modes
from services.errors import AppError

from .contracts import UiAdvancedSettingsViewModel, UiQuickLaunchSettingsViewModel
from .controller import DesktopGuiController, handle_ui_error
from .polling import TERMINAL_RUN_STATUSES, plan_poll_refresh
from .presentation import (
    STRICTNESS_LABELS,
    THEME_LABELS,
    get_ui_theme,
    label_for_strictness,
    label_for_theme,
    normalize_ui_theme,
    translate_asset_role,
    translate_paragraph_status,
    translate_provider,
    translate_run_stage,
    translate_run_status,
    translate_session_health,
    translate_severity,
    yes_no,
)


class DesktopTkApp:
    def __init__(self, controller: DesktopGuiController):
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
        self._theme_ids_by_label = {
            label_for_theme(value): value for value in THEME_LABELS
        }
        self.root = tk.Tk()
        self.root.title("Vid Img Downloader")
        self.root.geometry("1480x920")
        self.root.minsize(1180, 760)

        self.active_project_id: str | None = None
        self.active_run_id: str | None = None
        self._session_buttons: list[ttk.Button] = []
        self._text_widgets: list[tk.Text] = []
        self._last_paragraph_signature: tuple[tuple[int, str, str, bool], ...] = ()
        self._last_journal_signature: tuple[int, str, str] = (0, "", "")
        self._terminal_refresh_signature: tuple[str | None, str | None] | None = None
        self._poll_interval_active_ms = 500
        self._poll_interval_idle_ms = 1600
        self._theme_id = self.controller.get_ui_theme()

        self._build_style()
        self._init_vars()
        self._build_layout()
        self._apply_theme(self._theme_id)
        self.refresh()
        self.root.after(self._poll_interval_idle_ms, self._poll_refresh)

    def run(self) -> None:
        self.root.mainloop()

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

    def _init_vars(self) -> None:
        self.status_var = tk.StringVar(value="Готово")
        self.project_name_var = tk.StringVar()
        self.script_path_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.paragraph_selection_var = tk.StringVar()
        self.mode_label_var = tk.StringVar(value=self._mode_labels["sb_video_only"])
        self.strictness_var = tk.StringVar(value=label_for_strictness("balanced"))
        self.theme_var = tk.StringVar(value=label_for_theme(self._theme_id))
        self.slow_mode_var = tk.BooleanVar(value=True)
        self.supporting_image_limit_var = tk.IntVar(value=1)
        self.fallback_image_limit_var = tk.IntVar(value=1)
        self.attach_full_script_context_var = tk.BooleanVar(value=False)
        self.preset_name_var = tk.StringVar()
        self.gemini_key_var = tk.StringVar()
        self.pexels_key_var = tk.StringVar()
        self.pixabay_key_var = tk.StringVar()
        self.run_summary_var = tk.StringVar(value="Нет активного запуска")
        self.run_detail_var = tk.StringVar(value="")
        self.run_eta_var = tk.StringVar(value="")
        self.run_checkpoint_var = tk.StringVar(value="")
        self.free_provider_vars = {
            "pexels": tk.BooleanVar(value=True),
            "pixabay": tk.BooleanVar(value=True),
            "openverse": tk.BooleanVar(value=True),
            "wikimedia": tk.BooleanVar(value=True),
            "bing": tk.BooleanVar(value=False),
        }

        self.paragraph_workers_var = tk.IntVar(value=1)
        self.provider_workers_var = tk.IntVar(value=4)
        self.download_workers_var = tk.IntVar(value=4)
        self.relevance_workers_var = tk.IntVar(value=2)
        self.queue_size_var = tk.IntVar(value=8)
        self.launch_timeout_var = tk.IntVar(value=45000)
        self.navigation_timeout_var = tk.IntVar(value=30000)
        self.download_timeout_var = tk.DoubleVar(value=120.0)
        self.no_match_budget_var = tk.DoubleVar(value=20.0)
        self.top_k_var = tk.IntVar(value=24)
        self.retry_budget_var = tk.IntVar(value=2)
        self.full_script_context_budget_var = tk.IntVar(value=12000)
        self.cache_root_var = tk.StringVar()
        self.browser_profile_path_var = tk.StringVar()
        self.allow_generic_web_image_var = tk.BooleanVar(value=False)

    def _build_layout(self) -> None:
        root_frame = ttk.Frame(self.root, padding=14)
        root_frame.pack(fill="both", expand=True)

        header = ttk.Frame(root_frame)
        header.pack(fill="x")
        title_block = ttk.Frame(header)
        title_block.pack(side="left")
        ttk.Label(
            title_block, text="Медиа-станция по абзацам", style="Header.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            title_block,
            text="Темная тема, русский интерфейс и только нужные действия",
            style="Muted.TLabel",
        ).pack(anchor="w")
        ttk.Label(header, textvariable=self.status_var).pack(side="right")
        ttk.Label(header, text="Тема", style="SubHeader.TLabel").pack(
            side="right", padx=(0, 8)
        )
        self.theme_combobox = ttk.Combobox(
            header,
            textvariable=self.theme_var,
            values=[label_for_theme(item) for item in THEME_LABELS],
            state="readonly",
            width=14,
        )
        self.theme_combobox.pack(side="right", padx=(0, 14))
        self.theme_combobox.bind("<<ComboboxSelected>>", self.on_theme_changed)

        toolbar = ttk.Frame(root_frame, padding=(0, 10, 0, 10))
        toolbar.pack(fill="x")
        for text, command, style_name in (
            ("Открыть сценарий", self.on_open_script, "Accent.TButton"),
            ("Старт", self.on_start_run, "Accent.TButton"),
            ("Продолжить", self.on_resume_run, "TButton"),
            ("Пауза", self.on_pause_run, "TButton"),
            ("Остановить", self.on_abort_run, "TButton"),
            ("Повторить ошибки", self.on_retry_failed, "TButton"),
            ("Проверить сессию", self.on_check_session, "TButton"),
        ):
            ttk.Button(toolbar, text=text, command=command, style=style_name).pack(
                side="left", padx=(0, 8)
            )

        notebook = ttk.Notebook(root_frame)
        notebook.pack(fill="both", expand=True)

        main_tab = ttk.Frame(notebook, padding=10)
        keys_tab = ttk.Frame(notebook, padding=10)
        session_tab = ttk.Frame(notebook, padding=10)
        advanced_tab = ttk.Frame(notebook, padding=10)
        history_tab = ttk.Frame(notebook, padding=10)
        notebook.add(main_tab, text="Главная")
        notebook.add(keys_tab, text="Ключи API")
        notebook.add(session_tab, text="Сессия")
        notebook.add(advanced_tab, text="Эксперт")
        notebook.add(history_tab, text="История")

        self.main_notebook = notebook
        self._build_main_tab(main_tab)
        self._build_api_keys_tab(keys_tab)
        self._build_session_tab(session_tab)
        self._build_advanced_tab(advanced_tab)
        self._build_history_tab(history_tab)

    def _build_main_tab(self, parent: ttk.Frame) -> None:
        form = ttk.Frame(parent)
        form.pack(fill="x")
        self._labeled_entry(form, "Название проекта", self.project_name_var)
        self._labeled_entry(
            form,
            "Файл сценария",
            self.script_path_var,
            button_text="Обзор",
            button_command=self.on_browse_script,
        )
        self._labeled_entry(
            form,
            "Папка вывода",
            self.output_dir_var,
            button_text="Обзор",
            button_command=self.on_browse_output_dir,
        )
        self._labeled_entry(form, "Абзацы", self.paragraph_selection_var)

        mode_row = ttk.Frame(parent, padding=(0, 8, 0, 8))
        mode_row.pack(fill="x")
        ttk.Label(mode_row, text="Режим проекта", style="SubHeader.TLabel").pack(
            anchor="w"
        )
        self.mode_combobox = ttk.Combobox(
            mode_row,
            textvariable=self.mode_label_var,
            values=[item.label for item in self._mode_definitions],
            state="readonly",
        )
        self.mode_combobox.pack(fill="x")
        self.mode_combobox.bind(
            "<<ComboboxSelected>>", lambda _event: self.refresh_preview()
        )

        counts_frame = ttk.LabelFrame(parent, text="Media counts", padding=8)
        counts_frame.pack(fill="x", pady=(8, 8))
        self._labeled_entry(
            counts_frame,
            "Основные изображения",
            self.supporting_image_limit_var,
        )
        self._labeled_entry(
            counts_frame,
            "Резервные изображения",
            self.fallback_image_limit_var,
        )

        ai_frame = ttk.LabelFrame(parent, text="Gemini control", padding=8)
        ai_frame.pack(fill="x", pady=(0, 8))
        self.manual_prompt_text = ScrolledText(ai_frame, height=4, wrap="word")
        self.manual_prompt_text.pack(fill="x", pady=(0, 6))
        self._text_widgets.append(self.manual_prompt_text)
        ttk.Checkbutton(
            ai_frame,
            text="Прикреплять весь сценарий как контекст",
            variable=self.attach_full_script_context_var,
            command=self.refresh_preview,
        ).pack(anchor="w", pady=(0, 6))
        ttk.Button(
            ai_frame,
            text="Обновить intent через Gemini",
            command=self.on_enrich_project_intents,
        ).pack(anchor="w")

        ready_frame = ttk.LabelFrame(parent, text="Перед запуском", padding=8)
        ready_frame.pack(fill="x", pady=(8, 8))
        self.preview_text = ScrolledText(ready_frame, height=6, wrap="word")
        self.preview_text.pack(fill="x")
        self._text_widgets.append(self.preview_text)

        self._build_workspace(parent)

    def _build_api_keys_tab(self, parent: ttk.Frame) -> None:
        key_frame = ttk.LabelFrame(parent, text="Gemini", padding=8)
        key_frame.pack(fill="x", pady=(0, 8))
        ttk.Entry(key_frame, textvariable=self.gemini_key_var, show="*").pack(
            fill="x", pady=(0, 6)
        )
        key_buttons = ttk.Frame(key_frame)
        key_buttons.pack(fill="x")
        ttk.Button(
            key_buttons, text="Сохранить", command=self.on_store_gemini_key
        ).pack(side="left", padx=(0, 6))
        ttk.Button(key_buttons, text="Удалить", command=self.on_clear_gemini_key).pack(
            side="left"
        )

        stock_frame = ttk.LabelFrame(parent, text="Ключи стоков", padding=8)
        stock_frame.pack(fill="x", pady=(0, 8))
        self._provider_key_row(
            stock_frame,
            "Pexels API key",
            self.pexels_key_var,
            lambda: self.on_store_provider_key("pexels"),
            lambda: self.on_clear_provider_key("pexels"),
        )
        self._provider_key_row(
            stock_frame,
            "Pixabay API key",
            self.pixabay_key_var,
            lambda: self.on_store_provider_key("pixabay"),
            lambda: self.on_clear_provider_key("pixabay"),
        )

    def _build_advanced_tab(self, parent: ttk.Frame) -> None:
        strictness_row = ttk.Frame(parent)
        strictness_row.pack(fill="x", pady=(0, 8))
        ttk.Label(strictness_row, text="Строгость", style="SubHeader.TLabel").pack(
            anchor="w"
        )
        ttk.Combobox(
            strictness_row,
            textvariable=self.strictness_var,
            values=[label_for_strictness(item) for item in STRICTNESS_LABELS],
            state="readonly",
        ).pack(fill="x")

        ttk.Checkbutton(
            parent,
            text="Медленный режим браузера",
            variable=self.slow_mode_var,
            command=self.refresh_preview,
        ).pack(anchor="w", pady=(0, 8))

        providers_box = ttk.LabelFrame(
            parent, text="Источники бесплатных изображений", padding=8
        )
        providers_box.pack(fill="x", pady=(0, 8))
        ttk.Label(
            providers_box,
            text="Используются только в режимах с бесплатными изображениями.",
            style="SubHeader.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        for provider_id, label in (
            ("pexels", "Pexels"),
            ("pixabay", "Pixabay"),
            ("openverse", "Openverse"),
            ("wikimedia", "Wikimedia Commons"),
            ("bing", "Bing (обычные веб-изображения)"),
        ):
            ttk.Checkbutton(
                providers_box,
                text=label,
                variable=self.free_provider_vars[provider_id],
                command=self.refresh_preview,
            ).pack(anchor="w")

        preset_frame = ttk.LabelFrame(parent, text="Пресеты", padding=8)
        preset_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(
            preset_frame,
            text="Сохраняйте часто используемые наборы настроек.",
            style="SubHeader.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Entry(preset_frame, textvariable=self.preset_name_var).pack(
            fill="x", pady=(0, 6)
        )
        preset_buttons = ttk.Frame(preset_frame)
        preset_buttons.pack(fill="x")
        ttk.Button(preset_buttons, text="Сохранить", command=self.on_save_preset).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(preset_buttons, text="Загрузить", command=self.on_load_preset).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(preset_buttons, text="Экспорт", command=self.on_export_preset).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(preset_buttons, text="Импорт", command=self.on_import_preset).pack(
            side="left"
        )

        fields = (
            ("Потоки абзацев", self.paragraph_workers_var),
            ("Потоки провайдеров", self.provider_workers_var),
            ("Потоки скачивания", self.download_workers_var),
            ("Потоки релевантности", self.relevance_workers_var),
            ("Размер очереди", self.queue_size_var),
            ("Таймаут запуска, мс", self.launch_timeout_var),
            ("Таймаут навигации, мс", self.navigation_timeout_var),
            ("Таймаут скачивания, с", self.download_timeout_var),
            ("Лимит no-match, с", self.no_match_budget_var),
            ("Top-K релевантности", self.top_k_var),
            ("Лимит повторов", self.retry_budget_var),
            (
                "Бюджет full-script context",
                self.full_script_context_budget_var,
            ),
        )
        technical_frame = ttk.LabelFrame(
            parent, text="Технические параметры", padding=8
        )
        technical_frame.pack(fill="x", pady=(0, 8))
        for label, variable in fields:
            self._labeled_entry(technical_frame, label, variable)
        self._labeled_entry(technical_frame, "Папка кэша", self.cache_root_var)
        self._labeled_entry(
            technical_frame, "Путь к профилю браузера", self.browser_profile_path_var
        )
        ttk.Checkbutton(
            technical_frame,
            text="Разрешить обычный веб-поиск изображений",
            variable=self.allow_generic_web_image_var,
            command=self.refresh_preview,
        ).pack(anchor="w", pady=(8, 0))

        providers_box = ttk.LabelFrame(
            parent, text="Источники бесплатных изображений", padding=8
        )
        providers_box.pack(fill="x", pady=(0, 8))
        ttk.Label(
            providers_box,
            text="Используются только в режимах с бесплатными изображениями.",
            style="SubHeader.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        for provider_id, label in (
            ("pexels", "Pexels"),
            ("pixabay", "Pixabay"),
            ("openverse", "Openverse"),
            ("wikimedia", "Wikimedia Commons"),
            ("bing", "Bing (обычные веб-изображения)"),
        ):
            ttk.Checkbutton(
                providers_box,
                text=label,
                variable=self.free_provider_vars[provider_id],
                command=self.refresh_preview,
            ).pack(anchor="w")

        ttk.Checkbutton(
            parent,
            text="Медленный режим браузера",
            variable=self.slow_mode_var,
            command=self.refresh_preview,
        ).pack(anchor="w")

        strictness_row = ttk.Frame(parent)
        strictness_row.pack(fill="x", pady=(0, 8))
        ttk.Label(strictness_row, text="Строгость", style="SubHeader.TLabel").pack(
            anchor="w"
        )
        ttk.Combobox(
            strictness_row,
            textvariable=self.strictness_var,
            values=[label_for_strictness(item) for item in STRICTNESS_LABELS],
            state="readonly",
        ).pack(fill="x")

        preset_frame = ttk.LabelFrame(parent, text="Пресеты", padding=8)
        preset_frame.pack(fill="x", pady=(0, 8))
        ttk.Entry(preset_frame, textvariable=self.preset_name_var).pack(
            fill="x", pady=(0, 6)
        )
        preset_buttons = ttk.Frame(preset_frame)
        preset_buttons.pack(fill="x")
        ttk.Button(preset_buttons, text="Сохранить", command=self.on_save_preset).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(preset_buttons, text="Загрузить", command=self.on_load_preset).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(preset_buttons, text="Экспорт", command=self.on_export_preset).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(preset_buttons, text="Импорт", command=self.on_import_preset).pack(
            side="left"
        )

        key_frame = ttk.LabelFrame(parent, text="Ключ Gemini", padding=8)
        key_frame.pack(fill="x", pady=(0, 8))
        ttk.Entry(key_frame, textvariable=self.gemini_key_var, show="*").pack(
            fill="x", pady=(0, 6)
        )
        key_buttons = ttk.Frame(key_frame)
        key_buttons.pack(fill="x")
        ttk.Button(
            key_buttons, text="Сохранить", command=self.on_store_gemini_key
        ).pack(side="left", padx=(0, 6))
        ttk.Button(key_buttons, text="Удалить", command=self.on_clear_gemini_key).pack(
            side="left"
        )

        stock_frame = ttk.LabelFrame(parent, text="Ключи стоков", padding=8)
        stock_frame.pack(fill="x", pady=(0, 8))
        self._provider_key_row(
            stock_frame,
            "Pexels API key",
            self.pexels_key_var,
            lambda: self.on_store_provider_key("pexels"),
            lambda: self.on_clear_provider_key("pexels"),
        )
        self._provider_key_row(
            stock_frame,
            "Pixabay API key",
            self.pixabay_key_var,
            lambda: self.on_store_provider_key("pixabay"),
            lambda: self.on_clear_provider_key("pixabay"),
        )

    def _build_session_tab(self, parent: ttk.Frame) -> None:
        self.session_summary = ScrolledText(parent, height=12, wrap="word")
        self.session_summary.pack(fill="both", expand=True, pady=(0, 8))
        self._text_widgets.append(self.session_summary)

        buttons = ttk.Frame(parent)
        buttons.pack(fill="x")
        for text, command in (
            ("Войти в браузере", self.on_prepare_login),
            ("Проверить сессию", self.on_check_session),
            ("Открыть браузер Storyblocks", self.on_open_browser),
            ("Выйти", self.on_logout),
            ("Сменить аккаунт", self.on_switch_account),
            ("Сбросить сессию", self.on_reset_session),
        ):
            button = ttk.Button(buttons, text=text, command=command)
            button.pack(fill="x", pady=2)
            self._session_buttons.append(button)

    def _build_history_tab(self, parent: ttk.Frame) -> None:
        self.history_tree = ttk.Treeview(
            parent, columns=("project", "status", "created"), show="headings", height=10
        )
        for column, text, width in (
            ("project", "Проект", 110),
            ("status", "Статус", 90),
            ("created", "Создан", 170),
        ):
            self.history_tree.heading(column, text=text)
            self.history_tree.column(column, width=width, anchor="w")
        self.history_tree.pack(fill="both", expand=True)
        self.history_tree.bind("<<TreeviewSelect>>", self.on_history_selected)

    def _build_workspace(self, parent: ttk.Frame) -> None:
        progress_card = ttk.LabelFrame(parent, text="Что сейчас происходит", padding=8)
        progress_card.pack(fill="x", pady=(0, 10))
        ttk.Label(
            progress_card, textvariable=self.run_summary_var, style="SubHeader.TLabel"
        ).pack(anchor="w")
        ttk.Label(progress_card, textvariable=self.run_detail_var).pack(
            anchor="w", pady=(2, 0)
        )
        ttk.Label(progress_card, textvariable=self.run_eta_var).pack(
            anchor="w", pady=(2, 0)
        )
        ttk.Label(progress_card, textvariable=self.run_checkpoint_var).pack(
            anchor="w", pady=(2, 6)
        )
        self.run_progress_bar = ttk.Progressbar(
            progress_card, mode="determinate", maximum=100
        )
        self.run_progress_bar.pack(fill="x")

        journal_card = ttk.LabelFrame(parent, text="Журнал событий", padding=6)
        journal_card.pack(fill="both", expand=True, pady=(8, 0))
        journal_header = ttk.Frame(journal_card)
        journal_header.pack(fill="x", pady=(0, 6))
        ttk.Label(
            journal_header,
            text="Здесь отображается ход обработки, найденные результаты и ошибки.",
            style="SubHeader.TLabel",
        ).pack(side="left")
        ttk.Button(
            journal_header, text="Скачать логи", command=self.on_export_logs
        ).pack(side="right")
        self.journal_text = ScrolledText(journal_card, height=22, wrap="word")
        self.journal_text.pack(fill="both", expand=True)
        self._text_widgets.append(self.journal_text)

        self.project_tree = ttk.Treeview(parent)
        self.paragraph_tree = ttk.Treeview(parent)
        self.paragraph_text = ScrolledText(parent, height=1)
        self.video_queries_text = ScrolledText(parent, height=1)
        self.image_queries_text = ScrolledText(parent, height=1)
        self.selected_assets_tree = ttk.Treeview(parent)
        self.candidate_assets_tree = ttk.Treeview(parent)

    def _labeled_entry(
        self,
        parent,
        label: str,
        variable,
        button_text: str | None = None,
        button_command=None,
    ):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=3)
        ttk.Label(frame, text=label, style="SubHeader.TLabel").pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
        if button_text is not None and button_command is not None:
            ttk.Button(row, text=button_text, command=button_command).pack(
                side="left", padx=(6, 0)
            )

    def _provider_key_row(
        self,
        parent,
        label: str,
        variable,
        save_command,
        clear_command,
    ) -> None:
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=3)
        ttk.Label(frame, text=label, style="SubHeader.TLabel").pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=variable, show="*").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(row, text="Сохранить", command=save_command).pack(
            side="left", padx=(6, 6)
        )
        ttk.Button(row, text="Удалить", command=clear_command).pack(side="left")

    def _quick_form(self) -> UiQuickLaunchSettingsViewModel:
        selected_mode_id = self._mode_ids_by_label.get(
            self.mode_label_var.get(), "sb_video_only"
        )
        free_provider_ids = [
            provider_id
            for provider_id, variable in self.free_provider_vars.items()
            if variable.get()
        ]
        return UiQuickLaunchSettingsViewModel(
            project_name=self.project_name_var.get().strip(),
            script_path=self.script_path_var.get().strip(),
            output_dir=self.output_dir_var.get().strip(),
            paragraph_selection_text=self.paragraph_selection_var.get().strip(),
            selected_paragraphs=[],
            mode_id=selected_mode_id,
            strictness=self._strictness_ids_by_label.get(
                self.strictness_var.get().strip(), "balanced"
            ),
            slow_mode=self.slow_mode_var.get(),
            provider_ids=free_provider_ids,
            supporting_image_limit=max(0, self.supporting_image_limit_var.get()),
            fallback_image_limit=max(0, self.fallback_image_limit_var.get()),
            manual_prompt=self.manual_prompt_text.get("1.0", "end").strip(),
            attach_full_script_context=self.attach_full_script_context_var.get(),
        )

    def _advanced_form(self) -> UiAdvancedSettingsViewModel:
        return UiAdvancedSettingsViewModel(
            paragraph_workers=max(1, self.paragraph_workers_var.get()),
            provider_workers=max(1, self.provider_workers_var.get()),
            provider_queue_size=max(1, self.queue_size_var.get()),
            download_workers=max(1, self.download_workers_var.get()),
            download_queue_size=max(1, self.queue_size_var.get()),
            relevance_workers=max(1, self.relevance_workers_var.get()),
            relevance_queue_size=max(1, self.queue_size_var.get()),
            queue_size=max(1, self.queue_size_var.get()),
            search_timeout_seconds=max(
                1.0, float(max(1000, self.navigation_timeout_var.get())) / 1000.0
            ),
            relevance_timeout_seconds=max(
                1.0, float(max(1.0, self.download_timeout_var.get())) / 2.0
            ),
            launch_timeout_ms=max(1000, self.launch_timeout_var.get()),
            navigation_timeout_ms=max(1000, self.navigation_timeout_var.get()),
            downloads_timeout_seconds=max(1.0, self.download_timeout_var.get()),
            top_k_to_relevance=max(1, self.top_k_var.get()),
            retry_budget=max(0, self.retry_budget_var.get()),
            early_stop_quality_threshold=max(1.0, float(self.top_k_var.get()) / 3.0),
            fail_fast_storyblocks_errors=True,
            cache_root=self.cache_root_var.get().strip(),
            browser_profile_path=self.browser_profile_path_var.get().strip(),
            allow_generic_web_image=self.allow_generic_web_image_var.get(),
            no_match_budget_seconds=max(0.0, self.no_match_budget_var.get()),
            full_script_context_char_budget=max(
                1000, self.full_script_context_budget_var.get()
            ),
        )

    def refresh(self, *, preserve_forms: bool = False) -> None:
        state = self.controller.build_state(
            active_project_id=self.active_project_id, active_run_id=self.active_run_id
        )
        self._apply_state(state, preserve_forms=preserve_forms)

    def refresh_preview(self) -> None:
        if self.active_project_id is None:
            self.preview_text.delete("1.0", "end")
            self.preview_text.insert(
                "1.0", "Сначала откройте сценарий, чтобы увидеть параметры запуска."
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
        self.status_var.set(state.status_text)
        if not preserve_forms:
            self._set_if_blank(self.output_dir_var, state.quick_launch.output_dir)
            self.mode_label_var.set(
                self._mode_labels.get(
                    state.quick_launch.mode_id, self._mode_labels["sb_video_only"]
                )
            )
            self.slow_mode_var.set(state.quick_launch.slow_mode)
            self.strictness_var.set(label_for_strictness(state.quick_launch.strictness))
            self.paragraph_selection_var.set(
                state.quick_launch.paragraph_selection_text
            )
            self.supporting_image_limit_var.set(
                state.quick_launch.supporting_image_limit
            )
            self.fallback_image_limit_var.set(state.quick_launch.fallback_image_limit)
            self.attach_full_script_context_var.set(
                state.quick_launch.attach_full_script_context
            )
            self.manual_prompt_text.delete("1.0", "end")
            self.manual_prompt_text.insert("1.0", state.quick_launch.manual_prompt)
            selected_free = set(state.quick_launch.provider_ids)
            for provider_id, variable in self.free_provider_vars.items():
                variable.set(provider_id in selected_free)
            self.paragraph_workers_var.set(state.advanced.paragraph_workers)
            self.provider_workers_var.set(state.advanced.provider_workers)
            self.download_workers_var.set(state.advanced.download_workers)
            self.relevance_workers_var.set(state.advanced.relevance_workers)
            self.queue_size_var.set(state.advanced.queue_size)
            self.launch_timeout_var.set(state.advanced.launch_timeout_ms)
            self.navigation_timeout_var.set(state.advanced.navigation_timeout_ms)
            self.download_timeout_var.set(state.advanced.downloads_timeout_seconds)
            self.no_match_budget_var.set(state.advanced.no_match_budget_seconds)
            self.top_k_var.set(state.advanced.top_k_to_relevance)
            self.retry_budget_var.set(state.advanced.retry_budget)
            self.full_script_context_budget_var.set(
                state.advanced.full_script_context_char_budget
            )
            self.cache_root_var.set(state.advanced.cache_root)
            self.browser_profile_path_var.set(state.advanced.browser_profile_path)
            self.allow_generic_web_image_var.set(state.advanced.allow_generic_web_image)
            if not self.gemini_key_var.get().strip():
                self.gemini_key_var.set(self.controller.get_gemini_key() or "")
            if not self.pexels_key_var.get().strip():
                self.pexels_key_var.set(
                    self.controller.get_provider_api_key("pexels") or ""
                )
            if not self.pixabay_key_var.get().strip():
                self.pixabay_key_var.set(
                    self.controller.get_provider_api_key("pixabay") or ""
                )

        self._fill_history_tree(state.run_history)
        self._fill_paragraph_tree(state.paragraph_items)
        self._render_current_paragraph_detail(state.paragraph_items)
        self._render_session(state.session)
        self._render_preview(state.run_preview)
        self._render_run_progress(state.run_progress)
        self._set_session_actions_enabled(self.controller.session_actions_enabled())
        self._fill_event_journal(state.event_journal)

    def _set_session_actions_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self._session_buttons:
            button.configure(state=state)

    def _fill_project_tree(self, projects) -> None:
        return None

    def _fill_history_tree(self, runs) -> None:
        self.history_tree.delete(*self.history_tree.get_children())
        for run in runs:
            self.history_tree.insert(
                "",
                "end",
                iid=run.run_id,
                values=(
                    run.project_id,
                    translate_run_status(run.status),
                    run.created_at,
                ),
            )
        if self.active_run_id in self.history_tree.get_children():
            self.history_tree.selection_set(self.active_run_id)

    def _fill_paragraph_tree(self, paragraph_items) -> None:
        signature = self._paragraph_signature(paragraph_items)
        selected_paragraph_no = self._selected_paragraph_number()
        if signature == self._last_paragraph_signature:
            return
        self.paragraph_tree.delete(*self.paragraph_tree.get_children())
        for item in paragraph_items:
            label = f"P{item.paragraph_no} | {translate_paragraph_status(item.status)}"
            if not item.numbering_valid:
                label += " | проблема нумерации"
            self.paragraph_tree.insert(
                "",
                "end",
                iid=str(item.paragraph_no),
                text=label,
            )
        if (
            selected_paragraph_no is not None
            and str(selected_paragraph_no) in self.paragraph_tree.get_children()
        ):
            self.paragraph_tree.selection_set(str(selected_paragraph_no))
        elif paragraph_items:
            self.paragraph_tree.selection_set(str(paragraph_items[0].paragraph_no))
        self._last_paragraph_signature = signature

    def _render_preview(self, preview) -> None:
        self.preview_text.delete("1.0", "end")
        if preview is None:
            return
        lines = [
            f"Проект: {preview.project_name}",
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
        self.preview_text.insert("1.0", "\n".join(lines))

    def _render_run_progress(self, progress) -> None:
        if progress is None:
            self.run_summary_var.set("Нет активного запуска")
            self.run_detail_var.set("Откройте сценарий и нажмите Старт")
            self.run_eta_var.set("")
            self.run_checkpoint_var.set("")
            self.run_progress_bar.configure(value=0)
            return
        self.run_summary_var.set(
            f"Обработано {progress.project_progress_completed} из {progress.project_progress_total} абзацев"
        )
        stage = translate_run_stage(progress.current_stage)
        current = (
            f"абзац {progress.current_paragraph_no}"
            if progress.current_paragraph_no is not None
            else "ожидание"
        )
        self.run_detail_var.set(
            f"{translate_run_status(progress.status)} - {stage} - {current}"
        )
        self.run_eta_var.set(progress.eta_text)
        self.run_checkpoint_var.set(
            f"Найдено: {progress.paragraphs_matched} · Без результата: {progress.paragraphs_no_match} · Ошибки: {progress.paragraphs_failed}"
        )
        self.run_progress_bar.configure(value=progress.percent_complete)

    def _fill_event_journal(self, items) -> None:
        signature = self._journal_signature(items)
        if signature == self._last_journal_signature:
            return
        self.journal_text.delete("1.0", "end")
        lines: list[str] = []
        for item in items:
            context = []
            if item.paragraph_no is not None:
                context.append(f"P{item.paragraph_no}")
            if item.provider_name:
                context.append(translate_provider(item.provider_name))
            if item.query:
                context.append(item.query[:40])
            if item.current_asset_id:
                context.append(item.current_asset_id)
            suffix = f" [{' | '.join(context)}]" if context else ""
            lines.append(
                f"{item.created_at[-8:]} | {translate_severity(item.severity)} | {translate_run_stage(item.stage)} | {item.message}{suffix}"
            )
        self.journal_text.insert("1.0", "\n".join(lines))
        self._last_journal_signature = signature

    def _paragraph_signature(
        self, paragraph_items
    ) -> tuple[tuple[int, str, str, bool], ...]:
        return tuple(
            (
                item.paragraph_no,
                item.status,
                item.user_decision_status,
                item.numbering_valid,
            )
            for item in paragraph_items
        )

    def _journal_signature(self, items) -> tuple[int, str, str]:
        if not items:
            return (0, "", "")
        latest = items[0]
        return (len(items), latest.created_at, latest.message)

    def _render_current_paragraph_detail(self, paragraph_items) -> None:
        if not paragraph_items:
            return
        selected_paragraph_no = self._selected_paragraph_number()
        if selected_paragraph_no is not None:
            for item in paragraph_items:
                if item.paragraph_no == selected_paragraph_no:
                    self._render_paragraph_detail(item)
                    return
        self._render_paragraph_detail(paragraph_items[0])

    def _render_session(self, session) -> None:
        self.session_summary.delete("1.0", "end")
        lines = [
            f"Профиль: {session.profile_name or session.profile_id or 'не выбран'}",
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
        self.session_summary.insert("1.0", "\n".join(lines))

    def _render_paragraph_detail(self, item) -> None:
        self.paragraph_text.delete("1.0", "end")
        self.video_queries_text.delete("1.0", "end")
        self.image_queries_text.delete("1.0", "end")
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
        self.paragraph_text.insert("1.0", "\n".join(detail_lines))
        self.video_queries_text.insert("1.0", "\n".join(item.video_queries))
        self.image_queries_text.insert("1.0", "\n".join(item.image_queries))

        self.selected_assets_tree.delete(*self.selected_assets_tree.get_children())
        for asset in item.selected_assets:
            self.selected_assets_tree.insert(
                "",
                "end",
                iid=f"selected:{asset.asset_id}",
                values=(
                    translate_provider(asset.provider_name),
                    translate_asset_role(asset.role),
                    asset.title
                    + (f" | {asset.local_path}" if asset.local_path else ""),
                ),
            )
        self.candidate_assets_tree.delete(*self.candidate_assets_tree.get_children())

    def _selected_paragraph_numbers(self) -> list[int]:
        values: list[int] = []
        for item_id in self.paragraph_tree.selection():
            try:
                values.append(int(item_id))
            except ValueError:
                continue
        return values

    def _selected_paragraph_number(self) -> int | None:
        selected = self._selected_paragraph_numbers()
        return selected[0] if selected else None

    def _selected_candidate_asset_id(self) -> str | None:
        selected = self.candidate_assets_tree.selection()
        return selected[0] if selected else None

    def _set_if_blank(self, variable, value: str) -> None:
        if not variable.get().strip() and value:
            variable.set(value)

    def on_browse_script(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("Документы Word", "*.docx"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        self.script_path_var.set(path)
        if not self.project_name_var.get().strip():
            self.project_name_var.set(Path(path).stem.replace("_", " "))

    def on_browse_output_dir(self) -> None:
        path = filedialog.askdirectory(title="Выберите папку вывода")
        if path:
            self.output_dir_var.set(path)

    def on_open_script(self) -> None:
        try:
            summary = self.controller.open_script(
                self.script_path_var.get(),
                project_name=self.project_name_var.get().strip() or None,
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
        self.status_var.set(f"Запуск {run_id} начат")
        self.refresh(preserve_forms=True)

    def _apply_storyblocks_parallelism_autofix(self, exc: Exception) -> None:
        if not isinstance(exc, AppError):
            return
        if exc.code != "storyblocks_parallelism_guard":
            return
        self.paragraph_workers_var.set(1)
        self.status_var.set(
            "Потоки абзацев автоматически скорректированы до 1 для Storyblocks."
        )

    def on_resume_run(self) -> None:
        if self.active_run_id is None:
            return
        try:
            self.controller.resume_run_async(self.active_run_id, self._advanced_form())
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.status_var.set(f"Запуск {self.active_run_id} продолжен")
        self.refresh(preserve_forms=True)

    def on_pause_run(self) -> None:
        if self.active_run_id is None:
            return
        try:
            self.controller.pause_run(self.active_run_id)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.status_var.set(f"Запрошена пауза для {self.active_run_id}")
        self.refresh(preserve_forms=True)

    def on_stop_after_current(self) -> None:
        if self.active_run_id is None:
            return
        try:
            self.controller.stop_after_current(self.active_run_id)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.status_var.set(
            f"Остановка после текущего запрошена для {self.active_run_id}"
        )
        self.refresh(preserve_forms=True)

    def on_abort_run(self) -> None:
        if self.active_run_id is None:
            return
        try:
            self.controller.cancel_run(self.active_run_id)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.status_var.set(f"Запрошена остановка для {self.active_run_id}")
        self.refresh(preserve_forms=True)

    def on_retry_failed(self) -> None:
        if self.active_run_id is None:
            return
        try:
            run_id = self.controller.retry_failed_run_async(
                self.active_run_id, self._advanced_form()
            )
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.active_run_id = run_id
        self.status_var.set(f"Повторный запуск {run_id} начат")
        self.refresh(preserve_forms=True)

    def on_save_preset(self) -> None:
        name = self.preset_name_var.get().strip()
        if not name:
            messagebox.showwarning("Пресет", "Укажите имя пресета.")
            return
        try:
            self.controller.save_preset(name, self._quick_form(), self._advanced_form())
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.refresh()

    def on_load_preset(self) -> None:
        name = self.preset_name_var.get().strip()
        if not name:
            messagebox.showwarning("Пресет", "Укажите имя пресета.")
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
        name = self.preset_name_var.get().strip()
        if not name:
            messagebox.showwarning("Пресет", "Укажите имя пресета.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON files", "*.json")]
        )
        if not path:
            return
        try:
            self.controller.export_preset(name, path)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        messagebox.showinfo("Пресет", "Пресет экспортирован")

    def on_import_preset(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("JSON files", "*.json"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        try:
            preset = self.controller.import_preset(path)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.preset_name_var.set(preset.name)
        self.refresh()

    def on_store_gemini_key(self) -> None:
        notification = self.controller.set_gemini_key(self.gemini_key_var.get())
        self._show_notification(notification)

    def on_clear_gemini_key(self) -> None:
        self.controller.delete_gemini_key()
        self.gemini_key_var.set("")
        messagebox.showinfo("Ключ Gemini", "Ключ Gemini удален")

    def on_store_provider_key(self, provider_id: str) -> None:
        variable = {
            "pexels": self.pexels_key_var,
            "pixabay": self.pixabay_key_var,
        }.get(provider_id)
        if variable is None:
            return
        self._show_notification(
            self.controller.set_provider_api_key(provider_id, variable.get())
        )

    def on_clear_provider_key(self, provider_id: str) -> None:
        variable = {
            "pexels": self.pexels_key_var,
            "pixabay": self.pixabay_key_var,
        }.get(provider_id)
        if variable is not None:
            variable.set("")
        self._show_notification(self.controller.delete_provider_api_key(provider_id))

    def on_open_browser(self) -> None:
        self._run_session_action(self.controller.open_storyblocks_browser)

    def on_prepare_login(self) -> None:
        self._run_session_action(self.controller.prepare_storyblocks_login)

    def on_import_chrome_session(self) -> None:
        self._import_existing_session("chrome")

    def on_import_edge_session(self) -> None:
        self._import_existing_session("msedge")

    def on_reimport_session(self) -> None:
        self._run_session_action(self.controller.reimport_storyblocks_session)

    def on_logout(self) -> None:
        self._run_session_action(self.controller.logout_storyblocks)

    def on_switch_account(self) -> None:
        self._run_session_action(self.controller.switch_storyblocks_account)

    def on_check_session(self) -> None:
        self._run_session_action(self.controller.check_storyblocks_session)

    def on_reset_session(self) -> None:
        self._run_session_action(self.controller.reset_storyblocks_session)

    def on_mark_session_ready(self) -> None:
        self._run_session_action(self.controller.mark_storyblocks_session_ready)

    def on_clear_session_override(self) -> None:
        self._run_session_action(self.controller.clear_storyblocks_session_override)

    def on_clear_profile(self) -> None:
        self._run_session_action(self.controller.clear_storyblocks_profile)

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

    def on_project_selected(self, _event=None) -> None:
        selected = self.project_tree.selection()
        if not selected:
            return
        self.active_project_id = selected[0]
        self.project_name_var.set(
            self.project_tree.item(self.active_project_id, "values")[0]
        )
        self.refresh()

    def on_history_selected(self, _event=None) -> None:
        selected = self.history_tree.selection()
        if not selected:
            return
        self.active_run_id = selected[0]
        project_id = self.history_tree.item(self.active_run_id, "values")[0]
        self.active_project_id = project_id
        self.main_notebook.select(0)
        self.refresh()

    def on_export_logs(self) -> None:
        if self.active_run_id is None:
            return
        path = filedialog.asksaveasfilename(
            title="Сохранить журнал запуска",
            initialfile=f"run-{self.active_run_id}.log",
            defaultextension=".log",
            filetypes=[
                ("Log files", "*.log"),
                ("Text files", "*.txt"),
                ("Все файлы", "*.*"),
            ],
        )
        if not path:
            return
        try:
            saved = self.controller.export_run_log(self.active_run_id, path)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        messagebox.showinfo("Журнал событий", f"Лог сохранен: {saved}")

    def on_paragraph_selected(self, _event=None) -> None:
        paragraph_no = self._selected_paragraph_number()
        if paragraph_no is None or self.active_project_id is None:
            return
        item = self.controller.build_paragraph_detail(
            self.active_project_id, paragraph_no, self.active_run_id
        )
        if item is not None:
            self._render_paragraph_detail(item)

    def on_save_queries(self) -> None:
        if self.active_project_id is None:
            return
        paragraph_no = self._selected_paragraph_number()
        if paragraph_no is None:
            return
        video_queries = self.video_queries_text.get("1.0", "end").splitlines()
        image_queries = self.image_queries_text.get("1.0", "end").splitlines()
        try:
            self.controller.update_paragraph_queries(
                self.active_project_id,
                paragraph_no,
                video_queries=video_queries,
                image_queries=image_queries,
            )
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.refresh()

    def on_lock_asset(self) -> None:
        if self.active_run_id is None:
            return
        paragraph_no = self._selected_paragraph_number()
        asset_id = self._selected_candidate_asset_id()
        if paragraph_no is None or asset_id is None:
            return
        try:
            self.controller.lock_asset(self.active_run_id, paragraph_no, asset_id)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.refresh()

    def on_reject_asset(self) -> None:
        if self.active_run_id is None:
            return
        paragraph_no = self._selected_paragraph_number()
        asset_id = self._selected_candidate_asset_id()
        if paragraph_no is None or asset_id is None:
            return
        try:
            self.controller.reject_asset(self.active_run_id, paragraph_no, asset_id)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.refresh()

    def on_rerun_current_paragraph(self) -> None:
        if self.active_project_id is None:
            return
        paragraph_no = self._selected_paragraph_number()
        if paragraph_no is None:
            return
        try:
            run_id = self.controller.rerun_current_paragraph_async(
                self.active_project_id,
                paragraph_no,
                self._quick_form(),
                self._advanced_form(),
            )
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.active_run_id = run_id
        self.refresh(preserve_forms=True)

    def on_rerun_selected_paragraphs(self) -> None:
        if self.active_project_id is None:
            return
        paragraph_numbers = self._selected_paragraph_numbers()
        if not paragraph_numbers:
            return
        try:
            run_id = self.controller.rerun_selected_paragraphs_async(
                self.active_project_id,
                paragraph_numbers,
                self._quick_form(),
                self._advanced_form(),
            )
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self.active_run_id = run_id
        self.refresh(preserve_forms=True)

    def _poll_refresh(self) -> None:
        next_interval = self._poll_interval_idle_ms
        try:
            if self.active_project_id is None and self.active_run_id is None:
                self._terminal_refresh_signature = None
                return
            live_snapshot = self.controller.build_live_snapshot(
                active_project_id=self.active_project_id,
                active_run_id=self.active_run_id,
            )
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
                live_state = self.controller.build_live_run_state(
                    active_project_id=self.active_project_id,
                    active_run_id=live_snapshot.active_run_id,
                    selected_paragraph_no=self._selected_paragraph_number(),
                    live_snapshot=live_snapshot,
                )
                if live_state.paragraph_items:
                    self._apply_live_paragraph_items(live_state.paragraph_items)
            if poll_plan.should_heavy_refresh:
                self.refresh(preserve_forms=True)
            self._terminal_refresh_signature = poll_plan.terminal_signature
            next_interval = poll_plan.next_interval_ms
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
        finally:
            self.root.after(next_interval, self._poll_refresh)

    def _apply_live_state(self, state) -> None:
        self._apply_live_snapshot(state)
        self._apply_live_paragraph_items(state.paragraph_items)

    def _apply_live_snapshot(self, state) -> None:
        self.active_run_id = state.active_run_id
        self.status_var.set(state.status_text)
        self._render_run_progress(state.run_progress)
        self._set_session_actions_enabled(self.controller.session_actions_enabled())
        self._fill_event_journal(state.event_journal)

    def _apply_live_paragraph_items(self, paragraph_items) -> None:
        self._fill_paragraph_tree(paragraph_items)
        self._render_current_paragraph_detail(paragraph_items)

    def _apply_quick_form(self, quick: UiQuickLaunchSettingsViewModel) -> None:
        self.project_name_var.set(quick.project_name)
        self.script_path_var.set(quick.script_path)
        self.output_dir_var.set(quick.output_dir)
        self.paragraph_selection_var.set(quick.paragraph_selection_text)
        self.mode_label_var.set(
            self._mode_labels.get(quick.mode_id, self._mode_labels["sb_video_only"])
        )
        self.strictness_var.set(label_for_strictness(quick.strictness))
        self.slow_mode_var.set(quick.slow_mode)
        self.supporting_image_limit_var.set(quick.supporting_image_limit)
        self.fallback_image_limit_var.set(quick.fallback_image_limit)
        self.attach_full_script_context_var.set(quick.attach_full_script_context)
        self.manual_prompt_text.delete("1.0", "end")
        self.manual_prompt_text.insert("1.0", quick.manual_prompt)
        selected_free = set(quick.provider_ids)
        for provider_id, variable in self.free_provider_vars.items():
            variable.set(provider_id in selected_free)

    def _apply_advanced_form(self, advanced: UiAdvancedSettingsViewModel) -> None:
        self.paragraph_workers_var.set(advanced.paragraph_workers)
        self.provider_workers_var.set(advanced.provider_workers)
        self.download_workers_var.set(advanced.download_workers)
        self.relevance_workers_var.set(advanced.relevance_workers)
        self.queue_size_var.set(advanced.queue_size)
        self.launch_timeout_var.set(advanced.launch_timeout_ms)
        self.navigation_timeout_var.set(advanced.navigation_timeout_ms)
        self.download_timeout_var.set(advanced.downloads_timeout_seconds)
        self.no_match_budget_var.set(advanced.no_match_budget_seconds)
        self.top_k_var.set(advanced.top_k_to_relevance)
        self.retry_budget_var.set(advanced.retry_budget)
        self.full_script_context_budget_var.set(
            advanced.full_script_context_char_budget
        )
        self.cache_root_var.set(advanced.cache_root)
        self.browser_profile_path_var.set(advanced.browser_profile_path)
        self.allow_generic_web_image_var.set(advanced.allow_generic_web_image)

    def _run_session_action(self, action) -> None:
        try:
            session = action()
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return
        self._render_session(session)
        self.refresh()

    def _import_existing_session(self, browser_name: str) -> None:
        try:
            options = self.controller.discover_storyblocks_sessions(browser_name)
        except Exception as exc:
            self._show_notification(handle_ui_error(exc))
            return

        selected_path = ""
        selected_browser = browser_name
        if options:
            choices = [
                f"{index + 1}. {item.display_label}"
                for index, item in enumerate(options)
            ]
            selection = simpledialog.askstring(
                "Импорт существующей сессии",
                "Выберите найденный профиль браузера по номеру:\n\n"
                + "\n".join(choices),
                parent=self.root,
            )
            if not selection:
                return
            try:
                selected = options[int(selection.strip()) - 1]
            except Exception:
                messagebox.showwarning(
                    "Импорт сессии", "Введите корректный номер найденного профиля"
                )
                return
            selected_path = selected.profile_dir
            selected_browser = selected.browser_name
        else:
            selected_path = filedialog.askdirectory(
                title="Выберите папку профиля Chrome/Edge (Default/Profile X)"
            )
            if not selected_path:
                return
        self._run_session_action(
            lambda selected_path=selected_path,
            selected_browser=selected_browser: self.controller.import_storyblocks_session_from_path(
                selected_path,
                browser_name=selected_browser,
            )
        )

    def _show_notification(self, notification) -> None:
        self.status_var.set(notification.message)
        if notification.severity == "error":
            messagebox.showerror(notification.title, notification.message)
        elif notification.severity == "warning":
            messagebox.showwarning(notification.title, notification.message)
        else:
            messagebox.showinfo(notification.title, notification.message)

    def on_theme_changed(self, _event=None) -> None:
        theme_id = normalize_ui_theme(
            self._theme_ids_by_label.get(self.theme_var.get(), "dark")
        )
        if theme_id == self._theme_id:
            return
        self._theme_id = self.controller.set_ui_theme(theme_id)
        self._apply_theme(self._theme_id)

    def _apply_theme(self, theme_id: str) -> None:
        theme = get_ui_theme(theme_id)
        self.root.configure(bg=theme.window_bg)
        style = ttk.Style(self.root)
        style.configure("TFrame", background=theme.window_bg)
        style.configure("Card.TFrame", background=theme.surface_bg)
        style.configure("TLabel", background=theme.window_bg, foreground=theme.text)
        style.configure(
            "Header.TLabel",
            font=("Segoe UI", 18, "bold"),
            background=theme.window_bg,
            foreground=theme.text,
        )
        style.configure(
            "SubHeader.TLabel",
            font=("Segoe UI", 10, "bold"),
            background=theme.window_bg,
            foreground=theme.muted_text,
        )
        style.configure(
            "Muted.TLabel",
            background=theme.window_bg,
            foreground=theme.muted_text,
        )
        style.configure(
            "TButton",
            padding=(10, 6),
            background=theme.surface_alt_bg,
            foreground=theme.text,
        )
        style.map(
            "TButton",
            background=[("active", theme.selection), ("disabled", theme.surface_bg)],
            foreground=[("disabled", theme.muted_text)],
        )
        style.configure(
            "Accent.TButton",
            padding=(10, 6),
            background=theme.accent,
            foreground="#ffffff",
        )
        style.map(
            "Accent.TButton",
            background=[
                ("active", theme.accent_hover),
                ("pressed", theme.accent_pressed),
            ],
        )
        style.configure(
            "TEntry",
            fieldbackground=theme.input_bg,
            foreground=theme.text,
        )
        style.configure(
            "TCombobox",
            fieldbackground=theme.input_bg,
            background=theme.input_bg,
            foreground=theme.text,
            arrowcolor=theme.text,
        )
        style.map("TCombobox", fieldbackground=[("readonly", theme.input_bg)])
        style.configure(
            "TNotebook",
            background=theme.window_bg,
            borderwidth=0,
        )
        style.configure(
            "TNotebook.Tab",
            background=theme.surface_alt_bg,
            foreground=theme.text,
            padding=(12, 8),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", theme.accent), ("active", theme.selection)],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "TLabelframe",
            background=theme.surface_bg,
            foreground=theme.text,
        )
        style.configure(
            "TLabelframe.Label",
            background=theme.surface_bg,
            foreground=theme.muted_text,
        )
        style.configure(
            "Treeview",
            background=theme.input_bg,
            fieldbackground=theme.input_bg,
            foreground=theme.text,
            rowheight=24,
        )
        style.map(
            "Treeview",
            background=[("selected", theme.selection)],
            foreground=[("selected", theme.text)],
        )
        style.configure(
            "Treeview.Heading",
            background=theme.surface_alt_bg,
            foreground=theme.text,
        )
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor=theme.input_bg,
            background=theme.accent,
        )
        self.root.option_add("*TCombobox*Listbox*Background", theme.input_bg)
        self.root.option_add("*TCombobox*Listbox*Foreground", theme.text)
        self.root.option_add("*TCombobox*Listbox*selectBackground", theme.selection)
        self.root.option_add("*TCombobox*Listbox*selectForeground", theme.text)
        for widget in self._text_widgets:
            widget.configure(
                bg=theme.input_bg,
                fg=theme.text,
                insertbackground=theme.text,
                selectbackground=theme.selection,
                relief="flat",
                highlightthickness=1,
                highlightbackground=theme.border,
                highlightcolor=theme.accent,
            )


def launch_tk_app(controller: DesktopGuiController) -> None:
    DesktopTkApp(controller).run()
