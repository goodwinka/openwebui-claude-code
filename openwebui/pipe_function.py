"""Open WebUI Pipe Function для Claude Code.

Устанавливается в Open WebUI: Admin → Functions → Add Function.

Что делает:
- Проксирует весь чат к внутреннему Claude Code бэкенду
  (браузер → Open WebUI → внутренняя сеть → бэкенд)
- Добавляет команду `download <workspace>` — скачивает ZIP-архив
  через Open WebUI, без прямого доступа браузера к бэкенду
- Добавляет команду `files <workspace>` — показывает список файлов

Настройка через Valves (Admin → Functions → ⚙):
  BACKEND_URL           — внутренний URL бэкенда (например http://claude-code:8000)
  API_KEY               — значение api_secret_key из .env бэкенда
  MAX_DOWNLOAD_MB       — максимальный размер ZIP для скачивания (МБ)
  BACKEND_TIMEOUT       — таймаут запросов к бэкенду (секунд)
"""

from __future__ import annotations

import base64
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
        return [{"id": "claude-code", "name": "Claude Code"}]

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> AsyncGenerator[str, None]:
        messages = body.get("messages", [])
        if not messages:
            yield "Нет сообщений."
            return

        last_msg = messages[-1]
        content = (last_msg.get("content") or "").strip()
        user_id = self._user_id(__user__)

        # download <workspace>
        m = re.match(r"^(?:download|скачать)\s+(\S+)$", content, re.IGNORECASE)
        if m:
            async for chunk in self._download(user_id, m.group(1), __event_emitter__):
                yield chunk
            return

        # files <workspace> [path]
        m = re.match(r"^(?:files|файлы)\s+(\S+)(?:\s+(.+))?$", content, re.IGNORECASE)
        if m:
            async for chunk in self._list_files(
                user_id, m.group(1), m.group(2) or "", __event_emitter__
            ):
                yield chunk
            return

        # Обычный чат → проксируем к бэкенду
        async for chunk in self._proxy_chat(body, user_id, __event_emitter__):
            yield chunk

    # ─── helpers ──────────────────────────────────────────────────────

    def _user_id(self, user: Optional[dict]) -> str:
        if not user:
            return "default"
        return user.get("name") or user.get("id") or "default"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.valves.API_KEY}"}

    # ─── download ─────────────────────────────────────────────────────

    async def _download(
        self,
        user_id: str,
        workspace: str,
        emit,
    ) -> AsyncGenerator[str, None]:
        """Скачивает ZIP воркспейса и возвращает HTML-артефакт с кнопкой."""
        await self._emit_status(emit, f"Загрузка {workspace}.zip…", done=False)

        url = (
            f"{self.valves.BACKEND_URL}"
            f"/api/workspaces/{user_id}/{workspace}/download"
        )
        max_bytes = self.valves.MAX_DOWNLOAD_MB * 1024 * 1024

        try:
            async with httpx.AsyncClient(timeout=self.valves.BACKEND_TIMEOUT) as client:
                resp = await client.get(url, headers=self._headers())
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
                f"Используйте прямой доступ к бэкенду или увеличьте MAX_DOWNLOAD_MB."
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

        # HTML-артефакт: JavaScript создаёт Blob и триггерит скачивание.
        # Работает даже при CSP-ограничениях на data: URI.
        html = f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8">
<style>
  body {{ font-family: sans-serif; display: flex; align-items: center;
          justify-content: center; height: 100vh; margin: 0;
          background: #f8fafc; }}
  .card {{ text-align: center; padding: 32px; background: white;
           border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,.1); }}
  h2 {{ margin: 0 0 8px; color: #1e293b; }}
  p  {{ margin: 0 0 20px; color: #64748b; font-size: 14px; }}
  button {{
    padding: 10px 24px; background: #2563eb; color: white;
    border: none; border-radius: 8px; font-size: 16px;
    cursor: pointer; font-weight: 500;
  }}
  button:hover {{ background: #1d4ed8; }}
  #status {{ margin-top: 12px; color: #64748b; font-size: 13px; }}
</style>
</head>
<body>
<div class="card">
  <h2>📦 {filename}</h2>
  <p>{size_str}</p>
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
    a.href = url; a.download = '{filename}'; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    document.getElementById('status').textContent = 'Скачивание началось.';
  }} catch(e) {{
    document.getElementById('status').textContent = 'Ошибка: ' + e.message;
  }}
}}
</script>
</body>
</html>"""

        yield f"```html\n{html}\n```"

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
                resp = await client.get(url, headers=self._headers(), params=params)
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
            indent = "  " * entry["path"].count("/")
            if entry["is_dir"]:
                lines.append(f"{indent}📁 **{entry['name']}/**")
            else:
                size = entry.get("size", 0)
                size_str = f"{size / 1024:.1f} КБ" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} МБ"
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
                    headers=self._headers(),
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

    # ─── utils ────────────────────────────────────────────────────────

    @staticmethod
    async def _emit_status(emit, description: str, *, done: bool) -> None:
        if emit:
            await emit({"type": "status", "data": {"description": description, "done": done}})
