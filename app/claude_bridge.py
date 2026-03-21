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
import time
from collections.abc import AsyncIterator
from pathlib import Path

from app.config import settings
from app.models import ChatCompletionResponse, ChatCompletionChoice, ChatMessage

logger = logging.getLogger(__name__)


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

    try:
        async for chunk_text in _stream_via_sdk(prompt, system_prompt, workspace_dir):
            yield _format_sse_chunk(request_id, created, chunk_text)
    except ImportError:
        logger.info("claude_agent_sdk not available, falling back to CLI")
        async for chunk_text in _stream_via_cli(prompt, system_prompt, workspace_dir):
            yield _format_sse_chunk(request_id, created, chunk_text)

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

    try:
        async for chunk in _stream_via_sdk(prompt, system_prompt, workspace_dir):
            full_text += chunk
    except ImportError:
        async for chunk in _stream_via_cli(prompt, system_prompt, workspace_dir):
            full_text += chunk

    return ChatCompletionResponse(
        model="claude-code",
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=full_text.strip())
            )
        ],
    )


# ─── SDK-based streaming ─────────────────────────────────────────────


async def _stream_via_sdk(
    prompt: str,
    system_prompt: str | None,
    workspace_dir: Path,
) -> AsyncIterator[str]:
    """Стриминг через Python Agent SDK (claude_agent_sdk)."""
    from claude_agent_sdk import query, ClaudeAgentOptions

    options_kwargs: dict = {
        "allowed_tools": settings.allowed_tools_list,
        "permission_mode": settings.permission_mode,
        "cwd": str(workspace_dir),
        "model": settings.claude_model,
        "max_turns": settings.max_turns,
    }

    if system_prompt:
        options_kwargs["system_prompt"] = system_prompt

    options = ClaudeAgentOptions(**options_kwargs)

    async for message in query(prompt=prompt, options=options):
        text = _extract_text_from_message(message)
        if text:
            yield text


def _extract_text_from_message(message) -> str:
    """Извлекает текст из сообщения Agent SDK."""
    # AssistantMessage — текст от Claude
    if hasattr(message, "content") and isinstance(message.content, str):
        return message.content

    # content — список блоков (TextBlock и др.)
    if hasattr(message, "content") and isinstance(message.content, list):
        parts = []
        for block in message.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)

    # ResultMessage
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
    env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(workspace_dir),
        env=env,
    )

    assert proc.stdout is not None
    buffer = ""

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
                if "content" in event and isinstance(event["content"], str):
                    text = event["content"]
                elif "result" in event:
                    text = str(event["result"])
                elif event.get("type") == "assistant":
                    content = event.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text += block.get("text", "")

            if text:
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
