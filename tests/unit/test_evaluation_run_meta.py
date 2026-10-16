"""评估的可比性快照 ``build_run_meta``（CLAUDE.md 9.2"留痕"）。

这个文件的存在理由是一次**语料漂移的误报**：``chunk_count`` 原先来自
``deps.milvus_store.count()``——**不带租户**，数的是整个集合。而检索路径全程带租户
过滤（NO-19 要求过滤下推到 Milvus），别的租户的行**根本检索不到**。两者口径不一致，
代价不是"数字大一点"，而是这个字段会因与被评估语料**无关**的原因漂移。

实测（2026-10-16）：集成测试 ``tests/integration/test_milvus_integration.py`` 的
``seeded`` 夹具残留了 6 条（``test-tenant-a`` / ``test-tenant-b`` 各 3 条，
``document_id=doc-bb2b655b4444``，其 ``updated_at`` 是夹具里硬编码的 ``1700000000``，
可直接指认来源），于是同一份语料、同一份清单指纹（``7cb2f62b6912``）下的
``chunk_count`` 从 195 变成 201。``demo`` 租户——也就是黄金集真正考的那个租户——
**始终是 195，一条没变**。

若不修，一份"语料没变"的基线对比会喊出语料漂移，读表的人得把这次排查重做一遍；
更坏的是它与 D8/D9 是同一类失真：**快照里有个会因无关原因动的数字**，
"变了"就不再等于"真变了"。

所以这里钉住两件事：**租户必须传下去**，且**传的就是本轮运行的那个租户**
（不是某个写死的默认值——写死的默认值会让 ``--tenant`` 跑别的租户时报出错的数）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.graph.state import CompiledStateGraph

from agent_platform.evaluation.loader import GoldenSet, load_golden_set
from agent_platform.evaluation.runner import RunnerDeps, build_run_meta
from agent_platform.knowledge.milvus_store import MilvusStore
from agent_platform.knowledge.retriever import Retriever
from agent_platform.settings import Settings
from tests.conftest import FakeMilvusStore

GOLDEN_DIR = Path("tests/evals/golden")
_RUN_ID = "eval-20261017T000000Z-deadbeef"


@pytest.fixture
def golden() -> GoldenSet:
    """真实黄金集。

    用真的而不是造一个：本测试要的只是"有个能提供指纹与签署数的 ``GoldenSet``"，
    而真黄金集顺带把"它还装得回来"也一起验了——它恰是门禁的输入。
    """
    return load_golden_set(GOLDEN_DIR)


def _deps(settings: Settings, store: FakeMilvusStore) -> RunnerDeps:
    """组装 ``build_run_meta`` 真正用得着的那两个字段。

    ``graph`` / ``retriever`` 这一路径下**一次都不碰**（本函数只采快照，不跑图、
    不检索），所以给 ``None``：为它们各造一个 fake 等于凭空维护两份会腐烂的实现。
    ``cast`` 是如实说明"这里填的是占位"，而不是让类型检查闭嘴。
    """
    return RunnerDeps(
        settings=settings,
        graph=cast("CompiledStateGraph[Any, Any, Any, Any]", None),
        retriever=cast("Retriever", None),
        milvus_store=cast("MilvusStore", store),
    )


@pytest.fixture
def corpus_settings(fake_settings: Settings, tmp_path: Path) -> Settings:
    """把入库清单指到空目录，让 ``manifest_sha256`` 走"未取到"分支（与断言无关）。"""
    return fake_settings.model_copy(update={"ingest_manifest_path": tmp_path / "manifest.json"})


async def test_chunk_count_is_scoped_to_the_tenant_under_test(
    corpus_settings: Settings, golden: GoldenSet
) -> None:
    """语料快照必须按本轮租户收口，且传的就是调用方给的那个租户。"""
    store = FakeMilvusStore(chunk_count=195)
    meta = await build_run_meta(
        _deps(corpus_settings, store), golden, run_id=_RUN_ID, tenant_id="demo"
    )
    assert store.count_tenants == ["demo"], "count 必须收到本轮租户，不能是不带租户的调用"
    assert meta.corpus.chunk_count == 195


async def test_a_non_default_tenant_is_the_one_recorded(
    corpus_settings: Settings, golden: GoldenSet
) -> None:
    """``--tenant`` 换租户时，快照记的必须是换过之后的那个。

    只断言"传了个字符串"是不够的：把 ``tenant_id`` 写死成某个默认常量同样能通过
    上一条测试，却会在换租户跑时静默报出别的租户的语料规模——那正是本次要修的
    那类错误换了件衣服。
    """
    store = FakeMilvusStore(chunk_count=7)
    meta = await build_run_meta(
        _deps(corpus_settings, store),
        golden,
        run_id=_RUN_ID,
        tenant_id="eval-tenant-other",
    )
    assert store.count_tenants == ["eval-tenant-other"]
    assert meta.corpus.chunk_count == 7
