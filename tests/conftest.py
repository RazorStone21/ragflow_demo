"""共享测试夹具（CLAUDE.md 9.3：单测不依赖外部服务，模型/网关/库用 fake）。

约定：
- 单测一律使用 ``fake_settings``，不得读取真实 .env；
- 需要真实基础设施的用例放 ``tests/integration/`` 并用 ``@pytest.mark.integration``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

if TYPE_CHECKING:
    from langchain_core.messages import UsageMetadata

from agent_platform.settings import Settings

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
    ) -> None:
        self.reply = reply
        self.chunks = chunks
        self.raises = raises
        self.usage = usage
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
            is_last = index == len(parts) - 1
            yield AIMessageChunk(content=part, usage_metadata=self.usage if is_last else None)
