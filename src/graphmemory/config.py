"""配置管理"""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "graphmemory_dev"

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "graphmemory"
    postgres_password: str = "graphmemory_dev"
    postgres_db: str = "graphmemory"

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = "graphmemory_dev"

    # LLM
    openai_api_key: str = ""
    openai_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    ollama_base_url: str = "http://localhost:11434/v1"

    default_llm_provider: str = "openai"
    default_llm_model: str = "glm-4-plus"
    default_embedding_model: str = "embedding-3"

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "info"
    api_key: str = ""  # 若非空，/api 请求需带 X-API-Key 头

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/0"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
