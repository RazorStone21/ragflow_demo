"""幂等键支持（CLAUDE.md 8.3）。

复用 lifespan 里**已经打开**的 ``AsyncPostgresStore``：不引入新依赖、不新建表、
不让 API 层多持有一种连接。

命名空间取 ``(tenant_id, "idempotency")``，把租户放在**第一段**而不是拼进 key。
``BaseStore`` 的 namespace 本身就是隔离边界（CLAUDE.md 4.4 的长期记忆约定），
压平成一个字符串就等于把跨租户隔离降级成"key 里恰好没有冒号"这种约定——
一次拼接疏忽就是跨租户串数据。

**已知缺口（诚实记录，不掩饰）**：``BaseStore`` 的 TTL 只在构造 store 时给了 ``index``
配置才生效，当前 ``open_checkpoint_resources`` 未提供，故幂等条目**无界增长**。
演示期数据量可接受；已在 CHANGELOG 登记为基础设施阶段的待办，不在此处偷偷加定时清理
（那会引入一个不受应用生命周期管理的后台任务，NO-05）。
"""

from __future__ import annotations

import re

from fastapi import Request
from langgraph.store.base import BaseStore
from pydantic import ValidationError

from agent_platform.api.schemas.runs import RunResponse
from agent_platform.core.errors import ConflictError, ValidationFailedError

IDEMPOTENCY_NAMESPACE = "idempotency"
"""``BaseStore`` 命名空间的第二段；第一段永远是 ``tenant_id``。"""

IDEMPOTENCY_HEADER = "Idempotency-Key"
"""幂等键请求头（CLAUDE.md 8.3）。"""

_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
"""幂等键白名单：与 request_id 同理，这个值会成为 store 主键并进日志。"""


def read_idempotency_key(request: Request) -> str | None:
    """读取并校验 ``Idempotency-Key`` 头；未提供返回 ``None``。

    Raises:
        ValidationFailedError: 提供了但格式非法（422，不得重试）。
    """
    raw = request.headers.get(IDEMPOTENCY_HEADER)
    if raw is None:
        return None
    key = raw.strip()
    if not _IDEMPOTENCY_KEY_PATTERN.match(key):
        raise ValidationFailedError(
            f"Idempotency-Key 格式非法（长度 {len(key)}）",
            detail="Idempotency-Key 需为 1-128 位、仅含字母数字与 . _ : -",
        )
    return key


async def lookup_run(store: BaseStore, *, tenant_id: str, key: str) -> RunResponse | None:
    """查已缓存的运行结果；未命中返回 ``None``。

    Raises:
        ConflictError: 记录存在但无法反序列化。属于缺陷而非"未命中"——
            静默当作未命中会让同一个幂等键重新跑一次 run，那正是幂等要防的事。
    """
    item = await store.aget((tenant_id, IDEMPOTENCY_NAMESPACE), key)
    if item is None:
        return None
    try:
        return RunResponse.model_validate(item.value)
    except ValidationError as exc:
        raise ConflictError(
            f"幂等记录与当前契约不兼容（{exc.error_count()} 处错误）",
            detail="该幂等键对应的历史记录无法解析，请改用新的 Idempotency-Key。",
        ) from exc


async def remember_run(
    store: BaseStore, *, tenant_id: str, key: str, response: RunResponse
) -> None:
    """缓存本次运行结果，供同一幂等键的后续请求直接返回。"""
    await store.aput((tenant_id, IDEMPOTENCY_NAMESPACE), key, response.model_dump(mode="json"))


__all__: list[str] = [
    "IDEMPOTENCY_HEADER",
    "IDEMPOTENCY_NAMESPACE",
    "lookup_run",
    "read_idempotency_key",
    "remember_run",
]
