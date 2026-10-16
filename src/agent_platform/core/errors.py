"""统一异常层级与错误码（CLAUDE.md 3.6 / 8.2 / NO-06）。

约定：
- 每个异常绑定一个稳定 ``ErrorCode``，发布后不得复用或改变语义。
- 对外错误以 RFC 9457 Problem Details 表达，不泄露堆栈、SQL、内部地址与密钥。
- 仅捕获能处理的异常；禁止裸 ``except`` 吞异常。

用法::

    from agent_platform.core.errors import KnowledgeParseTimeoutError

    raise KnowledgeParseTimeoutError("解析超时", detail="文档 x.pdf 超过 3600s 未完成解析")
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_ERR_TYPE_PREFIX = "urn:agent-platform:error:"


class ErrorCode(StrEnum):
    """平台稳定错误码（集中管理，禁止在别处重复定义同义码，CLAUDE.md 8.2）。"""

    # 配置（启动期）
    CONFIG_INVALID = "CONFIG_INVALID"

    # 入参/鉴权/资源（4xx 语义，不得重试）
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"

    # 模型网关（CLAUDE.md 3.6 / 5.4）
    MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_ALL_UNAVAILABLE = "MODEL_ALL_UNAVAILABLE"

    # 知识检索（CLAUDE.md 第 7 章）
    KNOWLEDGE_UNAVAILABLE = "KNOWLEDGE_UNAVAILABLE"
    KNOWLEDGE_PARSE_TIMEOUT = "KNOWLEDGE_PARSE_TIMEOUT"
    KNOWLEDGE_PARSE_FAILED = "KNOWLEDGE_PARSE_FAILED"
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"

    # 工具与护栏
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"

    # 兜底
    INTERNAL_ERROR = "INTERNAL_ERROR"


def _error_type(code: ErrorCode) -> str:
    """返回错误码对应的 Problem Details ``type`` URI。"""
    return f"{_ERR_TYPE_PREFIX}{code.value.lower().replace('_', '-')}"


class AgentPlatformError(Exception):
    """平台异常基类。

    Args:
        message: 内部诊断信息（进受控日志，不外泄）。
        detail: 面向调用方的可理解说明（进 Problem Details 的 ``detail``）。

    Attributes:
        code: 稳定错误码。
        http_status: 映射的 HTTP 状态码。
        retryable: 是否属于幂等且可重试的错误（超时/429/5xx）。
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or "服务暂时不可用，请稍后重试。"

    def to_problem_details(
        self, *, request_id: str | None = None, instance: str | None = None
    ) -> ProblemDetail:
        """转换为 RFC 9457 Problem Details 响应体。"""
        return ProblemDetail(
            type=_error_type(self.code),
            title=self.code.value,
            status=self.http_status,
            detail=self.detail,
            instance=instance,
            code=self.code.value,
            request_id=request_id,
        )


# ---------------------------------------------------------------------------
# 配置（启动期 fail-fast，CLAUDE.md 3.5）
# ---------------------------------------------------------------------------
class ConfigurationError(AgentPlatformError):
    """配置缺失或非法；启动期抛出并打印缺失项名。"""

    code = ErrorCode.CONFIG_INVALID
    http_status = 500
    retryable = False


# ---------------------------------------------------------------------------
# 入参 / 鉴权 / 资源
# ---------------------------------------------------------------------------
class ValidationFailedError(AgentPlatformError):
    """请求参数语义校验失败（映射 422）。"""

    code = ErrorCode.VALIDATION_FAILED
    http_status = 422


class UnauthorizedError(AgentPlatformError):
    """未认证或凭据无效（映射 401；不得重试）。"""

    code = ErrorCode.UNAUTHORIZED
    http_status = 401

    def __init__(self, message: str = "missing or invalid credentials") -> None:
        super().__init__(message, detail="认证失败，请检查凭据。")


class ForbiddenError(AgentPlatformError):
    """已认证但无权限（映射 403；不得重试）。"""

    code = ErrorCode.FORBIDDEN
    http_status = 403


class NotFoundError(AgentPlatformError):
    """资源不存在（映射 404）。"""

    code = ErrorCode.NOT_FOUND
    http_status = 404


class ConflictError(AgentPlatformError):
    """资源冲突或幂等键冲突（映射 409）。"""

    code = ErrorCode.CONFLICT
    http_status = 409


# ---------------------------------------------------------------------------
# 模型网关（CLAUDE.md 3.6 / 第 5 章）
# ---------------------------------------------------------------------------
class ModelGatewayError(AgentPlatformError):
    """模型网关调用失败（非超时/限流类）。"""

    code = ErrorCode.MODEL_CALL_FAILED
    http_status = 502


class ModelTimeoutError(ModelGatewayError):
    """模型调用超时（可重试）。"""

    code = ErrorCode.MODEL_TIMEOUT
    retryable = True

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message, detail=detail or "模型响应超时，请稍后重试。")


class ModelRateLimitedError(ModelGatewayError):
    """模型网关限流（429，可重试）。"""

    code = ErrorCode.MODEL_RATE_LIMITED
    http_status = 429
    retryable = True

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message, detail=detail or "请求过于频繁，请稍后重试。")


class ModelAllUnavailableError(ModelGatewayError):
    """主备模型全部不可用（CLAUDE.md 3.6 要求明确告知用户并告警）。"""

    code = ErrorCode.MODEL_ALL_UNAVAILABLE
    http_status = 503

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message, detail=detail or "模型服务当前不可用，请联系管理员。")


# ---------------------------------------------------------------------------
# 知识检索（CLAUDE.md 第 7 章）
# ---------------------------------------------------------------------------
class KnowledgeError(AgentPlatformError):
    """知识后端（RAGFlow / Milvus）调用失败。"""

    code = ErrorCode.KNOWLEDGE_UNAVAILABLE
    http_status = 503


class KnowledgeParseTimeoutError(KnowledgeError):
    """文档解析在预算时间内未完成（扫描件 OCR 慢时出现）。"""

    code = ErrorCode.KNOWLEDGE_PARSE_TIMEOUT
    retryable = True


class KnowledgeParseFailedError(KnowledgeError):
    """文档解析以失败告终（``FAIL``/``CANCEL``），或解析成功但未产出任何可用切分。

    与重试无关：RAGFlow 报 ``FAIL`` 是确定性结果（缺模型、文件损坏等），
    自动重试只会重复同一次失败。**必须显式暴露**，不得当作"空文档"静默记为成功——
    实测踩过：5 份文档全部 ``FAIL``，脚本却报"成功 5 份、切分 0 条"，并把它们记进
    幂等清单，于是重跑全部跳过、问题被永久掩盖。
    """

    code = ErrorCode.KNOWLEDGE_PARSE_FAILED
    retryable = False


class RetrievalFailedError(KnowledgeError):
    """检索链路失败（向量库不可用等）。"""

    code = ErrorCode.RETRIEVAL_FAILED


# ---------------------------------------------------------------------------
# 工具与护栏
# ---------------------------------------------------------------------------
class ToolExecutionError(AgentPlatformError):
    """工具执行失败（对模型返回结构化错误，不抛裸堆栈，CLAUDE.md 6.5）。"""

    code = ErrorCode.TOOL_EXECUTION_FAILED
    http_status = 502


class GuardrailBlockedError(AgentPlatformError):
    """护栏拦截（不得重试，CLAUDE.md 3.6 / 10.3）。"""

    code = ErrorCode.GUARDRAIL_BLOCKED
    http_status = 403

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message, detail=detail or "请求被安全策略拦截。")


# ---------------------------------------------------------------------------
# Problem Details 响应模型（RFC 9457，CLAUDE.md 8.2）
# ---------------------------------------------------------------------------
class ProblemDetail(BaseModel):
    """RFC 9457 Problem Details（``application/problem+json``）。"""

    model_config = ConfigDict(frozen=True)

    type: str = Field(description="错误类型 URI（urn:agent-platform:error:<code>）")
    title: str = Field(description="错误码（稳定标识）")
    status: int = Field(ge=400, le=599, description="HTTP 状态码")
    detail: str = Field(description="面向调用方的可理解说明")
    instance: str | None = Field(default=None, description="触发出错的请求路径")
    code: str = Field(description="业务错误码（等同 title，便于客户端取用）")
    request_id: str | None = Field(default=None, description="请求 ID，便于对账追踪")
