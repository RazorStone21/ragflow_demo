"""集成测试的门控与连接夹具（CLAUDE.md 9.3）。

**门控是双重的，缺一不可**：

1. ``RUN_INTEGRATION_TESTS=true`` + 真实 ``.env``：没配就跑不了，直接 skip；
2. 逐个服务做**可达性探测**：开关打开了但 compose 没起，同样 skip 而不是失败。

第二条尤其重要：CI 里"开关忘记关"与"服务真的挂了"是两件事——前者该 skip，
后者才该 fail。把两者混成同一个红灯，会训练出"看到集成测试失败就重跑"的习惯，
那等于没有这道门禁。可达性探测放在 ``_support.py``（见该模块说明）。

所有资源夹具都是**函数级**：pytest-asyncio 的 loop scope 配的是 ``function``，
会话级 async 夹具会绑到另一个事件循环上，表现为难查的跨循环报错。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from psycopg import OperationalError
from pydantic import ValidationError

from agent_platform.core.checkpoint import CheckpointResources, open_checkpoint_resources
from agent_platform.core.errors import ConfigurationError
from agent_platform.settings import Settings, load_settings
from tests.integration._support import HINT

# 各测试模块自行声明 ``pytestmark = pytest.mark.integration``：conftest 里的 pytestmark
# 会被 pytest 忽略（既不报错也不生效），靠它打标记是个静默的坑。


@pytest.fixture(scope="session")
def integration_settings() -> Settings:
    """加载真实配置；未开启开关或缺少必填项时 skip。

    会话级且**同步**：只读配置、不碰事件循环，因此不受 ``function`` loop scope 限制。
    """
    try:
        settings = load_settings()
    except (ConfigurationError, ValidationError, OSError) as exc:
        # 只捕这三种"配置读不出来"的具体原因（NO-06：不吞异常）。本夹具的职责是
        # "这次能不能跑"，读不出配置就该是一条带提示的 skip，而不是让整个
        # -m integration 会话以 ERROR 结束。
        pytest.skip(f"无法加载 .env（{type(exc).__name__}: {exc}）\n{HINT}")
    if not settings.run_integration_tests:
        pytest.skip(f"RUN_INTEGRATION_TESTS 未开启\n{HINT}")
    return settings


@pytest.fixture
async def checkpoint_resources(
    integration_settings: Settings,
) -> AsyncIterator[CheckpointResources]:
    """真实 Postgres 的检查点资源（已 ``setup()`` 建表）。

    Raises:
        Skipped: Postgres 不可达——compose 没起，属于"跳过"而非"失败"。
    """
    dsn = integration_settings.postgres_dsn.get_secret_value()
    try:
        async with open_checkpoint_resources(dsn) as resources:
            yield resources
    except OperationalError as exc:
        pytest.skip(f"Postgres 不可达（{type(exc).__name__}）\n{HINT}")
