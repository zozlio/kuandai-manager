"""运行配置，全部通过环境变量注入，不写死在代码里。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# 默认的 AI 接入：中性的 OpenAI 兼容占位值。
# 真实端点请在「设置」页或环境变量里填（任意 OpenAI 兼容服务：
# OpenAI、DeepSeek、通义千问 DashScope、MiniMax 等）。
DEFAULT_AI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_AI_MODEL = "gpt-4o"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    # --- 基本信息 ---
    app_name: str = "kuandai-manager"
    data_dir: str = "/data"
    # 留空则用 sqlite:///{data_dir}/kuandai.db
    database_url: str = ""

    # --- AI 接口（OpenAI 兼容协议）---
    # 任意 OpenAI 兼容服务；三个都配齐才会启用 AI 功能
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    ai_timeout: float = 120.0
    ai_temperature: float = 0.3
    # 单次生成 token 上限：思考型模型不限制的话，思考阶段可能把整个
    # 请求拖到超时。部分端点的模型上限更低，超了会 400，可调小。
    ai_max_tokens: int = 8192
    # 视觉接口单独超时：识图比文本慢得多，不复用 ai_timeout
    vision_timeout: float = 300.0

    # --- 定时任务 ---
    enable_scheduler: bool = True
    # 每天几点跑一次日扣费 + 刷新提醒（默认 00:05，当天额度尽早扣掉）
    daily_check_hour: int = 0
    daily_check_minute: int = 5

    # --- 登录 ---
    # 放在 HTTPS 后面时打开，cookie 才会带 Secure 标记
    session_cookie_secure: bool = False

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir}/kuandai.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
