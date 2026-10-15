"""追踪埋点接口（CLAUDE.md 9.1 / NO-15）。

**演示阶段为占位实现**：Langfuse 自托管与 OpenTelemetry 接入顺延至阶段二
（见 ADR-0002 后续事项与 CHANGELOG v1.2.0 说明）。本模块先固定接口，
保证业务代码现在就有埋点调用点，阶段二替换实现即可，不改调用方。

规则提醒（阶段二落地时）：
- 默认自托管 Langfuse，禁止数据出域到 SaaS（NO-15）。
- 必须埋点：节点轨迹、模型调用（逻辑名/prompt 版本/token/耗时）、工具调用、
  检索命中、护栏拦截、HITL 决定、重试与 fallback。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_platform.core.logging import get_logger
from agent_platform.settings import Settings

_logger = get_logger(__name__)


@runtime_checkable
class TracingHook(Protocol):
    """追踪埋点最小接口（占位期为空操作，阶段二接入 Langfuse/OTel）。"""

    def record_model_call(
        self,
        *,
        logical_name: str,
        prompt_version: str | None,
        duration_ms: float,
        token_usage: dict[str, int] | None,
        agent: str | None = None,
    ) -> None:
        """记录一次模型调用（逻辑名、prompt 版本、耗时、token）。"""
        ...

    def record_retrieval(
        self,
        *,
        query: str,
        hit_count: int,
        top_score: float | None,
        tenant_id: str | None = None,
    ) -> None:
        """记录一次检索（命中数与最高分）。"""
        ...


class NoOpTracingHook:
    """空操作实现：仅输出 DEBUG 级结构化日志，不发送任何外部数据。"""

    def record_model_call(
        self,
        *,
        logical_name: str,
        prompt_version: str | None,
        duration_ms: float,
        token_usage: dict[str, int] | None,
        agent: str | None = None,
    ) -> None:
        """记录模型调用（占位期仅 DEBUG 日志）。"""
        _logger.debug(
            "model call",
            extra={
                "event": "model.call",
                "model": logical_name,
                "prompt_version": prompt_version,
                "duration_ms": round(duration_ms, 1),
                "token_usage": token_usage,
                "agent": agent,
            },
        )

    def record_retrieval(
        self,
        *,
        query: str,
        hit_count: int,
        top_score: float | None,
        tenant_id: str | None = None,
    ) -> None:
        """记录检索（占位期仅 DEBUG 日志，不记录 query 原文以免 PII 外泄）。"""
        _logger.debug(
            "retrieval",
            extra={
                "event": "retrieval.search",
                "hit_count": hit_count,
                "top_score": top_score,
                "tenant_id": tenant_id,
            },
        )


def build_tracing_hook(settings: Settings) -> TracingHook:
    """构造追踪钩子。

    演示阶段恒返回 ``NoOpTracingHook``；阶段二按 ``settings`` 中的 Langfuse
    配置返回真实实现（保持接口不变）。
    """
    del settings  # 阶段二将用于读取 Langfuse 配置
    return NoOpTracingHook()


__all__: list[str] = ["NoOpTracingHook", "TracingHook", "build_tracing_hook"]
