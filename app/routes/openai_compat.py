"""OpenAI-совместимые эндпоинты для Open WebUI.

/v1/models         — список доступных моделей
/v1/chat/completions — основной эндпоинт (streaming и non-streaming)

Идентификация пользователя (в порядке приоритета):
  1. HTTP-заголовок X-OpenWebUI-User-Name  (ENABLE_FORWARD_USER_INFO_HEADERS=true)
  2. HTTP-заголовок X-OpenWebUI-User-Id    (UUID, fallback)
  3. body → metadata → user_id             (если Pipe Function пробрасывает)
  4. body → user                           (стандартное поле OpenAI)
  5. "default"
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.claude_bridge import detect_workspace_command, run_claude_sync, stream_claude_response
from app.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionChoice,
    ChatMessage,
    ModelInfo,
    ModelListResponse,
)
from app.workspace_manager import workspace_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["OpenAI Compatible"])

WELCOME_MESSAGE = """\
Добро пожаловать в **Claude Code**!

Я AI-ассистент для работы с кодом. Умею читать, редактировать и создавать файлы, \
выполнять команды и работать с git прямо в вашем воркспейсе.

## Команды воркспейса

| Команда | Описание |
|---------|----------|
| `clone <url>` | Клонировать git-репозиторий |
| `workspace list` | Показать список ваших воркспейсов |
| `workspace switch <имя>` | Переключить активный воркспейс |
| `help` | Показать эту справку |

## Git-команды

| Команда | Описание |
|---------|----------|
| `push` | Запушить текущую ветку в origin |
| `create pr <заголовок>` | Создать Pull Request (GitHub) или Merge Request (GitLab) |
| `git status` | Показать текущую ветку |

> **GitLab** (в т.ч. локальная сеть): требуется `GITLAB_TOKEN` в `.env`
> **GitHub**: работает через `gh` CLI или `GITHUB_TOKEN` в `.env`

## Начало работы

- Клонируйте репозиторий: `clone https://github.com/user/repo`
- После клонирования автоматически создаётся ветка `claude/session-*`
- Когда готово: `push` → `create pr Мои изменения`
- Или просто опишите задачу — я работаю в вашем рабочем каталоге.

Чем могу помочь?"""

# ─── Helpers ──────────────────────────────────────────────────────

_SAFE_NAME_RE = re.compile(r"[^\w\-.]")


def _sanitize_user_id(raw: str) -> str:
    """Приводит любой идентификатор к безопасному имени директории."""
    name = _SAFE_NAME_RE.sub("_", raw)
    return name.strip("_.")[:64] or "default"


def _extract_user_id(request: Request, body: ChatCompletionRequest) -> str:
    """Извлекает user_id из всех возможных источников.

    Open WebUI по умолчанию НЕ передаёт user в payload при проксировании
    к внешним OpenAI-совместимым бэкендам. Metadata (user_id, chat_id)
    удаляется из payload перед отправкой.

    Способы получить user_id:
      1. ENABLE_FORWARD_USER_INFO_HEADERS=true в Open WebUI →
         заголовки X-OpenWebUI-User-Name / X-OpenWebUI-User-Id
      2. Кастомная Pipe Function, пробрасывающая metadata.user_id
      3. Поле body.user (стандарт OpenAI, не заполняется Open WebUI)
    """
    # 1. Заголовки от Open WebUI (ENABLE_FORWARD_USER_INFO_HEADERS)
    header_name = request.headers.get("x-openwebui-user-name")
    if header_name:
        return _sanitize_user_id(header_name)

    header_id = request.headers.get("x-openwebui-user-id")
    if header_id:
        return _sanitize_user_id(header_id)

    # 2. metadata.user_id (если Pipe Function или будущие версии OWU)
    if body.metadata and isinstance(body.metadata, dict):
        meta_user = body.metadata.get("user_id")
        if meta_user:
            return _sanitize_user_id(str(meta_user))

    # 3. Стандартное поле OpenAI
    if body.user:
        return _sanitize_user_id(body.user)

    return "default"


# ─── Routes ───────────────────────────────────────────────────────


@router.get("/models")
async def list_models() -> ModelListResponse:
    return ModelListResponse(
        data=[
            ModelInfo(id="claude-code", owned_by="anthropic"),
            ModelInfo(id="claude-code-opus", owned_by="anthropic"),
            ModelInfo(id="claude-code-haiku", owned_by="anthropic"),
        ]
    )


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
):
    """Основной эндпоинт.

    Определяет пользователя из заголовков / metadata / body.user.
    """
    user_id = _extract_user_id(request, body)

    # Команды управления воркспейсом / help / приветствие новой сессии
    if body.messages:
        last_msg = body.messages[-1]
        if last_msg.role == "user":
            ws_cmd = detect_workspace_command(last_msg.content)
            if ws_cmd:
                if ws_cmd[0] == "help":
                    return _help_response()
                return await _handle_workspace_command(user_id, ws_cmd)

            # Первое сообщение новой сессии — показываем приветствие
            if _is_new_session(body.messages):
                return _help_response()

    workspace_dir = await workspace_manager.ensure_workspace(user_id)

    logger.info(
        "Chat request from user=%s, workspace=%s, stream=%s",
        user_id, workspace_dir, body.stream,
    )

    if body.stream:
        return StreamingResponse(
            stream_claude_response(
                messages=body.messages,
                workspace_dir=workspace_dir,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        return await run_claude_sync(
            messages=body.messages,
            workspace_dir=workspace_dir,
        )


def _is_new_session(messages: list[ChatMessage]) -> bool:
    """Возвращает True, если в истории нет ни одного ответа ассистента."""
    return not any(m.role == "assistant" for m in messages)


def _help_response() -> ChatCompletionResponse:
    """Возвращает приветственное/справочное сообщение."""
    return ChatCompletionResponse(
        model="claude-code",
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=WELCOME_MESSAGE)
            )
        ],
    )


async def _handle_workspace_command(
    user_id: str,
    cmd: tuple[str, str | None],
) -> ChatCompletionResponse:
    """Обрабатывает команды управления воркспейсом из чата."""
    command, arg = cmd

    try:
        if command == "clone" and arg:
            info = await workspace_manager.clone_repo(user_id=user_id, repo_url=arg)
            branch_note = f"\n**Ветка:** `{info.git_branch}`" if info.git_branch else ""
            text = (
                f"Репозиторий склонирован!\n\n"
                f"**Имя:** {info.name}\n"
                f"**Путь:** {info.path}"
                f"{branch_note}\n\n"
                f"Воркспейс активирован. Все команды Claude Code теперь работают здесь.\n"
                f"Когда будете готовы: `push` — запушить ветку, `create pr <заголовок>` — создать PR."
            )

        elif command == "list":
            workspaces = await workspace_manager.list_workspaces(user_id)
            if not workspaces:
                text = "У вас пока нет воркспейсов."
            else:
                lines = ["**Ваши воркспейсы:**\n"]
                for ws in workspaces:
                    marker = "→ " if ws.is_active else "  "
                    remote = f" ({ws.git_remote})" if ws.git_remote else ""
                    lines.append(f"{marker}**{ws.name}**{remote}")
                text = "\n".join(lines)

        elif command == "switch" and arg:
            await workspace_manager.switch_workspace(user_id, arg)
            text = f"Активный воркспейс переключён на **{arg}**"

        elif command == "git_push":
            workspace_dir = await workspace_manager.ensure_workspace(user_id)
            branch = await workspace_manager.git_push(workspace_dir)
            text = (
                f"Ветка **{branch}** запушена в origin.\n\n"
                f"Введите `create pr <заголовок>` чтобы открыть pull request."
            )

        elif command == "create_pr" and arg:
            workspace_dir = await workspace_manager.ensure_workspace(user_id)
            pr_url = await workspace_manager.create_pr(workspace_dir, arg)
            text = f"Pull request создан!\n\n{pr_url}"

        elif command == "git_status":
            workspace_dir = await workspace_manager.ensure_workspace(user_id)
            branch = await workspace_manager.get_current_branch(workspace_dir)
            text = f"Текущая ветка: **{branch or 'неизвестно'}**"

        else:
            text = "Неизвестная команда воркспейса."

    except (ValueError, RuntimeError) as exc:
        text = f"Ошибка: {exc}"

    return ChatCompletionResponse(
        model="claude-code",
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=text)
            )
        ],
    )
