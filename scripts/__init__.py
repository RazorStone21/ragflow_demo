"""运维/数据/评测脚本（CLAUDE.md 2.2）。

本包的存在只为让测试能 ``from scripts import ingest`` 直接对脚本逻辑做单测：
脚本里的幂等契约（清单比对、指纹变更后的旧产物清理）是**业务语义**，
不测就只能靠手工跑、跑完也说不清对不对。

不引入任何副作用——``ingest.py`` / ``demo_chat.py`` 仍以
``uv run python scripts/xxx.py`` 方式直接执行，包化不改变它们的用法。
"""
