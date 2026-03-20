"""Тесты модуля claude_bridge (без вызова реального Claude)."""
from __future__ import annotations

import pytest

from app.claude_bridge import detect_workspace_command, _build_prompt, _extract_text_from_message
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
    msg = MagicMock()
    msg.content = "Hello world"
    result = _extract_text_from_message(msg)
    assert result == "Hello world"


def test_extract_text_list_content():
    block = MagicMock()
    block.text = "Block text"
    msg = MagicMock()
    msg.content = [block]
    result = _extract_text_from_message(msg)
    assert result == "Block text"


def test_extract_text_result_attr():
    class Msg:
        result = "Result text"

    result = _extract_text_from_message(Msg())
    assert result == "Result text"


def test_extract_text_empty():
    class Msg:
        pass

    result = _extract_text_from_message(Msg())
    assert result == ""


from unittest.mock import MagicMock
