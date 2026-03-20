#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════
# Open WebUI ↔ Claude Code Bridge — установка на bare-metal Linux
# Запускать от root: sudo bash scripts/setup.sh
# ═══════════════════════════════════════════════════════════════════

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -eq 0 ]] || error "Этот скрипт нужно запускать от root (sudo)"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
info "Директория проекта: $PROJECT_DIR"

# ─── Конфигурация ────────────────────────────────────────────────

SERVICE_USER="claude-code"
WORKSPACES_ROOT="/srv/workspaces"

# Подтягиваем из .env если есть
if [[ -f "$PROJECT_DIR/.env" ]]; then
    SERVICE_USER=$(grep -E '^SERVICE_USER=' "$PROJECT_DIR/.env" | cut -d= -f2 || echo "$SERVICE_USER")
    WORKSPACES_ROOT=$(grep -E '^WORKSPACES_ROOT=' "$PROJECT_DIR/.env" | cut -d= -f2 || echo "$WORKSPACES_ROOT")
fi

# ─── Системные зависимости ───────────────────────────────────────

info "Проверяю системные зависимости..."

if ! command -v python3 &>/dev/null; then
    info "Устанавливаю Python 3..."
    apt-get update -qq
    apt-get install -y -qq python3 python3-pip python3-venv
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 10 ]]; }; then
    error "Python 3.10+ обязателен (найден: $PYTHON_VERSION)"
fi
info "Python $PYTHON_VERSION ✓"

if ! command -v node &>/dev/null; then
    info "Устанавливаю Node.js 20.x..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y -qq nodejs
fi
NODE_VER=$(node -v | sed 's/v//' | cut -d. -f1)
[[ "$NODE_VER" -ge 18 ]] || error "Node.js 18+ обязателен"
info "Node.js $(node -v) ✓"

if ! command -v git &>/dev/null; then
    apt-get install -y -qq git
fi
info "Git $(git --version | awk '{print $3}') ✓"

# ─── Claude Code CLI ─────────────────────────────────────────────

if ! command -v claude &>/dev/null; then
    info "Устанавливаю Claude Code CLI..."
    npm install -g @anthropic-ai/claude-code
fi
command -v claude &>/dev/null && info "Claude Code CLI ✓" || warn "Claude Code CLI не найден в PATH"

# ─── Системный пользователь (один на весь сервис) ────────────────

if ! id "$SERVICE_USER" &>/dev/null; then
    info "Создаю системного пользователя: $SERVICE_USER"
    useradd \
        --system \
        --create-home \
        --home-dir "/home/$SERVICE_USER" \
        --shell /bin/bash \
        "$SERVICE_USER"
fi
info "Системный пользователь: $SERVICE_USER (uid=$(id -u "$SERVICE_USER")) ✓"

# ─── Python venv ─────────────────────────────────────────────────

VENV_DIR="$PROJECT_DIR/.venv"

if [[ ! -d "$VENV_DIR" ]]; then
    info "Создаю Python venv..."
    python3 -m venv "$VENV_DIR"
fi

info "Устанавливаю Python-зависимости..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q

# Agent SDK (может быть недоступен — тогда используется CLI fallback)
"$VENV_DIR/bin/pip" install claude-agent-sdk -q 2>/dev/null || {
    warn "claude-agent-sdk не доступен в PyPI — будет использоваться CLI fallback (claude -p)."
}

info "Python-зависимости установлены ✓"

# ─── Директория воркспейсов ──────────────────────────────────────

info "Создаю директорию воркспейсов: $WORKSPACES_ROOT"
mkdir -p "$WORKSPACES_ROOT"
chown "$SERVICE_USER:$SERVICE_USER" "$WORKSPACES_ROOT"
chmod 755 "$WORKSPACES_ROOT"

# ─── Права на проект ─────────────────────────────────────────────

chown -R "$SERVICE_USER:$SERVICE_USER" "$PROJECT_DIR"

# ─── .env ─────────────────────────────────────────────────────────

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    info "Копирую .env.example → .env"
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    chown "$SERVICE_USER:$SERVICE_USER" "$PROJECT_DIR/.env"
    chmod 600 "$PROJECT_DIR/.env"
    warn "Отредактируйте .env — укажите ANTHROPIC_API_KEY и API_SECRET_KEY!"
fi

# ─── Systemd ─────────────────────────────────────────────────────

SERVICE_FILE="/etc/systemd/system/claude-code-proxy.service"

info "Устанавливаю systemd-сервис..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Claude Code Proxy (Open WebUI Bridge)
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$VENV_DIR/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
info "Systemd-сервис установлен ✓"

# ─── Итог ─────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════"
echo -e "${GREEN}Установка завершена!${NC}"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "  Сервисный пользователь: $SERVICE_USER"
echo "  Воркспейсы:             $WORKSPACES_ROOT"
echo ""
echo "  1. Настройте:  nano $PROJECT_DIR/.env"
echo "  2. Запустите:  sudo systemctl start claude-code-proxy"
echo "                 sudo systemctl enable claude-code-proxy"
echo "  3. Проверьте:  curl http://localhost:8000/health"
echo "  4. Open WebUI: Settings → Connections → OpenAI API"
echo "     URL:     http://localhost:8000/v1"
echo "     API Key: <API_SECRET_KEY из .env>"
echo "  5. Логи:      sudo journalctl -u claude-code-proxy -f"
echo ""
