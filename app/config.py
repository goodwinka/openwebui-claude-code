from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Anthropic
    anthropic_api_key: str
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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def allowed_tools_list(self) -> list[str]:
        return [t.strip() for t in self.allowed_tools.split(",") if t.strip()]

    @property
    def effective_https_proxy(self) -> str:
        """Возвращает HTTPS-прокси, с fallback на HTTP-прокси."""
        return self.https_proxy or self.http_proxy


settings = Settings()
