# agent-platform — 企业级智能体平台（RAG 主线）

> 本仓库的工程规则以 [`CLAUDE.md`](CLAUDE.md) 为准。它既是最高约束，也是唯一事实来源，
> 写代码、评审、改动之前都请先读一遍。本文与它冲突时按它执行。

## 这是什么

面向政务与能源政策文档的 RAG 问答平台。目前跑通的是最小合规骨架和 RAG 主线。

编排用 LangGraph 1.x，见 [ADR-0001](docs/adr/0001-langgraph-as-orchestration-backbone.md)。
知识侧分两个后端（[ADR-0002](docs/adr/0002-ragflow-milvus-division-of-labor.md)）：RAGFlow
（v0.27.2，文档引擎设为 Infinity）管文档工程，解析、OCR、清洗、切分和文档生命周期都在它那里；
Milvus（v2.6）管检索，稠密向量与 BM25 混合检索、Rerank、引用溯源、租户过滤都由它承担。

扫描件、PDF 内嵌图与表格截图也走这条链路：RAGFlow 在解析期用视觉模型生成图片描述，
平台把图片二进制另存一份，检索证据和引用带上图片元数据，客户端凭会话取图
（[ADR-0004](docs/adr/0004-multimodal-document-evidence.md)）。

模型调用统一走 LiteLLM Proxy，业务代码不直连厂商（NO-02）。模型按逻辑名引用：`primary-chat`
对应 deepseek-chat，`embedding` 和 `rerank` 对应 SiliconFlow 的 Qwen3 系列，`vision-chat`
对应 Qwen3-VL（解析期的图片理解）。演示期这些请求发往云端 API，上线换成私有化部署只需改
网关映射。

架构决策记在 [`docs/adr/`](docs/adr/)，版本演进记在 [`CHANGELOG.md`](CHANGELOG.md)。

## 快速开始

需要 Docker Desktop（WSL2 后端）、[uv](https://docs.astral.sh/uv/) 和 Python 3.12
（`uv python install 3.12`）。

```bash
# 1) 配置与依赖。.env 严禁入库（NO-17），按 .env.example 的注释补齐必填项
cp .env.example .env
uv sync

# 2) 起基础设施：Postgres / Redis / Milvus / LiteLLM
docker compose up -d
docker compose ps           # 等全部 healthy

# 3) 初始化 RAGFlow：生成 API Key、核实模型 ID、设默认 vision 模型
#    步骤见 docs/runbooks/ragflow_setup.md

# 4) 入库语料。先加 --dry-run 看清单；能源文本与多模态演示集分别跑一遍
uv run python scripts/ingest.py --source "e:\BaiduNetdiskDownload\gov_doc" --subset scripts/energy_subset.txt
uv run python scripts/ingest.py --source "e:\BaiduNetdiskDownload\gov_doc" --subset scripts/multimodal_subset.txt

# 5) 起 API。main.py 没有模块级 app，所以用工厂形态
uv run uvicorn --factory agent_platform.main:create_app --loop asyncio:SelectorEventLoop --host 127.0.0.1 --port 8000
#    Windows 必须带 --loop asyncio:SelectorEventLoop，否则 psycopg 异步连接会抛
#    InterfaceError；Linux 默认就是 Selector。

# 6) 另开终端冒烟
uv run python scripts/demo_chat.py --question "2025年新能源电价机制的主要内容？"
```

多模态入库与取图的细节见 [multimodal_evidence.md](docs/runbooks/multimodal_evidence.md)。

## 多模态证据

链路分三段：RAGFlow 解析时把扫描页、内嵌图与表格截图交给视觉模型，生成描述并入切分
正文；入库脚本把图片二进制另存到 `EVIDENCE_IMAGE_STORE_DIR`（默认
`local_data/evidence_images`，按租户和文档分目录），Milvus 行里只存图片元数据
（`image_id`、`sha256`、`mime`、页码）；检索命中后，证据与引用带同样的元数据，客户端
凭会话取图：

```text
GET /api/v1/threads/{thread_id}/evidence/{chunk_id}/images/{image_id}
```

取图只认**本次会话证据里真实存在的图片**，URL 里的 ID 不当作全局查找键；除未认证返回
401 外，其余取不到的情形一律 404，避免把"图存在与否"暴露成探针。`scripts/demo_chat.py`
会把带图引用打印成可直接打开的地址。

配置、验证与故障处理见 [docs/runbooks/multimodal_evidence.md](docs/runbooks/multimodal_evidence.md)。
演示期视觉模型指向云端，图片内容出域，仅限公开演示语料；生产必须切私有化 VLM。图片存本地
文件系统也没有备份与生命周期管理，生产换对象存储是后续 ADR 项。

## 开发门禁

CLAUDE.md 第 9、12 章定下了这些检查，提交前逐条跑一遍：

```bash
uv run ruff format --check && uv run ruff check && uv run mypy && uv run pytest
RUN_INTEGRATION_TESTS=true uv run pytest -m integration   # 需要 compose 已经起来

# 评估：需要 postgres / milvus / litellm 就绪，且网关已加载 judge 路由
# RAGAS 在 eval 依赖组里，评估命令一律 --group eval（默认 uv sync 不安装）
uv run --group eval python -X utf8 scripts/eval_run.py --validate-only       # 只校验黄金集，不碰基础设施
uv run --group eval python -X utf8 scripts/eval_run.py --layer all           # 全量：检索 + 生成 + 裁判
uv run --group eval python -X utf8 scripts/eval_run.py --layer all \
  --baseline tests/evals/baselines/<file>.json                               # 与基线对比
```

评估处在观察期：报告、判定、留痕照常产出，`--baseline` 也照常检出回归，只是退出码还不接进
pre-commit。评估不进 pre-commit，因为它慢、要密钥、还要整套基础设施；硬塞进去只会让人养成加
`--no-verify` 的习惯。评审协议和缺陷登记在 `tests/evals/README.md`，指标口径、RAGAS 依赖组
与旧指标退役写在 [ADR-0006](docs/adr/0006-ragas-in-process-evaluation.md)。

2026-10-21 起指标方案已整体更换：检索看 Recall@8 / Precision@8 / MRR，生成看 RAGAS 的
Faithfulness / Answer Relevancy / Answer Correctness，另加引用准确率与拒答准确率；幻觉率
单独监控。`retrieval_hit` 与 `status_accuracy` 已退役。产物结构升到 schema v2，两份旧基线
（`2026-10-16-phase7-*.json`）只作历史，新代码读入即报不可读。

**检索三项的前置条件已具备**：21 条可答题的 `relevant_documents` 由 AI 起草（共 31 条），
并经 `ks` 于 2026-10-21 核对签署，`--require-relevance-labels` 已通过。逐题摘录与签署记录
在 `local_data/evals/relevance-label-review.md`（本地产物，不入库）。仍没有 v2 基线：全量与
引用人工抽检还没跑，先跑完再谈基线。

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
| `docs/runbooks/` | 环境与组件手册：本机环境、RAGFlow 部署、多模态证据链路 |
| `deploy/` | 本地 compose 片段与 K8s 模板 |

## 进度与边界

评估层的代码在 `src/agent_platform/evaluation/`，入口是 `scripts/eval_run.py`：
32 条黄金集、七个确定性判分器、RAGAS 三项进程内判分、引用裁判，产物留痕和基线对比都能用。
代码与离线单测已完成，**尚未在真实基础设施上跑过 v2 全量**——语料与标签均已就绪
（200 份文档 / 2004 条切分，21 份黄金集文档全部在库；21 条相关性标签已签署），但全量
还没跑，所以也没有 v2 基线。细节见 `tests/evals/README.md`。

还有几处只有接口、没有实现，引用时请按占位对待：Langfuse 和 OpenTelemetry 的追踪
（`core/tracing.py` 写好了但没接线）；NeMo Guardrails 与 Presidio 护栏
（`guardrails/__init__.py` 目前是 PassThrough，`container.py` 传的是 `guardrail=None`）；
K8s 部署（`deploy/k8s/` 只有一个占位 README）；HITL 中断恢复（全仓还没有任何 `interrupt()`，
所以黄金集里这个类别是 0 条，没有编造）。

演示期的偏离都写在 ADR 里。嵌入、重排与视觉理解经网关指向云端 API（ADR-0002、ADR-0004）；
裁判经网关指向云端 SiliconFlow 的 Qwen，与生成模型 DeepSeek 不同家族，自偏好偏置随之消失，
但评估数据（问题、证据、答案）仍会出域（ADR-0005）；图片存本地文件系统而不是对象存储
（ADR-0004）。生产环境必须换成私有化的 Qwen3 与 VLM、私有化的本地裁判模型，图片迁到
MinIO/S3，前三项届时只改网关映射与存储实现，业务代码不用动。语料不得出域，演示期只用
公开语料。

剩下的质量缺口是 D3：三条边界题的检索没取到支持答案的段落，模型照着现有证据拒答，
黄金集期望仍是 `answered`。旧 `retrieval_hit` 只核对文档名，看不出段落是否相关；
v2 的 Recall / Precision / MRR 要等相关性标签补齐后才能给出更细的证据。D1（生成侧
没有「模型自称无法回答」的分支）与 D2（裁判提示词少了「即使答得出也必须拒答」这一档）
已在 2026-10-17 修复。旧评估的 D11（拒答文案的 faithfulness 被记成测量失败）随自研
生成指标一并退役：v2 对非 `answered` 样例的 RAGAS 三项直接记「不适用」，由
`refusal_correctness` 兜底。

更完整的边界见 `CHANGELOG.md` 的 `v1.3.0`、`v1.6.0` 和「未发布 · 阶段七」三条。
