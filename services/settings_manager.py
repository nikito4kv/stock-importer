from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from typing import Any

from config.settings import ApplicationSettings, default_settings
from domain.models import Preset
from storage.serialization import read_json, write_json
from storage.repositories import PresetRepository, SettingsRepository

from .secrets import SecretStore


class SettingsManager:
    def __init__(
        self,
        settings_repository: SettingsRepository,
        preset_repository: PresetRepository,
        secret_store: SecretStore,
    ):
        self._settings_repository = settings_repository
        self._preset_repository = preset_repository
        self._secret_store = secret_store

    def load(self) -> ApplicationSettings:
        return self._settings_repository.load()

    def save(self, settings: ApplicationSettings) -> ApplicationSettings:
        return self._settings_repository.save(settings)

    def get_or_create(self) -> ApplicationSettings:
        settings = self.load()
        return self.save(settings or default_settings())

    def save_preset(self, preset: Preset) -> Preset:
        return self._preset_repository.save(preset)

    def load_preset(self, preset_name: str) -> Preset | None:
        return self._preset_repository.load(preset_name)

    def list_presets(self) -> list[str]:
        return self._preset_repository.list_names()

    def list_preset_objects(self) -> list[Preset]:
        return self._preset_repository.list_all()

    def apply_preset(
        self,
        settings: ApplicationSettings,
        preset_name: str,
    ) -> ApplicationSettings:
        preset = self.load_preset(preset_name)
        if preset is None:
            return settings
        return self._apply_snapshot(settings, preset.settings_snapshot)

    def _apply_snapshot(self, settings: ApplicationSettings, snapshot: dict[str, Any]) -> ApplicationSettings:
        return _merge_dataclass(settings, snapshot)

    def set_secret(self, name: str, value: str) -> None:
        self._secret_store.set_secret(name, value)

    def get_secret(self, name: str) -> str | None:
        return self._secret_store.get_secret(name)

    def delete_secret(self, name: str) -> None:
        self._secret_store.delete_secret(name)

    def export_preset(self, preset_name: str, destination: str | Path) -> Path:
        preset = self.load_preset(preset_name)
        if preset is None:
            raise KeyError(preset_name)
        path = Path(destination)
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        write_json(path, preset.to_dict())
        return path

    def import_preset(self, source: str | Path) -> Preset:
        data = read_json(Path(source))
        preset = Preset.from_dict(data)
        return self.save_preset(preset)


def _merge_dataclass(instance: Any, patch: dict[str, Any]) -> Any:
    updates: dict[str, Any] = {}
    field_map = {item.name: item for item in fields(instance)}
    for key, value in patch.items():
        if key not in field_map:
            continue
        current_value = getattr(instance, key)
        if is_dataclass(current_value) and isinstance(value, dict):
            updates[key] = _merge_dataclass(current_value, value)
        else:
            updates[key] = value
    return replace(instance, **updates)
