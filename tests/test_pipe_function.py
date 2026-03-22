"""Тесты для openwebui/pipe_function.py."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Добавляем openwebui/ в путь, чтобы импортировать pipe_function напрямую
sys.path.insert(0, str(Path(__file__).parent.parent / "openwebui"))
from pipe_function import Pipe


# ─── helpers ──────────────────────────────────────────────────────────


def make_pipe() -> Pipe:
    """Создаёт Pipe с тестовыми настройками."""
    pipe = Pipe()
    pipe.valves.BACKEND_URL = "http://test-backend:8000"
    pipe.valves.API_KEY = "test-key"
    pipe.valves.MAX_DOWNLOAD_MB = 10
    pipe.valves.BACKEND_TIMEOUT = 30
    return pipe


def make_http_mock(
    *,
    json_data: dict | None = None,
    content: bytes = b"",
    text: str = "",
    status_code: int = 200,
) -> MagicMock:
    """Возвращает mock для httpx.AsyncClient, настроенный для одного запроса."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.content = content
    resp.text = text

    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None

    client = AsyncMock()
    client.get.return_value = resp
    client.post.return_value = resp
    client.put.return_value = resp
    client.delete.return_value = resp

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


async def collect(gen) -> str:
    """Собирает все чанки из async-генератора в одну строку."""
    parts = []
    async for chunk in gen:
        parts.append(chunk)
    return "".join(parts)


# ─── _user_id ─────────────────────────────────────────────────────────


def test_user_id_none():
    assert Pipe()._user_id(None) == "default"


def test_user_id_name():
    assert Pipe()._user_id({"name": "alice", "id": "u1"}) == "alice"


def test_user_id_fallback_to_id():
    assert Pipe()._user_id({"id": "u42"}) == "u42"


def test_user_id_empty_dict():
    assert Pipe()._user_id({}) == "default"


def test_user_id_empty_name():
    assert Pipe()._user_id({"name": "", "id": "u1"}) == "u1"


# ─── _headers ─────────────────────────────────────────────────────────


def test_headers_contains_api_key():
    pipe = make_pipe()
    h = pipe._headers()
    assert h["Authorization"] == "Bearer test-key"
    assert "X-OpenWebUI-User-Name" not in h


def test_headers_with_user_id():
    pipe = make_pipe()
    h = pipe._headers("alice")
    assert h["X-OpenWebUI-User-Name"] == "alice"


# ─── pipes() ──────────────────────────────────────────────────────────


def test_pipes_returns_list():
    result = Pipe().pipes()
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["id"] == "claude-code-pipe"


# ─── pipe() routing ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipe_empty_messages():
    pipe = make_pipe()
    result = await collect(pipe.pipe({"messages": []}))
    assert "Нет" in result


@pytest.mark.parametrize("cmd", ["ws", "workspaces", "/ws", "workspace list", "workspace ls"])
@pytest.mark.asyncio
async def test_pipe_routes_workspace_panel(cmd):
    pipe = make_pipe()
    with patch.object(pipe, "_workspace_panel", return_value=_fake_gen("panel")) as m:
        await collect(pipe.pipe({"messages": [{"role": "user", "content": cmd}]}))
    m.assert_called_once()


@pytest.mark.asyncio
async def test_pipe_routes_clone():
    pipe = make_pipe()
    with patch.object(pipe, "_cmd_clone", return_value=_fake_gen("cloned")) as m:
        await collect(pipe.pipe({"messages": [{"role": "user", "content": "clone https://github.com/u/r.git"}]}))
    m.assert_called_once()


@pytest.mark.asyncio
async def test_pipe_routes_switch():
    pipe = make_pipe()
    with patch.object(pipe, "_cmd_switch", return_value=_fake_gen("switched")) as m:
        await collect(pipe.pipe({"messages": [{"role": "user", "content": "workspace switch my-proj"}]}))
    m.assert_called_once()


@pytest.mark.asyncio
async def test_pipe_routes_delete():
    pipe = make_pipe()
    with patch.object(pipe, "_cmd_delete", return_value=_fake_gen("deleted")) as m:
        await collect(pipe.pipe({"messages": [{"role": "user", "content": "workspace delete my-proj"}]}))
    m.assert_called_once()


@pytest.mark.asyncio
async def test_pipe_routes_download_no_arg():
    pipe = make_pipe()
    with patch.object(pipe, "_cmd_download_active", return_value=_fake_gen("dl")) as m:
        await collect(pipe.pipe({"messages": [{"role": "user", "content": "download"}]}))
    m.assert_called_once()


@pytest.mark.asyncio
async def test_pipe_routes_download_with_arg():
    pipe = make_pipe()
    with patch.object(pipe, "_download", return_value=_fake_gen("dl")) as m:
        await collect(pipe.pipe({"messages": [{"role": "user", "content": "download my-proj"}]}))
    m.assert_called_once()


@pytest.mark.asyncio
async def test_pipe_routes_files():
    pipe = make_pipe()
    with patch.object(pipe, "_list_files", return_value=_fake_gen("files")) as m:
        await collect(pipe.pipe({"messages": [{"role": "user", "content": "files my-proj src/"}]}))
    m.assert_called_once()


@pytest.mark.asyncio
async def test_pipe_routes_chat_fallthrough():
    pipe = make_pipe()
    with patch.object(pipe, "_proxy_chat", return_value=_fake_gen("chat")) as m:
        await collect(pipe.pipe({"messages": [{"role": "user", "content": "напиши тест"}]}))
    m.assert_called_once()


# ─── _workspace_panel ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_workspace_panel_renders_html():
    pipe = make_pipe()
    ws_list = [
        {"name": "my-proj", "is_active": True, "git_remote": "https://github.com/u/r", "git_branch": "main"},
        {"name": "default", "is_active": False, "git_remote": "", "git_branch": ""},
    ]
    with patch.object(pipe, "_get_workspaces", AsyncMock(return_value=ws_list)):
        result = await collect(pipe._workspace_panel("alice", None))

    assert "```html" in result
    assert "my-proj" in result
    assert "default" in result


@pytest.mark.asyncio
async def test_workspace_panel_empty():
    pipe = make_pipe()
    with patch.object(pipe, "_get_workspaces", AsyncMock(return_value=[])):
        result = await collect(pipe._workspace_panel("alice", None))
    assert "```html" in result
    assert "Clone" in result


@pytest.mark.asyncio
async def test_workspace_panel_escapes_xss():
    """Имя с </script> не должно ломать script-блок (инъекция закрывающего тега)."""
    pipe = make_pipe()
    # Самый опасный случай: попытка закрыть <script> тег изнутри JSON
    ws_list = [{"name": "</script><script>alert(1)//", "is_active": False, "git_remote": "", "git_branch": ""}]
    with patch.object(pipe, "_get_workspaces", AsyncMock(return_value=ws_list)):
        result = await collect(pipe._workspace_panel("alice", None))
    # </script> не должен присутствовать в сыром виде внутри <script>...</script>
    # (иначе браузер закроет тег досрочно и выполнит произвольный JS)
    script_block = result.split("<script>", 1)[-1].split("</script>", 1)[0] if "<script>" in result else result
    assert "</script>" not in script_block


# ─── _cmd_clone ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clone_success():
    pipe = make_pipe()
    info = {"name": "repo", "path": "/ws/alice/repo", "git_branch": "main"}
    with patch("httpx.AsyncClient", return_value=make_http_mock(json_data=info)):
        result = await collect(pipe._cmd_clone("alice", "https://github.com/u/r.git", None, None))
    assert "repo" in result
    assert "main" in result


@pytest.mark.asyncio
async def test_clone_no_url_no_call():
    pipe = make_pipe()
    result = await collect(pipe._cmd_clone("alice", "", None, None))
    assert "clone" in result.lower()


@pytest.mark.asyncio
async def test_clone_no_url_calls_dialog():
    pipe = make_pipe()
    call = AsyncMock(return_value={"value": "https://github.com/u/r.git"})
    info = {"name": "r", "path": "/ws/alice/r", "git_branch": ""}
    with patch("httpx.AsyncClient", return_value=make_http_mock(json_data=info)):
        result = await collect(pipe._cmd_clone("alice", "", call, None))
    call.assert_awaited_once()
    assert "r" in result


@pytest.mark.asyncio
async def test_clone_http_error():
    pipe = make_pipe()
    with patch("httpx.AsyncClient", return_value=make_http_mock(status_code=422, text="invalid url")):
        result = await collect(pipe._cmd_clone("alice", "bad-url", None, None))
    assert "Ошибка" in result
    assert "422" in result


@pytest.mark.asyncio
async def test_clone_connection_error():
    pipe = make_pipe()
    ctx = MagicMock()
    client = AsyncMock()
    client.post.side_effect = Exception("connection refused")
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient", return_value=ctx):
        result = await collect(pipe._cmd_clone("alice", "https://github.com/u/r.git", None, None))
    assert "Ошибка" in result


# ─── _cmd_switch ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_switch_success():
    pipe = make_pipe()
    with patch("httpx.AsyncClient", return_value=make_http_mock(json_data={})):
        result = await collect(pipe._cmd_switch("alice", "my-proj", None, None))
    assert "my-proj" in result


@pytest.mark.asyncio
async def test_switch_no_name_no_call():
    pipe = make_pipe()
    result = await collect(pipe._cmd_switch("alice", "", None, None))
    assert "switch" in result.lower()


@pytest.mark.asyncio
async def test_switch_no_name_calls_dialog():
    pipe = make_pipe()
    call = AsyncMock(return_value={"value": "my-proj"})
    with patch("httpx.AsyncClient", return_value=make_http_mock(json_data={})):
        result = await collect(pipe._cmd_switch("alice", "", call, None))
    call.assert_awaited_once()
    assert "my-proj" in result


@pytest.mark.asyncio
async def test_switch_not_found():
    pipe = make_pipe()
    resp = MagicMock()
    resp.status_code = 404
    resp.content = b'{"detail":"not found"}'
    resp.json.return_value = {"detail": "not found"}
    resp.raise_for_status.side_effect = httpx.HTTPStatusError("e", request=MagicMock(), response=resp)
    ctx = MagicMock()
    client = AsyncMock()
    client.put.return_value = resp
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient", return_value=ctx):
        result = await collect(pipe._cmd_switch("alice", "ghost", None, None))
    assert "Ошибка" in result
    assert "404" in result


# ─── _cmd_delete ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_success_with_confirm():
    pipe = make_pipe()
    call = AsyncMock(return_value=True)
    with patch("httpx.AsyncClient", return_value=make_http_mock(json_data={})):
        result = await collect(pipe._cmd_delete("alice", "old-proj", call, None))
    call.assert_awaited_once()
    assert "old-proj" in result
    assert "удалён" in result.lower()


@pytest.mark.asyncio
async def test_delete_cancelled():
    pipe = make_pipe()
    call = AsyncMock(return_value=False)
    result = await collect(pipe._cmd_delete("alice", "my-proj", call, None))
    assert "отменено" in result.lower()


@pytest.mark.asyncio
async def test_delete_no_call_no_confirm():
    """Без call-диалога удаление идёт без подтверждения."""
    pipe = make_pipe()
    with patch("httpx.AsyncClient", return_value=make_http_mock(json_data={})):
        result = await collect(pipe._cmd_delete("alice", "my-proj", None, None))
    assert "удалён" in result.lower()


@pytest.mark.asyncio
async def test_delete_http_error():
    pipe = make_pipe()
    with patch("httpx.AsyncClient", return_value=make_http_mock(status_code=403, text="forbidden")):
        result = await collect(pipe._cmd_delete("alice", "my-proj", None, None))
    assert "Ошибка" in result


# ─── _cmd_download_active ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_active_found():
    pipe = make_pipe()
    workspaces = [
        {"name": "inactive", "is_active": False},
        {"name": "my-proj", "is_active": True},
    ]
    zip_bytes = b"PK\x03\x04fake-zip-data"
    with (
        patch.object(pipe, "_get_workspaces", AsyncMock(return_value=workspaces)),
        patch.object(pipe, "_download", return_value=_fake_gen("zip-html")) as mock_dl,
    ):
        result = await collect(pipe._cmd_download_active("alice", None))
    mock_dl.assert_called_once_with("alice", "my-proj", None)
    assert "zip-html" in result


@pytest.mark.asyncio
async def test_download_active_none_active():
    pipe = make_pipe()
    with patch.object(pipe, "_get_workspaces", AsyncMock(return_value=[])):
        result = await collect(pipe._cmd_download_active("alice", None))
    assert "Нет активного" in result


# ─── _download ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_success():
    pipe = make_pipe()
    zip_bytes = b"PK\x03\x04" + b"x" * 100
    with patch("httpx.AsyncClient", return_value=make_http_mock(content=zip_bytes)):
        result = await collect(pipe._download("alice", "my-proj", None))
    assert "```html" in result
    assert "my-proj.zip" in result
    b64_expected = base64.b64encode(zip_bytes).decode()
    assert b64_expected in result


@pytest.mark.asyncio
async def test_download_too_large():
    pipe = make_pipe()
    pipe.valves.MAX_DOWNLOAD_MB = 1
    big_data = b"x" * (2 * 1024 * 1024)  # 2 МБ > 1 МБ лимит
    with patch("httpx.AsyncClient", return_value=make_http_mock(content=big_data)):
        result = await collect(pipe._download("alice", "my-proj", None))
    assert "MAX_DOWNLOAD_MB" in result or "Максимум" in result


@pytest.mark.asyncio
async def test_download_http_error():
    pipe = make_pipe()
    with patch("httpx.AsyncClient", return_value=make_http_mock(status_code=404, text="not found")):
        result = await collect(pipe._download("alice", "ghost", None))
    assert "Ошибка" in result
    assert "404" in result


# ─── _list_files ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_files_success():
    pipe = make_pipe()
    files_data = {
        "files": [
            {"path": "src", "name": "src", "is_dir": True},
            {"path": "src/main.py", "name": "main.py", "is_dir": False, "size": 1500},
            {"path": "README.md", "name": "README.md", "is_dir": False, "size": 512},
        ]
    }
    with patch("httpx.AsyncClient", return_value=make_http_mock(json_data=files_data)):
        result = await collect(pipe._list_files("alice", "my-proj", "", None))
    assert "src" in result
    assert "main.py" in result
    assert "README.md" in result
    assert "КБ" in result


@pytest.mark.asyncio
async def test_list_files_empty():
    pipe = make_pipe()
    with patch("httpx.AsyncClient", return_value=make_http_mock(json_data={"files": []})):
        result = await collect(pipe._list_files("alice", "my-proj", "", None))
    assert "пуст" in result.lower()


@pytest.mark.asyncio
async def test_list_files_http_error():
    pipe = make_pipe()
    with patch("httpx.AsyncClient", return_value=make_http_mock(status_code=500, text="internal error")):
        result = await collect(pipe._list_files("alice", "my-proj", "", None))
    assert "Ошибка" in result


@pytest.mark.asyncio
async def test_list_files_size_mb():
    """Файлы >= 1 МБ отображаются в МБ."""
    pipe = make_pipe()
    files_data = {
        "files": [
            {"path": "big.bin", "name": "big.bin", "is_dir": False, "size": 2 * 1024 * 1024},
        ]
    }
    with patch("httpx.AsyncClient", return_value=make_http_mock(json_data=files_data)):
        result = await collect(pipe._list_files("alice", "my-proj", "", None))
    assert "МБ" in result


# ─── _proxy_chat ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_proxy_chat_empty_delta_skipped():
    """Чанки с пустым content не попадают в вывод."""
    pipe = make_pipe()

    async def fake_aiter_lines():
        yield 'data: {"choices": [{"delta": {}}]}'           # нет content
        yield 'data: {"choices": [{"delta": {"content": ""}}]}'  # пустой content
        yield 'data: {"choices": [{"delta": {"content": "OK"}}]}'
        yield 'data: [DONE]'

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.aiter_lines = fake_aiter_lines

    stream_ctx = MagicMock()
    stream_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    stream_ctx.__aexit__ = AsyncMock(return_value=None)

    client = MagicMock()
    client.stream.return_value = stream_ctx

    client_ctx = MagicMock()
    client_ctx.__aenter__ = AsyncMock(return_value=client)
    client_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=client_ctx):
        result = await collect(pipe._proxy_chat(
            {"messages": [{"role": "user", "content": "hi"}]},
            "alice", None,
        ))

    assert result == "OK"


@pytest.mark.asyncio
async def test_proxy_chat_sse_parsing():
    """Парсинг SSE-чанков и yield текста."""
    pipe = make_pipe()

    async def fake_aiter_lines():
        for line in [
            'data: {"choices": [{"delta": {"content": "Hello"}}]}',
            'data: {"choices": [{"delta": {"content": " world"}}]}',
            'data: {"choices": [{"delta": {}}]}',  # пустой delta
            'not-data-line',                        # игнорируется
            'data: [DONE]',
        ]:
            yield line

    mock_stream_resp = MagicMock()
    mock_stream_resp.raise_for_status.return_value = None
    mock_stream_resp.aiter_lines = fake_aiter_lines

    stream_ctx = MagicMock()
    stream_ctx.__aenter__ = AsyncMock(return_value=mock_stream_resp)
    stream_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_client = MagicMock()
    mock_client.stream.return_value = stream_ctx

    client_ctx = MagicMock()
    client_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    client_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=client_ctx):
        result = await collect(pipe._proxy_chat(
            {"messages": [{"role": "user", "content": "hi"}]},
            "alice", None,
        ))

    assert result == "Hello world"


@pytest.mark.asyncio
async def test_proxy_chat_http_error():
    pipe = make_pipe()

    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"
    resp.raise_for_status.side_effect = httpx.HTTPStatusError("e", request=MagicMock(), response=resp)

    stream_ctx = MagicMock()
    stream_ctx.__aenter__ = AsyncMock(return_value=resp)
    stream_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_client = MagicMock()
    mock_client.stream.return_value = stream_ctx

    client_ctx = MagicMock()
    client_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    client_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=client_ctx):
        result = await collect(pipe._proxy_chat(
            {"messages": [{"role": "user", "content": "hi"}]},
            "alice", None,
        ))

    assert "Ошибка бэкенда" in result
    assert "401" in result


@pytest.mark.asyncio
async def test_proxy_chat_connection_error():
    pipe = make_pipe()

    mock_client = MagicMock()
    mock_client.stream.side_effect = Exception("connection refused")

    client_ctx = MagicMock()
    client_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    client_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=client_ctx):
        result = await collect(pipe._proxy_chat(
            {"messages": [{"role": "user", "content": "hi"}]},
            "alice", None,
        ))

    assert "Ошибка соединения" in result


@pytest.mark.asyncio
async def test_proxy_chat_passes_user_id():
    """user_id попадает в payload и заголовки."""
    pipe = make_pipe()
    captured = {}

    async def fake_aiter_lines():
        yield 'data: [DONE]'

    mock_stream_resp = MagicMock()
    mock_stream_resp.raise_for_status.return_value = None
    mock_stream_resp.aiter_lines = fake_aiter_lines

    stream_ctx = MagicMock()
    stream_ctx.__aenter__ = AsyncMock(return_value=mock_stream_resp)
    stream_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_client = MagicMock()

    def capture_stream(method, url, **kwargs):
        captured["json"] = kwargs.get("json", {})
        captured["headers"] = kwargs.get("headers", {})
        return stream_ctx

    mock_client.stream = capture_stream

    client_ctx = MagicMock()
    client_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    client_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=client_ctx):
        await collect(pipe._proxy_chat(
            {"messages": [{"role": "user", "content": "hi"}]},
            "bob", None,
        ))

    assert captured["json"]["user"] == "bob"
    assert captured["json"]["stream"] is True
    assert captured["headers"]["X-OpenWebUI-User-Name"] == "bob"


# ─── utils ────────────────────────────────────────────────────────────


async def _fake_gen(text: str):
    """Вспомогательный async-генератор для mock-замены методов."""
    yield text
