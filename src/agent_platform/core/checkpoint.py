"""PostgresSaver / PostgresStore 的构造与生命周期（CLAUDE.md 4.4 / NO-08）。

- 有状态图编译时必须挂 ``AsyncPostgresSaver`` 与 ``AsyncPostgresStore``；
  内存版仅限单测。
- 连接由 ``AsyncExitStack`` 统一持有，应用启动进入、关停退出；模块导入期不连库（CLAUDE.md 4.8）。
- ``setup()`` 幂等建表，首次部署自动完成，无需手工迁移。

用法（FastAPI lifespan / 脚本）::

    async with open_checkpoint_resources(settings.postgres_dsn.get_secret_value()) as res:
        graph = build_graph(checkpointer=res.saver, store=res.store)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore


@dataclass(frozen=True)
class CheckpointResources:
    """已就绪的检查点资源（二者均已完成 ``setup()``）。"""

    saver: AsyncPostgresSaver
    store: AsyncPostgresStore


@asynccontextmanager
async def open_checkpoint_resources(dsn: str) -> AsyncIterator[CheckpointResources]:
    """打开检查点与长期记忆存储，退出时自动释放连接池。

    Args:
        dsn: ``postgresql://user:pass@host:port/db`` 连接串
            （来自 ``Settings.postgres_dsn``，此处已是明文，调用方不得再落日志）。

    Yields:
        完成建表的 ``CheckpointResources``。

    Raises:
        psycopg.OperationalError: 数据库不可达（启动期 fail-fast，CLAUDE.md 3.5）。
    """
    async with (
        AsyncPostgresSaver.from_conn_string(dsn) as saver,
        AsyncPostgresStore.from_conn_string(dsn) as store,
    ):
        # setup() 幂等：已存在表结构时不重复变更
        await saver.setup()
        await store.setup()
        yield CheckpointResources(saver=saver, store=store)
