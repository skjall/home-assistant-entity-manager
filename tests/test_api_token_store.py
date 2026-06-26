"""Tests for the generated, hashed API token store."""

import json

from api_token_store import TOKEN_PREFIX, ApiTokenStore


def _store(tmp_path):
    return ApiTokenStore(str(tmp_path / "api_token.json"))


def test_generate_returns_prefixed_token_and_marks_configured(tmp_path):
    store = _store(tmp_path)
    assert store.exists() is False

    token = store.generate()
    assert token.startswith(TOKEN_PREFIX)
    assert len(token) > len(TOKEN_PREFIX) + 20
    assert store.exists() is True


def test_plaintext_is_not_persisted(tmp_path):
    path = tmp_path / "api_token.json"
    store = ApiTokenStore(str(path))
    token = store.generate()

    raw = path.read_text(encoding="utf-8")
    assert token not in raw
    assert "hash" in json.loads(raw)


def test_verify_accepts_correct_and_rejects_wrong(tmp_path):
    store = _store(tmp_path)
    token = store.generate()
    assert store.verify(token) is True
    assert store.verify(token + "x") is False
    assert store.verify("") is False


def test_regenerate_invalidates_previous_token(tmp_path):
    store = _store(tmp_path)
    old = store.generate()
    new = store.generate()
    assert new != old
    assert store.verify(old) is False
    assert store.verify(new) is True


def test_revoke_clears_token(tmp_path):
    store = _store(tmp_path)
    token = store.generate()
    store.revoke()
    assert store.exists() is False
    assert store.verify(token) is False


def test_verify_without_token_configured(tmp_path):
    store = _store(tmp_path)
    assert store.verify("em_anything") is False


def test_status_never_exposes_token(tmp_path):
    store = _store(tmp_path)
    token = store.generate()
    status = store.status()
    assert status["configured"] is True
    assert status["created_at"]
    assert status["last_used_at"] is None
    assert token not in json.dumps(status)


def test_verify_updates_last_used(tmp_path):
    store = _store(tmp_path)
    token = store.generate()
    assert store.status()["last_used_at"] is None
    store.verify(token)
    assert store.status()["last_used_at"] is not None


def test_token_persists_across_instances(tmp_path):
    path = str(tmp_path / "api_token.json")
    token = ApiTokenStore(path).generate()
    # A fresh instance on the same path sees the existing token.
    assert ApiTokenStore(path).verify(token) is True
