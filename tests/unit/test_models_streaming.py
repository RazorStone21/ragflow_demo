"""聊天模型流式契约：AIMessageChunk 透传与显式失败（CLAUDE.md 4.7 / 5.1 / NO-02）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from agent_platform.core.errors import ModelGatewayError, ModelTimeoutError
from agent_platform.core.models import ChatModel, _RunnableChatModelAdapter, build_chat_model
from agent_platform.settings import Settings


def _settings(*, fallback_chat_model: str = "") -> Settings:
    """内联等价于 conftest.fake_settings 的配置构造（_env_file=None，不读 .env）。"""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]  # pydantic-settings 运行时参数，不在字段签名中
        agent_api_bearer_token=SecretStr("test-bearer-token"),
        postgres_dsn=SecretStr("postgresql://tester:secret@localhost:5432/test_db"),
        litellm_api_key=SecretStr("test-litellm-key"),
        ragflow_api_key=SecretStr("test-ragflow-key"),
        litellm_base_url="http://gateway.test",
        primary_chat_model="primary-chat",
        fallback_chat_model=fallback_chat_model,
    )


class FakeRunnable:
    """假 LangChain Runnable：按构造参数产出结果/分块或抛出异常，并记录收到的调用参数。

    Args:
        result: ``ainvoke`` 的返回值（可为任意类型，用于构造契约破坏场景）。
        chunks: ``astream`` 逐块产出的对象（故意用 ``Any`` 承载任意类型）。
        raises: ``ainvoke``/``astream`` 被驱动时抛出的异常（模拟网关故障）。
    """

    def __init__(
        self,
        *,
        result: object = None,
        chunks: list[Any] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.result = result
        self.chunks: list[Any] = chunks if chunks is not None else []
        self.raises = raises
        self.invoke_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    # LangChain Runnable 协议规定入参名为 input，此处必须同名（与 core/models.py 一致）
    async def ainvoke(
        self,
        input: Any,  # noqa: A002  # LangChain Runnable 协议要求参数名为 input（关键字调用兼容）
        config: Any = None,
        **kwargs: Any,
    ) -> Any:
        """记录调用参数并返回预设结果（或抛出预设异常）。"""
        self.invoke_calls.append({"input": input, "config": config, "kwargs": kwargs})
        if self.raises is not None:
            raise self.raises
        return self.result

    async def astream(
        self,
        input: Any,  # noqa: A002  # 同上：与 LangChain Runnable 协议签名保持一致
        config: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """记录调用参数并逐块产出预设分块（或抛出预设异常）。"""
        self.stream_calls.append({"input": input, "config": config, "kwargs": kwargs})
        if self.raises is not None:
            raise self.raises
        for chunk in self.chunks:
            yield chunk


def _adapter(runnable: FakeRunnable) -> _RunnableChatModelAdapter:
    """用内联 fake 构造适配器（cast 满足 Runnable 鸭子类型，不依赖真 Runnable 实现）。"""
    return _RunnableChatModelAdapter(cast(Runnable[Any, Any], runnable))


class _InvokeOnlyModel:
    """只实现 ainvoke 的模型：用于证明协议同时要求 astream。"""

    async def ainvoke(
        self,
        input: Any,  # noqa: A002  # 同上：与 LangChain Runnable 协议签名保持一致
        config: Any = None,
        **kwargs: Any,
    ) -> AIMessage:
        """占位实现（不被调用）。"""
        return AIMessage(content="")


class _StreamOnlyModel:
    """只实现 astream 的模型：用于证明协议同时要求 ainvoke。"""

    async def astream(
        self,
        input: Any,  # noqa: A002  # 同上：与 LangChain Runnable 协议签名保持一致
        config: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[AIMessageChunk]:
        """占位实现（不被调用）。"""
        yield AIMessageChunk(content="")


async def test_astream_yields_chunks_unchanged_in_order() -> None:
    """流式透传：逐块、同序、同数且为同一对象，适配器不重组内容（CLAUDE.md 4.7）。"""
    chunks = [
        AIMessageChunk(content="新能源"),
        AIMessageChunk(content="电价"),
        AIMessageChunk(content="机制"),
    ]
    model = _adapter(FakeRunnable(chunks=list(chunks)))
    produced = [chunk async for chunk in model.astream("问题")]

    assert len(produced) == len(chunks)
    assert all(out is src for out, src in zip(produced, chunks, strict=True))
    assert [chunk.content for chunk in produced] == ["新能源", "电价", "机制"]


async def test_astream_empty_stream_yields_nothing() -> None:
    """底层零分块时适配器正常结束、零产出（空流不是契约破坏）。"""
    model = _adapter(FakeRunnable(chunks=[]))

    assert [chunk async for chunk in model.astream("问题")] == []


@pytest.mark.parametrize(
    ("bad_chunk", "type_name"),
    [
        (AIMessage(content="整段回答而非流式"), "AIMessage"),
        ({"delta": "普通字典"}, "dict"),
        ("裸字符串", "str"),
    ],
)
async def test_astream_rejects_non_chunk_with_type_name(bad_chunk: object, type_name: str) -> None:
    """底层产出非 AIMessageChunk 时抛 TypeError 且信息含实际类型名，不静默丢弃。"""
    model = _adapter(FakeRunnable(chunks=[bad_chunk]))

    with pytest.raises(TypeError) as excinfo:
        async for _ in model.astream("问题"):
            pass

    assert type_name in str(excinfo.value)


@pytest.mark.parametrize(
    "error",
    [ModelGatewayError("网关不可用"), ModelTimeoutError("模型超时"), RuntimeError("底层崩溃")],
)
async def test_astream_propagates_underlying_error_unchanged(error: Exception) -> None:
    """底层 astream 抛出的异常以同一对象原样冒泡，不被吞掉或改写（NO-06）。"""
    model = _adapter(FakeRunnable(raises=error))

    with pytest.raises(type(error)) as excinfo:
        async for _ in model.astream("问题"):
            pass

    assert excinfo.value is error


async def test_ainvoke_returns_aimessage_unchanged() -> None:
    """非流式调用原样返回底层 AIMessage 对象，不重建、不包装。"""
    message = AIMessage(content="回答", id="msg-answer")
    model = _adapter(FakeRunnable(result=message))

    assert await model.ainvoke("问题") is message


@pytest.mark.parametrize(
    ("bad_result", "type_name"),
    [({"content": "字典"}, "dict"), ("裸字符串", "str")],
)
async def test_ainvoke_rejects_non_aimessage_with_type_name(
    bad_result: object, type_name: str
) -> None:
    """底层 ainvoke 返回非 AIMessage 时抛 TypeError 且信息含实际类型名。"""
    model = _adapter(FakeRunnable(result=bad_result))

    with pytest.raises(TypeError) as excinfo:
        await model.ainvoke("问题")

    assert type_name in str(excinfo.value)


async def test_config_and_kwargs_are_forwarded_to_underlying_runnable() -> None:
    """config 与额外 kwargs 原样透传到底层 runnable 的 ainvoke/astream（不静默丢弃参数）。"""
    fake = FakeRunnable(result=AIMessage(content="回答"), chunks=[AIMessageChunk(content="回答")])
    model = _adapter(fake)
    config: RunnableConfig = {"tags": ["stream-contract"]}

    await model.ainvoke("问题", config=config, max_tokens=64)
    produced = [chunk async for chunk in model.astream("问题", config=config, temperature=0.5)]

    assert len(produced) == 1
    assert fake.invoke_calls[0]["input"] == "问题"
    assert fake.invoke_calls[0]["config"] is config
    assert fake.invoke_calls[0]["kwargs"] == {"max_tokens": 64}
    assert fake.stream_calls[0]["input"] == "问题"
    assert fake.stream_calls[0]["config"] is config
    assert fake.stream_calls[0]["kwargs"] == {"temperature": 0.5}


def test_adapter_instance_satisfies_chat_model_protocol() -> None:
    """适配器实例通过运行时协议检查 isinstance(obj, ChatModel)（对节点侧透明）。"""
    assert isinstance(_adapter(FakeRunnable()), ChatModel)


@pytest.mark.parametrize("model", [_InvokeOnlyModel(), _StreamOnlyModel()])
def test_chat_model_protocol_requires_ainvoke_and_astream(model: object) -> None:
    """协议把 ainvoke 与 astream 同时列为必需成员，只实现其一的类不满足。"""
    assert hasattr(ChatModel, "ainvoke")
    assert hasattr(ChatModel, "astream")
    assert not isinstance(model, ChatModel)


def test_build_chat_model_without_fallback_satisfies_protocol() -> None:
    """未配置备选模型时直接返回网关客户端，满足协议且不包多余适配器。"""
    model = build_chat_model(_settings())

    assert isinstance(model, ChatModel)
    assert not isinstance(model, _RunnableChatModelAdapter)


def test_build_chat_model_with_fallback_satisfies_protocol() -> None:
    """配置了不同的备选模型时返回主备链适配器，同样满足 ChatModel 协议。"""
    model = build_chat_model(_settings(fallback_chat_model="fallback-chat"))

    assert isinstance(model, _RunnableChatModelAdapter)
    assert isinstance(model, ChatModel)


def test_build_chat_model_can_refuse_the_fallback_chain() -> None:
    """``use_fallback=False`` 时即使配置了备选也返回主模型。

    裁判必须走这条路：换裁判型号等于换测量工具，不能由一次网关抖动悄悄换成
    另一个模型来判分（CLAUDE.md 9.2 固定裁判版本）。
    """
    model = build_chat_model(_settings(fallback_chat_model="fallback-chat"), use_fallback=False)

    assert isinstance(model, ChatModel)
    assert not isinstance(model, _RunnableChatModelAdapter)


def test_build_chat_model_ignores_fallback_equal_to_primary() -> None:
    """备选与主模型同名时不构造自我 fallback 链，直接返回主模型。"""
    model = build_chat_model(_settings(fallback_chat_model="primary-chat"))

    assert isinstance(model, ChatModel)
    assert not isinstance(model, _RunnableChatModelAdapter)


def test_build_chat_model_passes_output_constraints_to_upstream() -> None:
    """JSON 模式等输出约束必须进 ``model_kwargs``，否则请求不会带上它。

    引用裁判用它把"模型偶发写出未转义引号"挡在传输层（2026-10-21）。
    """
    model = build_chat_model(
        _settings(),
        use_fallback=False,
        model_kwargs={"response_format": {"type": "json_object"}},
    )

    assert isinstance(model, ChatOpenAI)
    assert model.model_kwargs == {"response_format": {"type": "json_object"}}
