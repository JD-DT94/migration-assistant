"""Per-project connection settings persistence."""

import json
import os
from pathlib import Path

from e2d.settings import (ProjectSettings, load_project_settings,
                          save_project_settings, update_project_settings)


def test_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("E2D_PROJECT_DIR", str(tmp_path))
    s = load_project_settings()
    assert s.dynatrace_env_url == ""
    assert s.default_heal is False


def test_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setenv("E2D_PROJECT_DIR", str(tmp_path))
    s = ProjectSettings(dynatrace_env_url="https://env", dynatrace_token="tok",
                        default_heal=True, default_heal_rules="by-without-braces")
    save_project_settings(s)
    loaded = load_project_settings()
    assert loaded.dynatrace_env_url == "https://env"
    assert loaded.default_heal is True
    assert loaded.default_heal_rules == "by-without-braces"
    path = tmp_path / ".e2d" / "project.json"
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_update_project_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("E2D_PROJECT_DIR", str(tmp_path))
    s = update_project_settings(dynatrace_env_url="https://new", default_verify=True)
    assert s.dynatrace_env_url == "https://new"
    assert s.default_verify is True
    loaded = load_project_settings()
    assert loaded.dynatrace_env_url == "https://new"


def test_cache_reset_between_tests(tmp_path, monkeypatch):
    monkeypatch.setenv("E2D_PROJECT_DIR", str(tmp_path))
    s1 = load_project_settings()
    s1.dynatrace_env_url = "https://cached"
    # simulate another process by clearing cache
    import e2d.settings as mod
    mod._settings_cache = None
    s2 = load_project_settings()
    assert s2.dynatrace_env_url == ""
