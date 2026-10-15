"""输入/输出护栏与 PII 脱敏（CLAUDE.md 10.3）。

**演示阶段占位**：NeMo Guardrails + Presidio 的完整实现顺延至阶段二
（见 CHANGELOG v1.2.0）。本包先固定接口契约：

- ``GuardrailDecision``：护栏判定结果（放行/拦截 + 理由 + 稳定错误码）。
- ``Guardrail``：输入检查、输出检查、工具调用前策略校验三个钩子。

阶段二接入要求（CLAUDE.md 10.3）：拦截提示词注入、越权指令、敏感内容、
被污染的检索内容（间接注入）；策略变更须带对抗用例评估并留痕。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from agent_platform.core.errors import ErrorCode


@dataclass(frozen=True)
class GuardrailDecision:
    """护栏判定结果。"""

    allowed: bool
    code: ErrorCode | None = None
    reason: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @classmethod
    def allow(cls) -> GuardrailDecision:
        """放行。"""
        return cls(allowed=True)

    @classmethod
    def block(
        cls, reason: str, *, code: ErrorCode = ErrorCode.GUARDRAIL_BLOCKED
    ) -> GuardrailDecision:
        """拦截并给出稳定错误码与理由。"""
        return cls(allowed=False, code=code, reason=reason)


@runtime_checkable
class Guardrail(Protocol):
    """护栏接口（输入/输出/工具调用三个检查点）。"""

    async def check_input(self, *, text: str, tenant_id: str) -> GuardrailDecision:
        """检查用户输入（注入、越权指令、敏感内容）。"""
        ...

    async def check_output(self, *, text: str, tenant_id: str) -> GuardrailDecision:
        """检查模型输出（泄露、违规内容）。"""
        ...

    async def check_tool_call(
        self, *, tool_name: str, arguments: dict[str, object]
    ) -> GuardrailDecision:
        """工具调用前策略校验（最小权限、高危动作）。"""
        ...


class PassThroughGuardrail:
    """演示阶段放行实现（**不做任何拦截**，仅保证调用链完整）。"""

    async def check_input(self, *, text: str, tenant_id: str) -> GuardrailDecision:
        """放行所有输入（占位）。"""
        del text, tenant_id
        return GuardrailDecision.allow()

    async def check_output(self, *, text: str, tenant_id: str) -> GuardrailDecision:
        """放行所有输出（占位）。"""
        del text, tenant_id
        return GuardrailDecision.allow()

    async def check_tool_call(
        self, *, tool_name: str, arguments: dict[str, object]
    ) -> GuardrailDecision:
        """放行所有工具调用（占位）。"""
        del tool_name, arguments
        return GuardrailDecision.allow()


__all__: list[str] = ["Guardrail", "GuardrailDecision", "PassThroughGuardrail"]
