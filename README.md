# agent-platform — 企业级智能体平台（RAG 主线）

> **开工前必读**：本仓库最高工程规则与唯一事实来源是 [`CLAUDE.md`](CLAUDE.md)。
> 人类工程师与 AI 编程助手在编写、评审、修改代码前都必须先阅读并遵守。
> 本文与 CLAUDE.md 冲突时，**一律以 CLAUDE.md 为准**。

## 这是什么

面向政务/能源政策文档的 RAG 问答平台。当前阶段（v1.2.0）交付**最小合规骨架 + RAG 主线**：

- **编排主干**：LangGraph 1.x（[ADR-0001](docs/adr/0001-langgraph-as-orchestration-backbone.md)）
- **知识后端（双后端分工，[ADR-0002](docs/adr/0002-ragflow-milvus-division-of-labor.md)）**：
  - **RAGFlow**（v0.27.2，内存引擎 Infinity）= 文档工程：解析 / OCR / 清洗 / 切分 / 文档管理
  - **Milvus**（v2.6）= 检索主链路：稠密 + BM25 混合检索 + Rerank + 引用溯源 + 租户过滤
- **模型网关**：LiteLLM Proxy（业务代码不直连厂商，NO-02）
- **模型**：LLM `primary-chat` → deepseek-chat；嵌入/重排 `embedding`/`rerank` → SiliconFlow Qwen3 系列
  （演示期走云端 API，上线切换私有化只改网关映射）

架构决策与规范演进记录在 [`docs/adr/`](docs/adr/) 与 [`CHANGELOG.md`](CHANGELOG.md)。

## 快速开始

前置：Docker Desktop（WSL2 后端）、[uv](https://docs.astral.sh/uv/)、Python 3.12（`uv python install 3.12`）。

```bash
# 1) 配置环境（.env 严禁入库，NO-17）
cp .env.example .env        # 必填项见 .env.example 尾部的说明，缺一项就起不来

# 2) 启动基础设施（Postgres / Redis / Milvus / LiteLLM）
docker compose up -d
docker compose ps           # 等全部 healthy

# 3) 首次使用 RAGFlow：按 docs/runbooks/ragflow_setup.md 完成初始化并生成 API Key，
#    并核实 SiliconFlow 模型 ID 与嵌入维度

# 4) 同步依赖
uv sync

# 5) 入库语料（先 dry-run，再真实入库；语料目录不入库）
uv run python scripts/ingest.py --source "e:\BaiduNetdiskDownload\gov_doc" --subset scripts/energy_subset.txt --dry-run
uv run python scripts/ingest.py --source "e:\BaiduNetdiskDownload\gov_doc" --subset scripts/energy_subset.txt

# 6) 启动 API（工厂形态：main.py 刻意没有模块级 app，见该模块文档字符串）
uv run uvicorn --factory agent_platform.main:create_app --host 127.0.0.1 --port 8000
#    仅 Windows 追加 --loop asyncio:SelectorEventLoop：默认的 ProactorEventLoop 不被
#    psycopg 异步模式支持，会直接抛 InterfaceError。Linux/容器无需此参数。
#    详见 docs/runbooks/local_env_setup.md「已知坑」。

# 7) 端到端冒烟（另开终端）
uv run python scripts/demo_chat.py --question "2025年新能源电价机制的主要内容？"
```

## 开发门禁（CLAUDE.md 第 9 / 12 章）

```bash
uv run ruff format --check && uv run ruff check && uv run mypy && uv run pytest
RUN_INTEGRATION_TESTS=true uv run pytest -m integration   # 需 compose 已启动
```

提交遵循 Conventional Commits；不得 `--no-verify`。AI 协作规则见 CLAUDE.md 12.4。

## 目录导航

| 路径 | 说明 |
|---|---|
| `src/agent_platform/core/` | 配置、异常、重试、日志、模型网关、检查点、流式、追踪 |
| `src/agent_platform/knowledge/` | RAGFlow 客户端、Milvus 存取、嵌入、重排、检索器 |
| `src/agent_platform/agents/` | 智能体（当前：`gov_policy_qa`） |
| `src/agent_platform/api/` | FastAPI 装配、路由、DTO、中间件 |
| `scripts/` | 入库与演示脚本（均带 `--help`，可重复执行） |
| `tests/unit/` | 离线单元测试（fake 掉一切外部依赖，必须快且确定） |
| `tests/integration/` | 需真实基础设施的集成测试（`RUN_INTEGRATION_TESTS` 门控，未就绪自动 skip） |
| `docs/adr/` | 架构决策记录（只增不改） |
| `docs/runbooks/` | 环境与组件的手册（本机环境、RAGFlow 部署） |
| `deploy/` | 本地 compose 片段与 K8s 模板 |

## 当前阶段边界（阶段二补齐）

Langfuse/OTel 追踪、NeMo Guardrails + Presidio 护栏、黄金集 + RAGAS 评估门禁、
K8s 部署、HITL 中断恢复、私有化 Qwen3 切换 —— 均为接口占位，见 `CHANGELOG.md` v1.2.0。
