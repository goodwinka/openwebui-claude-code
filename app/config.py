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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def allowed_tools_list(self) -> list[str]:
        return [t.strip() for t in self.allowed_tools.split(",") if t.strip()]


settings = Settings()
