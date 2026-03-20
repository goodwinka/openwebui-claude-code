"""Тесты аутентификации."""
from __future__ import annotations


def test_models_requires_auth(client):
    resp = client.get("/v1/models")
    assert resp.status_code == 401


def test_models_wrong_key(client):
    resp = client.get("/v1/models", headers={"Authorization": "Bearer wrong-key"})
    assert resp.status_code == 403


def test_models_valid_key(client, auth_headers):
    resp = client.get("/v1/models", headers=auth_headers)
    assert resp.status_code == 200


def test_chat_requires_auth(client):
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}], "stream": False},
    )
    assert resp.status_code == 401


def test_workspaces_requires_auth(client):
    resp = client.get("/api/workspaces?user_id=testuser")
    assert resp.status_code == 401
