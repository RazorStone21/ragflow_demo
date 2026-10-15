"""统一异常层级与错误码稳定性（CLAUDE.md 3.6 / 8.2）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_platform.core.errors import (
    AgentPlatformError,
    ErrorCode,
    GuardrailBlockedError,
    KnowledgeParseTimeoutError,
    ModelRateLimitedError,
    ModelTimeoutError,
    NotFoundError,
    ProblemDetail,
    UnauthorizedError,
)


def test_error_codes_are_unique() -> None:
    """错误码枚举值不得重复（稳定且不复用语义）。"""
    values = [code.value for code in ErrorCode]
    assert len(values) == len(set(values))


@pytest.mark.parametrize(
    ("exc", "expected_status", "expected_code"),
    [
        (NotFoundError("target missing"), 404, ErrorCode.NOT_FOUND),
        (UnauthorizedError(), 401, ErrorCode.UNAUTHORIZED),
        (ModelTimeoutError("timeout"), 502, ErrorCode.MODEL_TIMEOUT),
        (ModelRateLimitedError("429"), 429, ErrorCode.MODEL_RATE_LIMITED),
        (KnowledgeParseTimeoutError("parse slow"), 503, ErrorCode.KNOWLEDGE_PARSE_TIMEOUT),
        (GuardrailBlockedError("blocked"), 403, ErrorCode.GUARDRAIL_BLOCKED),
    ],
)
def test_problem_details_mapping(
    exc: AgentPlatformError, expected_status: int, expected_code: ErrorCode
) -> None:
    """异常到 Problem Details 的状态码与错误码映射正确。"""
    problem = exc.to_problem_details(request_id="req-1", instance="/api/v1/threads")

    assert isinstance(problem, ProblemDetail)
    assert problem.status == expected_status
    assert problem.code == expected_code.value
    assert problem.title == expected_code.value
    assert problem.type.startswith("urn:agent-platform:error:")
    assert problem.request_id == "req-1"
    assert problem.instance == "/api/v1/threads"


def test_retryable_flags() -> None:
    """可重试标记：超时/429/解析超时可重试；404/403/401 不可重试。"""
    assert ModelTimeoutError("t").retryable is True
    assert ModelRateLimitedError("r").retryable is True
    assert KnowledgeParseTimeoutError("p").retryable is True
    assert NotFoundError("n").retryable is False
    assert GuardrailBlockedError("g").retryable is False
    assert UnauthorizedError().retryable is False


def test_problem_detail_is_frozen() -> None:
    """Problem Details 不可变（CLAUDE.md 3.3 优先不可变 DTO）。"""
    problem = NotFoundError("x").to_problem_details()
    with pytest.raises(ValidationError):
        setattr(problem, "status", 500)  # noqa: B010  # 动态赋值以触发 pydantic frozen 校验


def test_default_detail_is_user_facing() -> None:
    """未指定 detail 时给出用户可理解文案，不泄露内部信息。"""
    problem = ModelTimeoutError("internal detail: db pool exhausted").to_problem_details()
    assert "db pool" not in problem.detail
    assert problem.detail  # 非空
