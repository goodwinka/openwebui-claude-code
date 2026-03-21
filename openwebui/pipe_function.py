"""Open WebUI Pipe Function для Claude Code.

Устанавливается в Open WebUI: Admin → Functions → Add Function.

Что делает:
- Управление воркспейсами через UI (workspace panel с кнопками)
- Проксирует чат к внутреннему Claude Code бэкенду (server-side, браузер не
  обращается к бэкенду напрямую)
- Скачивание файлов через Open WebUI (ZIP-артефакт с кнопкой)

Настройка через Valves (Admin → Functions → ⚙):
  BACKEND_URL     — внутренний URL бэкенда (например http://claude-code:8000)
  API_KEY         — значение api_secret_key из .env бэкенда
  MAX_DOWNLOAD_MB — максимальный размер ZIP (МБ, по умолчанию 50)
  BACKEND_TIMEOUT — таймаут запросов (сек, по умолчанию 300)

Команды в чате:
  ws / workspaces / /ws   — панель управления воркспейсами
  clone [url]             — клонировать репозиторий (диалог если URL не указан)
  workspace switch [name] — переключить воркспейс (диалог если имя не указано)
  workspace delete <name> — удалить воркспейс (диалог подтверждения)
  download [workspace]    — скачать ZIP (автоопределение активного если не указан)
  files <workspace> [path]— список файлов воркспейса
  Всё остальное           — проксируется к Claude Code агенту
"""

from __future__ import annotations

import base64
import html
import json
import re
from typing import Any, AsyncGenerator, Optional

import httpx
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        BACKEND_URL: str = Field(
            default="http://claude-code-proxy:8000",
            description="Внутренний URL Claude Code бэкенда",
        )
        API_KEY: str = Field(
            default="change-me",
            description="api_secret_key из .env бэкенда",
        )
        MAX_DOWNLOAD_MB: int = Field(
            default=50,
            description="Максимальный размер ZIP-архива для скачивания (МБ)",
        )
        BACKEND_TIMEOUT: int = Field(
            default=300,
            description="Таймаут запросов к бэкенду (секунд)",
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self) -> list[dict]:
        # Уникальный id — не конфликтует с моделями прямого подключения
        return [{"id": "claude-code-pipe", "name": "Claude Code"}]

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> AsyncGenerator[str, None]:
        messages = body.get("messages", [])
        if not messages:
            yield "Нет сообщений."
            return

        last_msg = messages[-1]
        content = (last_msg.get("content") or "").strip()
        user_id = self._user_id(__user__)
        emit = __event_emitter__
        call = __event_call__

        # ── workspace panel ───────────────────────────────────────────
        if re.match(r"^(?:ws|workspaces?|/ws|workspace\s+list|workspace\s+ls)$",
                    content, re.IGNORECASE):
            async for chunk in self._workspace_panel(user_id, emit):
                yield chunk
            return

        # ── clone ─────────────────────────────────────────────────────
        m = re.match(r"^clone(?:\s+(.+))?$", content, re.IGNORECASE)
        if m:
            async for chunk in self._cmd_clone(user_id, (m.group(1) or "").strip(), call, emit):
                yield chunk
            return

        # ── workspace switch ──────────────────────────────────────────
        m = re.match(r"^(?:workspace\s+switch|switch\s+workspace|switch)(?:\s+(\S+))?$",
                     content, re.IGNORECASE)
        if m:
            async for chunk in self._cmd_switch(user_id, (m.group(1) or "").strip(), call, emit):
                yield chunk
            return

        # ── workspace delete ──────────────────────────────────────────
        m = re.match(r"^(?:workspace\s+delete|delete\s+workspace?|delete)\s+(\S+)$",
                     content, re.IGNORECASE)
        if m:
            async for chunk in self._cmd_delete(user_id, m.group(1), call, emit):
                yield chunk
            return

        # ── download (без аргумента → активный воркспейс) ────────────
        if re.match(r"^(?:download|скачать)$", content, re.IGNORECASE):
            async for chunk in self._cmd_download_active(user_id, emit):
                yield chunk
            return

        # ── download <workspace> ──────────────────────────────────────
        m = re.match(r"^(?:download|скачать)\s+(\S+)$", content, re.IGNORECASE)
        if m:
            async for chunk in self._download(user_id, m.group(1), emit):
                yield chunk
            return

        # ── files <workspace> [path] ──────────────────────────────────
        m = re.match(r"^(?:files|файлы)\s+(\S+)(?:\s+(.+))?$", content, re.IGNORECASE)
        if m:
            async for chunk in self._list_files(user_id, m.group(1), m.group(2) or "", emit):
                yield chunk
            return

        # ── обычный чат → бэкенд ─────────────────────────────────────
        async for chunk in self._proxy_chat(body, user_id, emit):
            yield chunk

    # ─── helpers ──────────────────────────────────────────────────────

    def _user_id(self, user: Optional[dict]) -> str:
        if not user:
            return "default"
        return user.get("name") or user.get("id") or "default"

    def _headers(self, user_id: str | None = None) -> dict:
        h = {"Authorization": f"Bearer {self.valves.API_KEY}"}
        if user_id:
            # Бэкенд проверяет этот заголовок с наивысшим приоритетом (openai_compat.py)
            h["X-OpenWebUI-User-Name"] = user_id
        return h

    @staticmethod
    async def _emit_status(emit, description: str, *, done: bool) -> None:
        if emit:
            await emit({"type": "status", "data": {"description": description, "done": done}})

    # ─── workspace list ────────────────────────────────────────────────

    async def _get_workspaces(self, user_id: str) -> list[dict]:
        """Возвращает список воркспейсов пользователя с бэкенда."""
        url = f"{self.valves.BACKEND_URL}/api/workspaces"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    url,
                    headers=self._headers(user_id),
                    params={"user_id": user_id},
                )
                resp.raise_for_status()
                return resp.json().get("workspaces", [])
        except Exception:
            return []

    # ─── workspace panel ──────────────────────────────────────────────

    async def _workspace_panel(
        self,
        user_id: str,
        emit,
    ) -> AsyncGenerator[str, None]:
        """Возвращает HTML-артефакт с панелью управления воркспейсами."""
        await self._emit_status(emit, "Загрузка воркспейсов…", done=False)
        workspaces = await self._get_workspaces(user_id)
        await self._emit_status(emit, "Готово", done=True)

        # Сериализуем данные для JS — имена экранируем
        ws_data = [
            {
                "name": ws.get("name", ""),
                "is_active": ws.get("is_active", False),
                "git_remote": ws.get("git_remote") or "",
                "git_branch": ws.get("git_branch") or "",
            }
            for ws in workspaces
        ]
        ws_json = json.dumps(ws_data, ensure_ascii=False)

        html_page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<style>
  :root {{
    --bg: #f8fafc; --card: #fff; --border: #e2e8f0;
    --text: #1e293b; --muted: #64748b; --active: #16a34a;
    --btn-bg: #f1f5f9; --btn-hover: #e2e8f0;
    --accent: #2563eb; --accent-hover: #1d4ed8;
    --danger: #dc2626; --danger-hover: #b91c1c;
    --shadow: 0 1px 4px rgba(0,0,0,.08);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f172a; --card: #1e293b; --border: #334155;
      --text: #f1f5f9; --muted: #94a3b8; --active: #4ade80;
      --btn-bg: #334155; --btn-hover: #475569;
      --accent: #3b82f6; --accent-hover: #60a5fa;
      --danger: #f87171; --danger-hover: #fca5a5;
      --shadow: 0 1px 4px rgba(0,0,0,.4);
    }}
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text);
    padding: 16px; font-size: 14px;
  }}
  .header {{
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 12px;
  }}
  .header h2 {{ font-size: 16px; font-weight: 600; }}
  .ws-list {{ display: flex; flex-direction: column; gap: 8px; }}
  .ws-card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px; box-shadow: var(--shadow);
  }}
  .ws-card.active {{ border-color: var(--active); }}
  .ws-name {{
    font-weight: 600; font-size: 15px;
    display: flex; align-items: center; gap: 6px; margin-bottom: 4px;
  }}
  .dot {{
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--border); flex-shrink: 0;
  }}
  .active .dot {{ background: var(--active); }}
  .badge {{
    font-size: 11px; color: var(--active);
    font-weight: 500; padding: 1px 6px;
    border: 1px solid var(--active); border-radius: 10px;
  }}
  .ws-meta {{ color: var(--muted); font-size: 12px; margin-bottom: 10px; }}
  .actions {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .btn {{
    padding: 4px 10px; border-radius: 5px; border: 1px solid var(--border);
    background: var(--btn-bg); color: var(--text); cursor: pointer;
    font-size: 12px; font-weight: 500; transition: background .15s;
  }}
  .btn:hover {{ background: var(--btn-hover); }}
  .btn-accent {{
    background: var(--accent); color: #fff; border-color: var(--accent);
  }}
  .btn-accent:hover {{ background: var(--accent-hover); border-color: var(--accent-hover); }}
  .btn-danger {{
    color: var(--danger); border-color: var(--danger);
  }}
  .btn-danger:hover {{ background: var(--danger); color: #fff; }}
  .empty {{
    text-align: center; padding: 32px; color: var(--muted);
  }}
  #hint {{
    margin-top: 12px; padding: 8px 12px;
    background: var(--card); border: 1px solid var(--border);
    border-radius: 6px; font-size: 12px; color: var(--muted);
    display: none;
  }}
  #hint code {{
    background: var(--btn-bg); padding: 1px 5px;
    border-radius: 3px; font-family: monospace; color: var(--text);
  }}
</style>
</head>
<body>
<div class="header">
  <h2>Workspaces</h2>
  <button class="btn btn-accent" onclick="submitCmd('clone')">+ Clone</button>
</div>
<div class="ws-list" id="list"></div>
<div id="hint"></div>

<script>
const WS = {ws_json};

function he(s) {{
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function submitCmd(cmd) {{
  // Попытка через postMessage — работает в некоторых версиях Open WebUI
  const payload = {{ content: cmd }};
  ['input','command','submit','chat:input','chat:send'].forEach(type => {{
    try {{ parent.postMessage({{...payload, type}}, '*'); }} catch(e) {{}}
  }});
  // Гарантированный fallback: показываем команду пользователю
  const hint = document.getElementById('hint');
  hint.innerHTML = 'Введите в чат: <code>' + he(cmd) + '</code>';
  hint.style.display = 'block';
}}

function render() {{
  const list = document.getElementById('list');
  if (!WS.length) {{
    list.innerHTML = '<div class="empty">Нет воркспейсов.<br>Нажмите <b>+ Clone</b> чтобы создать.</div>';
    return;
  }}
  list.innerHTML = WS.map(ws => {{
    const isDefault = ws.name === 'default';
    const meta = [ws.git_remote, ws.git_branch].filter(Boolean).join(' · ');
    const deleteBtn = isDefault ? '' :
      `<button class="btn btn-danger" onclick="submitCmd('workspace delete ${{he(ws.name)}}')" title="Удалить">🗑 Delete</button>`;
    return `
    <div class="ws-card ${{ws.is_active ? 'active' : ''}}">
      <div class="ws-name">
        <span class="dot"></span>
        ${{he(ws.name)}}
        ${{ws.is_active ? '<span class="badge">active</span>' : ''}}
      </div>
      ${{meta ? `<div class="ws-meta">${{he(meta)}}</div>` : ''}}
      <div class="actions">
        ${{!ws.is_active ? `<button class="btn" onclick="submitCmd('workspace switch ${{he(ws.name)}}')">⇄ Switch</button>` : ''}}
        <button class="btn" onclick="submitCmd('files ${{he(ws.name)}}')">📄 Files</button>
        <button class="btn" onclick="submitCmd('download ${{he(ws.name)}}')">⬇ Download</button>
        ${{deleteBtn}}
      </div>
    </div>`;
  }}).join('');
}}

render();
</script>
</body>
</html>"""

        yield f"```html\n{html_page}\n```"

    # ─── clone ────────────────────────────────────────────────────────

    async def _cmd_clone(
        self,
        user_id: str,
        repo_url: str,
        call,
        emit,
    ) -> AsyncGenerator[str, None]:
        if not repo_url:
            if call:
                result = await call({
                    "type": "input",
                    "data": {
                        "title": "Clone Repository",
                        "message": "Enter the Git repository URL:",
                        "placeholder": "https://github.com/user/repo.git",
                    },
                })
                repo_url = ((result or {}).get("value") or "").strip()
            if not repo_url:
                yield "Укажите URL репозитория: `clone https://github.com/user/repo.git`"
                return

        await self._emit_status(emit, f"Клонирование {repo_url}…", done=False)

        try:
            async with httpx.AsyncClient(timeout=self.valves.BACKEND_TIMEOUT) as client:
                resp = await client.post(
                    f"{self.valves.BACKEND_URL}/api/workspaces/clone",
                    headers=self._headers(user_id),
                    json={"user_id": user_id, "repo_url": repo_url},
                )
                resp.raise_for_status()
                info = resp.json()
        except httpx.HTTPStatusError as exc:
            await self._emit_status(emit, "Ошибка", done=True)
            yield f"**Ошибка {exc.response.status_code}:** {exc.response.text}"
            return
        except Exception as exc:
            await self._emit_status(emit, "Ошибка", done=True)
            yield f"**Ошибка:** {exc}"
            return

        await self._emit_status(emit, "Готово", done=True)

        branch_note = f"\n**Ветка:** `{info['git_branch']}`" if info.get("git_branch") else ""
        yield (
            f"Репозиторий склонирован!\n\n"
            f"**Имя:** {info['name']}\n"
            f"**Путь:** {info['path']}"
            f"{branch_note}\n\n"
            f"Воркспейс активирован. Напишите `ws` чтобы открыть панель управления."
        )

    # ─── switch ───────────────────────────────────────────────────────

    async def _cmd_switch(
        self,
        user_id: str,
        ws_name: str,
        call,
        emit,
    ) -> AsyncGenerator[str, None]:
        if not ws_name:
            if call:
                result = await call({
                    "type": "input",
                    "data": {
                        "title": "Switch Workspace",
                        "message": "Enter workspace name:",
                        "placeholder": "my-project",
                    },
                })
                ws_name = ((result or {}).get("value") or "").strip()
            if not ws_name:
                yield "Укажите имя воркспейса: `workspace switch <name>`"
                return

        await self._emit_status(emit, f"Переключение на {ws_name}…", done=False)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.put(
                    f"{self.valves.BACKEND_URL}/api/workspaces/active",
                    headers=self._headers(user_id),
                    json={"user_id": user_id, "workspace": ws_name},
                )
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            await self._emit_status(emit, "Ошибка", done=True)
            detail = exc.response.json().get("detail", exc.response.text) if exc.response.content else exc.response.text
            yield f"**Ошибка {exc.response.status_code}:** {detail}"
            return
        except Exception as exc:
            await self._emit_status(emit, "Ошибка", done=True)
            yield f"**Ошибка:** {exc}"
            return

        await self._emit_status(emit, "Готово", done=True)
        yield f"Активный воркспейс переключён на **{ws_name}**."

    # ─── delete ───────────────────────────────────────────────────────

    async def _cmd_delete(
        self,
        user_id: str,
        ws_name: str,
        call,
        emit,
    ) -> AsyncGenerator[str, None]:
        if call:
            confirmed = await call({
                "type": "confirmation",
                "data": {
                    "title": "Delete Workspace",
                    "message": f"Delete workspace '{ws_name}'? This cannot be undone.",
                },
            })
            if not confirmed:
                yield f"Удаление **{ws_name}** отменено."
                return

        await self._emit_status(emit, f"Удаление {ws_name}…", done=False)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.delete(
                    f"{self.valves.BACKEND_URL}/api/workspaces/{user_id}/{ws_name}",
                    headers=self._headers(user_id),
                )
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            await self._emit_status(emit, "Ошибка", done=True)
            detail = exc.response.json().get("detail", exc.response.text) if exc.response.content else exc.response.text
            yield f"**Ошибка {exc.response.status_code}:** {detail}"
            return
        except Exception as exc:
            await self._emit_status(emit, "Ошибка", done=True)
            yield f"**Ошибка:** {exc}"
            return

        await self._emit_status(emit, "Готово", done=True)
        yield f"Воркспейс **{ws_name}** удалён."

    # ─── download active ──────────────────────────────────────────────

    async def _cmd_download_active(
        self,
        user_id: str,
        emit,
    ) -> AsyncGenerator[str, None]:
        await self._emit_status(emit, "Определение активного воркспейса…", done=False)
        workspaces = await self._get_workspaces(user_id)
        active = next((ws["name"] for ws in workspaces if ws.get("is_active")), None)
        if not active:
            await self._emit_status(emit, "Не найден", done=True)
            yield "Нет активного воркспейса. Используйте `download <name>`."
            return
        async for chunk in self._download(user_id, active, emit):
            yield chunk

    # ─── download <workspace> ─────────────────────────────────────────

    async def _download(
        self,
        user_id: str,
        workspace: str,
        emit,
    ) -> AsyncGenerator[str, None]:
        await self._emit_status(emit, f"Загрузка {workspace}.zip…", done=False)

        url = f"{self.valves.BACKEND_URL}/api/workspaces/{user_id}/{workspace}/download"
        max_bytes = self.valves.MAX_DOWNLOAD_MB * 1024 * 1024

        try:
            async with httpx.AsyncClient(timeout=self.valves.BACKEND_TIMEOUT) as client:
                resp = await client.get(url, headers=self._headers(user_id))
                resp.raise_for_status()
                data = resp.content
        except httpx.HTTPStatusError as exc:
            await self._emit_status(emit, "Ошибка", done=True)
            yield f"**Ошибка {exc.response.status_code}:** {exc.response.text}"
            return
        except Exception as exc:
            await self._emit_status(emit, "Ошибка", done=True)
            yield f"**Ошибка соединения:** {exc}"
            return

        if len(data) > max_bytes:
            await self._emit_status(emit, "Слишком большой файл", done=True)
            yield (
                f"Архив слишком большой ({len(data) // 1024 // 1024} МБ). "
                f"Максимум: {self.valves.MAX_DOWNLOAD_MB} МБ. "
                f"Увеличьте MAX_DOWNLOAD_MB в настройках функции."
            )
            return

        b64 = base64.b64encode(data).decode()
        filename = f"{workspace}.zip"
        size_str = (
            f"{len(data) / 1024:.1f} КБ"
            if len(data) < 1024 * 1024
            else f"{len(data) / 1024 / 1024:.1f} МБ"
        )

        await self._emit_status(emit, "Готово", done=True)

        html_page = f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8">
<style>
  :root {{
    --bg: #f8fafc; --card: #fff; --text: #1e293b; --muted: #64748b;
    --accent: #2563eb; --accent-hover: #1d4ed8; --shadow: 0 2px 12px rgba(0,0,0,.1);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f172a; --card: #1e293b; --text: #f1f5f9; --muted: #94a3b8;
      --accent: #3b82f6; --accent-hover: #60a5fa; --shadow: 0 2px 12px rgba(0,0,0,.4);
    }}
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg); display: flex; align-items: center;
    justify-content: center; height: 100vh;
  }}
  .card {{
    text-align: center; padding: 32px; background: var(--card);
    border-radius: 12px; box-shadow: var(--shadow); color: var(--text);
  }}
  h2 {{ margin: 0 0 6px; font-size: 18px; }}
  p  {{ margin: 0 0 20px; color: var(--muted); font-size: 14px; }}
  button {{
    padding: 10px 24px; background: var(--accent); color: #fff;
    border: none; border-radius: 8px; font-size: 15px;
    cursor: pointer; font-weight: 500; transition: background .15s;
  }}
  button:hover {{ background: var(--accent-hover); }}
  #status {{ margin-top: 10px; color: var(--muted); font-size: 13px; min-height: 18px; }}
</style>
</head>
<body>
<div class="card">
  <h2>📦 {html.escape(filename)}</h2>
  <p>{html.escape(size_str)}</p>
  <button onclick="download()">⬇ Скачать</button>
  <div id="status"></div>
</div>
<script>
function download() {{
  document.getElementById('status').textContent = 'Подготовка…';
  try {{
    const b64 = "{b64}";
    const bin = atob(b64);
    const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    const blob = new Blob([buf], {{ type: 'application/zip' }});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = '{html.escape(filename)}'; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    document.getElementById('status').textContent = 'Скачивание началось.';
  }} catch(e) {{
    document.getElementById('status').textContent = 'Ошибка: ' + e.message;
  }}
}}
</script>
</body>
</html>"""

        yield f"```html\n{html_page}\n```"

    # ─── files list ───────────────────────────────────────────────────

    async def _list_files(
        self,
        user_id: str,
        workspace: str,
        path: str,
        emit,
    ) -> AsyncGenerator[str, None]:
        await self._emit_status(emit, f"Получение списка файлов {workspace}…", done=False)

        params: dict[str, Any] = {"user_id": user_id}
        if path:
            params["path"] = path

        url = f"{self.valves.BACKEND_URL}/api/workspaces/{user_id}/{workspace}/files"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers=self._headers(user_id), params=params)
                resp.raise_for_status()
                result = resp.json()
        except httpx.HTTPStatusError as exc:
            await self._emit_status(emit, "Ошибка", done=True)
            yield f"**Ошибка {exc.response.status_code}:** {exc.response.text}"
            return
        except Exception as exc:
            await self._emit_status(emit, "Ошибка", done=True)
            yield f"**Ошибка:** {exc}"
            return

        await self._emit_status(emit, "Готово", done=True)

        files = result.get("files", [])
        if not files:
            yield f"Воркспейс **{workspace}** пуст."
            return

        lines = [f"**Файлы воркспейса `{workspace}`**\n"]
        for entry in files:
            depth = entry["path"].count("/")
            indent = "  " * depth
            if entry["is_dir"]:
                lines.append(f"{indent}📁 **{entry['name']}/**")
            else:
                size = entry.get("size", 0)
                size_str = (
                    f"{size / 1024:.1f} КБ" if size < 1024 * 1024
                    else f"{size / 1024 / 1024:.1f} МБ"
                )
                lines.append(f"{indent}📄 {entry['name']} *({size_str})*")

        yield "\n".join(lines)

    # ─── proxy chat ───────────────────────────────────────────────────

    async def _proxy_chat(
        self,
        body: dict,
        user_id: str,
        emit,
    ) -> AsyncGenerator[str, None]:
        """Проксирует сообщение к Claude Code бэкенду (streaming)."""
        url = f"{self.valves.BACKEND_URL}/v1/chat/completions"
        payload = {
            **body,
            "model": "claude-code",
            "user": user_id,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(timeout=self.valves.BACKEND_TIMEOUT) as client:
                async with client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=self._headers(user_id),
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield delta
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue

        except httpx.HTTPStatusError as exc:
            yield f"\n\n**Ошибка бэкенда {exc.response.status_code}:** {exc.response.text}"
        except Exception as exc:
            yield f"\n\n**Ошибка соединения с бэкендом:** {exc}"
