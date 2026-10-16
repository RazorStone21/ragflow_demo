"""评估测试的门控与夹具（CLAUDE.md 9.2 / 9.3）。

**门控是双重的**，与 ``tests/integration/conftest.py`` 同一套道理：

1. ``RUN_EVAL_TESTS=true`` + 可读的 ``.env``：没配就跑不了，直接 skip；
2. 逐个依赖跑**生产自己的就绪探针**：开关打开了但 compose 没起，同样 skip 而不是失败。

第二条尤其重要：CI 里"开关忘记关"与"服务真的挂了"是两件事——前者该 skip，
后者才该 fail。把两者混成同一个红灯，会训练出"看到评估测试失败就重跑"的习惯，
那等于没有这道门禁。

所有异步夹具都是**函数级**：pytest-asyncio 的 loop scope 配的是 ``function``，
会话级 async 夹具会绑到另一个事件循环上，表现为难查的跨循环报错。

各测试模块自行声明 ``pytestmark = pytest.mark.eval``：conftest 里的 pytestmark
会被 pytest 忽略（既不报错也不生效），靠它打标记是个静默的坑。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from psycopg import OperationalError
from pydantic import ValidationError

from agent_platform.api.container import open_production_deps
from agent_platform.core.errors import ConfigurationError
from agent_platform.evaluation.loader import GoldenSet, load_golden_set
from agent_platform.evaluation.runner import RunnerDeps
from agent_platform.evaluation.schemas import CaseCategory, GoldenCase
from agent_platform.settings import Settings, load_settings
from tests.evals._support import EVAL_HINT, unreachable_dependencies


@pytest.fixture(scope="session")
def eval_settings() -> Settings:
    """加载真实配置；未开启开关或缺少必填项时 skip。

    会话级且**同步**：只读配置、不碰事件循环，因此不受 ``function`` loop scope 限制。
    """
    try:
        settings = load_settings()
    except (ConfigurationError, ValidationError, OSError) as exc:
        # 只捕这三种"配置读不出来"的具体原因（NO-06：不吞异常）。本夹具的职责是
        # "这次能不能跑"，读不出配置就该是一条带提示的 skip，而不是让整个
        # -m eval 会话以 ERROR 结束。
        pytest.skip(f"无法加载 .env（{type(exc).__name__}: {exc}）\n{EVAL_HINT}")
    if not settings.run_eval_tests:
        pytest.skip(f"RUN_EVAL_TESTS 未开启\n{EVAL_HINT}")
    return settings


@pytest.fixture(scope="session")
def golden(eval_settings: Settings) -> GoldenSet:
    """``.env`` 所指的整个黄金集（纯文件读取，不连任何服务）。"""
    return load_golden_set(Path(eval_settings.eval_golden_dir))


@pytest.fixture(scope="session")
def smoke_case(golden: GoldenSet) -> GoldenCase:
    """一条典型样例，供烟测使用。

    取 ``typical`` 类的第一条，而不是硬编码某个样例 id：冒烟要回答的是"这条链路
    通不通"，写死 id 会在黄金集增删样例时无谓地碎掉，而那种碎掉与链路无关。
    """
    case = next((item for item in golden.cases if item.category is CaseCategory.TYPICAL), None)
    if case is None:
        pytest.skip("黄金集里没有 typical 类样例，无法烟测")
    return case


@pytest.fixture
async def eval_deps(eval_settings: Settings) -> AsyncIterator[RunnerDeps]:
    """走**生产装配路径**打开依赖，就绪后交出 ``RunnerDeps``。

    Raises:
        Skipped: Postgres 不可达（装配期即建连并建表），或任一就绪探针不通。

    **异常处理的范围是刻意收窄的**：``except`` 只包住 ``enter_async_context``
    这一句，不含 ``yield``。若把 ``yield`` 圈进同一个 ``try``，测试体里抛出的
    ``OperationalError`` 会被这里接住并译成 skip——"数据库连不上"与"用例自己
    写错了导致连接报错"被搅成同一条绿线，正是这道门禁最该防的失真。
    """
    async with AsyncExitStack() as stack:
        try:
            app_deps = await stack.enter_async_context(open_production_deps(eval_settings))
        except OperationalError as exc:
            pytest.skip(f"Postgres 不可达（{type(exc).__name__}）\n{EVAL_HINT}")

        unreachable = await unreachable_dependencies(app_deps)
        if unreachable:
            pytest.skip(f"依赖未就绪：{'、'.join(unreachable)}\n{EVAL_HINT}")

        yield RunnerDeps(
            settings=eval_settings,
            graph=app_deps.graph,
            retriever=app_deps.retriever,
            milvus_store=app_deps.milvus_store,
        )
