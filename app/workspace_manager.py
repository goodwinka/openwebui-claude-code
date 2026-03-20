"""Менеджер воркспейсов: создание директорий, git-клонирование.

Все процессы работают от одного системного пользователя (SERVICE_USER).
Изоляция между пользователями — через отдельные директории.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings
from app.models import WorkspaceInfo

logger = logging.getLogger(__name__)

_ACTIVE_WS_FILE = ".active_workspace"


def _sanitize_name(name: str) -> str:
    """Очистка имени для использования как имени директории."""
    name = re.sub(r"[^\w\-.]", "_", name)
    return name.strip("_.")[:64] or "workspace"


def _repo_name_from_url(url: str) -> str:
    """Извлекает имя репозитория из URL."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    name = path.split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return _sanitize_name(name) or "repo"


class WorkspaceManager:
    """Управление per-user воркспейсами.

    Один системный пользователь, изоляция через директории:
    /srv/workspaces/
    ├── alice/
    │   ├── .active_workspace   (содержит имя активного ws)
    │   ├── default/
    │   └── my-project/         (git clone)
    └── bob/
        ├── default/
        └── another-repo/
    """

    def __init__(self) -> None:
        self._root = settings.workspaces_root
        self._max_per_user = settings.max_workspaces_per_user
        self._lock = asyncio.Lock()
        self._ensure_root()

    def _ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    # ─── Paths ────────────────────────────────────────────────────

    def user_root(self, user_id: str) -> Path:
        return self._root / _sanitize_name(user_id)

    def workspace_path(self, user_id: str, workspace_name: str) -> Path:
        return self.user_root(user_id) / _sanitize_name(workspace_name)

    def _active_ws_path(self, user_id: str) -> Path:
        return self.user_root(user_id) / _ACTIVE_WS_FILE

    # ─── Public API ───────────────────────────────────────────────

    async def ensure_workspace(self, user_id: str) -> Path:
        """Возвращает путь к активному воркспейсу.

        Создаёт дефолтный если ни одного нет.
        """
        async with self._lock:
            user_dir = self.user_root(user_id)
            user_dir.mkdir(parents=True, exist_ok=True)

            active = self._get_active_workspace(user_id)
            if active and (user_dir / active).is_dir():
                return user_dir / active

            # Дефолтный воркспейс
            default_ws = user_dir / "default"
            default_ws.mkdir(exist_ok=True)
            self._set_active_workspace(user_id, "default")
            return default_ws

    async def clone_repo(
        self,
        user_id: str,
        repo_url: str,
        name: str | None = None,
        branch: str | None = None,
    ) -> WorkspaceInfo:
        """Клонирует git-репозиторий в воркспейс пользователя."""
        async with self._lock:
            user_dir = self.user_root(user_id)
            user_dir.mkdir(parents=True, exist_ok=True)

            ws_name = _sanitize_name(name or _repo_name_from_url(repo_url))
            ws_path = user_dir / ws_name

            # Лимит
            existing = self._list_workspaces_sync(user_id)
            if len(existing) >= self._max_per_user:
                raise ValueError(
                    f"User {user_id} already has {len(existing)} workspaces "
                    f"(max: {self._max_per_user})"
                )

            if ws_path.exists():
                raise ValueError(f"Workspace '{ws_name}' already exists")

            # git clone
            cmd = ["git", "clone", "--depth=1"]
            if branch:
                cmd += ["--branch", branch]
            cmd += [repo_url, str(ws_path)]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                if ws_path.exists():
                    shutil.rmtree(ws_path)
                raise RuntimeError(f"git clone failed: {stderr.decode()}")

            self._set_active_workspace(user_id, ws_name)

            return WorkspaceInfo(
                name=ws_name,
                path=str(ws_path),
                is_active=True,
                git_remote=repo_url,
                created_at=time.time(),
            )

    async def list_workspaces(self, user_id: str) -> list[WorkspaceInfo]:
        return self._list_workspaces_sync(user_id)

    async def switch_workspace(self, user_id: str, workspace_name: str) -> Path:
        ws_path = self.workspace_path(user_id, workspace_name)
        if not ws_path.is_dir():
            raise ValueError(f"Workspace '{workspace_name}' not found")
        self._set_active_workspace(user_id, workspace_name)
        return ws_path

    async def delete_workspace(self, user_id: str, workspace_name: str) -> None:
        if workspace_name == "default":
            raise ValueError("Cannot delete the default workspace")
        ws_path = self.workspace_path(user_id, workspace_name)
        if not ws_path.is_dir():
            raise ValueError(f"Workspace '{workspace_name}' not found")

        shutil.rmtree(ws_path)

        if self._get_active_workspace(user_id) == workspace_name:
            self._set_active_workspace(user_id, "default")

    def get_active_workspace_name(self, user_id: str) -> str | None:
        return self._get_active_workspace(user_id)

    # ─── Internal ─────────────────────────────────────────────────

    def _list_workspaces_sync(self, user_id: str) -> list[WorkspaceInfo]:
        user_dir = self.user_root(user_id)
        if not user_dir.is_dir():
            return []

        active = self._get_active_workspace(user_id)
        result: list[WorkspaceInfo] = []

        for item in sorted(user_dir.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                git_remote = None
                git_config = item / ".git" / "config"
                if git_config.exists():
                    try:
                        text = git_config.read_text()
                        for line in text.splitlines():
                            if "url = " in line:
                                git_remote = line.split("url = ", 1)[1].strip()
                                break
                    except OSError:
                        pass

                result.append(
                    WorkspaceInfo(
                        name=item.name,
                        path=str(item),
                        is_active=(item.name == active),
                        git_remote=git_remote,
                        created_at=item.stat().st_ctime,
                    )
                )
        return result

    def _get_active_workspace(self, user_id: str) -> str | None:
        path = self._active_ws_path(user_id)
        if path.exists():
            return path.read_text().strip() or None
        return None

    def _set_active_workspace(self, user_id: str, name: str) -> None:
        path = self._active_ws_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)


# Singleton
workspace_manager = WorkspaceManager()
