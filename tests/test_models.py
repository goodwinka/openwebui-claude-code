"""Тесты эндпоинта /v1/models."""
from __future__ import annotations


def test_list_models_structure(client, auth_headers):
    resp = client.get("/v1/models", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    assert isinstance(data["data"], list)
    assert len(data["data"]) > 0


def test_list_models_has_claude_code(client, auth_headers):
    resp = client.get("/v1/models", headers=auth_headers)
    ids = [m["id"] for m in resp.json()["data"]]
    assert "claude-code" in ids


def test_list_models_fields(client, auth_headers):
    resp = client.get("/v1/models", headers=auth_headers)
    for model in resp.json()["data"]:
        assert "id" in model
        assert "owned_by" in model
        assert model["owned_by"] == "anthropic"
