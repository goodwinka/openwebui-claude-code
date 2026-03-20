"""Тесты REST API воркспейсов /api/workspaces."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.models import WorkspaceInfo


def _ws_info(name: str, is_active: bool = False) -> WorkspaceInfo:
    return WorkspaceInfo(
        name=name,
        path=f"/tmp/test-workspaces/user/{name}",
        is_active=is_active,
        git_remote=None,
        created_at=1700000000.0,
    )


# ─── GET /api/workspaces ──────────────────────────────────────────────


def test_list_workspaces_empty(client, auth_headers):
    with patch(
        "app.workspace_manager.workspace_manager.list_workspaces",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = client.get("/api/workspaces?user_id=testuser", headers=auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == "testuser"
    assert data["workspaces"] == []
    assert data["active"] is None


def test_list_workspaces_with_items(client, auth_headers):
    workspaces = [_ws_info("default", is_active=True), _ws_info("my-project")]
    with patch(
        "app.workspace_manager.workspace_manager.list_workspaces",
        new_callable=AsyncMock,
        return_value=workspaces,
    ):
        resp = client.get("/api/workspaces?user_id=alice", headers=auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] == "default"
    assert len(data["workspaces"]) == 2


def test_list_workspaces_missing_user_id(client, auth_headers):
    resp = client.get("/api/workspaces", headers=auth_headers)
    assert resp.status_code == 422


# ─── POST /api/workspaces/clone ───────────────────────────────────────


def test_clone_repo_success(client, auth_headers, tmp_path):
    info = WorkspaceInfo(
        name="myrepo",
        path=str(tmp_path / "myrepo"),
        is_active=True,
        git_remote="https://github.com/example/myrepo.git",
        created_at=1700000000.0,
    )
    with patch(
        "app.workspace_manager.workspace_manager.clone_repo",
        new_callable=AsyncMock,
        return_value=info,
    ):
        resp = client.post(
            "/api/workspaces/clone",
            headers=auth_headers,
            json={"user_id": "alice", "repo_url": "https://github.com/example/myrepo.git"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "myrepo"
    assert data["is_active"] is True


def test_clone_repo_value_error(client, auth_headers):
    with patch(
        "app.workspace_manager.workspace_manager.clone_repo",
        new_callable=AsyncMock,
        side_effect=ValueError("Workspace 'myrepo' already exists"),
    ):
        resp = client.post(
            "/api/workspaces/clone",
            headers=auth_headers,
            json={"user_id": "alice", "repo_url": "https://github.com/example/myrepo.git"},
        )

    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]


def test_clone_repo_runtime_error(client, auth_headers):
    with patch(
        "app.workspace_manager.workspace_manager.clone_repo",
        new_callable=AsyncMock,
        side_effect=RuntimeError("git clone failed: not found"),
    ):
        resp = client.post(
            "/api/workspaces/clone",
            headers=auth_headers,
            json={"user_id": "alice", "repo_url": "https://github.com/example/bad.git"},
        )

    assert resp.status_code == 500


# ─── PUT /api/workspaces/active ───────────────────────────────────────


def test_switch_workspace_success(client, auth_headers, tmp_path):
    with patch(
        "app.workspace_manager.workspace_manager.switch_workspace",
        new_callable=AsyncMock,
        return_value=tmp_path / "my-project",
    ):
        resp = client.put(
            "/api/workspaces/active",
            headers=auth_headers,
            json={"user_id": "alice", "workspace": "my-project"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["active"] == "my-project"


def test_switch_workspace_not_found(client, auth_headers):
    with patch(
        "app.workspace_manager.workspace_manager.switch_workspace",
        new_callable=AsyncMock,
        side_effect=ValueError("Workspace 'missing' not found"),
    ):
        resp = client.put(
            "/api/workspaces/active",
            headers=auth_headers,
            json={"user_id": "alice", "workspace": "missing"},
        )

    assert resp.status_code == 404


# ─── DELETE /api/workspaces/{user_id}/{workspace_name} ───────────────


def test_delete_workspace_success(client, auth_headers):
    with patch(
        "app.workspace_manager.workspace_manager.delete_workspace",
        new_callable=AsyncMock,
    ):
        resp = client.delete(
            "/api/workspaces/alice/my-project",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


def test_delete_default_workspace(client, auth_headers):
    with patch(
        "app.workspace_manager.workspace_manager.delete_workspace",
        new_callable=AsyncMock,
        side_effect=ValueError("Cannot delete the default workspace"),
    ):
        resp = client.delete(
            "/api/workspaces/alice/default",
            headers=auth_headers,
        )

    assert resp.status_code == 400
    assert "default" in resp.json()["detail"]
