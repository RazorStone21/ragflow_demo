"""裁判层的离线单元测试（CLAUDE.md 9.2）。

**这个文件是因为一个真实缺陷才存在的。** ``judge.py`` 原先被整份排除在覆盖率之外
（"它要连网关"，理由成立），于是它的**失败路径没有一条被测过**——而缺陷恰好长在
那里：裁判输出两次都解析不出结构时，代码记 ``FAIL`` + ``score=0.0``。首次真实
基线对比里，一条样例因此被凭空记成"faithfulness 0.0000"，把聚合分压低 1/23，
报告读作"跌破"，该样例还进了"新失败样例"。**没有任何一条测试能拦住它，因为
那条路径从来没有运行过。**

所以这里用 fake ``ChatModel`` 把裁判的**每一条出口**都跑一遍：正常判定、
纠偏重试、两次都失败（→ ``ERROR`` 而非 0 分）、缓存命中、缓存损坏。
全部离线：不连网关、不读 ``local_data/``（缓存目录用 ``tmp_path``）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent_platform.agents.gov_policy_qa.state import AnswerStatus
from agent_platform.evaluation.judge import Judge, JudgeCache
from agent_platform.evaluation.prompts import FAITHFULNESS_JUDGE_PROMPT
from agent_platform.evaluation.schemas import (
    CaseCategory,
    CaseResult,
    EvalLayer,
    GoldenCase,
    JudgeSnapshot,
    MetricName,
    MetricVerdict,
)
from agent_platform.knowledge.schemas import EvidenceItem

# ---------------------------------------------------------------------------
# 构造器：模型与样例全部在内存里搭出来
# ---------------------------------------------------------------------------


class _ScriptedModel:
    """按剧本逐次返回输出的 fake ``ChatModel``（用完即复述最后一条）。"""

    def __init__(self, *outputs: str) -> None:
        self._outputs = list(outputs)
        self.calls: list[str] = []

    async def ainvoke(
        self,
        input: Any,  # noqa: A002  # 与 ChatModel 协议同名，保持可替换性（见 core/models.py）
        config: Any = None,
        **kwargs: Any,
    ) -> AIMessage:
        content = input[-1].content
        self.calls.append(content)
        index = min(len(self.calls) - 1, len(self._outputs) - 1)
        return AIMessage(content=self._outputs[index])


def _case(case_id: str = "c1") -> GoldenCase:
    return GoldenCase(
        id=case_id,
        category=CaseCategory.TYPICAL,
        capability="电价",
        question="贵州燃煤发电基准价是多少？",
        expected_status=AnswerStatus.ANSWERED,
    )


def _result(*, answer: str = "基准价为 0.3515 元/千瓦时 [1]。") -> CaseResult:
    return CaseResult(
        case_id="c1",
        category=CaseCategory.TYPICAL,
        question="贵州燃煤发电基准价是多少？",
        layer=EvalLayer.GRAPH,
        answer=answer,
        answer_status=AnswerStatus.ANSWERED.value,
        evidence=[
            EvidenceItem(
                chunk_id="chunk-1",
                content="贵州燃煤发电基准价为 0.3515 元/千瓦时。",
                score=0.9,
                document_name="0030.贵州电网销售电价表.xlsx",
                page_numbers=[2],
            )
        ],
    )


def _judge(tmp_path: Path, model: Any, *, max_tokens: int | None = None) -> Judge:
    """构造裁判。``max_tokens`` 不传即用生产默认值——不在这里另写一份默认数字，
    否则改了 ``Judge`` 的默认，测试仍会照旧通过。"""
    if max_tokens is None:
        return Judge(model, logical_name="judge", cache_dir=tmp_path / "cache")
    return Judge(model, logical_name="judge", cache_dir=tmp_path / "cache", max_tokens=max_tokens)


_FAITHFUL_OK = '{"claims": [{"claim": "基准价为 0.3515", "supported": true}], "reason": "有支撑"}'
_RELEVANCY_OK = '{"score": 0.9, "reason": "正面回答"}'
_NOT_JSON = "这不是 JSON，只是模型的散文。"


# ---------------------------------------------------------------------------
# 快照：改配置必须留痕（D9）
# ---------------------------------------------------------------------------
def test_snapshot_records_the_output_budget(tmp_path: Path) -> None:
    """裁判输出上限必须进快照。

    D8 修复时把上限从 1024 提到 4096，而快照里没有这一栏——于是一次真实的
    门禁参数变更在任何对比报告里都不可见。这里钉住它。
    """
    judge = _judge(tmp_path, _ScriptedModel(_FAITHFUL_OK), max_tokens=2048)
    snapshot = judge.snapshot()
    assert snapshot.max_tokens == 2048
    assert snapshot.temperature == 0.0  # 其余留痕项不因新增字段而丢失
    assert snapshot.prompt_version


def test_old_artifact_without_max_tokens_reads_as_unrecorded() -> None:
    """本字段引入前落盘的产物解析为 ``None``（未记录），**不是**某个默认数字。

    给个默认数字等于替旧产物编一个它从没记过的值：对比时那条"漂移"就分不清
    是真改了上限还是老产物压根没记过。
    """
    snapshot = JudgeSnapshot.model_validate({"backend": "in-house", "temperature": 0.0})
    assert snapshot.max_tokens is None


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
async def test_judge_scores_faithfulness_and_relevancy(tmp_path: Path) -> None:
    model = _ScriptedModel(_FAITHFUL_OK, _RELEVANCY_OK)
    scored = await _judge(tmp_path, model).score(_case(), _result())
    by_name = {metric.name: metric for metric in scored.metrics}
    assert by_name[MetricName.FAITHFULNESS].verdict is MetricVerdict.PASS
    assert by_name[MetricName.FAITHFULNESS].score == 1.0
    assert by_name[MetricName.ANSWER_RELEVANCY].score == 0.9
    assert FAITHFULNESS_JUDGE_PROMPT in scored.judge_raw


async def test_judge_skips_what_it_cannot_judge(tmp_path: Path) -> None:
    """没有证据核不了忠实、没有答案判不了相关——都是 ``NOT_APPLICABLE``，不硬凑分。"""
    model = _ScriptedModel("不会被调用")
    bare = CaseResult(
        case_id="c1",
        category=CaseCategory.TYPICAL,
        question="问题",
        layer=EvalLayer.RETRIEVAL,
    )
    scored = await _judge(tmp_path, model).score(_case(), bare)
    assert {metric.verdict for metric in scored.metrics} == {MetricVerdict.NOT_APPLICABLE}
    assert model.calls == [], "无据可核时不得白花一次模型调用"


# ---------------------------------------------------------------------------
# 失败路径：D8 就长在这里
# ---------------------------------------------------------------------------
async def test_unparseable_output_is_recorded_as_error_not_a_zero_score(tmp_path: Path) -> None:
    """**两次都解析不出来 → ``ERROR``，不带分数**，且不得伪装成 0 分。

    这是本文件的由来。写成 ``score=0.0`` 会让一次**测量故障**变成一次
    **质量回归**：聚合分被压低、报告显示"跌破"、样例进"新失败样例"——
    而模型什么都没做错。可见性由 ``ERROR`` 自己承担：它照样让样例判不通过。
    """
    model = _ScriptedModel(_NOT_JSON, _NOT_JSON)  # 首次 + 纠偏重试都不可解析
    scored = await _judge(tmp_path, model).score(_case(), _result())
    faithfulness = next(m for m in scored.metrics if m.name is MetricName.FAITHFULNESS)
    assert faithfulness.verdict is MetricVerdict.ERROR
    assert faithfulness.score is None, "未采集不得带分数——0.0 会被读成一个测量结果"
    assert "未采集到结果" in faithfulness.detail
    assert not scored.passed, "测不出来照样算这条样例没通过"


async def test_the_failed_attempts_are_both_preserved_for_humans(tmp_path: Path) -> None:
    """解析失败时，**两次**原始输出都要留住——「详见 judge_raw」不能是空话。

    "只重试一次"是**每条提示词**各一次：一条样例要判忠实与相关两件事，
    所以两次不可解析会看到 4 次调用，而不是 2 次（这个数差正是本测试第一版写错的地方）。
    """
    model = _ScriptedModel("首次的半截输出", "重试的半截输出")
    scored = await _judge(tmp_path, model).score(_case(), _result())
    assert "首次的半截输出" in scored.judge_raw
    assert "重试的半截输出" in scored.judge_raw
    assert len(model.calls) == 4, "两条提示词各调用两次（首次 + 一次纠偏）"
    assert "你上一次的输出不是合法的 JSON" not in model.calls[0]
    assert "你上一次的输出不是合法的 JSON" in model.calls[1], "第二次必须带上纠偏指令"


@pytest.mark.parametrize(
    ("label", "entry"),
    [
        ("不是 JSON", "这不是 JSON"),
        ("结构版本不符", '{"v": 99, "verdict": {}, "raw": "x"}'),
        ("缺少 raw 字段", '{"v": 1, "verdict": {}}'),
        ("verdict 不合裁判结构", '{"v": 1, "verdict": {"claims": []}, "raw": "x"}'),
    ],
)
async def test_an_unusable_cache_entry_falls_back_to_a_real_call(
    tmp_path: Path, label: str, entry: str
) -> None:
    """缓存不可用（坏了/版本不符/结构过时）要落到真实调用，**不是**当成失败。

    四种失效形态都要走同一条降级路：缓存是加速手段，**没有任何一条正确性依赖它**。
    若把读缓存失败当成判分失败，一次磁盘抖动就会让一批样例无辜记零分——
    与 D8 同一类的失真。
    """
    model = _ScriptedModel(_FAITHFUL_OK, _RELEVANCY_OK)
    await _judge(tmp_path, model).score(_case(), _result())

    cache = JudgeCache(tmp_path / "cache")
    key = JudgeCache.key(prompt_name=FAITHFULNESS_JUDGE_PROMPT, prompt=model.calls[0])
    cache.path_for(key).write_text(entry, encoding="utf-8")

    model.calls.clear()
    rescored = await _judge(tmp_path, model).score(_case(), _result())
    assert model.calls, f"缓存不可用（{label}）时必须真调一次"
    assert (
        next(m for m in rescored.metrics if m.name is MetricName.FAITHFULNESS).verdict
        is MetricVerdict.PASS
    )


async def test_a_cached_score_costs_no_model_call(tmp_path: Path) -> None:
    """缓存命中：同一个问题再判一次不得再花钱，且分数一致。"""
    model = _ScriptedModel(_FAITHFUL_OK, _RELEVANCY_OK)
    first = await _judge(tmp_path, model).score(_case(), _result())
    calls_after_first = len(model.calls)

    second = await _judge(tmp_path, model).score(_case(), _result())
    assert len(model.calls) == calls_after_first, "缓存命中不该再调模型"
    assert second.model_dump(exclude={"judge_raw"}) == first.model_dump(exclude={"judge_raw"})


@pytest.mark.parametrize(
    "raw",
    ["这不是 JSON", '{"claims": []}', '{"claims": [{"claim": "x"}]}', "null"],
)
async def test_structurally_invalid_judge_output_is_also_an_error(tmp_path: Path, raw: str) -> None:
    """不只是"不是 JSON"：**结构不合约定**（空断言、字段缺失）同样算未采集。"""
    scored = await _judge(tmp_path, _ScriptedModel(raw, raw)).score(_case(), _result())
    assert (
        next(m for m in scored.metrics if m.name is MetricName.FAITHFULNESS).verdict
        is MetricVerdict.ERROR
    )
