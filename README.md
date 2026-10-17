# agent-platform — 企业级智能体平台（RAG 主线）

> 本仓库的工程规则以 [`CLAUDE.md`](CLAUDE.md) 为准。它既是最高约束，也是唯一事实来源，
> 写代码、评审、改动之前都请先读一遍。本文与它冲突时按它执行。

## 这是什么

面向政务与能源政策文档的 RAG 问答平台。目前跑通的是最小合规骨架和 RAG 主线。

编排用 LangGraph 1.x，见 [ADR-0001](docs/adr/0001-langgraph-as-orchestration-backbone.md)。
知识侧分两个后端（[ADR-0002](docs/adr/0002-ragflow-milvus-division-of-labor.md)）：RAGFlow
（v0.27.2，文档引擎设为 Infinity）管文档工程，解析、OCR、清洗、切分和文档生命周期都在它那里；
Milvus（v2.6）管检索，稠密向量与 BM25 混合检索、Rerank、引用溯源、租户过滤都由它承担。

模型调用统一走 LiteLLM Proxy，业务代码不直连厂商（NO-02）。模型按逻辑名引用：`primary-chat`
对应 deepseek-chat，`embedding` 和 `rerank` 对应 SiliconFlow 的 Qwen3 系列。演示期这些请求
发往云端 API，上线换成私有化部署只需改网关映射。

架构决策记在 [`docs/adr/`](docs/adr/)，版本演进记在 [`CHANGELOG.md`](CHANGELOG.md)。

## 快速开始

前置条件：Docker Desktop（WSL2 后端）、[uv](https://docs.astral.sh/uv/)、Python 3.12
（`uv python install 3.12`）。

```bash
# 1) 配置环境。.env 严禁入库（NO-17），必填项见 .env.example 末尾，缺一项就起不来
cp .env.example .env

# 2) 启动基础设施：Postgres / Redis / Milvus / LiteLLM
docker compose up -d
docker compose ps           # 等全部 healthy

# 3) 第一次用 RAGFlow，先按 docs/runbooks/ragflow_setup.md 初始化并生成 API Key，
#    再核实 SiliconFlow 的模型 ID 和嵌入维度

# 4) 同步依赖
uv sync

# 5) 入库语料。先 dry-run 看清单，再真入库；语料目录本身不进仓库
uv run python scripts/ingest.py --source "e:\BaiduNetdiskDownload\gov_doc" --subset scripts/energy_subset.txt --dry-run
uv run python scripts/ingest.py --source "e:\BaiduNetdiskDownload\gov_doc" --subset scripts/energy_subset.txt

# 6) 启动 API。main.py 刻意没有模块级 app，所以这里用工厂形态
uv run uvicorn --factory agent_platform.main:create_app \
    --loop asyncio:SelectorEventLoop --host 127.0.0.1 --port 8000
#    Windows 上必须带 --loop asyncio:SelectorEventLoop：asyncio 默认的 ProactorEventLoop
#    不被 psycopg 的异步模式支持，少了这个参数会在启动时抛 InterfaceError。Linux 和容器
#    默认就是 Selector，不必加。详见 docs/runbooks/local_env_setup.md 的「已知坑」。

# 7) 端到端冒烟，另开一个终端
uv run python scripts/demo_chat.py --question "2025年新能源电价机制的主要内容？"
```

## 开发门禁

CLAUDE.md 第 9、12 章定下了这些检查，提交前逐条跑一遍：

```bash
uv run ruff format --check && uv run ruff check && uv run mypy && uv run pytest
RUN_INTEGRATION_TESTS=true uv run pytest -m integration   # 需要 compose 已经起来

# 评估：需要 postgres / milvus / litellm 就绪，且网关已加载 judge 路由
uv run python -X utf8 scripts/eval_run.py --validate-only                  # 只校验黄金集，不碰基础设施
uv run python -X utf8 scripts/eval_run.py --layer all                      # 全量跑一轮，产出产物
uv run python -X utf8 scripts/eval_run.py --layer all \
  --baseline tests/evals/baselines/2026-10-16-phase7-signed.json           # 与基线对比，检出回归
```

评估眼下处在观察期：报告、判定、留痕照常产出，`--baseline` 也照常检出回归，只是退出码
还不接进 pre-commit。评估本身不进 pre-commit，它慢，要密钥，还要整套基础设施，硬塞进去
只会让人养成加 `--no-verify` 的习惯。评审协议和缺陷登记在 `tests/evals/README.md`，框架
选型以及 RAGAS 违反了哪几条，写在 [ADR-0003](docs/adr/0003-evaluation-framework-and-judge.md)。

黄金集 28 条已经全部人工签署，`--require-reviewed` 能过。已入库的基线有两份，彼此不可比：
`2026-10-16-phase7-initial.json` 记的是「28 条草稿」时期的真实结果，
`2026-10-16-phase7-signed.json` 是签署之后现行的参照。两份文件的黄金集指纹不同，拿它们
互相对比会直接报出来。这两份基线都带着 D1、D2、D3 三条未修缺陷造成的失败，**都不代表达标**。

提交信息按 Conventional Commits 写，不得用 `--no-verify` 跳过钩子。AI 协作的规矩见
CLAUDE.md 12.4。

## 目录导航

| 路径 | 说明 |
|---|---|
| `src/agent_platform/core/` | 配置、异常、重试、日志、模型网关、检查点、流式、追踪 |
| `src/agent_platform/knowledge/` | RAGFlow 客户端、Milvus 存取、嵌入、重排、检索器 |
| `src/agent_platform/agents/` | 智能体，目前只有 `gov_policy_qa` |
| `src/agent_platform/api/` | FastAPI 装配、路由、DTO、中间件 |
| `scripts/` | 入库和演示脚本，都带 `--help`，可以重复执行 |
| `tests/unit/` | 离线单测，外部依赖全部 fake 掉，要快、要确定 |
| `tests/integration/` | 需要真实基础设施，由 `RUN_INTEGRATION_TESTS` 门控，服务没起自动 skip |
| `docs/adr/` | 架构决策记录，只增不改 |
| `docs/runbooks/` | 环境与组件手册：本机环境、RAGFlow 部署 |
| `deploy/` | 本地 compose 片段与 K8s 模板 |

## 进度与边界

评估层已经做完，代码在 `src/agent_platform/evaluation/`，入口是 `scripts/eval_run.py`：
28 条黄金集、六个确定性判分器、一层网关裁判，产物留痕和基线对比都能用。细节见
`tests/evals/README.md`。

还有几处只有接口、没有实现，引用时请按占位对待：Langfuse 和 OpenTelemetry 的追踪
（`core/tracing.py` 写好了但没接线）；NeMo Guardrails 与 Presidio 护栏
（`guardrails/__init__.py` 目前是 PassThrough，`container.py` 传的是 `guardrail=None`）；
K8s 部署（`deploy/k8s/` 只有一个占位 README）；HITL 中断恢复（全仓还没有任何 `interrupt()`，
所以黄金集里这个类别是 0 条，没有编造）。

演示期有两处偏离，边界都写在 ADR 里。嵌入和重排经网关指向云端 API（ADR-0002）；裁判和
生成共用同一个上游模型，存在自偏好偏置（ADR-0003）。生产环境必须换成私有化的 Qwen3 和
独立家族的本地裁判模型，届时只改网关映射，业务代码不用动。语料不得出域，演示期只用
公开语料。

已知的质量缺陷还没修：生成侧没有「模型自称无法回答」的分支，实质违反 NO-12；裁判提示词
v1 少了「即使答得出也必须拒答」这一档。逐条证据在 `tests/evals/README.md` 第五节。

更完整的边界见 `CHANGELOG.md` 的 `v1.3.0` 和「未发布 · 阶段七」两条。
