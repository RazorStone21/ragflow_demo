"""FastAPI 应用装配（CLAUDE.md 2.2：``main.py`` 仅装配，不写业务）。

**导入期零副作用**：本模块在 import 时不读环境、不连库、不建连接池。
配置与资源全部在 ``create_app`` / lifespan 内完成。

因此这里**刻意不写**模块级的 ``app = create_app()``：那会在 import 时就读配置，
缺失必填环境变量会直接变成 ``ImportError``，测试也无法先注入假配置再构造应用。
ASGI 入口改用工厂形态，启动命令为：

    uvicorn --factory agent_platform.main:create_app --host 0.0.0.0 --port 8000

依赖方向也在此定型：``api → agents → core/tools/knowledge → utils``（CLAUDE.md 2.3）。
本模块处于最外层，只做 include 与中间件编排，不含任何业务判断。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from agent_platform import __version__
from agent_platform.api.container import DepsFactory, open_production_deps
from agent_platform.api.middleware.errors import register_exception_handlers
from agent_platform.api.middleware.request_context import RequestContextMiddleware
from agent_platform.api.routes import evidence_images, health, runs, threads
from agent_platform.core.logging import configure_logging, get_logger
from agent_platform.settings import Settings, get_settings

logger = get_logger(__name__)

API_PREFIX = "/api/v1"
"""对外 API 前缀（CLAUDE.md 8.1；探针与文档端点不在其下）。"""


def create_app(
    *, settings: Settings | None = None, deps_factory: DepsFactory = open_production_deps
) -> FastAPI:
    """构造应用实例。

    Args:
        settings: 显式配置；``None`` 时经 ``get_settings()`` 从环境加载。
        deps_factory: 依赖工厂。测试注入内存实现即可完全不触碰 Postgres/Milvus，
            但**仍然走真实的 lifespan**——启停路径本身也是被测对象。

    Returns:
        完成中间件、异常处理器与路由装配的 FastAPI 实例。
    """
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """启动时装配依赖、关停时释放。"""
        configure_logging(level=resolved.log_level)
        logger.info("app.starting", extra={"event": "app.starting", "app_env": resolved.app_env})
        async with deps_factory(resolved) as deps:
            _app.state.deps = deps
            logger.info("app.ready", extra={"event": "app.ready", "agent": deps.agent_name})
            try:
                yield
            finally:
                # 先摘容器再让 async with 释放资源：关停瞬间到达的请求应当拿到
                # "服务正在关停"的 5xx，而不是拿到一个已被拆掉一半的依赖对象。
                del _app.state.deps
                logger.info("app.stopping", extra={"event": "app.stopping"})

    app = FastAPI(
        title="agent-platform",
        version=__version__,
        lifespan=lifespan,
        summary="企业级智能体平台（RAG 主线）",
    )

    # 中间件注册在异常处理器之外：错误响应同样要带 X-Request-Id，
    # 那要求 request_id 在 ExceptionMiddleware 之前就已写好。
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    app.include_router(health.probe_router)  # /healthz、/readyz：不带版本前缀

    api_v1 = APIRouter(prefix=API_PREFIX)
    api_v1.include_router(health.info_router)
    api_v1.include_router(threads.router)
    api_v1.include_router(runs.router)
    api_v1.include_router(evidence_images.router)
    app.include_router(api_v1)

    return app


__all__: list[str] = ["API_PREFIX", "create_app"]
