"""探针与版本信息 DTO（CLAUDE.md 11.6）。

与业务 DTO 分开建模：探针的消费者是 K8s 与运维脚本，字段要极简且**永不因业务迭代
而变形**——把探针响应和业务响应耦合在一起，是让"业务改字段导致集群开始重启"的经典途径。

``ReadyResponse.failed`` 只放依赖的**通用名**（``postgres`` / ``milvus``），
不放主机名、端口或异常原文：探针要回答的是"哪一类依赖不通"，
而连接串与堆栈属于 CLAUDE.md 8.2 / 10.1 明令不得出境的内部细节。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """``GET /healthz``：进程存活（不检查任何外部依赖）。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = Field(description="存活即 ok；不健康时本端点不会返回")
    version: str = Field(min_length=1, description="平台版本（agent_platform.__version__）")


class ReadyResponse(BaseModel):
    """``GET /readyz``：可接收流量（已逐项检查关键依赖）。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "unavailable"] = Field(description="全部依赖就绪为 ok，否则 unavailable")
    version: str = Field(min_length=1, description="平台版本")
    failed: list[str] = Field(
        default_factory=list, description="未通过的依赖名清单；空列表表示全部通过"
    )


class AgentVersion(BaseModel):
    """单个智能体的版本信息（取自 ``agents/registry.py``）。"""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class VersionResponse(BaseModel):
    """``GET /api/v1/version``：版本与已注册智能体清单。

    刻意**不**返回镜像 tag / git sha：当前没有接入构建期元数据，
    编一个出来只会让排障时更迷惑。待 CI 注入后再补，属于基础设施阶段的事项。
    """

    model_config = ConfigDict(frozen=True)

    version: str = Field(min_length=1, description="平台版本")
    agents: list[AgentVersion] = Field(default_factory=list, description="已注册智能体及版本")


__all__: list[str] = ["AgentVersion", "HealthResponse", "ReadyResponse", "VersionResponse"]
