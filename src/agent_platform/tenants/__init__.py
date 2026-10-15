"""多租户与权限上下文（CLAUDE.md 10.2 / NO-19）。

租户标识只能来自**认证上下文**（令牌解析结果），不得来自请求体；本包提供
运行期读写租户上下文的唯一入口，供智能体节点与 API 层复用。
"""

from __future__ import annotations

from agent_platform.tenants.context import TENANT_ID_CONFIG_KEY, tenant_id_from_config

__all__: list[str] = ["TENANT_ID_CONFIG_KEY", "tenant_id_from_config"]
