from __future__ import annotations

from dataclasses import dataclass

from .launch_profiles import LAUNCH_PROFILE_LABELS, list_launch_profile_ids


@dataclass(frozen=True, slots=True)
class UiThemeSpec:
    theme_id: str
    window_bg: str
    surface_bg: str
    surface_alt_bg: str
    input_bg: str
    border: str
    text: str
    muted_text: str
    accent: str
    accent_hover: str
    accent_pressed: str
    selection: str
    success: str
    warning: str
    danger: str


THEME_LABELS: dict[str, str] = {
    "dark": "Темная",
    "light": "Светлая",
}

STRICTNESS_LABELS: dict[str, str] = {
    "simple": "Простой",
    "balanced": "Баланс",
    "strict": "Строгий",
}

SESSION_HEALTH_LABELS: dict[str, str] = {
    "unknown": "Неизвестно",
    "ready": "Готово",
    "login_required": "Нужен вход",
    "challenge_detected": "Требуется проверка",
    "expired": "Сессия истекла",
    "blocked": "Доступ ограничен",
}

RUN_STATUS_LABELS: dict[str, str] = {
    "draft": "Черновик",
    "ready": "Готово",
    "running": "Выполняется",
    "paused": "На паузе",
    "completed": "Завершено",
    "failed": "С ошибкой",
    "cancelled": "Остановлено",
}

RUN_STAGE_LABELS: dict[str, str] = {
    "idle": "Ожидание",
    "ingestion": "Загрузка",
    "intent": "Анализ",
    "provider_search": "Поиск",
    "download": "Скачивание",
    "relevance": "Оценка",
    "persist": "Сохранение",
    "complete": "Готово",
}

PARAGRAPH_STATUS_LABELS: dict[str, str] = {
    "pending": "Ожидает",
    "selected": "Выбрано",
    "locked": "Закреплено",
    "partial_success": "Частично готово",
    "needs_review": "Нужна проверка",
    "failed": "Ошибка",
    "skipped": "Пропущено",
}

DECISION_STATUS_LABELS: dict[str, str] = {
    "auto_selected": "Автовыбор",
    "locked": "Закреплено",
    "user_rejected": "Отклонено",
    "needs_review": "Нужна проверка",
    "pending": "Ожидает",
}

SEVERITY_LABELS: dict[str, str] = {
    "debug": "Debug",
    "info": "Инфо",
    "warning": "Внимание",
    "error": "Ошибка",
}

PROVIDER_LABELS: dict[str, str] = {
    "storyblocks_video": "Storyblocks видео",
    "storyblocks_image": "Storyblocks изображения",
    "free_image": "Бесплатные изображения",
    "pexels": "Pexels",
    "pixabay": "Pixabay",
    "openverse": "Openverse",
}

ASSET_KIND_LABELS: dict[str, str] = {
    "video": "Видео",
    "image": "Изображение",
    "audio": "Аудио",
}

ASSET_ROLE_LABELS: dict[str, str] = {
    "candidate": "Кандидат",
    "primary": "Основной",
    "supporting": "Дополнительный",
    "fallback": "Резерв",
    "locked": "Закреплен",
}

_ERROR_TRANSLATIONS: dict[str, str] = {
    "Storyblocks session changes are disabled while a run is active.": "Во время активного запуска нельзя менять сессию Storyblocks.",
    "Run has no failed paragraphs to retry": "В этом запуске нет абзацев для повтора.",
    "Another run is already active in the desktop controller": "Уже выполняется другой запуск.",
    "Preset name is required": "Укажите имя пресета.",
    "Unexpected error": "Неожиданная ошибка",
    "Key is empty": "Ключ пустой.",
    "Key must not contain spaces": "Ключ не должен содержать пробелы.",
    "Key looks too short to be valid": "Ключ слишком короткий и выглядит некорректным.",
    "Key looks structurally valid and will be stored securely. Live API validation happens on first Gemini call.": "Ключ выглядит корректным и будет безопасно сохранен. Проверка API произойдет при первом обращении к Gemini.",
}

_DARK_THEME = UiThemeSpec(
    theme_id="dark",
    window_bg="#111827",
    surface_bg="#18212f",
    surface_alt_bg="#223046",
    input_bg="#0f1724",
    border="#2b3a52",
    text="#edf3ff",
    muted_text="#98a8c4",
    accent="#e07a5f",
    accent_hover="#ec8d74",
    accent_pressed="#c9654d",
    selection="#2f4666",
    success="#63c39b",
    warning="#f2c66d",
    danger="#ef7d7d",
)

_LIGHT_THEME = UiThemeSpec(
    theme_id="light",
    window_bg="#f2ede4",
    surface_bg="#fffaf1",
    surface_alt_bg="#f4ebdd",
    input_bg="#ffffff",
    border="#d8cab7",
    text="#2d241d",
    muted_text="#726253",
    accent="#aa5a44",
    accent_hover="#bf6b53",
    accent_pressed="#934935",
    selection="#ead9c3",
    success="#43876a",
    warning="#a97926",
    danger="#b85656",
)

THEMES: dict[str, UiThemeSpec] = {
    "dark": _DARK_THEME,
    "light": _LIGHT_THEME,
}


def normalize_ui_theme(theme_id: str | None) -> str:
    return "light" if (theme_id or "").strip().casefold() == "light" else "dark"


def get_ui_theme(theme_id: str | None) -> UiThemeSpec:
    return THEMES[normalize_ui_theme(theme_id)]


def label_for_theme(theme_id: str) -> str:
    return THEME_LABELS.get(theme_id, theme_id)


def label_for_strictness(value: str) -> str:
    return STRICTNESS_LABELS.get(value, value)


def label_for_launch_profile(value: str) -> str:
    return LAUNCH_PROFILE_LABELS.get(value, value)


def launch_profile_value_from_label(label: str) -> str:
    target = (label or "").strip()
    for value in list_launch_profile_ids():
        translated = LAUNCH_PROFILE_LABELS[value]
        if translated == target:
            return value
    return "normal"


def strictness_value_from_label(label: str) -> str:
    target = (label or "").strip()
    for value, translated in STRICTNESS_LABELS.items():
        if translated == target:
            return value
    return "balanced"


def map_label(mapping: dict[str, str], value: str | None) -> str:
    candidate = (value or "").strip()
    return mapping.get(candidate, candidate or "-")


def translate_provider(value: str | None) -> str:
    return map_label(PROVIDER_LABELS, value)


def translate_run_status(value: str | None) -> str:
    return map_label(RUN_STATUS_LABELS, value)


def translate_run_stage(value: str | None) -> str:
    return map_label(RUN_STAGE_LABELS, value)


def translate_session_health(value: str | None) -> str:
    return map_label(SESSION_HEALTH_LABELS, value)


def translate_paragraph_status(value: str | None) -> str:
    return map_label(PARAGRAPH_STATUS_LABELS, value)


def translate_decision_status(value: str | None) -> str:
    return map_label(DECISION_STATUS_LABELS, value)


def translate_severity(value: str | None) -> str:
    return map_label(SEVERITY_LABELS, value)


def translate_asset_kind(value: str | None) -> str:
    return map_label(ASSET_KIND_LABELS, value)


def translate_asset_role(value: str | None) -> str:
    return map_label(ASSET_ROLE_LABELS, value)


def yes_no(value: bool) -> str:
    return "Да" if value else "Нет"


def on_off(value: bool) -> str:
    return "Вкл" if value else "Выкл"


def translate_error_text(message: str) -> str:
    return _ERROR_TRANSLATIONS.get(message, message)
