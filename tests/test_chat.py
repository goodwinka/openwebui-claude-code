"""Тесты эндпоинта /v1/chat/completions."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ─── Non-streaming ────────────────────────────────────────────────────


def _mock_run_claude_sync(text: str = "Привет! Чем могу помочь?"):
    """Создаёт mock для run_claude_sync."""
    from app.models import ChatCompletionChoice, ChatCompletionResponse, ChatMessage

    async def _fake(*args, **kwargs):
        return ChatCompletionResponse(
            model="claude-code",
            choices=[
                ChatCompletionChoice(
                    message=ChatMessage(role="assistant", content=text)
                )
            ],
        )

    return _fake


def test_chat_non_streaming(client, auth_headers, tmp_path):
    with (
        patch(
            "app.routes.openai_compat.run_claude_sync",
            side_effect=_mock_run_claude_sync(),
        ),
        patch(
            "app.workspace_manager.workspace_manager.ensure_workspace",
            new_callable=AsyncMock,
            return_value=tmp_path,
        ),
    ):
        resp = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "messages": [
                    {"role": "assistant", "content": "Предыдущий ответ"},
                    {"role": "user", "content": "Привет"},
                ],
                "stream": False,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert "Привет" in data["choices"][0]["message"]["content"]


def test_chat_response_structure(client, auth_headers, tmp_path):
    with (
        patch(
            "app.routes.openai_compat.run_claude_sync",
            side_effect=_mock_run_claude_sync("OK"),
        ),
        patch(
            "app.workspace_manager.workspace_manager.ensure_workspace",
            new_callable=AsyncMock,
            return_value=tmp_path,
        ),
    ):
        resp = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "messages": [{"role": "user", "content": "test"}],
                "stream": False,
            },
        )

    data = resp.json()
    assert "id" in data
    assert "created" in data
    assert "model" in data
    assert "choices" in data
    assert "usage" in data


# ─── Streaming ────────────────────────────────────────────────────────


async def _fake_stream(*args, **kwargs):
    chunks = ["Привет", "! Как", " дела?"]
    import json as _json
    import time

    req_id = "chatcmpl-test"
    created = int(time.time())
    for text in chunks:
        chunk = {
            "id": req_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": "claude-code",
            "choices": [
                {"index": 0, "delta": {"content": text}, "finish_reason": None}
            ],
        }
        yield f"data: {_json.dumps(chunk)}\n\n"
    stop = {
        "id": req_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "claude-code",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {_json.dumps(stop)}\n\n"
    yield "data: [DONE]\n\n"


def test_chat_streaming(client, auth_headers, tmp_path):
    with (
        patch(
            "app.routes.openai_compat.stream_claude_response",
            return_value=_fake_stream(),
        ),
        patch(
            "app.workspace_manager.workspace_manager.ensure_workspace",
            new_callable=AsyncMock,
            return_value=tmp_path,
        ),
    ):
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "messages": [
                    {"role": "assistant", "content": "Предыдущий ответ"},
                    {"role": "user", "content": "Привет"},
                ],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            content_type = resp.headers.get("content-type", "")
            assert "text/event-stream" in content_type

            raw = resp.read().decode()

    assert "data:" in raw
    assert "[DONE]" in raw


# ─── User ID extraction ───────────────────────────────────────────────


_HISTORY = [
    {"role": "assistant", "content": "Предыдущий ответ"},
    {"role": "user", "content": "hi"},
]


def test_user_id_from_header(client, auth_headers, tmp_path):
    """X-OpenWebUI-User-Name используется как user_id."""
    headers = {**auth_headers, "X-OpenWebUI-User-Name": "alice"}
    with (
        patch(
            "app.routes.openai_compat.run_claude_sync",
            side_effect=_mock_run_claude_sync("ok"),
        ),
        patch(
            "app.workspace_manager.workspace_manager.ensure_workspace",
            new_callable=AsyncMock,
            return_value=tmp_path,
        ) as mock_ws,
    ):
        client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"messages": _HISTORY, "stream": False},
        )

    mock_ws.assert_awaited_once_with("alice")


def test_user_id_from_body(client, auth_headers, tmp_path):
    """body.user используется как fallback user_id."""
    with (
        patch(
            "app.routes.openai_compat.run_claude_sync",
            side_effect=_mock_run_claude_sync("ok"),
        ),
        patch(
            "app.workspace_manager.workspace_manager.ensure_workspace",
            new_callable=AsyncMock,
            return_value=tmp_path,
        ) as mock_ws,
    ):
        client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"messages": _HISTORY, "stream": False, "user": "bob"},
        )

    mock_ws.assert_awaited_once_with("bob")


def test_user_id_default(client, auth_headers, tmp_path):
    """Без user_id используется 'default'."""
    with (
        patch(
            "app.routes.openai_compat.run_claude_sync",
            side_effect=_mock_run_claude_sync("ok"),
        ),
        patch(
            "app.workspace_manager.workspace_manager.ensure_workspace",
            new_callable=AsyncMock,
            return_value=tmp_path,
        ) as mock_ws,
    ):
        client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"messages": _HISTORY, "stream": False},
        )

    mock_ws.assert_awaited_once_with("default")


# ─── Workspace commands in chat ───────────────────────────────────────


def test_workspace_list_command_in_chat(client, auth_headers):
    """Команда 'workspace list' обрабатывается без вызова Claude."""
    with patch(
        "app.workspace_manager.workspace_manager.list_workspaces",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "messages": [{"role": "user", "content": "workspace list"}],
                "stream": False,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    assert "воркспейс" in content.lower() or "workspace" in content.lower()
