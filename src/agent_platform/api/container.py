"""应用级依赖容器与生产装配（CLAUDE.md 2.2 / 4.8）。

**模块导入期不建立任何连接**：全部资源在 ``open_production_deps`` 的异步上下文内创建，
由 FastAPI lifespan 驱动（CLAUDE.md 4.8）。这样单测可以整体替换工厂、
完全不触碰 Postgres / Milvus / 网关，同时仍然走真实的启停路径（CLAUDE.md 9.3）。

容器取 frozen dataclass 而非 ``app.dependency_overrides``：图是在 lifespan 里
用真实资源装配出来的**对象**，而 ``dependency_overrides`` 替换的是**可调用对象**——
对已构造好的实例它无能为力，最终还是要找个地方存放。既然如此不如直接做成
一个显式的不可变容器，注入点唯一、类型明确。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore

from agent_platform.agents.gov_policy_qa.graph import build_graph
from agent_platform.agents.gov_policy_qa.state import AGENT_NAME
from agent_platform.core.checkpoint import open_checkpoint_resources
from agent_platform.core.models import ChatModel, build_chat_model
from agent_platform.guardrails import Guardrail
from agent_platform.knowledge.embedder import LiteLLMEmbedder
from agent_platform.knowledge.gateway_client import GatewayHttpClient
from agent_platform.knowledge.images import ImageStore, open_image_store
from agent_platform.knowledge.milvus_store import MilvusStore, open_milvus_store
from agent_platform.knowledge.reranker import LiteLLMReranker
from agent_platform.knowledge.retriever import Retriever
from agent_platform.settings import Settings

_READYZ_PROBE_THREAD = "readyz:readyz:readyz"
"""就绪探针使用的合成会话 ID。

``aget_tuple`` 查不到即返回 ``None``——只要不抛异常就证明 Postgres 可达，
且探针**不会创建任何数据**。
"""


@dataclass(frozen=True)
class ReadinessCheck:
    """一项就绪检查。

    ``name`` 会出现在 ``/readyz`` 的失败清单里，因此只放**依赖的通用名**
    （``postgres`` / ``milvus``），不放主机名、端口、连接串——探针只需要回答
    "哪一类依赖不通"，泄露拓扑细节是 CLAUDE.md 8.2 / 10.1 明令禁止的。
    """

    name: str
    probe: Callable[[], Awaitable[None]]
    """正常返回即视为就绪，抛异常即视为未就绪（由 ``/readyz`` 汇总）。"""


@dataclass(frozen=True)
class AppDeps:
    """应用级依赖（由 lifespan 注入 ``app.state``，仅 ``api/`` 消费）。"""

    settings: Settings
    graph: CompiledStateGraph[Any, Any, Any, Any]
    store: BaseStore
    """检查点同源的长期记忆/幂等存储（``AsyncPostgresStore``）。

    与图共用同一个实例：再开一个 store 连接池只会让"清理/备份/TTL 配置"出现两个来源。
    """
    agent_name: str
    """本进程装配的智能体名；``POST /runs`` 据此拒绝指向未装配智能体的请求。"""
    retriever: Retriever
    milvus_store: MilvusStore
    """向量库句柄本身。

    ``retriever`` 只回答"这个查询命中什么"，回答不了"库里现在有多少切片"；而评测
    需要后者来记录语料快照（重入库会让历史基线不可比，必须在产物里留指纹）。
    单独暴露已打开的实例，而不是在 ``Retriever`` 上加一个转发的 count——那会让
    检索器多一个与检索无关的职责。
    """
    chat: ChatModel
    chat_logical_name: str
    guardrail: Guardrail | None
    image_store: ImageStore
    """证据图片存储（ADR-0004）。取图端点只经此读取，不拼路径、不访对象存储。"""
    readiness_checks: tuple[ReadinessCheck, ...]


DepsFactory = Callable[[Settings], AbstractAsyncContextManager[AppDeps]]
"""依赖工厂：生产用 ``open_production_deps``，测试注入内存实现。"""


@asynccontextmanager
async def open_production_deps(settings: Settings) -> AsyncIterator[AppDeps]:
    """装配生产依赖，并交出容器；退出时按逆序释放全部资源。

    顺序即依赖顺序：网关 → Milvus → 检索器 → 聊天模型 → 检查点 → 图。
    任一环节失败都会经 ``AsyncExitStack`` 回滚已建立的资源，不留半开连接。
    """
    async with AsyncExitStack() as stack:
        gateway = await stack.enter_async_context(GatewayHttpClient(settings))
        # 图片目录在此一次性建好并验证可写：启动时就失败，好过第一次取图才发现。
        image_store = open_image_store(settings)
        await image_store.ensure_ready()
        milvus_store = await stack.enter_async_context(open_milvus_store(settings))
        retriever = Retriever(
            milvus_store,
            LiteLLMEmbedder(
                gateway,
                logical_model=settings.embedding_model,
                expected_dim=settings.milvus_dim,
            ),
            LiteLLMReranker(gateway, logical_model=settings.rerank_model),
            top_k=settings.evidence_top_k,
            candidates=settings.retrieval_candidates,
            similarity_threshold=settings.similarity_threshold,
            rerank_score_threshold=settings.rerank_score_threshold,
            vector_weight=settings.hybrid_vector_weight,
        )
        chat = build_chat_model(settings, logical_name=settings.primary_chat_model)
        resources = await stack.enter_async_context(
            open_checkpoint_resources(
                settings.postgres_dsn.get_secret_value(),
                connect_timeout_s=settings.postgres_connect_timeout_s,
            )
        )
        graph = build_graph(
            retriever=retriever,
            chat=chat,
            chat_logical_name=settings.primary_chat_model,
            checkpointer=resources.saver,
            store=resources.store,
            guardrail=None,  # 放行实现；NeMo Guardrails 接入前不假装有拦截能力
        )

        async def _check_postgres() -> None:
            await resources.saver.aget_tuple({"configurable": {"thread_id": _READYZ_PROBE_THREAD}})

        async def _check_milvus() -> None:
            await milvus_store.count()

        yield AppDeps(
            settings=settings,
            graph=graph,
            store=resources.store,
            agent_name=AGENT_NAME,
            retriever=retriever,
            milvus_store=milvus_store,
            chat=chat,
            chat_logical_name=settings.primary_chat_model,
            guardrail=None,
            image_store=image_store,
            readiness_checks=(
                ReadinessCheck(name="postgres", probe=_check_postgres),
                ReadinessCheck(name="milvus", probe=_check_milvus),
            ),
        )


__all__: list[str] = ["AppDeps", "DepsFactory", "ReadinessCheck", "open_production_deps"]
