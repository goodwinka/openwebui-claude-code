"""OpenAI-совместимые эндпоинты для Open WebUI.

/v1/models         — список доступных моделей
/v1/chat/completions — основной эндпоинт (streaming и non-streaming)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.auth import verify_api_key
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


@router.get("/models")
async def list_models(
    _: str = Depends(verify_api_key),
) -> ModelListResponse:
    return ModelListResponse(
        data=[
            ModelInfo(id="claude-code", owned_by="anthropic"),
            ModelInfo(id="claude-code-opus", owned_by="anthropic"),
            ModelInfo(id="claude-code-haiku", owned_by="anthropic"),
        ]
    )


@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    _: str = Depends(verify_api_key),
):
    """Основной эндпоинт.

    Open WebUI передаёт `user` в теле запроса — определяет воркспейс.
    """
    user_id = request.user or "default"
    user_id = user_id.replace("@", "_").replace(" ", "_")

    # Команды управления воркспейсом прямо из чата
    if request.messages:
        last_msg = request.messages[-1]
        if last_msg.role == "user":
            ws_cmd = detect_workspace_command(last_msg.content)
            if ws_cmd:
                return await _handle_workspace_command(user_id, ws_cmd)

    workspace_dir = await workspace_manager.ensure_workspace(user_id)

    logger.info(
        "Chat request from user=%s, workspace=%s, stream=%s",
        user_id, workspace_dir, request.stream,
    )

    if request.stream:
        return StreamingResponse(
            stream_claude_response(
                messages=request.messages,
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
            messages=request.messages,
            workspace_dir=workspace_dir,
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
            text = (
                f"Репозиторий склонирован!\n\n"
                f"**Имя:** {info.name}\n"
                f"**Путь:** {info.path}\n\n"
                f"Воркспейс активирован. Все команды Claude Code теперь работают здесь."
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
