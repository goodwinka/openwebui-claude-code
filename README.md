# Open WebUI ↔ Claude Code Bridge

Мост между Open WebUI и Claude Agent SDK — Claude Code как "модель" в Open WebUI
с изолированными воркспейсами для каждого пользователя.

## Архитектура

```
Open WebUI (:8080)
    │  OpenAI-compatible API
    ▼
FastAPI Proxy (:8000)           ← работает от пользователя claude-code
    ├── /v1/chat/completions    → Claude Agent SDK (async query)
    ├── /v1/models              → список "моделей"
    ├── /api/workspaces         → управление воркспейсами
    └── /api/workspaces/clone   → git clone
    │
    ▼
Per-user workspaces (/srv/workspaces/)
    ├── alice/default/
    ├── alice/my-project/       ← git clone
    ├── bob/default/
    └── bob/another-repo/
```

Один системный пользователь (`claude-code`) — все процессы от его имени.
Изоляция между пользователями — через отдельные рабочие директории (cwd).

## Как это работает

1. **Open WebUI** подключается к FastAPI как к OpenAI-совместимому API
2. **FastAPI** извлекает `user` из тела запроса → определяет рабочую директорию
3. **Claude Agent SDK** выполняет промпт в контексте директории пользователя
4. Результат стримится обратно в формате SSE, совместимом с OpenAI

## Быстрый старт

### Требования

- Linux (Ubuntu 22.04+ / Debian 12+)
- Python 3.10+
- Node.js 18+
- Anthropic API ключ
- Open WebUI (установленный отдельно)

### Установка

```bash
# 1. Клонируем проект
git clone <repo-url> /opt/openwebui-claude-code
cd /opt/openwebui-claude-code

# 2. Установка (от root — создаёт пользователя claude-code, venv, systemd)
sudo bash scripts/setup.sh

# 3. Настройка
sudo nano .env   # ANTHROPIC_API_KEY и API_SECRET_KEY

# 4. Запуск
sudo systemctl start claude-code-proxy
sudo systemctl enable claude-code-proxy

# 5. Проверка
curl http://localhost:8000/health
curl -H "Authorization: Bearer YOUR_KEY" http://localhost:8000/v1/models
```

### Настройка Open WebUI

1. Settings → Connections → Add OpenAI API connection
2. **URL**: `http://localhost:8000/v1`
3. **API Key**: значение `API_SECRET_KEY` из `.env`
4. В списке моделей появится `claude-code`

## Управление воркспейсами

### Из чата (Open WebUI)

```
clone https://github.com/user/repo.git     — склонировать репо
workspace list                               — список воркспейсов
workspace switch my-project                  — переключить активный
```

### Через REST API

```bash
# Список
curl -H "Authorization: Bearer $KEY" \
  "http://localhost:8000/api/workspaces?user_id=alice"

# Клонировать
curl -X POST -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"alice","repo_url":"https://github.com/user/repo.git"}' \
  "http://localhost:8000/api/workspaces/clone"

# Переключить
curl -X PUT -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"alice","workspace":"my-project"}' \
  "http://localhost:8000/api/workspaces/active"

# Удалить
curl -X DELETE -H "Authorization: Bearer $KEY" \
  "http://localhost:8000/api/workspaces/alice/my-project"
```

## Конфигурация (.env)

| Переменная | Описание | По умолчанию |
|---|---|---|
| `ANTHROPIC_API_KEY` | API ключ Anthropic | *обязательно* |
| `API_SECRET_KEY` | Ключ аутентификации прокси | *обязательно* |
| `SERVICE_USER` | Системный пользователь | `claude-code` |
| `WORKSPACES_ROOT` | Корень воркспейсов | `/srv/workspaces` |
| `CLAUDE_MODEL` | Модель Claude | `sonnet` |
| `ALLOWED_TOOLS` | Разрешённые инструменты | `Read,Edit,Write,Bash,Glob,Grep` |
| `PERMISSION_MODE` | Режим подтверждений | `acceptEdits` |
| `MAX_TURNS` | Макс. итераций агента | `50` |
| `MAX_WORKSPACES_PER_USER` | Макс. воркспейсов на юзера | `10` |
| `PORT` | Порт сервера | `8000` |

## Структура проекта

```
openwebui-claude-code/
├── app/
│   ├── main.py              # FastAPI приложение
│   ├── config.py             # Конфигурация (pydantic-settings)
│   ├── auth.py               # Проверка API ключа
│   ├── claude_bridge.py      # Мост к Claude Agent SDK + CLI fallback
│   ├── workspace_manager.py  # Управление воркспейсами
│   ├── models.py             # Pydantic-модели
│   └── routes/
│       ├── openai_compat.py  # /v1/chat/completions, /v1/models
│       └── workspaces.py     # /api/workspaces/*
├── scripts/
│   └── setup.sh              # Установка (создаёт юзера, venv, systemd)
├── systemd/
│   └── claude-code-proxy.service
├── .env.example
├── requirements.txt
└── README.md
```

## Двойная стратегия подключения к Claude

1. **Python Agent SDK** (`claude-agent-sdk`) — основной путь, нативный async
2. **CLI fallback** (`claude -p`) — если SDK не установлен, используется CLI

Переключение автоматическое: если `import claude_agent_sdk` бросает `ImportError`,
прокси переходит на CLI. Оба пути дают одинаковый SSE-стрим на выходе.
