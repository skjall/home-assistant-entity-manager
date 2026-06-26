"""Tests for fetching MQTT credentials from the Supervisor (graceful degradation)."""

import asyncio

import aiohttp

import mqtt_credentials


class _FakeResp:
    def __init__(self, status, data):
        self.status = status
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._data


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, headers=None):
        return self._resp


def _patch(monkeypatch, status, data):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: _FakeSession(_FakeResp(status, data)))


def _run(coro):
    return asyncio.run(coro)


def test_credentials_ok(monkeypatch):
    _patch(
        monkeypatch,
        200,
        {
            "result": "ok",
            "data": {"host": "core-mosquitto", "port": 1883, "ssl": False, "username": "addons", "password": "pw"},
        },
    )
    creds = _run(mqtt_credentials.get_mqtt_credentials())
    assert creds == {"host": "core-mosquitto", "port": 1883, "username": "addons", "password": "pw", "ssl": False}


def test_credentials_403_returns_none(monkeypatch):
    _patch(monkeypatch, 403, {"result": "error"})
    assert _run(mqtt_credentials.get_mqtt_credentials()) is None


def test_credentials_no_host_returns_none(monkeypatch):
    _patch(monkeypatch, 200, {"result": "ok", "data": {}})
    assert _run(mqtt_credentials.get_mqtt_credentials()) is None


def test_credentials_no_token_returns_none(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    assert _run(mqtt_credentials.get_mqtt_credentials()) is None
