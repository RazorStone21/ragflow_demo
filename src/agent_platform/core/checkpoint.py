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
from psycopg.conninfo import conninfo_to_dict, make_conninfo

DEFAULT_CONNECT_TIMEOUT_S = 10.0
"""建连超时默认值（秒）。可经 ``Settings.postgres_connect_timeout_s`` 调整。"""


@dataclass(frozen=True)
class CheckpointResources:
    """已就绪的检查点资源（二者均已完成 ``setup()``）。"""

    saver: AsyncPostgresSaver
    store: AsyncPostgresStore


def apply_connect_timeout(dsn: str, connect_timeout_s: float) -> str:
    """给连接串补上 ``connect_timeout``，已有则原样尊重。

    **为什么必须有这个超时**（本机实测，Postgres 由 Docker 只发布在 IPv4）：libpq 不带
    超时地连一个不可达地址时会一直挂住（内核 TCP 重传），而不是报错。
    ``socket.getaddrinfo("localhost", 5432)`` 在本机返回 ``['::1', '127.0.0.1']``，
    libpq 先试 ``::1`` —— Docker 没有 IPv6 监听，于是启动阶段**无限期卡死**、没有任何
    输出，排查成本极高。

    超时值按**每个地址**计算，因此它的作用是**把无限期挂起变成有界等待**，而不是让
    ``localhost`` 报错。实测（连一次 ``saver.setup()``）：

    - ``localhost`` ＋ ``connect_timeout=10``：10.1s 后**连上**（先在 ``::1`` 上卡满 10s，
      再回落到 ``127.0.0.1`` 成功）；
    - ``127.0.0.1`` ＋ ``connect_timeout=10``：0.0s 连上；
    - 不可解析主机（``*.invalid``）：立即 ``OperationalError``（与超时无关）；
    - 不可达 IP（``10.255.255.1``）＋ ``connect_timeout=5``：5.0s ``OperationalError``。

    也就是说：超时兜住最坏情况，**真正消除那 10 秒停顿的是把 DSN 写成 ``127.0.0.1``**
    （见 ``.env.example`` 与 ``docs/runbooks/local_env_setup.md``）。两者互补，缺一不可：
    前者让配置事故变成一条错误信息，后者让它不发生。

    用 ``setdefault`` 而非 ``make_conninfo(dsn, connect_timeout=...)``：后者会**覆盖**
    连接串里已有的同名参数（实测），使运维在 DSN 中的显式设置被静默吃掉。
    """
    # 归一化成 ``dict[str, str]``：``conninfo_to_dict`` 的值类型是 ``str | int | None``，
    # 直接 ``**`` 展开会被 mypy 判为与首个位置参数 ``conninfo: str`` 不兼容；
    # 而 None 值在 libpq 语义里表示"该参数未设置"，只能丢弃、不能 ``str()`` 成字面量 "None"。
    params = {key: str(value) for key, value in conninfo_to_dict(dsn).items() if value is not None}
    params.setdefault("connect_timeout", str(connect_timeout_s))
    return make_conninfo(**params)


@asynccontextmanager
async def open_checkpoint_resources(
    dsn: str, *, connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
) -> AsyncIterator[CheckpointResources]:
    """打开检查点与长期记忆存储，退出时自动释放连接池。

    Args:
        dsn: ``postgresql://user:pass@host:port/db`` 连接串
            （来自 ``Settings.postgres_dsn``，此处已是明文，调用方不得再落日志）。
        connect_timeout_s: 建连超时（秒）。连接串中若已自带 ``connect_timeout``，
            以连接串为准（见 ``apply_connect_timeout``）。

    Yields:
        完成建表的 ``CheckpointResources``。

    Raises:
        psycopg.OperationalError: 数据库不可达（启动期 fail-fast，CLAUDE.md 3.5）。
    """
    dsn = apply_connect_timeout(dsn, connect_timeout_s)
    async with (
        AsyncPostgresSaver.from_conn_string(dsn) as saver,
        AsyncPostgresStore.from_conn_string(dsn) as store,
    ):
        # setup() 幂等：已存在表结构时不重复变更
        await saver.setup()
        await store.setup()
        yield CheckpointResources(saver=saver, store=store)
