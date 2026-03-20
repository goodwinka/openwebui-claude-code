"""REST API для управления воркспейсами."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import verify_api_key
from app.models import (
    CloneRequest,
    SwitchWorkspaceRequest,
    WorkspaceInfo,
    WorkspaceListResponse,
)
from app.workspace_manager import workspace_manager

router = APIRouter(prefix="/api/workspaces", tags=["Workspaces"])


@router.get("")
async def list_workspaces(
    user_id: str = Query(..., description="ID пользователя"),
    _: str = Depends(verify_api_key),
) -> WorkspaceListResponse:
    """Список воркспейсов пользователя."""
    workspaces = await workspace_manager.list_workspaces(user_id)
    active = next((ws.name for ws in workspaces if ws.is_active), None)
    return WorkspaceListResponse(
        user_id=user_id,
        workspaces=workspaces,
        active=active,
    )


@router.post("/clone")
async def clone_repo(
    request: CloneRequest,
    _: str = Depends(verify_api_key),
) -> WorkspaceInfo:
    """Клонирует git-репозиторий в воркспейс."""
    try:
        return await workspace_manager.clone_repo(
            user_id=request.user_id,
            repo_url=request.repo_url,
            name=request.name,
            branch=request.branch,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.put("/active")
async def switch_workspace(
    request: SwitchWorkspaceRequest,
    _: str = Depends(verify_api_key),
) -> dict:
    """Переключает активный воркспейс."""
    try:
        path = await workspace_manager.switch_workspace(
            request.user_id, request.workspace
        )
        return {"status": "ok", "active": request.workspace, "path": str(path)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/{user_id}/{workspace_name}")
async def delete_workspace(
    user_id: str,
    workspace_name: str,
    _: str = Depends(verify_api_key),
) -> dict:
    """Удаляет воркспейс."""
    try:
        await workspace_manager.delete_workspace(user_id, workspace_name)
        return {"status": "deleted", "workspace": workspace_name}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
