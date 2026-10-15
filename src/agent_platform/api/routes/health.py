"""存活/就绪探针与版本信息（CLAUDE.md 11.6）。

**两个路由器，两种挂载位置**：探针挂在根路径（``/healthz``、``/readyz``），
版本信息挂在 ``/api/v1`` 下。K8s 的 probe 路径是集群配置的一部分，
把它挪到带版本前缀的路径下，会让"API 升版本"顺手改掉集群健康检查——
那不是 API 演进该有的副作用。

探针**不鉴权**：kubelet 不持有业务凭据，而探针只回答通/不通，不含任何业务数据。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from starlette.responses import JSONResponse

from agent_platform import __version__
from agent_platform.agents.registry import AGENTS
from agent_platform.api.deps import DepsDep
from agent_platform.api.schemas.health import (
    AgentVersion,
    HealthResponse,
    ReadyResponse,
    VersionResponse,
)
from agent_platform.core.logging import get_logger

logger = get_logger(__name__)

probe_router = APIRouter(tags=["health"])
"""根路径探针（不鉴权、不带版本前缀）。"""

info_router = APIRouter(tags=["health"])
"""``/api/v1`` 下的版本信息。"""


@probe_router.get("/healthz", response_model=HealthResponse, summary="存活探针")
async def healthz() -> HealthResponse:
    """进程存活检查。

    刻意**不**检查 Postgres/Milvus：存活探针一旦检查外部依赖，
    依赖抖动就会触发容器重启，而重启既修不好依赖、又荡平了正在跑的 run。
    "依赖是否可用"是 ``/readyz`` 的职责。
    """
    return HealthResponse(status="ok", version=__version__)


@probe_router.get(
    "/readyz",
    response_model=ReadyResponse,
    summary="就绪探针",
    responses={503: {"model": ReadyResponse}},
)
async def readyz(deps: DepsDep) -> JSONResponse:
    """就绪检查：逐项探测关键依赖，全部通过才返回 200。"""
    failed: list[str] = []
    for check in deps.readiness_checks:
        try:
            async with asyncio.timeout(deps.settings.readyz_timeout_s):
                await check.probe()
        except TimeoutError:
            failed.append(check.name)
            logger.warning(
                "readyz.timeout",
                extra={"event": "readyz.timeout", "dependency": check.name},
            )
        except Exception as exc:
            # 这不是"吞异常"：就绪探针的本职工作就是把"依赖不可用"翻译成状态码
            # （CLAUDE.md 11.6）。异常按类型记入日志，响应体只出依赖名，
            # 堆栈与连接串一律不出境（CLAUDE.md 8.2 / 10.1）。
            failed.append(check.name)
            logger.warning(
                "readyz.failed",
                extra={
                    "event": "readyz.failed",
                    "dependency": check.name,
                    "error": type(exc).__name__,
                },
            )
    payload = ReadyResponse(
        status="unavailable" if failed else "ok", version=__version__, failed=failed
    )
    return JSONResponse(status_code=503 if failed else 200, content=payload.model_dump(mode="json"))


@info_router.get("/version", response_model=VersionResponse, summary="版本信息")
async def version() -> VersionResponse:
    """返回平台版本与已注册智能体清单。"""
    return VersionResponse(
        version=__version__,
        agents=[AgentVersion(name=spec.name, version=spec.version) for spec in AGENTS],
    )


__all__: list[str] = ["info_router", "probe_router"]
