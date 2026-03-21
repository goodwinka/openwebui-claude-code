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
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings
from app.models import WorkspaceInfo

logger = logging.getLogger(__name__)

_ACTIVE_WS_FILE = ".active_workspace"


def _parse_remote(remote_url: str) -> tuple[str, str, str]:
    """Определяет платформу и извлекает host и путь проекта из remote URL.

    Поддерживает форматы:
      https://github.com/owner/repo.git
      https://gitlab.company.local/group/project.git
      git@github.com:owner/repo.git
      git@gitlab.company.local:group/project.git

    Возвращает (platform, host, project_path):
      platform: "github" | "gitlab" | "unknown"
      host:     "github.com" | "gitlab.company.local" | ...
      project_path: "owner/repo" | "group/subgroup/project"
    """
    url = remote_url.strip()

    # SSH формат: git@host:path
    ssh_match = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", url)
    if ssh_match:
        host = ssh_match.group(1).lower()
        path = ssh_match.group(2).strip("/")
    else:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]

    if "github.com" in host:
        platform = "github"
    elif "gitlab" in host:
        platform = "gitlab"
    else:
        # Для self-hosted: если хост не github — считаем GitLab
        # (наиболее распространённый self-hosted вариант)
        platform = "gitlab"

    return platform, host, path


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
            depth = settings.git_clone_depth
            cmd = ["git", "clone"] + ([f"--depth={depth}"] if depth > 0 else [])
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

            branch_name = await self._init_claude_branch(ws_path)
            logger.info("Created branch %s in %s", branch_name, ws_path)

            self._set_active_workspace(user_id, ws_name)

            return WorkspaceInfo(
                name=ws_name,
                path=str(ws_path),
                is_active=True,
                git_remote=repo_url,
                git_branch=branch_name,
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

    # ─── Git helpers ──────────────────────────────────────────────

    def _proxy_env(self) -> dict[str, str]:
        """Возвращает env-переменные прокси для git-процессов."""
        env: dict[str, str] = {}
        if settings.http_proxy:
            env["http_proxy"] = settings.http_proxy
            env["HTTP_PROXY"] = settings.http_proxy
        if settings.effective_https_proxy:
            env["https_proxy"] = settings.effective_https_proxy
            env["HTTPS_PROXY"] = settings.effective_https_proxy
        return env

    async def _run_git(
        self, args: list[str], cwd: Path
    ) -> tuple[int, str, str]:
        """Runs a git subcommand in cwd. Returns (returncode, stdout, stderr).

        Proxy env vars are injected automatically if configured.
        """
        env = os.environ.copy()
        env.update(self._proxy_env())
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    async def _init_claude_branch(self, ws_path: Path) -> str:
        """Sets local git identity and creates a claude/session-* branch.

        Called once immediately after a successful clone.
        Returns the new branch name.
        """
        branch_name = settings.git_branch_prefix + datetime.now().strftime("%Y%m%d-%H%M%S")

        await self._run_git(
            ["config", "--local", "user.name", settings.git_user_name], ws_path
        )
        await self._run_git(
            ["config", "--local", "user.email", settings.git_user_email], ws_path
        )

        rc, _, stderr = await self._run_git(
            ["checkout", "-b", branch_name], ws_path
        )
        if rc != 0:
            raise RuntimeError(f"Failed to create branch: {stderr.strip()}")

        return branch_name

    async def get_current_branch(self, ws_path: Path) -> str | None:
        """Returns the current git branch name, or None if not in a git repo."""
        rc, stdout, _ = await self._run_git(
            ["branch", "--show-current"], ws_path
        )
        if rc != 0:
            return None
        return stdout.strip() or None

    async def git_push(self, ws_path: Path) -> str:
        """Pushes the current branch to origin. Returns the branch name."""
        branch = await self.get_current_branch(ws_path)
        if not branch:
            raise RuntimeError("Not on any branch (detached HEAD or not a git repo)")

        rc, _, stderr = await self._run_git(
            ["push", "-u", "origin", branch], ws_path
        )
        if rc != 0:
            raise RuntimeError(f"git push failed: {stderr.strip()}")
        return branch

    async def create_pr(self, ws_path: Path, title: str) -> str:
        """Creates a PR/MR. Dispatches to GitHub or GitLab based on remote URL."""
        branch = await self.get_current_branch(ws_path)
        if not branch:
            raise RuntimeError("Not on any branch — cannot create a PR.")

        rc, remote_url, _ = await self._run_git(
            ["remote", "get-url", "origin"], ws_path
        )
        if rc != 0 or not remote_url.strip():
            raise RuntimeError("Cannot determine remote URL for origin.")

        platform, host, project_path = _parse_remote(remote_url.strip())

        if platform == "github":
            return await self._create_github_pr(ws_path, branch, title)
        elif platform == "gitlab":
            if not settings.gitlab_token:
                raise RuntimeError(
                    "GITLAB_TOKEN не настроен. "
                    "Добавьте токен в .env: GITLAB_TOKEN=glpat-xxxx"
                )
            return await self._create_gitlab_mr(host, project_path, branch, title)
        else:
            raise RuntimeError(
                f"Неизвестная платформа для remote '{remote_url.strip()}'. "
                "Поддерживаются GitHub и GitLab."
            )

    async def _create_github_pr(
        self, ws_path: Path, branch: str, title: str
    ) -> str:
        """Creates a GitHub PR via gh CLI, with GitHub API fallback."""
        if shutil.which("gh"):
            proc = await asyncio.create_subprocess_exec(
                "gh", "pr", "create",
                "--title", title,
                "--body", "",
                "--head", branch,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(ws_path),
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode(errors="replace").strip()
            # gh failed — попробуем через API если есть токен
            gh_err = stderr.decode(errors="replace").strip()
            if not settings.github_token:
                raise RuntimeError(f"gh pr create failed: {gh_err}")

        # Fallback: GitHub REST API
        if not settings.github_token:
            raise RuntimeError(
                "GitHub CLI (`gh`) не установлен и GITHUB_TOKEN не задан. "
                "Установите gh: https://cli.github.com или добавьте GITHUB_TOKEN в .env"
            )
        _, remote_url, _ = await self._run_git(
            ["remote", "get-url", "origin"], ws_path
        )
        _, _, project_path = _parse_remote(remote_url.strip())
        return await self._github_api_create_pr(project_path, branch, title)

    async def _github_api_create_pr(
        self, project_path: str, branch: str, title: str
    ) -> str:
        """Creates a GitHub PR via REST API."""
        import httpx

        proxy = settings.effective_https_proxy or None
        headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
        }
        # Определяем base branch через API
        async with httpx.AsyncClient(proxy=proxy, timeout=settings.git_api_timeout) as client:
            repo_resp = await client.get(
                f"https://api.github.com/repos/{project_path}",
                headers=headers,
            )
            repo_resp.raise_for_status()
            default_branch = repo_resp.json().get("default_branch", "main")

            pr_resp = await client.post(
                f"https://api.github.com/repos/{project_path}/pulls",
                headers=headers,
                json={"title": title, "body": "", "head": branch, "base": default_branch},
            )
            pr_resp.raise_for_status()
            return pr_resp.json()["html_url"]

    async def _create_gitlab_mr(
        self, host: str, project_path: str, branch: str, title: str
    ) -> str:
        """Creates a GitLab Merge Request via REST API."""
        import httpx
        from urllib.parse import quote

        base_url = f"https://{host}/api/v4"
        encoded_path = quote(project_path, safe="")
        headers = {"PRIVATE-TOKEN": settings.gitlab_token}
        proxy = settings.effective_https_proxy or None

        async with httpx.AsyncClient(
            proxy=proxy, timeout=settings.git_api_timeout, verify=settings.gitlab_ssl_verify
        ) as client:
            # Получаем default branch проекта
            proj_resp = await client.get(
                f"{base_url}/projects/{encoded_path}",
                headers=headers,
            )
            if proj_resp.status_code == 401:
                raise RuntimeError("GitLab: неверный GITLAB_TOKEN (401 Unauthorized)")
            if proj_resp.status_code == 404:
                raise RuntimeError(
                    f"GitLab: проект '{project_path}' не найден (404). "
                    "Проверьте токен и доступ к репозиторию."
                )
            proj_resp.raise_for_status()
            default_branch = proj_resp.json().get("default_branch", "main")

            # Создаём MR
            mr_resp = await client.post(
                f"{base_url}/projects/{encoded_path}/merge_requests",
                headers=headers,
                json={
                    "source_branch": branch,
                    "target_branch": default_branch,
                    "title": title,
                    "remove_source_branch": settings.gitlab_mr_remove_source_branch,
                },
            )
            if mr_resp.status_code == 409:
                raise RuntimeError(
                    "GitLab: Merge Request для этой ветки уже существует."
                )
            mr_resp.raise_for_status()
            return mr_resp.json()["web_url"]


# Singleton
workspace_manager = WorkspaceManager()
