"""T-11 regression: ConfigStore.auth_key TTL cache."""

import json
from time import monotonic

import pytest

import services.config as config_module
from services.config import ConfigStore


class _FakeSettingsRepo:
    def get(self):
        return {}

    def update(self, data):
        pass


class _FakeGroupsRepo:
    def get(self):
        return {"items": []}

    def update(self, data):
        pass


def _store(tmp_path, auth_key="file-key"):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"auth-key": auth_key}), encoding="utf-8")
    return ConfigStore(
        path,
        settings_repository=_FakeSettingsRepo(),
        groups_repository=_FakeGroupsRepo(),
    )


def _expire(store):
    store._auth_key_cache = (
        monotonic() - config_module._AUTH_KEY_CACHE_TTL_SECONDS - 1.0,
        "stale",
    )


def test_auth_key_does_not_reread_within_ttl(tmp_path, monkeypatch):
    monkeypatch.delenv("CHATGPT2API_AUTH_KEY", raising=False)
    store = _store(tmp_path)
    calls = []
    original = config_module.read_json_object

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(config_module, "read_json_object", counting)
    store._auth_key_cache = None
    assert store.auth_key == "file-key"
    assert store.auth_key == "file-key"
    assert store.auth_key == "file-key"
    assert len(calls) == 1


def test_auth_key_file_change_visible_after_ttl(tmp_path, monkeypatch):
    monkeypatch.delenv("CHATGPT2API_AUTH_KEY", raising=False)
    store = _store(tmp_path)
    store.path.write_text(json.dumps({"auth-key": "rotated"}), encoding="utf-8")
    assert store.auth_key == "file-key"  # still inside TTL
    _expire(store)
    assert store.auth_key == "rotated"


def test_auth_key_env_takes_precedence_and_uses_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATGPT2API_AUTH_KEY", "env-key-1")
    store = _store(tmp_path, auth_key="file-key")
    store._auth_key_cache = None
    assert store.auth_key == "env-key-1"
    monkeypatch.setenv("CHATGPT2API_AUTH_KEY", "env-key-2")
    assert store.auth_key == "env-key-1"  # cached within TTL
    _expire(store)
    assert store.auth_key == "env-key-2"
