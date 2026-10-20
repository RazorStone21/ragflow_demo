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
    readyz_timeout_s: float = Field(
        default=3.0, gt=0, le=30, description="就绪探针单次检查超时（CLAUDE.md 11.6）"
    )
    max_question_chars: int = Field(
        default=2000,
        ge=1,
        le=20000,
        description="单次提问最大字符数（超出由 HTTP 边界拒绝，避免超长输入冲击检索与模型成本）",
    )

    # --- PostgreSQL（LangGraph checkpoint / store）---
    postgres_dsn: SecretStr = Field(description="postgresql://user:pass@host:port/db")
    postgres_connect_timeout_s: float = Field(
        default=10.0,
        gt=0,
        le=120,
        description="建连超时秒数；缺失则 libpq 可能无限期挂住（见 core/checkpoint.py）",
    )
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
    judge_model: str = Field(
        default="judge",
        min_length=1,
        description="评估裁判模型的网关逻辑名（CLAUDE.md 5.2 / 9.2）；仅评估层使用",
    )
    model_timeout_s: float = Field(default=120.0, gt=0, le=600)
    model_max_retries: int = Field(default=2, ge=0, le=5)

    # --- RAGFlow（文档工程后端）---
    ragflow_base_url: str = "http://localhost:9380"
    ragflow_api_key: SecretStr = Field(description="RAGFlow 控制台生成的 API Key")
    ragflow_dataset_name: str = Field(default="gov_policy", min_length=1, max_length=128)
    ragflow_parse_timeout_s: int = Field(default=3600, ge=60, le=86400)
    # 数据集级解析配置（RAGFlow v0.27.2 的上传接口只认 table_column_* 两个键，
    # 其余键必须在创建/更新数据集时设置，详见 ADR-0004 与 ragflow_client.ensure_dataset）
    ragflow_layout_recognize: str = Field(
        default="DeepDOC",
        min_length=1,
        max_length=32,
        description="版面/表格识别器；`DeepDOC` 启用 OCR 与表格结构识别",
    )
    ragflow_image_context_size: int = Field(
        default=0,
        ge=0,
        le=4096,
        description="图片切分上溯的相邻文本字符数；0 表示不附带上下文",
    )
    ragflow_table_context_size: int = Field(
        default=0, ge=0, le=4096, description="表格切分上溯的相邻文本字符数"
    )
    ragflow_chunk_token_num: int = Field(default=512, ge=64, le=8192)
    ragflow_task_page_size: int = Field(
        default=12, ge=1, le=64, description="解析任务分片页数（CPU 模式下影响单任务耗时）"
    )

    # --- 证据图片（ADR-0004；演示期本地文件存储，生产换对象存储）---
    evidence_image_store_dir: Path = Path("local_data/evidence_images")
    evidence_image_max_bytes: int = Field(
        default=8 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024, description="单张图片字节上限"
    )
    evidence_image_download_timeout_s: float = Field(default=60.0, gt=0, le=600)
    evidence_image_download_concurrency: int = Field(
        default=4, ge=1, le=16, description="入库时并发下载图片的上限"
    )

    # --- Milvus（检索主链路，ADR-0002）---
    milvus_uri: str = "http://localhost:19530"
    milvus_token: SecretStr = SecretStr("")
    milvus_collection: str = Field(
        default="gov_policy_chunks_v2",
        min_length=1,
        max_length=128,
        description="ADR-0004 起为 v2 集合（含 media_type/images 字段）；旧集合保留用于回滚",
    )
    milvus_dim: int = Field(default=4096, ge=64, le=16384)

    # --- 检索策略（变更须过评估门禁，CLAUDE.md 9.2 / NO-16）---
    evidence_top_k: int = Field(default=8, ge=1, le=50)
    retrieval_candidates: int = Field(default=64, ge=1, le=512)
    similarity_threshold: float = Field(default=0.2, ge=0.0, le=1.0)
    rerank_score_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    hybrid_vector_weight: float = Field(default=0.7, ge=0.0, le=1.0)

    # --- 入库脚本 ---
    ingest_manifest_path: Path = Path("local_data/ingest_manifest.json")

    # --- 评估（CLAUDE.md 9.2 / NO-16）---
    eval_golden_dir: Path = Path("tests/evals/golden")
    eval_thresholds_path: Path = Path("tests/evals/config/thresholds.toml")
    """门禁阈值文件。

    **刻意是文件而不是本模型里的字段**：阈值必须随黄金集版本化、可逐行 review 理由，
    且不能被环境变量悄悄调低——那等于给"改门禁"开后门。这里的 Path 只决定去**哪里读**，
    读到的值会原样写进产物与对比报告，所以改路径不会让阈值悄悄变松而不留痕。
    """
    eval_runs_dir: Path = Path("local_data/evals/runs")
    eval_judge_cache_dir: Path = Path("local_data/evals/cache")
    eval_ragas_dir: Path = Path("local_data/evals/ragas")
    """RAGAS 旁挂产物的落盘目录（隔离环境跑出来，见 ADR-0003）。"""

    # --- 测试 ---
    run_integration_tests: bool = False
    run_eval_tests: bool = Field(
        default=False,
        description="true 时运行需真实基础设施 + 裁判模型的评估测试（CLAUDE.md 9.2）",
    )


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
