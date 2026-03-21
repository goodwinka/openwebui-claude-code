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

### Идентификация пользователей (важно!)

Open WebUI **по умолчанию не передаёт** имя пользователя внешним бэкендам.
Без настройки все пользователи попадут в один воркспейс `default/`.

**Рекомендуемый способ** — включите переменную окружения в Open WebUI:

```
ENABLE_FORWARD_USER_INFO_HEADERS=true
```

Это заставит Open WebUI отправлять HTTP-заголовки:
- `X-OpenWebUI-User-Name` — имя пользователя (например `alice`)
- `X-OpenWebUI-User-Id` — UUID пользователя
- `X-OpenWebUI-User-Email` — email
- `X-OpenWebUI-User-Role` — роль (`admin` / `user`)

Наш прокси извлечёт из них `user_id` для маршрутизации воркспейсов.

**Приоритет определения пользователя:**

1. Заголовок `X-OpenWebUI-User-Name` (самый надёжный)
2. Заголовок `X-OpenWebUI-User-Id` (UUID, fallback)
3. Поле `metadata.user_id` в теле запроса (если используется Pipe Function)
4. Поле `user` в теле запроса (стандарт OpenAI)
5. `"default"` — если ничего не найдено

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

## Pipe Function (если бэкенд за прокси)

Если браузер пользователя не имеет прямого доступа к бэкенду (бэкенд в
закрытой сети, за NAT или firewall), используйте Pipe Function — она делает
все запросы к бэкенду **изнутри Open WebUI** (server-side), а пользователь
взаимодействует только с Open WebUI.

### Установка

1. **Admin panel → Functions → Add Function**
2. Скопировать содержимое `openwebui/pipe_function.py`
3. Сохранить, открыть настройки (⚙) и задать:

| Valve | Значение |
|---|---|
| `BACKEND_URL` | Внутренний URL бэкенда, например `http://claude-code:8000` |
| `API_KEY` | Значение `API_SECRET_KEY` из `.env` бэкенда |
| `MAX_DOWNLOAD_MB` | Максимальный размер ZIP (по умолчанию 50 МБ) |
| `BACKEND_TIMEOUT` | Таймаут запросов (по умолчанию 300 сек) |

4. Отключить прямое подключение Open WebUI к бэкенду (или оставить оба)
5. Выбрать модель **Claude Code** (через Pipe Function) в чате

### Доступные команды через Pipe Function

```
download <workspace>        — скачать ZIP-архив воркспейса
files <workspace> [path]    — показать список файлов
```

Остальные сообщения (обычный чат, `clone`, `workspace list` и т.д.) прозрачно
проксируются к бэкенду.

### Схема с Pipe Function

```
Браузер
    │  HTTP (публичная сеть)
    ▼
Open WebUI (:8080)
    │  Pipe Function (server-side, внутренняя сеть)
    ▼
Claude Code бэкенд (:8000)   ← недоступен из браузера напрямую
    │
    ▼
Claude Agent SDK → /srv/workspaces/
```

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
├── openwebui/
│   └── pipe_function.py      # Pipe Function для Open WebUI (прокси + скачивание)
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
