"""Fixtures для тестов сервера."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Устанавливаем env до импорта приложения
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-123")
os.environ.setdefault("API_SECRET_KEY", "test-secret")
os.environ.setdefault("WORKSPACES_ROOT", "/tmp/test-workspaces")

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
import pytest_asyncio
from fastapi.testclient import TestClient

from app.main import app

API_KEY = "test-secret"
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture(scope="session")
def client():
    """Синхронный TestClient для большинства тестов."""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def auth_headers():
    return AUTH_HEADERS
