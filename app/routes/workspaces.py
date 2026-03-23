"""REST API для управления воркспейсами."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from app.models import (
    CloneRequest,
    DownloadZipRequest,
    FileEntry,
    FileListResponse,
    SwitchWorkspaceRequest,
    WorkspaceInfo,
    WorkspaceListResponse,
)
from app.workspace_manager import workspace_manager

router = APIRouter(prefix="/api/workspaces", tags=["Workspaces"])

# Директории, которые исключаем из листинга/скачивания
_EXCLUDED_DIRS = {".git"}
# Максимальный размер одного файла при скачивании (100 МБ)
_MAX_FILE_SIZE = 100 * 1024 * 1024


def _resolve_safe(ws_path: Path, rel_path: str) -> Path:
    """Возвращает абсолютный путь внутри ws_path.

    Выбрасывает HTTPException 400 при попытке path traversal.
    """
    # Нормализуем и убеждаемся, что путь не выходит за пределы воркспейса
    try:
        target = (ws_path / rel_path).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    try:
        target.relative_to(ws_path.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal is not allowed")
    return target


def _list_files_recursive(
    ws_path: Path,
    sub_path: Path,
    prefix: str,
) -> list[FileEntry]:
    """Рекурсивно обходит директорию и возвращает список FileEntry."""
    entries: list[FileEntry] = []
    try:
        items = sorted(sub_path.iterdir(), key=lambda p: (p.is_file(), p.name))
    except PermissionError:
        return entries

    for item in items:
        if item.name in _EXCLUDED_DIRS:
            continue
        rel = (prefix + "/" + item.name).lstrip("/")
        stat = item.stat()
        if item.is_dir():
            entries.append(
                FileEntry(
                    path=rel,
                    name=item.name,
                    is_dir=True,
                    modified_at=stat.st_mtime,
                )
            )
            entries.extend(_list_files_recursive(ws_path, item, rel))
        else:
            entries.append(
                FileEntry(
                    path=rel,
                    name=item.name,
                    is_dir=False,
                    size=stat.st_size,
                    modified_at=stat.st_mtime,
                )
            )
    return entries


def _get_ws_path(user_id: str, workspace_name: str) -> Path:
    """Возвращает путь к воркспейсу или 404."""
    ws_path = workspace_manager.workspace_path(user_id, workspace_name)
    if not ws_path.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Workspace '{workspace_name}' not found for user '{user_id}'",
        )
    return ws_path


# ─── Existing endpoints ───────────────────────────────────────────


@router.get("")
async def list_workspaces(
    user_id: str = Query(..., description="ID пользователя"),
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
) -> dict:
    """Удаляет воркспейс."""
    try:
        await workspace_manager.delete_workspace(user_id, workspace_name)
        return {"status": "deleted", "workspace": workspace_name}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ─── File listing & download ──────────────────────────────────────


@router.get("/{user_id}/{workspace_name}/files")
async def list_files(
    user_id: str,
    workspace_name: str,
    path: str = Query("", description="Относительный путь внутри воркспейса (пусто = корень)"),
) -> FileListResponse:
    """Возвращает список файлов воркспейса (рекурсивно).

    Параметр `path` позволяет ограничить листинг поддиректорией.
    """
    ws_path = _get_ws_path(user_id, workspace_name)
    start = _resolve_safe(ws_path, path) if path else ws_path.resolve()
    if not start.is_dir():
        raise HTTPException(status_code=404, detail=f"Path '{path}' not found or not a directory")

    entries = _list_files_recursive(ws_path.resolve(), start, path.strip("/"))
    return FileListResponse(
        workspace=workspace_name,
        root=path.strip("/"),
        files=entries,
    )


@router.get("/{user_id}/{workspace_name}/files/download")
async def download_file(
    user_id: str,
    workspace_name: str,
    path: str = Query(..., description="Относительный путь к файлу внутри воркспейса"),
) -> FileResponse:
    """Скачивает один файл из воркспейса."""
    ws_path = _get_ws_path(user_id, workspace_name)
    target = _resolve_safe(ws_path, path)

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"File '{path}' not found")
    if target.is_dir():
        raise HTTPException(status_code=400, detail="Path points to a directory, use download-zip")
    if target.stat().st_size > _MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {_MAX_FILE_SIZE // 1024 // 1024} MB). Use download-zip.",
        )

    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type="application/octet-stream",
    )


@router.post("/{user_id}/{workspace_name}/files/download-zip")
async def download_files_zip(
    user_id: str,
    workspace_name: str,
    request: DownloadZipRequest,
) -> StreamingResponse:
    """Скачивает выбранные файлы/папки как ZIP-архив.

    Передайте список относительных путей в теле запроса.
    Если `paths` пустой — скачивается весь воркспейс.
    """
    ws_path = _get_ws_path(user_id, workspace_name)
    ws_resolved = ws_path.resolve()

    paths = request.paths

    def _iter_path(p: Path, arcname_prefix: str):
        """Генерирует (file_path, arcname) для добавления в архив."""
        if p.is_file():
            yield p, arcname_prefix
        elif p.is_dir():
            for item in sorted(p.rglob("*")):
                # Пропускаем .git
                parts = item.relative_to(p).parts
                if any(part in _EXCLUDED_DIRS for part in parts):
                    continue
                if item.is_file():
                    rel = item.relative_to(ws_resolved)
                    yield item, str(rel)

    def _build_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            if not paths:
                # Весь воркспейс
                for item in sorted(ws_resolved.rglob("*")):
                    parts = item.relative_to(ws_resolved).parts
                    if any(part in _EXCLUDED_DIRS for part in parts):
                        continue
                    if item.is_file():
                        arcname = str(item.relative_to(ws_resolved))
                        zf.write(item, arcname)
            else:
                for rel_path in paths:
                    target = _resolve_safe(ws_path, rel_path)
                    if not target.exists():
                        continue
                    for file_path, arcname in _iter_path(target, rel_path):
                        zf.write(file_path, arcname)
        return buf.getvalue()

    zip_bytes = _build_zip()
    zip_filename = f"{workspace_name}.zip"

    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'},
    )


@router.get("/{user_id}/{workspace_name}/download")
async def download_workspace_zip(
    user_id: str,
    workspace_name: str,
) -> StreamingResponse:
    """Скачивает весь воркспейс как ZIP-архив (без .git)."""
    ws_path = _get_ws_path(user_id, workspace_name)
    ws_resolved = ws_path.resolve()

    def _build_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(ws_resolved.rglob("*")):
                parts = item.relative_to(ws_resolved).parts
                if any(part in _EXCLUDED_DIRS for part in parts):
                    continue
                if item.is_file():
                    arcname = str(item.relative_to(ws_resolved))
                    zf.write(item, arcname)
        return buf.getvalue()

    zip_bytes = _build_zip()
    zip_filename = f"{workspace_name}.zip"

    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'},
    )
