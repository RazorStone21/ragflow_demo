"""HTTP 边界（CLAUDE.md 2.2 / 第 8 章）：路由、DTO、依赖注入与中间件。

依赖方向单向向内：``api → agents → core/tools/knowledge → utils``。
本包不得被内层导入；节点、工具与 ``core/`` 也不得依赖 FastAPI 的 ``Request/Response``
（CLAUDE.md 2.3）。

约定（本阶段确立，规则文件同步见 CHANGELOG 的「阶段四」条目）：

- **成功响应直接返回资源 DTO**，不使用 ``{"data": ...}`` 包装（CLAUDE.md 8.2 二选一）；
- 错误响应统一 ``application/problem+json``（RFC 9457），由 ``middleware/errors.py`` 统一产出；
- SSE 帧的线格式**唯一**由 ``core/streaming.py`` 决定，本包只决定"发什么、何时发"。

本 ``__init__`` 不做任何再导出：``api`` 是入口层，转发式再导出只会制造第二个导入路径。
"""
