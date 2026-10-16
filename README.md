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
uv run uvicorn --factory agent_platform.main:create_app \
    --loop asyncio:SelectorEventLoop --host 127.0.0.1 --port 8000
#    --loop asyncio:SelectorEventLoop 在 Windows 上必需：asyncio 默认的 ProactorEventLoop
#    不被 psycopg 异步模式支持，缺了会在启动时抛 InterfaceError；Linux/容器默认即
#    Selector，无需此参数。详见 docs/runbooks/local_env_setup.md「已知坑」。

# 7) 端到端冒烟（另开终端）
uv run python scripts/demo_chat.py --question "2025年新能源电价机制的主要内容？"
```

## 开发门禁（CLAUDE.md 第 9 / 12 章）

```bash
uv run ruff format --check && uv run ruff check && uv run mypy && uv run pytest
RUN_INTEGRATION_TESTS=true uv run pytest -m integration   # 需 compose 已启动

# 评估（阶段七；需 postgres / milvus / litellm 已启动，且网关已加载 judge 路由）
uv run python -X utf8 scripts/eval_run.py --validate-only                  # 零基础设施，只校验黄金集
uv run python -X utf8 scripts/eval_run.py --layer all                      # 全量评估，产出产物
uv run python -X utf8 scripts/eval_run.py --layer all \
  --baseline tests/evals/baselines/2026-10-16-phase7-signed.json           # 与基线对比，检出回归
```

评估**当前处于观察期**：照常产出报告、判定与留痕，`--baseline` 照常检出回归，但退出码暂不接入
pre-commit。**评估不进 pre-commit**——它慢、要密钥、要基础设施，放进去只会训练人加
`--no-verify`。评审协议与缺陷登记见 `tests/evals/README.md`，框架选型与被违反的条例见
[ADR-0003](docs/adr/0003-evaluation-framework-and-judge.md)。

黄金集 28 条**已全部人工签署**（`--require-reviewed` 通过）。当前入库基线有**两份**，且
**互不可比**：`2026-10-16-phase7-initial.json` 是"28 条草稿"期的真实记录，
`2026-10-16-phase7-signed.json` 是签署后的现行参照物（两者的黄金集指纹不同，对比会显式
喊出来）。两份都记着 D1/D2/D3 三条未修缺陷对应的失败，**均不代表达标**。

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

## 当前阶段边界（阶段七补齐）

**已落地**：评估层（`src/agent_platform/evaluation/` + `scripts/eval_run.py`）——28 条黄金集、
六个确定性判分器、网关裁判层、产物留痕与基线对比，见 `tests/evals/README.md`。

**仍为接口占位**：Langfuse/OTel 追踪（`core/tracing.py` 已就绪但未接线）、NeMo Guardrails + Presidio
护栏（`guardrails/__init__.py` 为 PassThrough，`container.py` 传 `guardrail=None`）、K8s 部署
（`deploy/k8s/` 仅占位 README）、HITL 中断恢复（全仓无 `interrupt()`，故黄金集该类别为 0 条，
**不编造**）。

**仍为演示期偏离（已封边界、入册）**：嵌入/重排经网关指向云端 API（ADR-0002）、裁判复用
同一上游导致同源自偏好偏置（ADR-0003）。**生产须切私有化 Qwen3 与独立家族的本地裁判模型**，
切换只改网关映射、业务代码零改动。**语料不得出域**——演示期仅限公开演示语料。

**已知质量缺陷（已登记、待修复）**：生成侧无"模型自称无法回答"分支（NO-12 实质违反）、
裁判提示词 v1 缺"即使答得出也必须拒答"一档。逐条证据见 `tests/evals/README.md` 第五节。

完整边界见 `CHANGELOG.md` 的 `v1.3.0` 与「未发布 · 阶段七」两条。
