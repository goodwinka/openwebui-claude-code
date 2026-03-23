"""Мост между OpenAI-compatible API и Claude Agent SDK.

Все процессы работают от одного системного пользователя.
Изоляция — через cwd (рабочую директорию воркспейса).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path

from app.config import settings
from app.models import ChatCompletionResponse, ChatCompletionChoice, ChatMessage

logger = logging.getLogger(__name__)

# Максимальная длина вывода инструмента в чате (символов)
_MAX_TOOL_OUTPUT = 3000

# Patterns that indicate the Claude CLI is not authenticated
_AUTH_ERROR_PATTERNS = (
    "not logged in",
    "please run /login",
    "authentication required",
    "not authenticated",
)

_AUTH_ERROR_HELP = (
    "**Authentication Error**: The Claude CLI is not authenticated.\n\n"
    "**For a local LLM (recommended):** set `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` in your `.env`:\n\n"
    "```\n"
    "ANTHROPIC_API_KEY=local\n"
    "ANTHROPIC_BASE_URL=http://localhost:11434\n"
    "```\n\n"
    "**For the Anthropic cloud API:** authenticate the service user:\n\n"
    "```bash\n"
    "sudo -u claude-code claude login\n"
    "```"
)


def _is_auth_error(text: str) -> bool:
    """Returns True if the text indicates a Claude CLI authentication error."""
    lower = text.lower()
    return any(pattern in lower for pattern in _AUTH_ERROR_PATTERNS)


def _build_prompt(messages: list[ChatMessage]) -> tuple[str, str | None]:
    """Собирает промпт из списка сообщений.

    Возвращает (prompt, system_prompt).
    """
    system_prompt = None
    parts: list[str] = []

    for msg in messages:
        if msg.role == "system" and system_prompt is None:
            system_prompt = msg.content
        elif msg.role == "user":
            parts.append(msg.content)
        elif msg.role == "assistant":
            parts.append(f"[Previous assistant response]: {msg.content}")

    prompt = parts[-1] if parts else "Hello"
    return prompt, system_prompt


def detect_workspace_command(prompt: str) -> tuple[str, str | None] | None:
    """Определяет, содержит ли промпт команду управления воркспейсом.

    Возвращает (command, arg) или None.
    """
    lower = prompt.strip().lower()

    clone_match = re.match(r"^clone\s+(https?://\S+)$", prompt.strip(), re.IGNORECASE)
    if clone_match:
        return ("clone", clone_match.group(1))

    if lower == "workspace list":
        return ("list", None)

    switch_match = re.match(
        r"^workspace\s+switch\s+(\S+)$", prompt.strip(), re.IGNORECASE
    )
    if switch_match:
        return ("switch", switch_match.group(1))

    if lower in ("help", "/help", "помощь", "команды"):
        return ("help", None)

    if re.match(r"^(git\s+)?push$", lower):
        return ("git_push", None)

    pr_match = re.match(
        r"^(?:create\s+pr|pull\s+request)\s*(.*)$", prompt.strip(), re.IGNORECASE
    )
    if pr_match:
        title = pr_match.group(1).strip() or "Claude Code changes"
        return ("create_pr", title)

    if re.match(r"^(git\s+)?status$", lower):
        return ("git_status", None)

    return None


async def stream_claude_response(
    messages: list[ChatMessage],
    workspace_dir: Path,
) -> AsyncIterator[str]:
    """Выполняет промпт через Claude Agent SDK и стримит SSE-чанки."""
    prompt, system_prompt = _build_prompt(messages)
    request_id = f"chatcmpl-{int(time.time() * 1000)}"
    created = int(time.time())

    auth_error_reported = False

    try:
        async for chunk_text in _stream_via_sdk(prompt, system_prompt, workspace_dir):
            if _is_auth_error(chunk_text):
                auth_error_reported = True
                yield _format_sse_chunk(request_id, created, _AUTH_ERROR_HELP)
                break
            yield _format_sse_chunk(request_id, created, chunk_text)
    except ImportError:
        logger.info("claude_agent_sdk not available, falling back to CLI")
        async for chunk_text in _stream_via_cli(prompt, system_prompt, workspace_dir):
            if _is_auth_error(chunk_text):
                auth_error_reported = True
                yield _format_sse_chunk(request_id, created, _AUTH_ERROR_HELP)
                break
            yield _format_sse_chunk(request_id, created, chunk_text)
    except Exception as exc:
        if not auth_error_reported:
            logger.error("claude_agent_sdk error: %s", exc)
            exc_str = str(exc)
            if _is_auth_error(exc_str) or "exit code 1" in exc_str:
                yield _format_sse_chunk(request_id, created, _AUTH_ERROR_HELP)
            else:
                error_text = f"**Error**: Claude Agent SDK failed: {exc}"
                yield _format_sse_chunk(request_id, created, error_text)
        else:
            # Auth error was already reported; just log the follow-up exception
            logger.debug("Suppressed follow-up exception after auth error: %s", exc)

    # Финальный чанк stop
    stop_chunk = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "claude-code",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(stop_chunk)}\n\n"
    yield "data: [DONE]\n\n"


async def run_claude_sync(
    messages: list[ChatMessage],
    workspace_dir: Path,
) -> ChatCompletionResponse:
    """Non-streaming: собирает полный ответ."""
    prompt, system_prompt = _build_prompt(messages)
    full_text = ""

    auth_error_reported = False

    try:
        async for chunk in _stream_via_sdk(prompt, system_prompt, workspace_dir):
            if _is_auth_error(chunk):
                auth_error_reported = True
                full_text = _AUTH_ERROR_HELP
                break
            full_text += chunk
    except ImportError:
        async for chunk in _stream_via_cli(prompt, system_prompt, workspace_dir):
            if _is_auth_error(chunk):
                auth_error_reported = True
                full_text = _AUTH_ERROR_HELP
                break
            full_text += chunk
    except Exception as exc:
        if not auth_error_reported:
            logger.error("claude_agent_sdk error: %s", exc)
            exc_str = str(exc)
            if _is_auth_error(exc_str) or "exit code 1" in exc_str:
                full_text = _AUTH_ERROR_HELP
            else:
                full_text = f"**Error**: Claude Agent SDK failed: {exc}"
        else:
            logger.debug("Suppressed follow-up exception after auth error: %s", exc)

    return ChatCompletionResponse(
        model="claude-code",
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=full_text.strip())
            )
        ],
    )


# ─── Tool display helpers ─────────────────────────────────────────────


def _format_tool_use(tool_name: str, tool_input: dict) -> str:
    """Форматирует вызов инструмента для отображения в чате."""
    lo = tool_name.lower()
    if lo == "bash":
        cmd = tool_input.get("command", "")
        return f"\n```bash\n$ {cmd}\n```\n"
    elif lo == "read":
        path = tool_input.get("file_path", tool_input.get("path", ""))
        return f"\n*Читаю: `{path}`*\n"
    elif lo == "write":
        path = tool_input.get("file_path", "")
        return f"\n*Создаю: `{path}`*\n"
    elif lo == "edit":
        path = tool_input.get("file_path", "")
        return f"\n*Редактирую: `{path}`*\n"
    elif lo == "glob":
        pattern = tool_input.get("pattern", "")
        return f"\n*Glob: `{pattern}`*\n"
    elif lo == "grep":
        pattern = tool_input.get("pattern", "")
        return f"\n*Grep: `{pattern}`*\n"
    return f"\n*{tool_name}*\n"


def _format_tool_result(content: str | list) -> str:
    """Форматирует результат выполнения инструмента для отображения."""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        content = "".join(parts)

    if not content or not content.strip():
        return ""

    truncated = ""
    if len(content) > _MAX_TOOL_OUTPUT:
        content = content[:_MAX_TOOL_OUTPUT]
        truncated = "\n*... (вывод обрезан)*"

    return f"\n```\n{content.rstrip()}\n```{truncated}\n"


# ─── SDK-based streaming ─────────────────────────────────────────────


async def _stream_via_sdk(
    prompt: str,
    system_prompt: str | None,
    workspace_dir: Path,
) -> AsyncIterator[str]:
    """Стриминг через Python Agent SDK (claude_agent_sdk)."""
    from claude_agent_sdk import query, ClaudeAgentOptions

    # Prefer the system claude CLI over the SDK's bundled binary,
    # since the system CLI carries the user's auth session/credentials.
    system_cli = settings.claude_cli_path or shutil.which("claude")

    options_kwargs: dict = {
        "allowed_tools": settings.allowed_tools_list,
        "permission_mode": settings.permission_mode,
        "cwd": str(workspace_dir),
        "model": settings.claude_model,
        "max_turns": settings.max_turns,
    }
    if system_cli:
        options_kwargs["cli_path"] = system_cli
        logger.debug("Using system claude CLI: %s", system_cli)

    if system_prompt:
        options_kwargs["system_prompt"] = system_prompt

    options = ClaudeAgentOptions(**options_kwargs)

    # Отслеживаем, был ли показан текст из AssistantMessage.
    # Если да — ResultMessage не показываем (он дублирует финальный ответ).
    text_yielded = False

    async for message in query(prompt=prompt, options=options):
        # ResultMessage: есть result-строка, нет непустого content.
        # Такая комбинация надёжнее, чем проверка атрибута type.
        has_result = hasattr(message, "result") and isinstance(message.result, str)
        has_content = hasattr(message, "content") and bool(message.content)
        is_result_msg = has_result and not has_content

        # ResultMessage показываем только если AssistantMessage не дал текста
        if is_result_msg and text_yielded:
            continue

        text = _extract_text_from_message(message)
        if text:
            if not is_result_msg:
                text_yielded = True
            yield text


def _extract_text_from_message(message) -> str:
    """Извлекает текст из сообщения Agent SDK, включая вызовы инструментов и их вывод."""
    # content как строка (простой AssistantMessage)
    if hasattr(message, "content") and isinstance(message.content, str):
        return message.content

    # content как список блоков (TextBlock, ToolUseBlock, ToolResultBlock)
    if hasattr(message, "content") and isinstance(message.content, list):
        parts = []
        for block in message.content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    parts.append(_format_tool_use(
                        block.get("name", ""), block.get("input", {}),
                    ))
                elif btype == "tool_result":
                    rc = block.get("content", "")
                    if rc:
                        parts.append(_format_tool_result(rc))
            else:
                # SDK-объекты: проверяем атрибут type первым
                btype = getattr(block, "type", None)
                if btype == "text":
                    parts.append(getattr(block, "text", ""))
                elif btype == "tool_use":
                    parts.append(_format_tool_use(
                        getattr(block, "name", ""), getattr(block, "input", {}),
                    ))
                elif btype == "tool_result":
                    rc = getattr(block, "content", "")
                    if rc:
                        parts.append(_format_tool_result(rc))
                elif btype is None:
                    # Fallback для SDK-объектов без явного атрибута type
                    if hasattr(block, "text"):
                        parts.append(block.text)

        combined = "".join(parts)
        # Не делаем ранний return при пустом списке — проваливаемся к result-проверке
        if combined:
            return combined

    # ResultMessage (или сообщение с пустым content, но непустым result)
    if hasattr(message, "result") and isinstance(message.result, str):
        return message.result

    return ""


# ─── CLI-based streaming (fallback) ──────────────────────────────────


async def _stream_via_cli(
    prompt: str,
    system_prompt: str | None,
    workspace_dir: Path,
) -> AsyncIterator[str]:
    """Стриминг через CLI (`claude -p`). Fallback если SDK не установлен."""
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json"]

    if system_prompt:
        cmd += ["--system-prompt", system_prompt]

    allowed = settings.allowed_tools
    if allowed:
        cmd += ["--allowedTools", allowed]

    cmd += ["--model", settings.claude_model]

    env = os.environ.copy()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(workspace_dir),
        env=env,
    )

    assert proc.stdout is not None
    buffer = ""
    text_yielded = False  # отслеживаем, был ли показан текст из assistant-событий

    async for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace")
        buffer += line

        while "\n" in buffer:
            json_line, buffer = buffer.split("\n", 1)
            json_line = json_line.strip()
            if not json_line:
                continue

            try:
                event = json.loads(json_line)
            except json.JSONDecodeError:
                yield json_line
                continue

            text = ""
            if isinstance(event, dict):
                event_type = event.get("type")

                if event_type == "assistant":
                    # Сообщение ассистента: текст + вызовы инструментов
                    content = event.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text += block.get("text", "")
                            elif block.get("type") == "tool_use":
                                text += _format_tool_use(
                                    block.get("name", ""),
                                    block.get("input", {}),
                                )

                elif event_type == "user":
                    # Результаты инструментов (tool_result)
                    content = event.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            result_content = block.get("content", "")
                            if result_content:
                                text += _format_tool_result(result_content)

                elif event_type == "result":
                    # Финальный результат — показываем только если assistant не дал текста
                    if not text_yielded:
                        result_text = event.get("result", "")
                        if isinstance(result_text, str) and result_text:
                            text = result_text

                # Обратная совместимость со старыми/нестандартными форматами
                elif "content" in event and isinstance(event["content"], str):
                    text = event["content"]

            if text:
                if event.get("type") not in ("result",):
                    text_yielded = True
                yield text

    await proc.wait()

    if proc.returncode != 0:
        assert proc.stderr is not None
        stderr = await proc.stderr.read()
        logger.error("claude CLI exited with %d: %s", proc.returncode, stderr.decode())


# ─── Helpers ──────────────────────────────────────────────────────────


def _format_sse_chunk(request_id: str, created: int, text: str) -> str:
    """Форматирует текст в SSE-чанк совместимый с OpenAI."""
    chunk = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "claude-code",
        "choices": [
            {
                "index": 0,
                "delta": {"content": text},
                "finish_reason": None,
            }
        ],
    }
    return f"data: {json.dumps(chunk)}\n\n"
