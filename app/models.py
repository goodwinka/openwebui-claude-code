from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field


# ─── OpenAI-compatible request/response ──────────────────────────


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "claude-code"
    messages: list[ChatMessage]
    stream: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    user: str | None = None  # Open WebUI передаёт user_id здесь


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = "stop"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{int(time.time())}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "claude-code"
    choices: list[ChatCompletionChoice]
    usage: UsageInfo = Field(default_factory=UsageInfo)


class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str = "claude-code"
    choices: list[dict[str, Any]]


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 1700000000
    owned_by: str = "anthropic"


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelInfo]


# ─── Workspace ───────────────────────────────────────────────────


class WorkspaceInfo(BaseModel):
    name: str
    path: str
    is_active: bool = False
    git_remote: str | None = None
    created_at: float | None = None


class CloneRequest(BaseModel):
    user_id: str
    repo_url: str
    name: str | None = None
    branch: str | None = None


class SwitchWorkspaceRequest(BaseModel):
    user_id: str
    workspace: str


class WorkspaceListResponse(BaseModel):
    user_id: str
    workspaces: list[WorkspaceInfo]
    active: str | None = None
