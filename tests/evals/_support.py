"""评估测试的共享工具（与 ``tests/integration/_support.py`` 同构）。

为什么不塞进 ``conftest.py``：理由与 ``tests/integration/_support.py`` 相同——
pytest 会按自己的模块名把 conftest 单独导入一次，测试再 ``from tests.evals.conftest
import ...`` 就会拿到**第二个**模块对象，夹具与模块级状态随之分叉。

为什么不直接复用 ``tests.integration._support`` 的提示语：那里写的是
``RUN_INTEGRATION_TESTS=true``，而评估测试由 ``RUN_EVAL_TESTS`` 门控。照抄那份
提示会让人打开**错误的开关**、然后困惑于为什么仍然 skip。可达性探测的思想是
通用的，但两个门禁的开关名不同，提示语必须各写各的。
"""

from __future__ import annotations

import asyncio

from psycopg import OperationalError
from pymilvus import MilvusException

from agent_platform.api.container import AppDeps

EVAL_HINT = """
评估测试未运行。启用步骤：
  1. 复制 .env.example 为 .env 并填好密钥；
  2. docker compose up -d 并等全部服务 healthy；
  3. 在 .env 中设 RUN_EVAL_TESTS=true（注意**不是** RUN_INTEGRATION_TESTS）；
  4. uv run pytest -m eval
"""

_UNREACHABLE: tuple[type[BaseException], ...] = (TimeoutError, OperationalError, MilvusException)
"""**只有这几种**才翻译成"依赖不可用"。

刻意比 ``api/routes/health.py`` 的 ``except Exception`` 窄：那里是探针要对外
回答状态码，任何异常都得译成一个结果；这里是测试门控，把 ``TypeError`` 之类
的真缺陷也译成 skip，就正好落进 NO-06 说的"让流水线假绿"。名单里三项分别对应
"探测超时"、"Postgres 连不上"、"Milvus 连不上"，其余一律让它照常失败。
"""


async def unreachable_dependencies(deps: AppDeps, *, timeout_s: float = 5.0) -> list[str]:
    """逐项跑**生产自己的**就绪探针，返回不通的依赖名（全通返回空表）。

    复用 ``AppDeps.readiness_checks`` 而不是在测试里另写一套探测：那套探针是
    ``/readyz`` 的判据，也就是"生产认为这个依赖能不能用"的唯一定义。另写一套
    必然出现分叉——``tests/integration/_support.py`` 的 ``trust_env`` 注释记过
    这个教训的实测版本：探测走了一条与实际调用不同的网络路径，答的就不是同一个
    问题，同一份代码两次运行给出不同的 skip/ERROR 组合。

    Returns:
        未就绪的依赖名清单（如 ``["milvus"]``）；**不抛异常**，由调用方决定
        是 skip 还是失败。
    """
    unreachable: list[str] = []
    for check in deps.readiness_checks:
        try:
            async with asyncio.timeout(timeout_s):
                await check.probe()
        except _UNREACHABLE:
            unreachable.append(check.name)
    return unreachable
