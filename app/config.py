"""运行配置，全部通过环境变量注入，不写死在代码里。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_AI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_AI_MODEL = "gpt-4o"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "kuandai-manager"
    data_dir: str = "/data"
    database_url: str = ""

    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    ai_timeout: float = 120.0
    ai_temperature: float = 0.3
    ai_max_tokens: int = 8192
    vision_timeout: float = 300.0

    enable_scheduler: bool = True
    daily_check_hour: int = 0
    daily_check_minute: int = 5

    session_cookie_secure: bool = False

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir}/kuandai.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
