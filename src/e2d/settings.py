"""Per-project connection settings, persisted to disk.

Stores Dynatrace tenant credentials and Elastic source connection details in
``.e2d/project.json`` under the current working directory (or a directory given
by ``E2D_PROJECT_DIR``). The file is created with mode 0600 and is gitignored
by convention — it holds tokens.

Settings are loaded once per process and cached; ``save_project_settings``
writes back and updates the cache.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class ProjectSettings:
    """Connection and behaviour settings for one project directory."""
    dynatrace_env_url: str = ""
    dynatrace_token: str = ""          # platform token (never logged)
    elastic_kibana_url: str = ""
    elastic_es_url: str = ""
    elastic_token: str = ""
    elastic_auth_scheme: str = "ApiKey"
    verify_tls: bool = True
    default_heal: bool = False
    default_verify: bool = False
    default_heal_rules: str = ""       # comma-separated, empty = all

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectSettings":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


_SETTINGS_FILE = "project.json"
_settings_cache: Optional[ProjectSettings] = None


def _project_dir() -> Path:
    override = os.environ.get("E2D_PROJECT_DIR")
    if override:
        return Path(override)
    return Path.cwd()


def _settings_path() -> Path:
    return _project_dir() / ".e2d" / _SETTINGS_FILE


def load_project_settings() -> ProjectSettings:
    """Load settings from disk, or return defaults. Cached per process."""
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache
    path = _settings_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _settings_cache = ProjectSettings.from_dict(data)
            return _settings_cache
        except (ValueError, OSError):
            pass
    _settings_cache = ProjectSettings()
    return _settings_cache


def save_project_settings(settings: ProjectSettings) -> Path:
    """Write settings to disk with owner-only permissions."""
    global _settings_cache
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings.to_dict(), indent=2) + "\n",
                    encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    _settings_cache = settings
    return path


def update_project_settings(**kwargs: Any) -> ProjectSettings:
    """Load, apply keyword overrides, save, and return the new settings."""
    current = load_project_settings()
    for key, value in kwargs.items():
        if hasattr(current, key):
            setattr(current, key, value)
    save_project_settings(current)
    return current
