"""共享测试夹具（CLAUDE.md 9.3：单测不依赖外部服务，模型/网关/库用 fake）。

约定：
- 单测一律使用 ``fake_settings``，不得读取真实 .env；
- 需要真实基础设施的用例放 ``tests/integration/`` 并用 ``@pytest.mark.integration``。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import SecretStr

from agent_platform.agents.gov_policy_qa.graph import build_graph
from agent_platform.agents.gov_policy_qa.state import AGENT_NAME
from agent_platform.api.container import AppDeps, ReadinessCheck
from agent_platform.knowledge.retriever import REASON_NO_CANDIDATES, REASON_SUFFICIENT
from agent_platform.knowledge.schemas import EvidenceItem, RetrievalMeta, RetrievalOutcome
from agent_platform.main import API_PREFIX, create_app
from agent_platform.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from langchain_core.messages import UsageMetadata
    from langgraph.graph.state import CompiledStateGraph
    from langgraph.store.base import BaseStore

#: 会被测试污染/干扰的进程环境变量（构造 Settings 前清理）
_ENV_VARS_TO_CLEAR = (
    "APP_ENV",
    "AGENT_API_BEARER_TOKEN",
    "POSTGRES_DSN",
    "LITELLM_API_KEY",
    "RAGFLOW_API_KEY",
    "MILVUS_DIM",
    "PRIMARY_CHAT_MODEL",
    "RUN_INTEGRATION_TESTS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认清除可能影响配置解析的环境变量，保证测试确定性。"""
    for name in _ENV_VARS_TO_CLEAR:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake_settings() -> Settings:
    """构造一份合法的测试配置（不读 .env、不读环境变量）。"""
    # _env_file 由 pydantic-settings 在运行时注入，不在模型字段签名中
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        agent_api_bearer_token=SecretStr("test-bearer-token"),
        postgres_dsn=SecretStr("postgresql://tester:secret@localhost:5432/test_db"),
        litellm_api_key=SecretStr("test-litellm-key"),
        ragflow_api_key=SecretStr("test-ragflow-key"),
    )


@pytest.fixture
def anyio_backend() -> str:
    """pytest-asyncio 场景下的后端占位（保持与 anyio 生态兼容）。"""
    return "asyncio"


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]] | None:
    """Windows 上改用 Selector 事件循环，否则 psycopg 异步连接直接拒绝工作。

    psycopg 的异步实现不支持 Windows 默认的 ``ProactorEventLoop``，会抛
    ``InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode``。
    Linux/容器（生产目标）默认就是 Selector，因此这里只对 Windows 生效，其余平台返回
    ``None`` 走 pytest-asyncio 的默认行为。

    用本 hook 而不是覆盖 ``event_loop_policy`` 夹具：后者在 pytest-asyncio 1.4 已废弃。
    之所以不是"设一次全局 policy"，是因为事件循环由 ``loop_factory`` 直接造出来，
    全局 policy 会被绕过（uvicorn 0.54 同样是这个原因，见 README 的 ``--loop`` 说明）。
    """
    if sys.platform != "win32":
        return None
    return {"win32-selector": asyncio.SelectorEventLoop}


class FakeChatModel:
    """满足 ``ChatModel`` 协议的最小 fake（固定文本，可注入异常，支持流式）。

    Args:
        reply: ``ainvoke`` 返回的整段文本，也是 ``astream`` 默认切块的原文。
        chunks: 自定义流式分块；``None`` 时按句号切分 ``reply``。
        raises: ``ainvoke``/``astream`` 抛出的异常（模拟网关故障）。
        usage: 末块携带的 token 用量（模拟网关返回）。
    """

    def __init__(
        self,
        reply: str = "测试回答",
        *,
        chunks: list[str] | None = None,
        raises: BaseException | None = None,
        usage: UsageMetadata | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.reply = reply
        self.chunks = chunks
        self.raises = raises
        self.usage = usage
        self.delay_s = delay_s
        self.calls: list[Any] = []
        self.stream_calls: int = 0

    # LangChain Runnable 协议规定入参名为 input，此处必须同名（同 core/models.py）
    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:  # noqa: A002
        """记录调用并返回固定 AIMessage（或抛出预设异常）。"""
        from langchain_core.messages import AIMessage

        self.calls.append({"input": input, "config": config, "kwargs": kwargs})
        if self.raises is not None:
            raise self.raises
        return AIMessage(content=self.reply)

    async def astream(
        self,
        input: Any,  # noqa: A002  # LangChain Runnable 协议规定入参名，同 ainvoke
        config: Any = None,
        **kwargs: Any,
    ) -> Any:
        """按分块产出 ``AIMessageChunk``（模拟逐 token 流）。"""
        from langchain_core.messages import AIMessageChunk

        self.stream_calls += 1
        if self.raises is not None:
            raise self.raises
        parts = self.chunks if self.chunks is not None else [self.reply]
        for index, part in enumerate(parts):
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            is_last = index == len(parts) - 1
            yield AIMessageChunk(content=part, usage_metadata=self.usage if is_last else None)


# ---------------------------------------------------------------------------
# RAG 场景的共享 fake 与构造器（单元测试用，全部离线且确定）
# ---------------------------------------------------------------------------

TEST_QUESTION = "新能源电价机制如何确定？"
"""测试通用问题（与 ``tests/unit/test_agent_graph.py`` 同源，便于对照）。"""

CITED_REPLY = "依据《电价办法》第3条[1]。"
"""带合法引用的默认模型回答。

必须带 ``[1]``：``generate_answer`` 在输出边界做**确定性**引用校验，
不带引用的文本会被判为 ``citation_missing`` 并整体替换为兜底文案（NO-12）。
默认 fake 若用无引用文本，所有"正常路径"用例都会悄悄跑在兜底分支上。
"""


class FakeRetriever:
    """满足 ``Retriever`` 契约的确定性检索器。

    Args:
        outcome: 首个检索结果。
        more: 后续调用的结果（逐个消费）；不传则每次返回 ``outcome``。
        raises: 抛出的异常（模拟向量库/嵌入服务故障）。
        delay_s: 每次检索前的等待时长（用于心跳等时序用例）。
    """

    def __init__(
        self,
        outcome: RetrievalOutcome,
        *more: RetrievalOutcome,
        raises: BaseException | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.outcomes = [outcome, *more]
        self.raises = raises
        self.delay_s = delay_s
        self.calls: list[tuple[str, str]] = []

    async def search(
        self, query: str, *, tenant_id: str, dataset_id: str | None = None
    ) -> RetrievalOutcome:
        """记录 ``(问题, 租户)`` 后返回预设结果（或抛出预设异常）。"""
        del dataset_id
        self.calls.append((query, tenant_id))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.raises is not None:
            raise self.raises
        if len(self.outcomes) > 1:
            return self.outcomes.pop(0)
        return self.outcomes[0]


def make_evidence(chunk_id: str, score: float = 0.9) -> EvidenceItem:
    """构造一条带完整引用信息的证据（CLAUDE.md 7.4：无引用不得作为事实结论）。"""
    return EvidenceItem(
        chunk_id=chunk_id,
        content=f"第 {chunk_id} 段：居民电价按年度核定并公示。",
        score=score,
        document_name="新能源电价政策.pdf",
        page_numbers=[3],
        source_uri="gov_doc/新能源电价政策.pdf",
    )


def sufficient_outcome(*chunk_ids: str) -> RetrievalOutcome:
    """构造"证据充足"的检索结果（锚定 ``generate_answer`` 分支）。"""
    evidence = [make_evidence(chunk_id) for chunk_id in (chunk_ids or ("c1",))]
    return RetrievalOutcome(
        evidence=evidence,
        meta=RetrievalMeta(
            query=TEST_QUESTION,
            candidate_count=len(evidence),
            evidence_count=len(evidence),
            top_score=evidence[0].score,
            score_threshold=0.3,
            is_sufficient=True,
            reason=REASON_SUFFICIENT,
            rerank_applied=False,
        ),
    )


def insufficient_outcome() -> RetrievalOutcome:
    """构造"无证据"的检索结果（降级分支，非异常，NO-12）。"""
    return RetrievalOutcome(
        evidence=[],
        meta=RetrievalMeta(
            query=TEST_QUESTION,
            candidate_count=0,
            evidence_count=0,
            top_score=None,
            score_threshold=0.3,
            is_sufficient=False,
            reason=REASON_NO_CANDIDATES,
            rerank_applied=False,
        ),
    )


# ---------------------------------------------------------------------------
# API 层装配（CLAUDE.md 9.3：单测用内存实现走真实启停路径，不触碰外部服务）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApiStack:
    """一套内存版应用装配。

    ``store`` 用内存实现而非 Postgres：单测不依赖外部服务（CLAUDE.md 9.3）；
    幂等命名空间与租户隔离的**语义**仍逐字复用生产实现（``api/idempotency.py``）。
    """

    settings: Settings
    graph: CompiledStateGraph[Any, Any, Any, Any]
    store: BaseStore
    retriever: Any
    """注入的检索器（fake）；类型为 Any 是因为 ``AppDeps`` 声明的是具体 ``Retriever``。"""
    chat: Any
    """注入的模型（fake）；同上。"""
    agent_name: str
    readiness_checks: tuple[ReadinessCheck, ...]


def build_api_stack(
    settings: Settings,
    *,
    retriever: Any = None,
    chat: Any = None,
    readiness_checks: tuple[ReadinessCheck, ...] | None = None,
    checkpointer: Any = None,
    store: Any = None,
) -> ApiStack:
    """装配一套依赖：默认内存 checkpointer + 内存 store + fake 检索器/模型。

    ``InMemorySaver`` 仅供单测（NO-08：生产长流程必须 PostgresSaver）。集成测试
    经 ``checkpointer`` / ``store`` 传入 ``open_checkpoint_resources`` 的真实资源，
    其余装配逐字复用，保证"集成验证的就是生产那条装配路径"。
    """
    resolved_retriever = retriever if retriever is not None else FakeRetriever(sufficient_outcome())
    resolved_chat = chat if chat is not None else FakeChatModel(CITED_REPLY)
    resolved_store = store if store is not None else InMemoryStore()
    resolved_saver = checkpointer if checkpointer is not None else InMemorySaver()
    graph = build_graph(
        retriever=resolved_retriever,  # type: ignore[arg-type]  # 注入 fake（CLAUDE.md 9.3）
        chat=resolved_chat,
        chat_logical_name=settings.primary_chat_model,
        checkpointer=resolved_saver,
        store=resolved_store,
        guardrail=None,
    )
    if readiness_checks is None:
        readiness_checks = (ReadinessCheck(name="postgres", probe=_always_ready),)
    return ApiStack(
        settings=settings,
        graph=graph,
        store=resolved_store,
        retriever=resolved_retriever,
        chat=resolved_chat,
        agent_name=AGENT_NAME,
        readiness_checks=readiness_checks,
    )


async def _always_ready() -> None:
    """恒通过的就绪探针（默认值）。"""
    return


def build_test_app(stack: ApiStack) -> FastAPI:
    """用给定装配构造 ASGI 应用（走真实 ``create_app`` 与 lifespan）。"""

    @asynccontextmanager
    async def _deps(_settings: Settings) -> AsyncIterator[AppDeps]:
        # 忽略传入的 settings：容器里已持有同一份，避免出现两个配置来源
        del _settings
        yield AppDeps(
            settings=stack.settings,
            graph=stack.graph,
            store=stack.store,
            agent_name=stack.agent_name,
            retriever=stack.retriever,
            chat=stack.chat,
            chat_logical_name=stack.settings.primary_chat_model,
            guardrail=None,
            readiness_checks=stack.readiness_checks,
        )

    return create_app(settings=stack.settings, deps_factory=_deps)


@dataclass
class ApiHarness:
    """已启动的测试客户端与其背后的装配。"""

    client: httpx.AsyncClient
    stack: ApiStack
    app: FastAPI

    @property
    def auth_headers(self) -> dict[str, str]:
        """合法 Bearer 头（租户由服务端配置解析，不由前端传入，NO-19）。"""
        return {
            "Authorization": f"Bearer {self.stack.settings.agent_api_bearer_token.get_secret_value()}"
        }

    async def create_thread(self) -> str:
        """经 API 创建一个真实会话（走 POST /threads + 鉴权），返回 thread_id。"""
        response = await self.client.post(f"{API_PREFIX}/threads", headers=self.auth_headers)
        assert response.status_code == 201, response.text
        return str(response.json()["thread_id"])


OpenApi = Callable[..., AbstractAsyncContextManager["ApiHarness"]]
"""``api_factory`` 返回的开启器签名（供测试文件标注用）。"""


@pytest.fixture
def api_factory(fake_settings: Settings) -> OpenApi:
    """返回一个开启器：``async with api_factory(...) as api:``。

    走真实 lifespan（``app.router.lifespan_context``）——``httpx.ASGITransport``
    **不会**自动运行 lifespan，不显式进入就会得到 `/readyz` 500（deps 未注入）。

    传输层固定 ``raise_app_exceptions=False``：Starlette 的 ``ServerErrorMiddleware``
    在调用 500 处理器之后**仍会重新抛出**异常，为 True 时断言到的是异常而非响应。
    """

    @asynccontextmanager
    async def _open(
        *,
        settings: Settings | None = None,
        retriever: Any = None,
        chat: Any = None,
        readiness_checks: tuple[ReadinessCheck, ...] | None = None,
        checkpointer: Any = None,
        store: Any = None,
    ) -> AsyncIterator[ApiHarness]:
        resolved = settings if settings is not None else fake_settings
        stack = build_api_stack(
            resolved,
            retriever=retriever,
            chat=chat,
            readiness_checks=readiness_checks,
            checkpointer=checkpointer,
            store=store,
        )
        app = build_test_app(stack)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://testserver", timeout=30.0
            ) as client,
        ):
            yield ApiHarness(client=client, stack=stack, app=app)

    return _open


@pytest.fixture
async def api(api_factory: OpenApi) -> AsyncIterator[ApiHarness]:
    """默认装配的已启动客户端（多数用例只需这一个）。"""
    async with api_factory() as harness:
        yield harness
