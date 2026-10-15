"""请求/响应 DTO（CLAUDE.md 8.1 / 8.5：DTO 即契约）。

本包只放 **HTTP 边界的请求与响应模型**。两点边界说明：

- SSE 的**帧结构** ``SseEvent`` 属于 ``core/streaming.py``（CLAUDE.md 2.2 将
  「SSE/流事件封装」划归 ``core/``）。此处不再导出它——同一产物两个导入路径会
  让"以哪个为准"变得含糊。
- 领域模型（``Citation`` / ``RetrievalMeta`` / ``EvidenceItem``）**直接复用**
  既有定义，不复制字面量（CLAUDE.md 3.3 不得在多处重复定义同义结构）。
"""
