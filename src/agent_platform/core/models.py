"""经 LiteLLM 网关的统一模型客户端构造（CLAUDE.md 第 5 章 / NO-02）。

- 业务代码只以**逻辑名**（``primary-chat`` 等）引用模型，具体型号在网关映射；
  换型号/切私有化 vLLM 不改业务代码。
- 每次调用显式设置：超时、最大重试、温度、max_tokens（CLAUDE.md 5.3）。
- 主→备 fallback 链：网关层为主（LiteLLM ``fallbacks``），应用层可选
  （``FALLBACK_CHAT_MODEL`` 配置了才启用），避免"全部不可用"无兜底（CLAUDE.md 3.6 / 5.4）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_openai import ChatOpenAI

from agent_platform.settings import Settings

DEFAULT_TEMPERATURE = 0.0
"""默认温度：检索问答场景要求确定性（CLAUDE.md 5.3，评测固定随机性）。"""


@runtime_checkable
class ChatModel(Protocol):
    """聊天模型最小接口（真实 ``ChatOpenAI`` 与测试 fake 均满足）。"""

    async def ainvoke(
        self,
        input: Any,  # noqa: A002  # LangChain Runnable 协议要求参数名为 input（关键字参数调用兼容）
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        """异步调用模型并返回 ``AIMessage``。"""
        ...

    def astream(
        self,
        input: Any,  # noqa: A002  # 同上：与 LangChain Runnable 协议签名保持一致
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AIMessageChunk]:
        """异步流式调用模型，逐块产出 ``AIMessageChunk``（CLAUDE.md 4.7）。

        Note:
            声明为普通 ``def`` 返回异步迭代器（而非 ``async def``），与
            LangChain ``Runnable.astream`` 及异步生成器实现均兼容。
        """
        ...


def _make_chat_openai(
    settings: Settings,
    *,
    logical_name: str,
    temperature: float,
    max_tokens: int | None,
) -> ChatOpenAI:
    """构造指向 LiteLLM 网关的 ``ChatOpenAI``（内部复用）。"""
    # 参数名与 langchain-openai 的字段 alias 对齐（base_url=openai_api_base 的 alias、
    # timeout=request_timeout 的 alias、max_completion_tokens=max_tokens 的 alias，已实测）。
    return ChatOpenAI(
        model=logical_name,
        base_url=settings.litellm_base_url,
        api_key=settings.litellm_api_key,
        timeout=settings.model_timeout_s,
        max_retries=settings.model_max_retries,
        temperature=temperature,
        max_completion_tokens=max_tokens,
    )


def build_chat_model(
    settings: Settings,
    *,
    logical_name: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int | None = None,
) -> ChatModel:
    """构造聊天模型（可选挂载应用层 fallback 链）。

    Args:
        settings: 平台配置（网关地址、密钥、超时、重试次数）。
        logical_name: 模型逻辑名；默认取 ``PRIMARY_CHAT_MODEL``。
        temperature: 采样温度；评测与事实问答应保持 0。
        max_tokens: 输出上限；``None`` 交由网关默认。

    Returns:
        满足 ``ChatModel`` 协议的模型对象；配置了 ``FALLBACK_CHAT_MODEL`` 时
        返回主备链（主失败自动切备）。
    """
    primary_name = logical_name or settings.primary_chat_model
    primary = _make_chat_openai(
        settings, logical_name=primary_name, temperature=temperature, max_tokens=max_tokens
    )
    fallback_name = settings.fallback_chat_model
    if not fallback_name or fallback_name == primary_name:
        return primary
    fallback = _make_chat_openai(
        settings, logical_name=fallback_name, temperature=temperature, max_tokens=max_tokens
    )
    chain: Runnable[Any, Any] = primary.with_fallbacks([fallback])
    return _RunnableChatModelAdapter(chain)


class _RunnableChatModelAdapter:
    """把带 fallback 的 ``Runnable`` 适配为 ``ChatModel`` 协议（保持节点侧类型简单）。"""

    def __init__(self, runnable: Runnable[Any, Any]) -> None:
        self._runnable = runnable

    async def ainvoke(
        self,
        input: Any,  # noqa: A002  # 同上：与 LangChain Runnable 协议签名保持一致
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        """调用底层 Runnable 并断言返回 ``AIMessage``。"""
        result = await self._runnable.ainvoke(input, config=config, **kwargs)
        if not isinstance(result, AIMessage):
            msg = f"聊天模型返回了非 AIMessage 类型: {type(result).__name__}"
            raise TypeError(msg)
        return result

    async def astream(
        self,
        input: Any,  # noqa: A002  # 同上：与 LangChain Runnable 协议签名保持一致
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AIMessageChunk]:
        """流式调用底层 Runnable（主模型失败时由 ``with_fallbacks`` 切换备选）。

        Raises:
            TypeError: 底层产出非 ``AIMessageChunk``——契约破坏，显式失败而非静默丢弃。
        """
        async for chunk in self._runnable.astream(input, config=config, **kwargs):
            if not isinstance(chunk, AIMessageChunk):
                msg = f"聊天模型流式返回了非 AIMessageChunk 类型: {type(chunk).__name__}"
                raise TypeError(msg)
            yield chunk
