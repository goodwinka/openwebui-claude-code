from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

# Load .env into os.environ early so that ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL
# (intentionally not declared as Settings fields) are available to CLI subprocesses.
# When running under systemd, EnvironmentFile= already does this; load_dotenv() is
# a no-op if the variables are already set (override=False by default).
load_dotenv(override=False)


class Settings(BaseSettings):
    # ANTHROPIC_API_KEY и ANTHROPIC_BASE_URL намеренно не объявлены здесь:
    # systemd загружает их из .env напрямую в окружение процесса (EnvironmentFile=),
    # и Claude Code CLI/SDK подхватывают их из os.environ автоматически.
    claude_model: str = "sonnet"
    allowed_tools: str = "Read,Edit,Write,Bash,Glob,Grep"
    permission_mode: str = "acceptEdits"
    max_turns: int = 50
    claude_cli_path: str = ""  # Override path to claude CLI binary (empty = auto-detect)

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
