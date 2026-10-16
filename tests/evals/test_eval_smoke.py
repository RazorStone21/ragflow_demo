"""评估链路的冒烟测试：证明"评估跑得起来"，**不**判"质量好不好"。

分工必须说清楚：这里断言的是**接线**——语料查得到、图出得来答案、裁判路由存在
且回得出可解析的裁决。质量断言属于黄金集与阈值门禁（``scripts/eval_run.py``）。
两者混在一个文件里，会让"链路断了"和"分数掉了"以同样的红灯呈现，而这两件事的
处置完全不同：前者修依赖，后者改 Prompt/检索策略。

这个文件有实测依据：``--layer all`` 曾在 Windows 上以 ``NotImplementedError``
（事件循环不能建子进程）与 ``InvalidThreadIdError``（会话段超长）整轮失败，两次
都是"不真跑就发现不了"的缺陷——当时没有任何自动化的东西会拦下它们。
"""

from __future__ import annotations

import pytest

from agent_platform.agents.gov_policy_qa.state import AnswerStatus
from agent_platform.core.models import build_chat_model
from agent_platform.evaluation.judge import Judge
from agent_platform.evaluation.runner import (
    EVAL_FOREIGN_TENANT_ID,
    RunnerDeps,
    new_eval_run_id,
    run_case,
)
from agent_platform.evaluation.schemas import (
    CaseResult,
    EvalLayer,
    GoldenCase,
    MetricName,
    MetricVerdict,
)

# 各模块自行打标记：conftest 里的 pytestmark 会被 pytest 忽略（静默失效）。
pytestmark = pytest.mark.eval


@pytest.fixture(scope="module")
def eval_run_id() -> str:
    """本次烟测的运行 ID。

    **不能用固定值**：``thread_id`` 由 ``(样例, run_id)`` 派生，写死就意味着一轮
    跑完的检查点会被下一轮复用，而图状态的 ``messages`` 用 ``add_messages`` 归约
    ——新问题会**追加**到旧消息之后。于是第二轮看到的是被污染的轨迹，它多半仍然
    "通过"，因为你验的已经不是同一个输入了。
    """
    return new_eval_run_id()


@pytest.fixture
async def graph_result(
    eval_deps: RunnerDeps, smoke_case: GoldenCase, eval_run_id: str
) -> CaseResult:
    """跑一次**完整图**，供"生成"与"裁判"两个用例复用（省一次真模型调用）。

    Raises:
        AssertionError: 图执行出错。放在夹具里断言而不是让每个用例各写一遍：
            一条出错的 ``CaseResult`` 不应该被当成"可用的输入"传给下游用例。
    """
    result, _ = await run_case(
        eval_deps,
        smoke_case,
        layer=EvalLayer.GRAPH,
        run_id=eval_run_id,
        tenant_id=eval_deps.settings.demo_tenant_id,
    )
    assert not result.error, f"图执行失败：{result.error}"
    return result


async def test_retrieval_reaches_corpus(
    eval_deps: RunnerDeps, smoke_case: GoldenCase, eval_run_id: str
) -> None:
    """检索层查得到证据（最便宜的一层，先证明语料与向量库是通的）。"""
    result, _ = await run_case(
        eval_deps,
        smoke_case,
        layer=EvalLayer.RETRIEVAL,
        run_id=eval_run_id,
        tenant_id=eval_deps.settings.demo_tenant_id,
    )
    assert not result.error, result.error
    assert result.evidence, f"{smoke_case.id} 检索不到任何证据（语料未入库？）"
    assert result.retrieval_meta is not None, "缺少检索判定元数据，拒答正确性无从判定"
    for item in result.evidence:
        # 无文档名的证据等于无引用，而 NO-12 要求结论必须可溯源。
        assert item.document_name, f"证据 {item.chunk_id} 缺少文档名"


async def test_foreign_tenant_retrieves_nothing(
    eval_deps: RunnerDeps, smoke_case: GoldenCase
) -> None:
    """什么都不拥有的租户检索同一问题必须一无所获（NO-19 的负向判据）。

    用负向判据而不是"两个租户结果不同"：后者依赖语料的租户分布，前者断言的是
    "租户过滤真的下推到了数据源"。这里只覆盖检索层这一道；RLS 与 ``thread_id``
    命名空间的验证在 ``tests/integration/``，两者不可互相替代。
    """
    outcome = await eval_deps.retriever.search(
        smoke_case.question, tenant_id=EVAL_FOREIGN_TENANT_ID
    )
    assert not outcome.evidence, (
        f"陌生租户 {EVAL_FOREIGN_TENANT_ID} 检索到了 {len(outcome.evidence)} 条证据，租户过滤未生效"
    )


async def test_graph_produces_answer_and_trace(graph_result: CaseResult) -> None:
    """图跑到终态，产出合法状态、非空答案与可复核的节点轨迹。"""
    assert graph_result.node_trace, "节点轨迹为空——轨迹评估无从谈起"
    assert graph_result.thread_id, "未记录 thread_id，检查点无法反查（清理与复现都依赖它）"
    assert graph_result.answer_status in {status.value for status in AnswerStatus}, (
        f"非法的答案状态 {graph_result.answer_status!r}"
    )
    assert graph_result.answer.strip(), "图产出了空答案"


async def test_judge_route_returns_parsed_verdict(
    eval_deps: RunnerDeps, smoke_case: GoldenCase, graph_result: CaseResult
) -> None:
    """裁判路由存在，且输出能被解析成裁决（ADR-0003 的裁判接线）。

    这条用例专门拦"网关没加载 ``judge`` 路由"这一类故障：那种情况下裁判调用会
    整体失败，指标记成 FAIL 而**不是**跳过，于是门禁看起来只是"分数低"。
    """
    judge = Judge(
        build_chat_model(eval_deps.settings, logical_name=eval_deps.settings.judge_model),
        logical_name=eval_deps.settings.judge_model,
        cache_dir=eval_deps.settings.eval_judge_cache_dir,
    )
    scored = await judge.score(smoke_case, graph_result)
    by_name = {metric.name: metric for metric in scored.metrics}

    for name in (MetricName.FAITHFULNESS, MetricName.ANSWER_RELEVANCY):
        assert name in by_name, f"裁判未产出 {name.value}"
        metric = by_name[name]
        # 这条样例检索得到证据、也出得来答案，因此两项都应**真的判了**；
        # 若此处变成"不适用"，说明适用性判据写错了，而它会把门禁悄悄放空。
        assert metric.verdict is not MetricVerdict.NOT_APPLICABLE, f"{name.value} 意外不适用"
        assert metric.score is not None

    assert scored.judge_raw, "裁判原始输出未留痕（§9.2 要求可判'样例错还是模型错'）"
