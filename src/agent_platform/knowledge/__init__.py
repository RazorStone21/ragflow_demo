"""知识层：文档工程（RAGFlow）与检索主链路（Milvus）的客户端封装。

分层（ADR-0002 双后端分工）：

- ``ragflow_client`` —— 仅做文档工程（上传/解析/切分），不做检索；
- ``milvus_schema`` / ``milvus_store`` —— 检索主链路（混合检索 + 引用 + 租户过滤）；
- ``embedder`` / ``reranker`` / ``gateway_client`` —— 经 LiteLLM 网关的模型调用（NO-02）；
- ``retriever`` —— 编排上述组件，产出带引用的证据；
- ``schemas`` —— 跨层数据契约（``ChunkRecord`` / ``EvidenceItem`` 等）。

依赖方向：``agents`` → ``knowledge`` → ``core``/``utils``；
本包**不得**反向导入任何具体智能体或 FastAPI 组件（CLAUDE.md 2.3）。
"""
