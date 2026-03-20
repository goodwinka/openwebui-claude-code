"""Тесты эндпоинта /health."""
from __future__ import annotations


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "workspaces_root" in data


def test_health_no_auth_required(client):
    """Health не требует авторизации."""
    resp = client.get("/health")
    assert resp.status_code == 200
