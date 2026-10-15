"""节点类型别名（叶子模块）。

单独成文件的原因：节点实现模块需要引用该别名，若把它放在 ``nodes/__init__.py``
会形成"包初始化 ← 子模块"的循环导入。此处不导入任何节点实现，保证依赖单向。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

GovPolicyQaNode = Callable[..., Awaitable[dict[str, Any]]]
"""节点可调用类型：接收状态（只读），返回**部分状态**字典（NO-04）。

各节点的注入参数（``writer`` / ``config``）由 LangGraph 按签名判定，
静态类型无法精确表达，故此处使用可变参数签名；参数约定见 ``nodes`` 包文档。
"""

__all__: list[str] = ["GovPolicyQaNode"]
