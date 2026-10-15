"""统一配置入口（CLAUDE.md 3.5）：全仓库唯一读取环境变量的位置。

规则：
- 除本模块外，任何位置不得出现 ``os.getenv`` / ``os.environ`` 读取配置；
  其余模块一律通过注入 ``Settings`` 获取配置。
- 启动期 fail-fast：缺失必填项立即抛 ``ConfigurationError`` 并列出缺失的环境变量名。
- ``.env.example`` 必须与本文件的字段清单保持同步（NO-20）。

用法::

    from agent_platform.settings import get_settings

    settings = get_settings()
    dsn = settings.postgres_dsn.get_secret_value()
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_platform.core.errors import ConfigurationError

AppEnv = Literal["dev", "test", "staging", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """平台配置（字段 alias 即环境变量名，全大写）。

    必填项（无默认值，缺失即启动失败）：
    ``AGENT_API_BEARER_TOKEN``、``POSTGRES_DSN``、``LITELLM_API_KEY``、``RAGFLOW_API_KEY``。
    """

    # 环境变量名 = 字段名大写（pydantic-settings 默认大小写不敏感匹配，无需 alias_generator）
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # --- 运行环境 ---
    app_env: AppEnv = "dev"
    log_level: LogLevel = "INFO"

    # --- HTTP 服务 ---
    agent_api_host: str = "0.0.0.0"
    agent_api_port: int = Field(default=8000, ge=1, le=65535)
    agent_api_bearer_token: SecretStr = Field(
        description="演示期静态 Bearer Token（生产替换为 OIDC/JWT 校验）"
    )
    demo_tenant_id: str = Field(default="demo", min_length=1, max_length=64)
    demo_user_id: str = Field(default="demo_user", min_length=1, max_length=64)
    sse_heartbeat_s: int = Field(default=15, ge=1, le=120)

    # --- PostgreSQL（LangGraph checkpoint / store）---
    postgres_dsn: SecretStr = Field(description="postgresql://user:pass@host:port/db")
    redis_url: str = "redis://localhost:6380/0"

    # --- LiteLLM 网关（CLAUDE.md 5.1 / NO-02）---
    litellm_base_url: str = "http://localhost:4000"
    litellm_api_key: SecretStr = Field(description="LiteLLM 网关 Master Key")
    primary_chat_model: str = Field(default="primary-chat", min_length=1)
    fallback_chat_model: str = Field(
        default="", description="应用层备选模型逻辑名；留空则仅由网关层 fallback 兜底"
    )
    embedding_model: str = Field(default="embedding", min_length=1)
    rerank_model: str = Field(default="rerank", min_length=1)
    model_timeout_s: float = Field(default=120.0, gt=0, le=600)
    model_max_retries: int = Field(default=2, ge=0, le=5)

    # --- RAGFlow（文档工程后端）---
    ragflow_base_url: str = "http://localhost:9380"
    ragflow_api_key: SecretStr = Field(description="RAGFlow 控制台生成的 API Key")
    ragflow_dataset_name: str = Field(default="gov_policy", min_length=1, max_length=128)
    ragflow_parse_timeout_s: int = Field(default=3600, ge=60, le=86400)

    # --- Milvus（检索主链路，ADR-0002）---
    milvus_uri: str = "http://localhost:19530"
    milvus_token: SecretStr = SecretStr("")
    milvus_collection: str = Field(default="gov_policy_chunks", min_length=1, max_length=128)
    milvus_dim: int = Field(default=4096, ge=64, le=16384)

    # --- 检索策略（变更须过评估门禁，CLAUDE.md 9.2 / NO-16）---
    evidence_top_k: int = Field(default=8, ge=1, le=50)
    retrieval_candidates: int = Field(default=64, ge=1, le=512)
    similarity_threshold: float = Field(default=0.2, ge=0.0, le=1.0)
    rerank_score_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    hybrid_vector_weight: float = Field(default=0.7, ge=0.0, le=1.0)

    # --- 入库脚本 ---
    ingest_manifest_path: Path = Path("local_data/ingest_manifest.json")

    # --- 测试 ---
    run_integration_tests: bool = False


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    """加载并校验配置；缺失必填项时抛出带变量名的 ``ConfigurationError``。

    Args:
        env_file: .env 文件路径；传 ``None`` 则仅从进程环境读取（测试用）。

    Raises:
        ConfigurationError: 配置缺失或非法，异常信息列出具体环境变量名。
    """
    try:
        # _env_file 由 pydantic-settings 在运行时注入，不在模型字段签名中
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as exc:
        missing: list[str] = []
        invalid: list[str] = []
        for err in exc.errors():
            field_name = str(err["loc"][0]).upper() if err["loc"] else "<unknown>"
            if err["type"] == "missing":
                missing.append(field_name)
            else:
                invalid.append(f"{field_name}（{err['msg']}）")
        problems: list[str] = []
        if missing:
            problems.append("缺失必填环境变量: " + ", ".join(sorted(missing)))
        if invalid:
            problems.append("非法配置项: " + "; ".join(sorted(invalid)))
        detail = " | ".join(problems) if problems else str(exc)
        raise ConfigurationError(f"配置校验失败: {detail}", detail=detail) from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级配置单例（FastAPI/脚本统一入口）。

    测试中如需覆盖，先调用 ``get_settings.cache_clear()``。
    """
    return load_settings()
