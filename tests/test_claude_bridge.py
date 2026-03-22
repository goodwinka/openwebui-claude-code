"""Тесты модуля claude_bridge (без вызова реального Claude)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.claude_bridge import (
    detect_workspace_command,
    _build_prompt,
    _extract_text_from_message,
    _format_tool_use,
    _format_tool_result,
)
from app.models import ChatMessage


# ─── detect_workspace_command ─────────────────────────────────────────


@pytest.mark.parametrize(
    "prompt, expected",
    [
        ("clone https://github.com/user/repo.git", ("clone", "https://github.com/user/repo.git")),
        ("CLONE https://github.com/user/repo", ("clone", "https://github.com/user/repo")),
        ("workspace list", ("list", None)),
        ("WORKSPACE LIST", ("list", None)),
        ("workspace switch my-project", ("switch", "my-project")),
        ("hello world", None),
        ("clone", None),  # нет URL
        ("clone not-a-url", None),
    ],
)
def test_detect_workspace_command(prompt, expected):
    result = detect_workspace_command(prompt)
    assert result == expected


# ─── _build_prompt ────────────────────────────────────────────────────


def test_build_prompt_user_only():
    messages = [ChatMessage(role="user", content="Hello")]
    prompt, system = _build_prompt(messages)
    assert prompt == "Hello"
    assert system is None


def test_build_prompt_with_system():
    messages = [
        ChatMessage(role="system", content="You are a helper"),
        ChatMessage(role="user", content="Hi"),
    ]
    prompt, system = _build_prompt(messages)
    assert prompt == "Hi"
    assert system == "You are a helper"


def test_build_prompt_last_user_wins():
    messages = [
        ChatMessage(role="user", content="First question"),
        ChatMessage(role="assistant", content="First answer"),
        ChatMessage(role="user", content="Second question"),
    ]
    prompt, _ = _build_prompt(messages)
    assert prompt == "Second question"


def test_build_prompt_empty():
    prompt, system = _build_prompt([])
    assert prompt == "Hello"
    assert system is None


# ─── _extract_text_from_message ───────────────────────────────────────


def test_extract_text_string_content():
    msg = SimpleNamespace(content="Hello world")
    assert _extract_text_from_message(msg) == "Hello world"


def test_extract_text_list_text_block():
    """TextBlock с type='text' извлекается корректно."""
    block = SimpleNamespace(type="text", text="Block text")
    msg = SimpleNamespace(content=[block])
    assert _extract_text_from_message(msg) == "Block text"


def test_extract_text_list_text_block_dict():
    """TextBlock как dict."""
    msg = SimpleNamespace(content=[{"type": "text", "text": "Dict text"}])
    assert _extract_text_from_message(msg) == "Dict text"


def test_extract_text_tool_use_block():
    """ToolUseBlock форматируется как bash-команда."""
    block = SimpleNamespace(type="tool_use", name="Bash", input={"command": "pytest"})
    msg = SimpleNamespace(content=[block])
    result = _extract_text_from_message(msg)
    assert "pytest" in result
    assert "```bash" in result


def test_extract_text_tool_use_read():
    """ToolUseBlock для Read форматируется как курсив."""
    block = SimpleNamespace(type="tool_use", name="Read", input={"file_path": "/app/main.py"})
    msg = SimpleNamespace(content=[block])
    result = _extract_text_from_message(msg)
    assert "/app/main.py" in result


def test_extract_text_tool_result_block():
    """ToolResultBlock показывает вывод в code-блоке."""
    block = SimpleNamespace(type="tool_result", content="test output\n")
    msg = SimpleNamespace(content=[block])
    result = _extract_text_from_message(msg)
    assert "test output" in result
    assert "```" in result


def test_extract_text_tool_result_dict():
    """ToolResultBlock как dict."""
    msg = SimpleNamespace(content=[{"type": "tool_result", "content": "cmd output"}])
    result = _extract_text_from_message(msg)
    assert "cmd output" in result


def test_extract_text_result_attr():
    """ResultMessage с атрибутом result и без content."""
    msg = SimpleNamespace(result="Result text")
    assert _extract_text_from_message(msg) == "Result text"


def test_extract_text_result_fallthrough_empty_content():
    """ResultMessage с пустым content[] не теряет result."""
    msg = SimpleNamespace(content=[], result="Fallthrough text")
    assert _extract_text_from_message(msg) == "Fallthrough text"


def test_extract_text_empty():
    assert _extract_text_from_message(SimpleNamespace()) == ""


def test_extract_text_mixed_blocks():
    """Несколько блоков в одном сообщении объединяются."""
    msg = SimpleNamespace(content=[
        {"type": "text", "text": "Запускаю тесты"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -v"}},
    ])
    result = _extract_text_from_message(msg)
    assert "Запускаю тесты" in result
    assert "pytest -v" in result


# ─── _format_tool_use ─────────────────────────────────────────────────


def test_format_tool_use_bash():
    result = _format_tool_use("Bash", {"command": "ls -la"})
    assert "ls -la" in result
    assert "```bash" in result


def test_format_tool_use_read():
    result = _format_tool_use("Read", {"file_path": "/foo/bar.py"})
    assert "/foo/bar.py" in result
    assert "Читаю" in result


def test_format_tool_use_write():
    result = _format_tool_use("Write", {"file_path": "/out.txt"})
    assert "Создаю" in result


def test_format_tool_use_edit():
    result = _format_tool_use("Edit", {"file_path": "/edit.py"})
    assert "Редактирую" in result


def test_format_tool_use_unknown():
    result = _format_tool_use("MyTool", {})
    assert "MyTool" in result


# ─── _format_tool_result ─────────────────────────────────────────────


def test_format_tool_result_str():
    result = _format_tool_result("output text")
    assert "output text" in result
    assert "```" in result


def test_format_tool_result_empty():
    assert _format_tool_result("") == ""
    assert _format_tool_result([]) == ""


def test_format_tool_result_list():
    result = _format_tool_result([{"type": "text", "text": "from list"}])
    assert "from list" in result


def test_format_tool_result_truncation():
    long_text = "x" * 5000
    result = _format_tool_result(long_text)
    assert "обрезан" in result
    assert len(result) < 4000


from unittest.mock import MagicMock
