from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Anthropic / Local model
    # Для локальной модели: установите ANTHROPIC_BASE_URL и любой ключ (например "local")
    anthropic_api_key: str = "local"
    # URL локального сервера, совместимого с Anthropic API (например Ollama-прокси).
    # Если пусто — используется официальный API Anthropic.
    anthropic_base_url: str = ""
    claude_model: str = "sonnet"
    allowed_tools: str = "Read,Edit,Write,Bash,Glob,Grep"
    permission_mode: str = "acceptEdits"
    max_turns: int = 50

    # Auth
    api_secret_key: str = "change-me"

    # Workspaces
    workspaces_root: Path = Path("/srv/workspaces")
    max_workspaces_per_user: int = 10

    # Системный пользователь (один на весь сервис)
    service_user: str = "claude-code"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # Git интеграция
    gitlab_token: str = ""          # GitLab personal/project access token
    github_token: str = ""          # GitHub token (fallback если gh CLI недоступен)
    http_proxy: str = ""            # HTTP-прокси, например http://proxy.local:3128
    https_proxy: str = ""           # HTTPS-прокси (если пусто — используется http_proxy)
    git_user_name: str = "Claude Code"              # имя автора git-коммитов
    git_user_email: str = "claude-code@localhost"   # email автора git-коммитов
    git_clone_depth: int = 1                        # 0 = полный clone
    git_branch_prefix: str = "claude/session-"     # префикс создаваемых веток
    git_api_timeout: int = 30                       # таймаут запросов к GitHub/GitLab API (сек)
    gitlab_ssl_verify: bool = False                 # True — проверять SSL (для публичных инстансов)
    gitlab_mr_remove_source_branch: bool = False    # удалять ветку после merge MR

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def allowed_tools_list(self) -> list[str]:
        return [t.strip() for t in self.allowed_tools.split(",") if t.strip()]

    @property
    def effective_https_proxy(self) -> str:
        """Возвращает HTTPS-прокси, с fallback на HTTP-прокси."""
        return self.https_proxy or self.http_proxy


settings = Settings()
