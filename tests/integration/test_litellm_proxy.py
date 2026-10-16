"""真实 LiteLLM 网关：逻辑名映射、嵌入维度、重排路由（CLAUDE.md 第 5 章）。

这个文件存在的理由是三条**当时只能靠读源码判断、无法离线证实**的结论：

1. ``hosted_vllm`` 能否把 SiliconFlow 的 ``/v1/rerank`` 正确路由过去
   （见 ``deploy/docker/litellm/config.yaml`` 的注释，那里已标明"未经运行时验证"）；
2. SiliconFlow 上 ``Qwen3-Embedding-8B`` 的实际维度是否等于 ``MILVUS_DIM``；
3. ``deepseek/deepseek-chat`` 是否真能被网关按逻辑名 ``primary-chat`` 解析。

三件事的共性：猜错了不会在离线测试里暴露，而是在真实入库/问答时以"维度不符""模型不存在"
的形式炸出来。因此它们必须在真网关上跑一次，把猜测变成证据。

运行前提：``.env`` 填好 DEEPSEEK_API_KEY 与 SILICONFLOW_API_KEY，
``docker compose up -d litellm``，且 ``RUN_INTEGRATION_TESTS=true``。
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest

from agent_platform.core.models import build_chat_model
from agent_platform.knowledge.gateway_client import GatewayHttpClient
from tests.integration._support import require_http

if TYPE_CHECKING:
    from agent_platform.settings import Settings

pytestmark = pytest.mark.integration

PROBE_TEXT = "居民生活用电实行阶梯电价，第一档为每月零至二百四十千瓦时。"
RERANK_DOCS = [
    "新能源发电项目上网电价由省级价格主管部门核定并公示。",
    "居民生活用电实行阶梯电价，按月度用电量分档计价。",
    "电网企业应当及时结算可再生能源电价附加补助资金。",
]


@pytest.fixture
async def gateway(integration_settings: Settings) -> AsyncIterator[GatewayHttpClient]:
    """连上真实网关；进程没起则 skip。

    注意 ``401`` **不算** skip：能连上网关却认证失败，说明密钥配错了，
    那是必须修的问题，跳过只会让它在下一次真实入库时才出现。
    """
    await require_http(f"{integration_settings.litellm_base_url}/health/liveliness", name="LiteLLM")
    async with GatewayHttpClient(integration_settings) as client:
        yield client


async def test_logical_names_are_registered(
    gateway: GatewayHttpClient, integration_settings: Settings
) -> None:
    """三个逻辑名都必须已注册：业务代码只认逻辑名，缺一个就是部署缺陷。"""
    models = await gateway.list_models()
    for logical_name in (
        integration_settings.primary_chat_model,
        integration_settings.embedding_model,
        integration_settings.rerank_model,
    ):
        assert logical_name in models, f"网关未注册逻辑名 {logical_name}，实际: {sorted(models)}"


async def test_embedding_dimension_matches_milvus_config(
    gateway: GatewayHttpClient, integration_settings: Settings
) -> None:
    """嵌入维度必须等于 ``MILVUS_DIM``。

    这是全仓最容易"配错了却没人发现"的一处：不等的话 Milvus 建集合时才会报维度冲突，
    而那时已经花掉了整批语料的解析时间。在这里用一次请求换掉那段时间。
    """
    vectors = await gateway.embed([PROBE_TEXT], logical_model=integration_settings.embedding_model)
    assert len(vectors) == 1
    actual = len(vectors[0])
    assert actual == integration_settings.milvus_dim, (
        f"嵌入模型输出 {actual} 维，而 MILVUS_DIM={integration_settings.milvus_dim}。"
        " 改 .env 里的 MILVUS_DIM 与索引维度保持一致（改维度需重建集合）。"
    )
    assert any(value != 0.0 for value in vectors[0]), "嵌入不得为全零向量"


def _cosine(first: list[float], second: list[float]) -> float:
    """两向量夹角余弦（纯 Python，避免为一个断言引入 numpy 依赖）。"""
    dot = sum(a * b for a, b in zip(first, second, strict=True))
    norm = math.sqrt(sum(a * a for a in first)) * math.sqrt(sum(b * b for b in second))
    return dot / norm


async def test_embedding_direction_is_stable(
    gateway: GatewayHttpClient, integration_settings: Settings
) -> None:
    """同一文本两次嵌入的**方向**应高度一致——否则检索排序无法复现。

    为什么断言余弦而不是逐元素相等（2026-10-16 实测证据）：SiliconFlow 推理侧做动态
    批处理，一个请求里的 N 个输入会被拆进**不同的前向批**，不同前向批的浮点归约顺序
    不同，结果在低位上就不一致。用 ``[T]*4`` 探测读出的分组签名：直连 8 轮中 4 轮为
    ``DDDEEE``（拆成 ``[T] + [T,T,T]``，子批内逐位相同、子批间有差），经网关 8 轮中
    1 轮为 ``EEDEDD``。拆与不拆逐次随机，**与是否经过网关无关**（直连同样复现，故不是
    本仓客户端或 LiteLLM 的问题）。后果：逐元素比较哪怕放宽到 ``abs=1e-3`` 也注定偶发
    失败——实测逐元素最大差最坏 **3.5e-3**，此前 ``abs=1e-6`` 的用例 5 次跑挂 2 次。

    真正决定"排序能否复现"的是方向：实测跨请求余弦最坏 **0.999903**（1-cos≈9.7e-5，
    40 次请求比对）。阈值取 ``1 - 1e-3``，与实测最坏值留约一个数量级余量——上游分批
    策略不受本仓控制，把阈值卡在实测值上等于把偶发性重新引回门禁。
    """
    first, second = await gateway.embed(
        [PROBE_TEXT] * 2, logical_model=integration_settings.embedding_model
    )
    similarity = _cosine(first, second)
    assert similarity >= 1 - 1e-3, (
        f"同一批内同一文本的嵌入方向不一致（cos={similarity!r}）；"
        " 这通常意味着上游换模型或换了分批策略，检索排序将不可复现"
    )


async def test_rerank_returns_relevance_ordered(
    gateway: GatewayHttpClient, integration_settings: Settings
) -> None:
    """重排必须返回 ``index`` + ``relevance_score``，且与问题相关的那条排最前。

    这条路径（``hosted_vllm`` → SiliconFlow ``/v1/rerank``）此前只能靠读 LiteLLM 源码
    推断，本用例是它的运行时证据。断言"相关那条排第一"而不只断言"有返回"：
    路由接错时往往会返回一堆等分或乱序结果，只查结构会漏掉。
    """
    items = await gateway.rerank(
        "居民阶梯电价是怎么分档的？",
        RERANK_DOCS,
        logical_model=integration_settings.rerank_model,
        top_n=3,
    )
    assert len(items) == len(RERANK_DOCS)
    assert {item.index for item in items} == {0, 1, 2}, "索引须覆盖全部候选，否则映射会错位"
    scores = [item.relevance_score for item in items]
    assert all(isinstance(score, float) for score in scores)
    best = max(items, key=lambda item: item.relevance_score)
    assert best.index == 1, (
        f"最相关的应是第 2 条（阶梯电价），实际: {[(i.index, i.relevance_score) for i in items]}"
    )
    assert scores != sorted(scores), "全同分说明重排没生效，只是原样返回"


@pytest.mark.usefixtures("gateway")
async def test_primary_chat_answers_via_gateway(
    integration_settings: Settings,
) -> None:
    """按逻辑名调用对话模型并取回非空文本（NO-02：只经网关，不直连厂商）。

    经 ``usefixtures`` 挂 ``gateway`` 而不是直接构造模型：该夹具先探服务可达性，
    网关没起时本条与同文件其余用例一样是 skip。少了这层门禁，同一份用例在网关没起时
    会变成 fail（且失败信息指向"密钥不对"这个错误方向）。

    曾在此处 ``except httpx.HTTPStatusError`` 并提示"请检查 DEEPSEEK_API_KEY"：
    那是**永不执行的死分支**——``ChatOpenAI`` 把传输层错误包装成 ``openai.*`` 系列
    （实测网关不可达时抛 ``OpenAIConnectionError``，密钥被拒时抛 ``OpenAIAPIError``），
    从不抛 ``httpx.HTTPStatusError``。留一个看起来友好、实际不可能触发的提示，
    比没有提示更误导人，故删掉、让真实异常照原样暴露。
    """
    model = build_chat_model(integration_settings, max_tokens=64)
    reply = await model.ainvoke("只回答两个字：你好。")
    assert reply.content, "模型返回了空内容"
