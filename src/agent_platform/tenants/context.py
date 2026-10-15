"""租户上下文的运行期读取（CLAUDE.md 4.4 / 10.2 / NO-19）。

LangGraph 的 ``RunnableConfig.configurable`` 是运行期上下文的传递通道：API 层在
启动 run 时写入**经认证解析**出的 ``tenant_id``，节点只读不写。这样做的理由：

- 节点因此不接收任何租户相关的用户输入，不存在"前端伪造租户"的入口（NO-19）；
- ``tenant_id`` 不进入 State，不会被 checkpoint 持久化后被后续 run 覆盖或误用；
- 缺失即失败（fail-closed）：宁可 401 也不以"默认租户"检索，避免跨租户串数据。
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from agent_platform.core.errors import UnauthorizedError

TENANT_ID_CONFIG_KEY = "tenant_id"
"""``config["configurable"]`` 中的租户键名（API 层写入，节点读取）。"""


def tenant_id_from_config(config: RunnableConfig | None) -> str:
    """从运行配置中取出已认证的租户标识。

    Args:
        config: LangGraph 注入的运行配置。

    Returns:
        非空租户标识字符串。

    Raises:
        UnauthorizedError: 配置缺失或租户标识为空——说明调用链未完成认证上下文注入。
            此处**不**回退到默认租户，避免降级为跨租户检索（NO-19 fail-closed）。
    """
    # config 与 configurable 对称判型：只兜 falsy 的话，truthy 非 dict 的 config
    # （调用方违反 RunnableConfig 契约时）会抛 AttributeError，把 401 变成 500。
    # 两种路径都不返回租户（fail-closed 不变），但错误映射必须停在 401（CLAUDE.md 8.2）。
    configurable: Any = config.get("configurable") if isinstance(config, dict) else None
    if not isinstance(configurable, dict):
        configurable = {}
    tenant_id = configurable.get(TENANT_ID_CONFIG_KEY)
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise UnauthorizedError("缺少已认证的租户上下文")
    return tenant_id.strip()
