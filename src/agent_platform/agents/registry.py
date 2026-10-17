"""智能体注册表（CLAUDE.md 4.6 / 4.8）。

每个已交付的智能体在此声明：名称、负责人、版本、可见工具（最小权限白名单）、
权限与护栏等级，以及**与 ``langgraph.json`` 完全一致的入口路径**。注册表是
API 层与运维查询的权威清单；``tests/unit/test_agent_registry.py`` 锁定它与
``langgraph.json``、模块路径三者的一致性，避免"注册了但跑不起来"。

新增智能体时必须同步：① 本节新增 ``AgentSpec``；② ``langgraph.json`` 的
``graphs`` 增加同名条目；③ 更新 ``CLAUDE.md`` 与 ``CHANGELOG.md``（NO-20）。
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from agent_platform.core.errors import NotFoundError, ValidationFailedError

GUARDRAIL_LEVEL_STANDARD = "standard"
"""护栏等级：standard = 输入/输出经基础护栏；high = 额外人工确认（HITL）。"""


class AgentSpec(BaseModel):
    """单个智能体的注册信息（不可变）。"""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, description="智能体名（= langgraph.json 中 graphs 的键）")
    owner: str = Field(min_length=1, description="负责人/责任团队")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", description="语义化版本")
    entrypoint: str = Field(
        min_length=1,
        description="langgraph.json 使用的相对路径入口，形如 ./src/.../graph.py:build_graph",
    )
    tools: tuple[str, ...] = Field(
        default=(), description="可见工具白名单（最小权限，CLAUDE.md 6.7 / NO-11）"
    )
    guardrail_level: str = Field(default=GUARDRAIL_LEVEL_STANDARD, description="护栏等级")
    description: str = Field(default="", description="用途说明")

    @property
    def import_ref(self) -> str:
        """返回 importlib 可加载的 ``模块:属性`` 引用。

        ``entrypoint`` 与 ``langgraph.json`` 逐字一致（CLI 惯用 ``./`` 前缀，
        CLAUDE.md 附录示例同形），但 importlib 不接受该前缀，故在此统一剥掉；
        不带 ``./`` 的写法同样接受，避免本属性成为两处声明的隐性耦合点。
        """
        module_path, _, attribute = self.entrypoint.partition(":")
        trimmed = module_path.removeprefix("./").removeprefix("src/").removesuffix(".py")
        return f"{trimmed.replace('/', '.')}:{attribute}"

    def load_factory(self) -> Callable[..., Any]:
        """导入并返回该智能体的 ``build_graph`` 工厂（不在此处调用/编译）。

        Raises:
            ValidationFailedError: 入口路径格式非法（缺 ``:``）。
            NotFoundError: 目标模块或属性不存在（部署包不完整）。
        """
        module_name, sep, attribute = self.import_ref.partition(":")
        if not sep or not attribute:
            raise ValidationFailedError(
                f"智能体入口格式非法（应为 路径.py:函数）: {self.entrypoint}"
            )
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise NotFoundError(f"智能体模块不可导入: {module_name}") from exc
        factory: object = getattr(module, attribute, None)
        if not callable(factory):
            raise NotFoundError(f"智能体入口不可调用: {self.entrypoint}")
        return cast(Callable[..., Any], factory)


AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        name="gov_policy_qa",
        owner="平台架构组",
        version="0.2.0",
        entrypoint="./src/agent_platform/agents/gov_policy_qa/graph.py:build_graph",
        tools=(),  # 纯检索问答：不挂任何工具，最小权限（NO-11）
        guardrail_level=GUARDRAIL_LEVEL_STANDARD,
        description="政务能源政策问答：Milvus 混合检索 + 重排，带引用作答，无证据不编造",
    ),
)
"""已注册智能体（顺序即展示顺序）。"""

_BY_NAME: dict[str, AgentSpec] = {spec.name: spec for spec in AGENTS}


def agent_names() -> tuple[str, ...]:
    """返回全部已注册智能体名（与 ``langgraph.json`` 的键一致）。"""
    return tuple(spec.name for spec in AGENTS)


def get_agent(name: str) -> AgentSpec:
    """按名称取注册信息。

    Raises:
        NotFoundError: 名称未注册（映射 404，不泄露内部清单）。
    """
    spec = _BY_NAME.get(name)
    if spec is None:
        raise NotFoundError(f"未注册的智能体: {name}")
    return spec
