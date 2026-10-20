# CLAUDE.md — 企业级智能体开发工程规则

> 本文件是本仓库（及由本仓库派生的所有智能体服务）的**最高工程规则与唯一事实来源（Single Source of Truth）**。
> 所有人类工程师与 AI 编程助手（Claude Code / Cursor / Copilot / Trae 等）在编写、评审、修改代码前，都必须先阅读并遵守本文件。
> 当本文件与临时口头约定、网络教程、AI 即兴建议冲突时，**一律以本文件为准**；若认为本文件需要变更，走[第 14 章「规范演进」](#14-规范演进与维护)流程，而不是就地违反。

| 项 | 值 |
|---|---|
| 文档版本 | **v1.5.1** |
| 生效日期 | 2026-10-20 |
| 状态 | Active（强制执行） |
| 适用范围 | 企业级智能体平台后端、智能体应用、MCP 工具服务、RAG/知识服务、部署与评估流水线 |
| 编排主干（已锁定） | **LangGraph 1.x**（详见 [第 1 章](#1-技术栈锁定)、[ADR-0001](docs/adr/0001-langgraph-as-orchestration-backbone.md)） |
| 维护方 | 平台架构组 |
| 配套文件 | `CHANGELOG.md`（版本演进台账）、`docs/adr/`（架构决策记录） |

**约定语气（RFC 2119）**：**必须 / 不得（MUST / MUST NOT）**＝强制，违反不予合入；**应当（SHOULD）**＝默认遵守，偏离须在 PR 说明理由；**可（MAY）**＝可选。

---

## 0. AI 编程助手与开发者总则

1. **开工前必读**：每次会话/每个任务开始时，必须先通读本文件与 `README`、相关 `docs/adr/`；改动既有模块前必须先读该模块代码与测试，不得凭命名猜测其行为。
2. **最小改动**：只改任务要求的范围。不得顺手重构无关代码、不得改动与任务无关的依赖版本、不得删除他人的注释与结论性备注。
3. **规则同步义务**：当你的改动引入了新依赖、新目录、新的跨模块约定、新的环境变量或改变了既有约定，**必须在同一次提交中更新 `CLAUDE.md` 与 `CHANGELOG.md`**（必要时新增 ADR）。代码与规则不一致视为缺陷。
4. **事实与来源**：不得臆造 API、参数、类名或版本号。不确定的框架用法必须查官方文档或源码核实；引用版本号必须来自实际依赖锁定文件或官方发布页。
5. **失败要出声**：遇到测试失败、校验不过、依赖冲突、许可证风险，必须定位根因并修复或显式上报，**不得**通过注释掉测试、加 `# type: ignore`、裸 `except`、`--no-verify` 等方式让流程"变绿"。
6. **安全默认**：不得在代码、Prompt、日志、提交信息中硬编码或泄露密钥、连接串、客户数据、系统提示词全文。涉及对外/写操作/删除操作，先确认授权与可逆性。
7. **不确定时**：选择与本文件一致的最小方案；若存在会改变架构或有多种合理解释的分歧，**先提出问题并给出推荐项与权衡，不得擅自拍板**。
8. **交付即验证**：完成后必须运行对应的格式化、类型检查、单测/集成测试与最小真实路径验证（见[第 9 章](#9-可观测性评估与测试)），并在交付说明中写明验证了什么、结果如何、还缺什么。
9. **减少生成文本的AI味**：项目中生成的中文文本要贴合日常使用习惯，减少AI自创的词语，避免AI常见的重复性表达，增加语言的自然变化性，按照汉语言文学标准调整表达水准，减少无意义的介词、连词，去除明显的AI痕迹和机器化表达。

---

## 1. 技术栈（锁定）

下表为**基线版本（已验证可协同工作）**。补丁版本应随官方稳定版更新；** minor / major 升级、替换任何"锁定/必选"组件，必须提交 ADR 并经架构组评审**。所有运行时依赖必须私有化可获取（内网镜像），且许可证经法务确认（见 [10.6](#106-许可证与合规红线)）。

| 分层 | 选型（锁定/必选） | 基线版本 | 备选（需 ADR） | 私有化 / License |
|---|---|---|---|---|
| 语言运行时 | **Python** | ≥ 3.11，< 3.13 | — | PSF |
| 包/环境管理 | **uv**（workspace monorepo） | 最新稳定 | poetry / pdm（全仓统一，不得多套并存） | MIT/Apache |
| 编排框架 | **LangGraph** | 1.x（1.0 GA 2025-10-22，基线 1.2.x；本仓落地 **1.2.15**） | AutoGen / CrewAI / Dify 画布**不得替代核心编排** | 库 MIT；Platform 商业（Self-Hosted Enterprise 可离线） |
| 持久化检查点 | **langgraph-checkpoint-postgres**（`PostgresSaver` / `PostgresStore`） | 3.1.x（本仓落地 **3.2.0**） | Redis 仅作队列/缓存，不作权威检查点 | MIT |
| 多智能体 | **langgraph-supervisor / langgraph-swarm** | 与 LangGraph 1.x 匹配 | 自研 Subgraph 编排 | MIT |
| 模型/组件 SDK | **LangChain 1.0**（`langchain-classic` 与独立集成包）；检索子系统可用 **LlamaIndex** | 1.x（本仓落地 **langchain-openai 1.7.2**） | — | MIT（`langchain-community` 已归档，**禁止新增引用**） |
| 模型网关 | **LiteLLM Proxy**（统一入口） | v1.104+（**PyPI 口径**；**镜像 tag 编号不跟 PyPI 发布号**，本仓以镜像 tag 为权威落地值：**`main-v1.82.3`**） | Higress；New-API（**AGPL，未过法务禁止入生产**） | LiteLLM MIT |
| 工具/上下文协议 | **MCP（Model Context Protocol）** | 2025 后稳定规范，`pip install "langchain[mcp]"` | A2A 仅用于跨厂商智能体互操作（预研） | 开放标准 |
| HTTP API | **FastAPI** + Uvicorn(worker 由 Gunicorn 管理) | 0.115+（本仓落地 **0.143.1**） | — | MIT |
| 数据校验 | **Pydantic v2**（`pydantic-settings` 管配置） | 2.x（本仓落地 **2.14.1**） | — | MIT |
| 流式协议 | **SSE**（`sse-starlette` / LangGraph 原生 `astream`） | — | WebSocket（仅双向交互场景，需 ADR） | MIT |
| 关系/检查点库 | **PostgreSQL** | 15+ | — | PostgreSQL License |
| 缓存/队列 | **Redis**（LangGraph 任务队列、缓存、限流） | 7+ | — | RSAL/开源版（注意 7.4 后 license，内网自用合规） |
| 向量库 | **Milvus**（生产） / **pgvector**（轻量单机） | Milvus 2.6（本仓落地 **v2.6.24** standalone） | Qdrant / Weaviate（需 ADR） | Apache-2.0 / PostgreSQL |
| RAG 平台 | **RAGFlow**（知识后端，经 REST/MCP 调用） | v0.27+（本仓实测 **v0.27.2**，`DOC_ENGINE=infinity`、CPU） | 自研 LlamaIndex 管线；**不得用其画布替代 LangGraph 编排** | Apache-2.0（注意其附加条款） |
| 嵌入/重排 | **Qwen3-Embedding / Qwen3-Reranker**（**生产须私有化**） | 最新稳定（**本仓演示期经网关指向云端 API，语料出域，仅限公开演示语料**——边界见 [第 7 章](#7-知识与-rag-规范)第 8 条） | bge-m3 / bge-reranker | Apache（各模型权重 License 单独确认） |
| 解析期视觉模型 | **网关逻辑名 `vision-chat`**（RAGFlow 注册为 `vision` 类型模型并设为默认，解析时把图片转成文字描述） | 演示期经网关指向云端 Qwen3-VL；**生产必须切换为私有化 VLM**（[ADR-0004](docs/adr/0004-multimodal-document-evidence.md)） | 本地 Qwen3-VL / bge-vl（需显存与精度压测） | 模型权重 License 单独确认 |
| 证据图片存储 | **本地文件存储**（`knowledge/images.py` 的 `ImageStore` 协议隔离） | 演示期 `local_data/evidence_images/`，按租户/文档/切分分目录 | MinIO / S3 对象存储（生产，需 ADR） | 本仓自研；对象存储按其自身许可 |
| 知识图谱/本体 | **owlready2** + Protégé（OWL/SWRL 编辑）；GraphRAG/LightRAG 按需 | — | — | MIT/Apache |
| 长期记忆 | **LangGraph `PostgresStore`**（默认同栈）；按需 **Mem0 / Graphiti** | — | Letta | MIT/Apache（Mem0 部分能力受限，需核实） |
| 可观测性 | **Langfuse（自托管）** + **OpenTelemetry** | Langfuse 稳定自托管版 | LangSmith（SaaS，涉数据出域禁止）/ Arize Phoenix | Langfuse 核心 MIT |
| 评估 | **RAGAS / DeepEval / Promptfoo** + 自研黄金集 | RAGAS 最新稳定（本仓隔离环境实测 **0.4.3**）；确定性判分器自研，**无版本依赖** | LangSmith eval（离线合规前禁用） | MIT/Apache（**RAGAS 见下注**） |
| 安全护栏 | **NeMo Guardrails** + Llama Guard/Prompt Guard（本地权重）+ **Presidio**（PII 脱敏） | 最新稳定 | 自研规则中间件 | Apache/MIT（**权重 License 须法务确认**） |
| 推理运行时 | **vLLM / SGLang**（OpenAI 兼容端点） | vLLM 0.11+ | TGI / 厂商 MindIE 等 | Apache |
| 国产算力适配 | 昇腾（vLLM-Ascend + MindIE）、摩尔线程（SGLang 主线）为官方级；其余需 30 天压测 | — | 寒武纪/海光/沐曦/昆仑芯（多为厂商 Day-0 宣称） | 以厂商 BOM 为准 |
| 容器/编排 | **Docker + Kubernetes**（LangGraph 官方唯一持续测试的编排目标） | — | docker-compose 仅用于本地/单机 | Apache |
| 代码质量 | **ruff**（lint+format）、**mypy**、**pytest / pytest-asyncio**、**pre-commit** | 最新稳定 | — | MIT |
| 密钥管理 | 环境变量注入 + K8s Secret / Vault（生产） | — | 严禁 `.env` 入库 | — |

> 基线版本以仓库 `pyproject.toml` / `uv.lock` / 镜像 tag 为**权威落地值**；本表用于说明意图，升级流程见 [第 14 章](#14-规范演进与维护)。
>
> **评估行注（2026-10-16，经授权偏离，逐条登记于 [ADR-0003](docs/adr/0003-evaluation-framework-and-judge.md)）**：本仓经用户明确授权使用 **RAGAS**，而 RAGAS 0.4.3 的 `requires_dist` **无条件**依赖已归档的 `langchain-community`——这**违反 [NO-14](#13-禁止事项hard-no强制)** 与下表上方的 SDK 行（"`langchain-community` 已归档，**禁止新增引用**"）。缓解形态已落地：RAGAS **只作隔离环境的离线判分器**（`uv run --no-project --with …`，吃录制产物），项目 `pyproject.toml` 与 `uv.lock` **零改动**，**交付物不含 `langchain-community`**。此外 §3.8 的"维护活跃度"存疑（ragas 最近一次发布距今约 9 个月无更新），故 ADR-0003 同时设定**重新评估触发条件**。**该行不得被援引为先例去放宽 NO-14**：确定性判分器（引用完整性、状态、拒答、租户隔离、提示词卫生、检索命中）全部自研，**零第三方依赖**才是本仓的默认形态。
>
> **评估行注之二（裁判模型）**：裁判复用 `primary-chat` 的上游模型，存在自偏好偏置，`faithfulness` / `answer_relevancy` **偏乐观、不得当中性分数**；§5.8 要求生产使用本地裁判模型，本仓演示期属**偏离**，已随每份产物落 `run.judge.caveat` 并在报告中显著展示。

---

## 2. 仓库与目录约定

### 2.1 仓库形态

采用 **uv workspace 单体仓库（monorepo）**，智能体、工具服务、知识服务、评估在同一仓库内分包管理，保证类型与契约一致、可原子化重构。**不得**在未经 ADR 的情况下拆成多个相互隐式依赖的仓库。

### 2.2 标准目录结构

```text
技术栈/
├── CLAUDE.md                     # 本文件：最高工程规则
├── CHANGELOG.md                  # 规范与平台版本演进台账
├── README.md                     # 项目简介、快速开始、指向 CLAUDE.md
├── pyproject.toml                # workspace 与依赖、工具配置（ruff/mypy/pytest）
├── uv.lock                       # 依赖锁定（必须提交）
├── langgraph.json                # LangGraph Server 入口声明（agents 映射、dependencies、env）
├── docker-compose.yml            # 本地依赖：postgres / redis / milvus / langfuse / litellm
├── Dockerfile                    # 运行时镜像（api-server 与 worker 同镜像、不同启动命令）
├── .env.example                  # 所有环境变量的模板（仅占位，无真实值，必须提交）
├── .gitignore
├── .pre-commit-config.yaml
├── .claude/                      # Claude Code 协作资产（纳入版本管理，团队共享）
│   ├── commands/                 # 斜杠命令：scaffold / new-agent / new-tool / guard-review
│   ├── agents/                   # 自定义子代理（如 spec-reviewer，按需新增）
│   └── settings.json             # 命令权限白/黑名单（高危操作强制人审，.env 禁止读取）
├── src/
│   └── agent_platform/
│       ├── __init__.py
│       ├── main.py               # FastAPI 应用装配（仅装配，不写业务）
│       ├── settings.py           # pydantic-settings 统一配置（唯一读取环境变量处）
│       ├── config/               # 静态配置、模型/网关/护栏的声明式配置
│       ├── core/                 # 框架无关的核心能力
│       │   ├── models.py         # 经网关的统一模型客户端构造（含 fallback）
│       │   ├── checkpoint.py     # PostgresSaver / PostgresStore 单例与生命周期
│       │   ├── streaming.py      # SSE/流事件封装
│       │   ├── errors.py         # 统一异常层级与错误码
│       │   ├── retry.py          # tenacity 重试/退避/熔断策略
│       │   └── tracing.py        # Langfuse / OTel 初始化与上下文注入
│       ├── agents/               # 所有智能体（每个智能体一个目录）
│       │   ├── registry.py       # 智能体注册表（与 langgraph.json 保持一致）
│       │   └── <agent_name>/
│       │       ├── graph.py      # StateGraph 装配与编译（build_graph()）
│       │       ├── state.py      # State Schema（Pydantic/TypedDict，唯一来源）
│       │       ├── edges.py      # 条件路由函数
│       │       ├── nodes/        # 节点：一节点一文件，单一职责
│       │       ├── subgraphs/    # 子图/多智能体协作
│       │       ├── prompts/      # Prompt 模板（版本化，.md/.jinja，不内联在节点里）
│       │       └── schemas.py    # 该智能体内部结构化输出/工具结果模型
│       ├── tools/                # 工具
│       │   ├── builtin/          # 内置 Python 工具（薄封装，类型化）
│       │   ├── mcp_clients/      # 外部 MCP Server 客户端装配
│       │   └── registry.py       # 工具注册、权限与元数据
│       ├── knowledge/            # RAG/知识检索客户端（对接 RAGFlow/Milvus/图谱）
│       ├── memory/               # 记忆命名空间、摘要与长期记忆策略
│       ├── guardrails/           # 输入/输出护栏、PII 脱敏、工具调用策略
│       ├── api/                  # HTTP 边界
│       │   ├── routes/           # 路由（按资源分文件），对齐 threads/runs 语义
│       │   ├── schemas/          # 请求/响应 DTO（Pydantic v2）
│       │   ├── deps.py           # 依赖注入（鉴权、租户、会话、客户端）
│       │   └── middleware/       # 统一错误、request_id、租户上下文、访问日志
│       ├── tenants/              # 多租户/权限上下文与行级安全辅助
│       ├── evaluation/           # 评估集、评测器、CI 评测任务
│       └── utils/                # 纯函数工具（无业务、无副作用）
├── tests/
│   ├── unit/                     # 与 src 同构的纯单元测试
│   ├── integration/              # 需 postgres/redis/milvus/网关 的集成测试
│   ├── evals/                    # 黄金集、轨迹评估、回归基线（含 fixtures）
│   └── conftest.py
├── deploy/
│   ├── docker/                   # 镜像与各服务 compose 片段
│   └── k8s/                      # Deployment(api-server/worker)、HPA、Service、ConfigMap、Secret 模板
├── docs/
│   ├── adr/                      # 架构决策记录（编号，不可变）
│   ├── agent-kickoffs/           # 各业务智能体的 Claude Code 开场提示词（.txt，可直接粘贴）
│   ├── claude-code-prompts.md    # Claude Code vibe coding 提示词指南与模板库
│   ├── runbooks/                 # 故障处置手册
│   └── api/                      # 接口补充说明（OpenAPI 自动生成，不手写重复）
└── scripts/                      # 运维/数据/评测脚本（必须可重复执行、带 --help）
```

> **落地现状（2026-10-20 核对：规范声明必须与仓库现实一致，不得据上表假设某目录已存在）**
>
> **已落地**：`src/agent_platform/` 下的 `core/`、`knowledge/`（含证据图片本地存储 `images.py`，ADR-0004）、`agents/`、`api/`、`tenants/`、`guardrails/`（`sanitize.py` 不可信内容中和）、`evaluation/`、`utils/`；`tests/` 下 `unit/`、`integration/`、`evals/`（含黄金集 28 条、阈值 `config/thresholds.toml`、入库基线 `baselines/`、评审协议 `README.md`）；`scripts/`（含 `eval_run.py`、`eval_ragas.py`）、`deploy/docker/`、`deploy/k8s/`、`docs/adr/`、`docs/runbooks/`、`langgraph.json`、`docker-compose.yml`、`.claude/settings.json`。
>
> **`evaluation/` 的落地范围（2026-10-16 起，此前列为空壳）**：已具备**确定性判分器**（引用完整性、状态、拒答、租户隔离、提示词卫生、检索命中；零 I/O 零网络）、**裁判层**（版本化提示词资产 + 判分缓存 + 原始输出留痕）、**门禁判定与基线对比**（含配置漂移检测与 `--promote-baseline`）、**黄金集装载与签署分流**。**尚未具备**：HITL 样例（图中无 `interrupt()`，schema 预留取值但**不编造**样例）、分路检索分数（`vector_score` / `bm25_score` 结构性未采集）、间接注入的端到端对抗夹具（需写 Milvus，默认不做）。完整清单与逐条证据见 `tests/evals/README.md` 第五节。
>
> **仅有空壳（目录在、能力未实现）**：`guardrails/` 中除 `sanitize.py` 外的护栏（NeMo Guardrails / Presidio 未接入）。引用时按"接口占位"对待，不得当作已有能力。
>
> **尚未创建（待建，非已具备）**：`config/`、`memory/`、`tools/`、`agents/<name>/subgraphs/`、`docs/agent-kickoffs/`、`docs/claude-code-prompts.md`、`docs/api/`、`Dockerfile`，以及 [12.4](#124-ai-协作claude-code--vibe-coding) 要求沉淀的 `.claude/commands/`（`scaffold` / `new-agent` / `new-tool` / `guard-review`）与 `.claude/agents/`。任务涉及这些路径时，先建目录再引用。

### 2.3 放置规则（什么代码放哪里）

- **编排逻辑只允许出现在 `agents/<name>/`**；`core/`、`tools/`、`knowledge/` 不得反向 import 某个具体智能体。
- **环境变量只允许在 `settings.py` 读取一次**；其余位置一律注入 `Settings`，不得出现 `os.getenv` 散落调用。
- **HTTP 边界只允许在 `api/`**：节点、工具、core 不得直接依赖 FastAPI 的 `Request/Response`。
- **工具实现只允许在 `tools/`**；节点通过注册表/MCP 获取工具，不得在节点内 new 客户端、拼 URL。
- **第三方系统对接**（RAGFlow、Milvus、业务系统、MCP）必须经 `knowledge/`、`tools/` 中的客户端封装，禁止在节点里裸发 HTTP。
- 依赖方向必须**单向向内**：`api → agents → core/tools/knowledge → utils`。**不得**循环依赖、不得让内层反向依赖外层。
- 一个文件一个清晰职责；**节点文件不得超过 ~150 行**，超出说明职责过多，应拆分。
- 文件名、目录名一律 **snake_case**；智能体目录名用稳定的英文小写标识（如 `metallurgy_inspection`），不用中文、空格、日期。

---

## 3. 编码规范

### 3.1 通用 Python

- 遵循 **PEP 8**，以 **ruff format** 为唯一格式标准，**ruff check** 为 lint 标准；行宽 100。
- 必须通过 **mypy** 静态检查（`core/`、`agents/`、`api/` 为严格级），不得在无充分理由下使用 `Any` 与 `# type: ignore`；如必须使用，须注释原因并限定最小范围。
- 公共函数、类、节点、工具**必须**有类型标注与 docstring（说明用途、参数、返回、副作用、可恢复异常）。
- 函数小而专，单一职责；避免"上帝函数/上帝节点"；优先纯函数与显式数据流，避免隐藏的全局可变状态。
- **不得**使用通配导入（`from x import *`）、不得在导入侧产生副作用、不得依赖隐式导入顺序。
- 数据时间一律 **UTC、naive→aware 明确**，对外序列化用 ISO 8601（带 `Z`/时区）；金额、计量等精确数值用 `Decimal` 并明确单位。

### 3.2 异步模型

- LangGraph/LangChain、FastAPI、IO 与外部调用**全程 `async/await`**。
- **不得**在 async 路径调用阻塞 IO（同步 `requests`、`time.sleep`、同步数据库驱动、阻塞文件读写）；需要时用 `httpx.AsyncClient`、`asyncio.sleep`、异步驱动，或用 `anyio.to_thread.run_sync` 显式隔离并注明。
- **不得**在节点内自行 `asyncio.create_task` 制造不受图管理的后台任务；长任务交给 LangGraph 任务队列/worker。
- 并发外部调用用 `asyncio.gather` 并设置并发上限与异常返回策略（`return_exceptions` 后显式处理）。

### 3.3 类型与数据校验（Pydantic v2）

- 所有跨边界数据（HTTP DTO、State、工具入参/出参、配置、外部系统响应）**必须**用 Pydantic v2 模型或明确的 `TypedDict` 定义并校验。
- 外部输入**默认不可信**：在边界完成校验、归一化与裁剪，内部只流转已校验对象。
- 优先使用不可变模型（`model_config = ConfigDict(frozen=True)`）描述 DTO/事件；字段必须有类型与约束（长度、范围、枚举）。
- 模型与枚举集中定义、跨层复用，**不得**在多处重复定义同义结构。

### 3.4 命名

| 对象 | 风格 | 示例 |
|---|---|---|
| 包/模块/文件/变量/函数 | snake_case | `build_graph()`、`postgres_saver.py` |
| 类 / Pydantic 模型 / 异常 | PascalCase | `InspectionState`、`ModelGatewayError` |
| 常量 | UPPER_SNAKE_CASE | `DEFAULT_TIMEOUT_S` |
| 私有实现 | 前缀 `_` | `_format_trace()` |
| 布尔 | `is_/has_/should_` 前缀 | `is_human_needed` |
| LangGraph 节点函数 | 动词短语、语义即动作 | `retrieve_evidence(state)` |
| 路由函数 | `route_after_*` | `route_after_review` |
| 环境变量 | UPPER_SNAKE，带前缀 | `LITELLM_BASE_URL`、`LANGFUSE_PUBLIC_KEY` |

- 命名必须表达业务意图，禁止 `data`、`tmp`、`process2`、`handle_it` 等无意义名；缩写仅使用团队共识词。

### 3.5 配置管理

- 唯一配置入口为 `settings.py`（`pydantic-settings`），按环境（dev/test/staging/prod）加载；`.env.example` 必须与实际配置项保持同步并随变更更新。
- **必须**区分：模型名、网关地址、超时/重试、各功能开关、连接串、密钥引用、租户策略。
- **不得**在代码中硬编码：API key、模型/服务 URL、数据库连接串、客户标识、可调阈值（阈值应入配置并给默认值）。
- 配置必须在启动时**fail-fast 校验**（缺失必填项立即报错并打印缺失项名，而非运行中才崩）。
- 密钥通过 K8s Secret / Vault 注入，仅以环境变量进入进程；日志与异常中必须脱敏。

### 3.6 错误处理、重试与降级

- 定义统一异常层级（`core/errors.py`）：业务异常、模型网关异常、工具异常、知识检索异常、护栏拦截异常、超时/限流异常；每个异常绑定稳定**错误码**。
- **只捕获你能处理的异常**，**禁止裸 `except:` / `except Exception:` 后静默吞掉**；必须捕获时要记录上下文并转换/重抛为明确异常。
- 所有外部调用（模型、工具、RAG、业务系统）**必须**显式设置超时，并用 **tenacity** 做有界重试：仅对幂等且可重试的错误（超时、429、5xx）重试，指数退避 + 抖动 + 最大次数；对 4xx、鉴权失败、护栏拦截**不得**重试。
- 模型调用必须配置**主→备 fallback**（经网关），并对"全部不可用"给出明确的用户可理解错误与告警。
- 错误信息对外不泄露内部拓扑、密钥、系统提示词与原始堆栈；详细堆栈仅进受控日志/Trace。

### 3.7 日志与追踪

- 使用结构化日志（JSON，统一字段：`timestamp/level/request_id/thread_id/tenant_id/agent/node/run_id/event/message`），**禁止 `print`**。
- 日志级别语义固定：`ERROR` 需人工介入、`WARN` 可自愈异常或降级、`INFO` 关键业务节点、`DEBUG` 细节（生产默认关闭）。
- **不得**记录密钥、完整系统提示词、客户敏感数据/PII（PII 经 Presidio 脱敏后再记）。
- 每次运行必须贯穿 `request_id / thread_id / run_id / tenant_id`，并与 Langfuse/OTel trace 关联（见 [第 9 章](#9-可观测性评估与测试)）。

### 3.8 依赖管理

- 统一用 **uv**：新增依赖必须 `uv add` 并说明用途，提交 `uv.lock`；**不得**直接手改锁定文件、不得引入未使用依赖。
- 新增运行时依赖前必须确认：① 必要性（标准库或现有库能否覆盖）；② 许可证（[10.6](#106-许可证与合规红线)）；③ 是否可私有化获取；④ 维护活跃度。涉及"锁定"层替换须 ADR。
- 依赖应"薄封装、可替换"：对外部 SDK 的引用集中在 `core/`/`tools/`/`knowledge/`，业务与节点只依赖内部封装接口。

---

## 4. LangGraph 智能体开发规范（核心）

### 4.1 State（状态）

- 每个智能体在 `state.py` 中定义**唯一** State Schema（推荐 `Pydantic` 模型或 `TypedDict` + 显式 reducer）。
- 必须为列表/聚合类字段显式声明 **reducer**（如 `add_messages`、`operator.add`、自定义合并），避免并发节点覆盖。
- State 只存**编排必需**的信息：消息、检索证据、结构化中间产物、路由标记、HITL 状态；**不得**把连接、客户端、大文件二进制、密钥放入 State（Checkpoint 会持久化它们）。
- 大对象（文档全文、图片、批量结果）存对象存储/知识库，State 中只保留引用 ID。
- State 字段变更必须向后兼容评估（旧 checkpoint 需可恢复）；破坏性字段变更须 ADR 与迁移说明。

### 4.2 Node（节点）

- 节点签名统一为 `async def node(state: S, /, writer=None, config: RunnableConfig) -> dict`（返回**部分状态字典**）。
- 节点必须**接近纯函数**：输入只来自 State/config，输出为 State 更新片段；**不得原地修改（mutate）入参 state**，不得依赖进程内可变全局。
- 一个节点只做一件事；LLM 调用、工具调用、检索、审批判断应是不同节点，便于回放与评估。
- 节点内**不得**包含：硬编码模型直连、裸 HTTP、密钥、内联大段系统提示词（提示词放 `prompts/` 并版本化）、无超时的外部调用。
- 节点的副作用（写库、发消息、调外部系统）必须**幂等**，并能在中断恢复/重放时安全重复（以 `run_id`/业务幂等键去重）。
- 节点必须处理"证据/工具结果缺失"分支，不得假设上游一定成功。

### 4.3 Edge 与路由

- 路由逻辑集中在 `edges.py` 的纯函数中（`route_after_x(state) -> 下一节点名`），**不得**在节点内部用命令式代码硬跳转。
- 路由必须是**确定性、可测试**的：给定 State 可断言其走向；涉及 LLM 判断走向时，必须约束输出为固定枚举并对非法值有兜底（落 `human/error` 节点）。
- 必须显式定义 `START`、`END` 与所有分支（含失败/兜底/人工分支），不得依赖隐式默认走向。

### 4.4 持久化、Checkpointer 与 Store

- 任何**有状态、长流程、需人机协同或可恢复**的图，编译时**必须**挂载 `PostgresSaver`（生产）与 `PostgresStore`；**禁止**仅用内存 `MemorySaver` 跑生产长流程（内存版仅限单测）。
- Checkpointer/Store 在 `core/checkpoint.py` 统一构造与管理连接生命周期，使用连接池与受控序列化。
- 会话寻址采用多租户命名空间：`thread_id = "{tenant_id}:{user_id}:{session_id}"`，长期记忆 `Store` 的 namespace 同样按租户/用户隔离（见 [10.2](#102-多租户与权限)）。
- 必须利用 checkpoint 实现**断点续跑与重放**；不得另起一套会话状态表与图状态竞争。
- 升级 LangGraph 或调整 State 前，必须验证历史 checkpoint 的可恢复性（见 [9.3](#93-测试分层与门禁)）。

### 4.5 人机协同（Human-in-the-loop）

- 需要人工确认/补充的环节，必须用 LangGraph 原生 **`interrupt()` + `Command(resume=...)`** 实现"中断—持久化—恢复"，**不得**用自建标志位 + 轮询模拟。
- 中断点必须明确：暂停哪些节点、恢复时需要什么输入、谁有权恢复（绑定角色/权限）、超时如何处理（升级/终止/转派）。
- 审批结果必须校验后写入 State 并留痕（谁、何时、决定、依据），支持审计与时间旅行（time-travel）分叉。

### 4.6 子图与多智能体

- 复杂流程必须用 **Subgraph** 分层封装（每个子域一个子图），父图只做编排与路由，避免单图膨胀。
- 多智能体协作默认使用 **langgraph-supervisor**（中心调度）；**swarm**（对等交接）仅在明确需要去中心化交接时使用并在 ADR 说明。
- 智能体之间只通过**显式契约**（共享 State 字段 / 结构化消息 / 工具结果 schema）通信，不得依赖隐式上下文。
- 每个智能体必须在 `agents/registry.py` 与 `langgraph.json` 注册，声明名称、负责人、版本、可见工具、权限、护栏等级。

### 4.7 流式（Streaming）

- 面向用户的对话/长任务**必须**支持流式：图侧用 `astream` / `astream_events`，HTTP 侧用 **SSE** 输出标准事件（token、节点进入/退出、工具调用、检索、中断请求、完成、错误）。
- 必须区分事件类型并给出稳定的事件 schema（见 [8.4](#84-流式-sse-规范)）；错误事件也要走流，不得让客户端"挂起"。
- 不得在 token 流中泄露未脱敏内容；最终落地到 State/库的必须是完整、经护栏校验的结果。

### 4.8 编译与装配

- 每个智能体暴露唯一工厂 `build_graph(**deps) -> CompiledStateGraph`，依赖（模型、工具、store、护栏）通过参数注入，**不得**在模块导入期连库/连网。
- `graph.py` 只做"声明节点—边—编译"，不放业务实现。
- 编译必须挂载 checkpointer、护栏中间件、追踪；开发/测试可通过依赖参数替换为 fake/memory 实现。

---

## 5. 模型调用规范

1. **所有模型调用必须经 LiteLLM 网关**（`ChatOpenAI(base_url=LITELLM_BASE_URL)` 或 LiteLLM 统一客户端），业务代码**不得**直连各厂商 SDK、不得散落各厂 API key。云端模型与内网 vLLM/SGLang 端点都在网关注册为统一模型名。
2. 模型必须以**逻辑名 + 版本**引用（如 `primary-chat`、`fallback-chat`、`judge`、`embedding`、`rerank`），具体型号在网关/配置映射；换型号不改业务代码。
3. 每次调用必须显式设置：超时、最大重试、温度/采样参数（按任务类型固化，评测用固定随机种子或 `temperature=0`）、`max_tokens`、必要时 `json/tool` 输出约束。
4. 必须配置**模型 fallback 链与限流/熔断**（[3.6](#36-错误处理重试与降级)）；高风险写操作的工具调用不得在 fallback 中静默重复执行。
5. **Prompt 一律资产化**：放 `agents/<name>/prompts/`，模板与代码分离、命名版本化（如 `review.system.v3.jinja`）；变量经模板渲染并做注入/转义；**不得**在节点里内联拼接大段提示词。
6. 结构化输出必须用模型的 function/JSON 模式 + Pydantic 校验，解析失败进入重试/纠偏/兜底分支，不得信任未校验的模型文本。
7. 必须记录每次调用的模型逻辑名、token 用量、耗时、成本归属（租户/场景）到 Langfuse，用于成本核算与容量规划。
8. 评估/裁判（judge）模型在私有化场景应使用**本地模型**，数据不得出域。

---

## 6. 工具与 MCP 规范

1. **新工具一律实现/封装为 MCP Server**（外部标准接入）或在 `tools/builtin` 提供类型化薄封装并由统一 MCP/工具层暴露；旧的私有插件协议应逐步迁移，不再新增。
2. 每个工具必须声明：**名称、用途描述（供模型选择）、入参 schema（Pydantic/JSON Schema）、出参 schema、副作用等级（只读/写/危险）、所需权限、是否幂等、超时与重试策略、所属租户范围**。
3. 工具描述应清晰说明"何时用、何时不用、关键约束"，参数语义明确；不得让模型自由文本传参。
4. **有副作用的工具**（下单、改数据、发指令、删除）必须：幂等（幂等键）、经权限校验、经输入护栏校验、在需要时走 HITL 确认；**不得**仅凭模型输出直接执行高危动作。
5. 工具内部对外部系统的访问必须设超时、重试（仅幂等可重试）、限流与审计日志；工具异常返回**结构化错误**（不抛裸堆栈给模型），并在错误中给出可操作的下一步。
6. 工具结果必须裁剪到模型可用的最小必要信息，大结果分页/摘要 + 引用，不得把整库/整页无差别灌入上下文。
7. 工具按最小权限授权给智能体（注册表白名单）；**不得**给智能体挂"万能工具集"。跨域系统对接优先 MCP，跨智能体互通在确需跨厂商时才评估 A2A。

---

## 7. 知识与 RAG 规范

1. **RAGFlow 作为知识工程后端**（解析、切分、索引、检索、引用），通过 REST/MCP 被智能体调用；**编排与决策仍唯一在 LangGraph**，**禁止**用 RAGFlow/Dify/FastGPT 的可视化画布承载核心业务流程。
2. 向量库生产用 **Milvus 2.6**（独立、可扩、可备份）；轻量单机可用 pgvector；选型替换须 ADR。Embedding/Rerank 用可私有化的 Qwen3 系列（或 bge 系列），模型升级需重建/双跑索引并评测。
3. 文档接入必须有规范管线：来源登记 → 解析 → 清洗 → 切分（带文档/章节/页码元数据）→ 嵌入 → 索引 → 质检；每批可追溯来源与版本。
4. 检索默认**混合检索**（向量 + 关键词/元数据过滤）+ **Rerank**；必须带权限/租户过滤（用户只能检到有权限的知识），并返回**引用（来源、章节、页码、链接）**，无引用不得作为事实结论。
5. 检索节点必须处理"无结果/低置信"分支（澄清、扩检、转人工），不得让模型在无证据时编造；答案与证据绑定，支持溯源。
6. 领域本体/规则（OWL/SWRL）用 Protégé 编辑、owlready2 加载，作为结构化约束/推理层与 RAG 互补；图谱（GraphRAG/LightRAG）用于多跳关系问题，是否引入须 ADR。
7. 知识库变更（切分策略、模型、Top-K、阈值、rerank）属于**检索质量变更**，必须跑评估集回归（[9.2](#92-评估门禁)）后方可上线。
8. **双后端分工的落地形态（[ADR-0002](docs/adr/0002-ragflow-milvus-division-of-labor.md)，2026-10-16 实测跑通）**：RAGFlow 只承担**文档工程**（解析 / OCR / 版面与表格识别 / 切分 / 文档生命周期），**检索主链路自建在 Milvus**（稠密 + BM25 混合检索 → 阈值 → 重排 → 证据与引用），两者由 `knowledge/` 衔接，职责**不得互相越界**。同步的幂等键为**源文件路径 + 内容 sha256**，Milvus 主键直接沿用 RAGFlow 的 `chunk_id`（`auto_id=False`），故重复同步是 **upsert 覆盖而非新增**；**解析失败（`FAIL` / `CANCEL` / 零切分）必须显式失败并做补偿删除**，不得记成"成功但零切分"——实测发生过，其后果是这批文件被永久跳过且不留痕迹。租户过滤表达式由本平台代码构造（`knowledge/milvus_schema`），**调用方无法绕过**（NO-19）；**引用编号必须与答案正文中的编号一致**，且无证据不得给出事实性结论（NO-12）。**演示期偏离（已封边界）**：嵌入与重排经 LiteLLM 网关指向**云端 API**，语料因此出域——**仅限公开演示语料，不得用于客户数据与生产环境**；生产必须切私有化 Qwen3，切换只改网关映射、业务代码零改动（[10.1](#101-密钥与数据保护)）。

9. **图片证据（[ADR-0004](docs/adr/0004-multimodal-document-evidence.md)，2026-10-20 起）**：多模态**只做在解析期**——RAGFlow 用 `vision-chat`（[第 1 章](#1-技术栈锁定)）把图片转成文字描述写进切分正文，后续嵌入、检索、答案生成全部沿用文本链路，生成模型不看像素。切分的 `media_type` 按 RAGFlow 的 `doc_type_kwd` 映射（`table` / `image`，其余为 `text`）；该字段**必须从切分详情接口逐条读取**——RAGFlow v0.27.2 的切分列表接口会把非字符串取值一律写成 `text`（Infinity 引擎存的是 keyword 列表），只看列表会把表格与图片全判成文本。详情读取的并发上限由 `RAGFLOW_CHUNK_DETAIL_CONCURRENCY` 控制（默认 8），任一条读不到即**整份文件判失败**并走补偿删除，不得把类型静默降级成文本；**`image_id` 非空就下载并附图片引用，与媒体类型无关**（PDF 正文块合并内嵌图时也带图）。图片二进制经 `ImageStore` 协议落本地文件，路径按 `{tenant_id}/{document_id}/{chunk_id}/{image_id}.{ext}` 隔离，**类型按魔数判定**、单张有字节上限、写入原子化；生产换对象存储走新 ADR。平台图片 ID 由内容 sha256 生成（`img_` + 摘要前 32 位），**RAGFlow 的 `image_id`、存储路径与对象 key 一律不得进入证据、Milvus 或 API 响应**。图片切分（`doc_type_kwd=image`）下载或落盘失败时**整份文件判失败**并走补偿删除，不得降级成"有切分无图片"的假成功；表格与正文附图允许降级为 `images=[]`，但必须记 WARNING 并计数。重入库会使旧图片 ID 失效，会话回看旧引用时按 404 处理（保留期策略见 ADR-0004 未决项）。

---

## 8. 接口（API）风格

### 8.1 通用约定

- 对外统一 **REST + JSON**，由 FastAPI 承载；统一前缀 `/api/v1`，资源用**复数名词**（`/threads`、`/runs`、`/agents`、`/tools`）。
- 会话与运行语义**对齐 LangGraph Server**：`POST /threads`、`POST /threads/{id}/runs`（或 `/runs/stream`）、`GET /threads/{id}/state`、`POST /threads/{id}/history`、HITL 用 resume；自有业务资源另建资源，不得另造一套会话状态。
- 只用标准 HTTP 语义：`GET` 查询、`POST` 创建/动作、`PATCH` 局部更新、`DELETE` 删除；`GET` 必须安全幂等、不得触发副作用。
- 所有请求/响应必须用 `api/schemas/` 的 **Pydantic v2 DTO**，OpenAPI 自动生成且必须保持可生成、无错误；字段用 snake_case（JSON 可配置别名），枚举稳定。
- 时间用 ISO 8601 UTC；分页用游标（cursor + limit），返回 `next_cursor`；批量/写操作支持幂等键（`Idempotency-Key` 头）。
- 每个请求必须有 `X-Request-Id`（无则服务端生成并在响应/日志回传）；鉴权用 Bearer Token，租户从令牌上下文解析，**不得**由前端裸传租户 ID 决定权限。

### 8.2 统一响应与错误

- 成功响应直接返回资源 DTO（或统一包 `{ "data": ... }`，全仓二选一并保持一致，不得混用）。
- 错误统一采用 **RFC 9457（Problem Details，`application/problem+json`）**：`type/title/status/detail/instance` + 业务 `code` + `request_id`；错误码集中在 `core/errors.py` 管理，稳定且不复用语义。
- 状态码语义固定：400 参数错误、401 未认证、403 无权限、404 不存在、409 冲突/幂等冲突、422 语义校验失败、429 限流、4xx 不重试（客户端纠正）、5xx 服务端错误（可重试）。
- 错误响应**不得**包含堆栈、SQL、内部地址、密钥、系统提示词。

### 8.3 幂等、并发与版本兼容

- 所有产生副作用的 POST/PATCH/DELETE 必须支持幂等键；服务端缓存首次结果并对重复请求返回同一结果。
- 乐观并发用版本字段（`If-Match`/版本号），冲突返回 409。
- API 版本通过 URI（`/v1`）演进；破坏性变更升版本，旧版本按弃用策略保留并在响应头/文档标注 `Deprecation`；字段只增不删，删除需过渡期。

### 8.4 流式（SSE）规范

- 流式端点 `POST .../runs/stream`，`Content-Type: text/event-stream`；事件为 `event: <type>\ndata: <json>\n\n`。
- 标准事件类型（枚举，全平台统一）：`run.started`、`node.entered`、`message.delta`、`tool.called`、`tool.result`、`retrieval.evidence`、`human.interrupt`（需人工，附 resume 信息）、`node.exited`、`run.completed`、`run.error`。
- 每个事件携带 `run_id/thread_id/request_id` 与序号 `seq`；`run.error` 必须给出错误码与可理解信息并终止流；服务端需心跳（`: ping` 注释或约定事件）保活。
- 客户端可凭 `thread_id + checkpoint` 在断线后恢复；中断（HITL）通过 resume 端点续跑，而非新建会话。

### 8.5 接口文档与契约

- 接口用途、鉴权、错误码、示例必须在 OpenAPI 中完整；DTO 即契约，**不得**出现"文档写一套、代码做一套"。
- 破坏性接口变更必须在 PR 中说明影响面、迁移方式，并更新前端/集成方与评估用例。

### 8.6 证据图片端点

- 路径固定为 `GET /api/v1/threads/{thread_id}/evidence/{chunk_id}/images/{image_id}`，**只服务于会话内已出现的证据**：鉴权后校验会话归属，再从该会话检查点的 `evidence` 中精确匹配 `chunk_id` 与 `image_id`，匹配不到一律 404——包括路径非法、会话不存在、跨租户、块不在证据里、图片不属于该块、文件缺失。只有未认证返回 401。
- **不得**提供"按知识库/文档直接取图"的端点，也不得返回对象存储签名 URL：那等于开放知识库遍历，绕开会话级租户隔离（NO-19）。
- 成功响应必须带 `Content-Type`（白名单 MIME）、`Cache-Control: private, no-store`、`X-Content-Type-Options: nosniff` 与 `Content-Disposition: inline`。
- 图片元数据（`ImageRef`）只随 `retrieval.evidence`、`run.completed.citations` 等既有事件带出，**不新增 SSE 事件类型**；生成证据的提示词中不得出现图片二进制、`image_id` 或 `data:` 内联内容。

---

## 9. 可观测性、评估与测试

### 9.1 可观测性

- 自托管 **Langfuse** 为 LLM/智能体追踪主平台，并接入 **OpenTelemetry**（ traces/metrics/logs 关联同一 trace 上下文）；数据内网留存，**禁止**默认上报 SaaS（LangSmith 等需离线合规评估）。
- 必须埋点：每次 run 的完整节点轨迹、每次模型调用（模型逻辑名、prompt 版本、token、耗时、成本）、每次工具调用（名、入参摘要、结果状态、耗时）、检索（命中文档、分数）、护栏拦截、HITL 决定、重试与 fallback。
- 关键指标看板与告警：成功率、各节点耗时 P50/P95/P99、token/成本、重试率、fallback 触发率、护栏拦截率、人工介入率、队列深度（worker 扩缩容依据）。
- 每次发布必须可对比发布前后的关键指标；trace 必须可按租户/会话/智能体检索。

### 9.2 评估门禁

- 每个智能体与每条 RAG 管线必须维护**黄金集（golden set）**与**轨迹评估**（`tests/evals/`）：典型/边界/对抗样例，覆盖正确性、工具选择、检索命中/引用、安全（注入、越权、PII）、HITL 分支。
- 评估框架用 RAGAS/DeepEval/Promptfoo，LLM-as-judge 的裁判模型与标准固定并记录版本；指标与阈值在配置中声明。
- **以下变更必须跑评估回归并达标方可合入/上线**：Prompt 改动、模型/温度、检索策略（切分/Top-K/阈值/rerank/embedding）、工具描述、图结构、护栏策略。结果留痕（分数、样例、对比）。
- 评估集与代码同库、随缺陷持续补充（每修复一个真实坏例，沉淀为回归用例）；不得用训练/评估同源数据"自评自满"。
- **本仓落地形态（2026-10-16，[ADR-0003](docs/adr/0003-evaluation-framework-and-judge.md)）**：判分分两层——**确定性判分器**（引用完整性、状态、拒答、租户隔离、提示词卫生、检索命中；零 I/O 零网络、可离线单测）为主干，**模型裁判**只用于确定性判分覆盖不到的语义项（`faithfulness` / `answer_relevancy`）。**RAGAS 经授权使用但以隔离环境离线判分器的形态落地**（它无条件依赖已归档的 `langchain-community`，违反 NO-14，见第 1 章脚注）；**交付物不得引入 `langchain-community`**。裁判与生成同源时必须在产物与报告中显著标注偏置，**不得当中性分数**使用。阈值放 `tests/evals/config/thresholds.toml`，**刻意不进 `settings.py`**——环境变量意味着可以一行 `export` 把门禁调松而产物里看不出区别；文件则必经一次提交、一次评审。**评估不得进入 pre-commit**（慢、要密钥、要基础设施，放进去只会训练人加 `--no-verify`）。**门禁语义不得放宽**：指标未被采集（样本量不足或该指标本轮缺席）一律**不判通过**——"没采够就是没证据"，与 NO-12 同一条道理。**"没测出来"必须与"考砸了"分开建模**：测量本身失败（裁判输出无法解析、超时等）的指标**不得带分数**，必须在契约里与 `fail`/`not_applicable` 并列成第三种取值；把它记成 `0.0` 等于凭空造一个数据点，一次测量故障会被读成一次质量回归——首份基线对比时真实发生过（`faithfulness` 被压低 1/23，报告显示"跌破"、样例进"新失败样例"，而模型什么都没做错）。**未采集仍不判通过**（可见性一分不减），但不得污染聚合分。
- **图片证据的两项确定性指标（ADR-0004）**：`image_evidence_hit`（按文档名 + 页码核对证据里是否出现应命中的图片，**不要求** `media_type=image`——RAGFlow 会把 VLM 描述过的图标成 `table`）与 `image_citation_integrity`（引用的图片必须与对应证据逐张一致，缺图、多图、错图均判失败）。两者都走三态语义：无此类样例时为 `not_applicable`，测量本身失败时不得带分数。没有 `must_hit` 的样例不参与命中率分母。
- **评估层的依赖方向**：`evaluation → agents/core/knowledge/settings`，**任何运行时模块不得 import 评估层**（否则生产镜像多背一份评估资产，且"生产路径依赖评估代码"会让评估的独立性消失）。

### 9.3 测试分层与门禁

- **单元测试（unit）**：节点、路由、工具、schema、纯函数，不依赖外部服务（模型/网关/库用 fake/mock），必须快、确定性。
- **集成测试（integration）**：用 testcontainers/本地 compose 起真实 Postgres/Redis/Milvus/网关，验证 checkpoint 持久化、中断恢复、fallback、SSE 事件序列、多租户隔离。
- **关键必测**：State reducer 并发不覆盖；节点不 mutate 入参；HITL 中断后 resume 正确；副作用幂等（重放不重复执行）；越权工具被拒；无证据时不编造；历史 checkpoint 在版本升级后可恢复。
- 覆盖率门槛：`core/`、`agents/`、`tools/`、`api/` 行覆盖率 ≥ 80%（关键分支必须覆盖）；测试必须可重复、相互独立，不得依赖外网与真实密钥。
- pre-commit/CI 必须依次通过：ruff format --check → ruff check → mypy → 单元测试 →（CI）集成测试 → 评估门禁；任一不过不得合入。
- **评估门禁的落地形态（2026-10-16，观察期）**：评估层已落地（`scripts/eval_run.py`，退出码 0/1/2/3/4），命令链为 `pytest(unit) → pytest -m integration → pytest -m eval（RUN_EVAL_TESTS=true）`。**当前处于观察期**：评估照常产出报告、判定与留痕，`--baseline` 照常检出回归，但**退出码暂不接入 pre-commit**（待黄金集经人工逐条核对签署后再议转阻断）。**观察期只豁免"阻断"这一步，不豁免判定本身**：产物里的结论不得放宽，未采集的指标一律不判通过。`evaluation/` 行覆盖率门槛与 `core/` 等同（关键分支必须覆盖；确定性判分器要求 100%）。**不得以"这个模块要连外部服务"为由整体豁免它的覆盖率**：`evaluation/judge.py` 曾因此豁免，代价是它的失败路径一条都没被跑过，上述"测量故障记成 0 分"的缺陷活了整整一轮才在真实对比中暴露。要连外部服务的模块用 fake 依赖拉回离线单测（该模块现为 95%），只有**真正只能**在真实基础设施上执行的部分才可豁免，且须逐条写明理由。

---

## 10. 安全与合规

### 10.1 密钥与数据保护

- 密钥仅来自 Secret/环境变量注入，**禁止**入库、入日志、入异常、入前端、进 Prompt 与 checkpoint；定期轮转。
- 客户数据、文档、对话默认**不出域**；私有化部署全部组件（模型、嵌入、rerank、judge、Langfuse、向量库）内网可运行。
- PII/敏感字段经 **Presidio** 识别与脱敏后再进日志/向第三方（含模型）传递；数据保留与删除策略明确并可执行（含向量库与 checkpoint 的删除）。

### 10.2 多租户与权限

- 全链路租户上下文：`thread_id`、记忆 namespace、向量检索过滤、对象存储路径、**证据图片目录**（`{tenant_id}/{document_id}/…`，路径组件校验后 `resolve` 越界检查）、审计日志都按 `tenant_id/user_id` 隔离；数据库层启用 **Postgres 行级安全（RLS）** 作为纵深防线，不仅靠应用层 where。
- 工具与知识按最小权限授权；高危动作须角色校验 + HITL；越权访问必须拒绝并记录安全事件。
- 不得信任前端传入的租户/用户标识，一律以认证令牌解析结果为准。

### 10.3 护栏与提示词安全

- 输入/输出必须经 **NeMo Guardrails**（及本地 Llama Guard/Prompt Guard）：拦截提示词注入、越权指令、敏感/违规内容、被污染的检索内容（间接注入）；工具调用前做策略校验。
- 系统提示词视为受控资产，不随意外泄；外部内容（检索结果、工具返回、用户上传）在 Prompt 中必须以"不可信数据"隔离标注，禁止其中的指令被执行。
- 护栏策略变更等同安全变更，须含对抗用例评估与留痕；拦截事件可观测、可审计。

### 10.4 审计与留痕

- 高危操作、HITL 决定、工具写操作、权限变更、配置/模型/Prompt 变更必须审计（谁、何时、对什么、改了什么、结果），日志防篡改、可追溯。
- 支持会话完整回放（基于 checkpoint + trace）用于事故复盘与合规检查。

### 10.5 供应链与运行时安全

- 基础镜像最小化、非 root 运行、固定基础镜像 digest；依赖锁版本、定期扫漏洞（SBOM + CVE 扫描）；内网镜像仓库统一管理。
- 容器不挂宿主机敏感目录；Secret 不进镜像与层；网络默认最小开放（api-server/worker 不直接暴露，GPU/模型池独立网段）。

### 10.6 许可证与合规红线

- 引入依赖前必须确认 License 并经法务确认。**特别红线**：
  - **Dify / FastGPT** 为"修改版 Apache-2.0、source-available"（禁止未授权多租户 SaaS、禁止去除 logo / 商业限制），**不得**作为对外多租户运营底座直接使用。
  - **New-API** 为 **AGPL**，对外提供网络服务触发开源传染性，未过法务**禁止**入生产。
  - **PyMuPDF、jina-v4、Llama Guard 4** 等存在商用/来源限制；模型权重 License 单独确认。
  - LangGraph **库为 MIT 可自由私有化**；**LangGraph Platform 为商业许可**，离线 Self-Hosted Enterprise 需采购授权，不得绕过授权使用。
- 面向多个能源/工业客户私有化交付时，上述条款直接影响商业模式，选型与交付前必须由法务出具意见并存档。

---

## 11. 部署与环境

1. 部署目标为 **Kubernetes**（LangGraph 官方持续测试的编排环境）；本地开发用 docker-compose 起依赖。
2. 使用 **LangGraph Server** 形态：`langgraph.json` 声明智能体入口与依赖；运行进程分离为 **api-server**（无状态、接入/SSE）与 **worker**（执行图、消费 Redis 队列），**二者都不挂载 GPU**；按 `redis_queue_depth` 对 worker 做 HPA。
3. **GPU/推理独立成池**：vLLM/SGLang 提供 OpenAI 兼容端点，回接 LiteLLM；**同一模型不得在 H800 与国产卡上混跑**（一致性/性能不可控）；国产卡适配（昇腾/摩尔线程为官方级，其余）上线前必须完成 ≥30 天稳定性与精度压测并留报告。
4. 有状态依赖（Postgres、Redis、Milvus）必须高可用、定期备份与恢复演练；checkpoint 与知识库纳入备份/保留策略。
5. 配置经环境/ConfigMap/Secret 注入，一套镜像多环境（dev/staging/prod 仅配置不同）；**禁止**为不同环境打不同镜像或在镜像内固化环境地址。
6. 必须提供 `/healthz`（存活）与 `/readyz`（就绪，检查关键依赖）与版本/构建信息端点；优雅关停（排空在跑 run、关闭连接）。
7. 发布采用滚动/灰度，支持回滚到上一镜像与上一 Prompt/配置版本；发布后观察第 9.1 节指标。
8. 离线/信创环境交付前必须在**断网环境**实测：镜像、依赖、模型权重、前端静态资源、图表/字体等全部自包含，不依赖公网 CDN。

---

## 12. Git 工作流与提交规范

### 12.1 分支模型

- `main`：始终可发布，受保护，仅经 PR 合入，要求全部检查通过 + 评审。
- `develop`（如采用）：集成分支；功能分支 `feature/<scope>-<短描述>`、修复 `fix/<id>-<短描述>`、发布 `release/vX.Y.Z`。
- 分支短小、单一目的；不得在一个分支混装多个无关改动。

### 12.2 提交信息（Conventional Commits）

- 格式：`<type>(<scope>): <subject>`，`type ∈ feat | fix | docs | style | refactor | perf | test | build | ci | chore | revert`。
- 与本规则相关的提交：新增/修改工程规则用 `docs(standards): ...`；架构决策落地用 `chore(adr): ...`。
- subject 用祈使句、简明（≤50 字为佳）；正文说明"为什么"与影响面；破坏性变更加 `BREAKING CHANGE:` 段。
- 示例：`feat(agents): add metallurgy inspection HITL approval node`、`fix(tools): make work-order tool idempotent on retry`、`docs(standards): require MCP for all new tools`。

### 12.3 评审与门禁

- 所有变更经 Pull Request：至少 1 名同领域评审 + 代码所有者（`CODEOWNERS`）；规则/架构/安全/许可证变更需架构组或安全/法务会签。
- 合并前必须满足：CI 全绿、覆盖率达标、评估门禁达标（涉及智能体质量时）、规则文件与 ADR/CHANGELOG 已同步、无密钥扫描告警。
- **禁止** `--no-verify` 跳过钩子、禁止绕过评审直推 `main`。
- `pre-commit` 至少包含：ruff（format+lint）、mypy（增量）、文件尾空格/编码、大文件拦截、密钥扫描（gitleaks 等）。

### 12.4 AI 协作（Claude Code / Vibe Coding）

- 在本仓库使用 Claude Code（或同类 AI 编程助手）时，`CLAUDE.md` 为其自动加载的最高规则；任务提示词写法、模板与工作流见 [`docs/claude-code-prompts.md`](docs/claude-code-prompts.md)，**提示词引用规则，不得复制或绕过规则**。
- 高频、可复用动作必须沉淀为 `.claude/commands/` 斜杠命令（已内置 `scaffold`、`new-agent`、`new-tool`、`guard-review`）；命令、`.claude/agents/` 子代理与 `settings.json` **纳入版本管理、团队共享**，不得用本地私有配置改变强制规则。
- 非平凡任务必须**先输出实施计划（State/契约/文件清单/测试点/风险）经人确认后再写代码**；按小步可验证增量推进，每步实际运行 ruff/mypy/pytest 自证，并按 Conventional Commits 提交。
- 以下动作 AI **不得自行决定，必须先经人确认**：新增/替换依赖与许可证事项、改动架构或目录、安全与多租户策略、连生产或真实写接口、`git push`、删除文件/代码、跳过校验（`--no-verify`）、高危副作用执行；`.claude/settings.json` 应对这些命令默认拒绝或转人工确认。
- AI 产出代码与人类代码同标准，必须通过第 9 章全部门禁与 `/guard-review` 合规审查后方可合入；AI 引入的新约定同样触发第 14 章规则更新义务（NO-20）。

---

## 13. 禁止事项（Hard No，强制）

> 以下为红线，违反即不予合入；Code Review 可直接引用编号（NO-xx）。

- **NO-01** 不得用任何其它框架/可视化画布（Dify、RAGFlow、FastGPT、AutoGen、CrewAI 等）替代 LangGraph 承载核心业务编排；它们只能作为知识后端、工具服务或对照参考。
- **NO-02** 不得在业务代码中直连模型厂商 API 或散落各厂密钥；所有模型调用必须经 LiteLLM 网关。
- **NO-03** 不得硬编码密钥、连接串、内部 URL、客户标识、模型名于代码/Prompt/测试夹具。
- **NO-04** 不得在节点中原地修改（mutate）传入的 State；节点必须返回部分状态字典。
- **NO-05** 不得在 async 路径使用阻塞调用（`requests`、`time.sleep`、同步驱动）或创建不受图管理的后台任务。
- **NO-06** 不得裸 `except`/吞异常/打印后继续；不得用 `# type: ignore`、注释测试、`--no-verify` 让流水线"假绿"。
- **NO-07** 不得使用 `print` 调试并遗留；不得记录密钥、系统提示词全文与未脱敏 PII。
- **NO-08** 不得在生产长流程中仅用内存 checkpointer；有状态图必须挂 PostgresSaver/Store。
- **NO-09** 不得用自建标志位 + 轮询模拟人机协同；HITL 必须用 `interrupt()`/`Command(resume=)`。
- **NO-10** 不得让高危/有副作用工具仅凭模型输出直接执行（须幂等 + 权限 + 护栏 + 必要时 HITL）。
- **NO-11** 不得给智能体挂载超出最小权限的工具集，或让工具接受模型自由文本参数。
- **NO-12** 不得在无检索证据/无引用时让模型给出事实性结论；不得忽略"无结果/低置信"分支。
- **NO-13** 不得引入 AGPL / source-available / 商用受限组件（New-API、Dify、FastGPT、受限模型权重等）进入对外生产而未经法务书面确认。
- **NO-14** 不得新增对已归档的 `langchain-community` 的依赖（迁移至 `langchain-classic` 或独立集成包）。**注（2026-10-16）**：RAGAS 经用户授权采用，而它**无条件**依赖 `langchain-community`——这一处偏离已逐条登记于 [ADR-0003](docs/adr/0003-evaluation-framework-and-judge.md)，其缓解形态是"RAGAS 只作**隔离环境**离线判分器，**交付物与锁文件零污染**"。**本注只说明该条被违在哪里、如何缓解，不改变 NO-14 的效力**：除该授权项外，任何新增对该归档库的引用仍不予合入；也不得援引此注为其它库开同类口子。
- **NO-15** 不得让数据出域到 SaaS 可观测/评估平台（LangSmith 等）而未通过离线合规评估；默认自托管 Langfuse。
- **NO-16** 不得在未跑评估回归并达标的情况下，改动 Prompt/模型/检索策略/图结构/护栏后直接上线。
- **NO-17** 不得提交 `.env`、真实密钥、模型权重、大体积二进制、客户数据到仓库。
- **NO-18** 不得在同一模型上混跑 H800 与国产加速卡；国产卡未经压测不得承载生产流量。
- **NO-19** 不得破坏多租户隔离（绕过 `thread_id`/namespace/向量过滤/RLS），不得信任前端传入的租户身份。
- **NO-20** 不得让代码与本规则不一致：新增依赖/目录/环境变量/跨模块约定却不更新 `CLAUDE.md`、`CHANGELOG.md`（及必要 ADR）。

---

## 14. 规范演进与维护

### 14.1 版本语义

- `CLAUDE.md` 采用语义化版本 **MAJOR.MINOR.PATCH**：
  - **MAJOR**：强制性、破坏性约定变更（目录重构、技术栈替换、禁止项调整导致既有代码需改造）。
  - **MINOR**：新增分层/工具/约定、向后兼容的能力扩展（如新增一类组件、放宽后又收紧的可选项）。
  - **PATCH**：措辞澄清、纠错、示例更新、版本号同步，不改变约束强度。
- 每次合入规则变更，必须在文件头更新版本号与生效日期，并在 `CHANGELOG.md` 追加一条（版本、日期、变更摘要、动机、影响、PR/ADR 链接）。

### 14.2 何时必须更新本规则

出现以下任一情况，提交者**必须**在同一 PR 更新本规则：新增/替换/升级技术栈组件；新增或调整顶层目录；改变 State/API/错误/流式契约；改变安全、多租户、许可证策略；新增一条强制约定或禁止项；基线版本升级。

### 14.3 决策记录（ADR）

- 凡"锁定/替换技术选型、架构分层、跨模块契约、安全与部署策略"的决策，必须在 `docs/adr/` 新增一篇编号 ADR（`NNNN-kebab-title.md`），包含：背景、决策、备选方案与权衡、后果（正面/负面/风险）、合规与许可证、迁移与回滚、日期与决策者。
- ADR **只增不改**（决策被推翻时新增一篇" supersede/废止"ADR 并指向旧篇），保留演进历史。首篇为 [ADR-0001](docs/adr/0001-langgraph-as-orchestration-backbone.md)（锁定 LangGraph 技术栈）。

### 14.4 评审与生效

- 规则变更走 `docs(standards)` 提交，须架构组评审；涉及安全/许可证/对外契约的，按第 12.3 节会签。
- 规则一经合入 `main` 即对全仓库生效；存量代码与规则不符时登记技术债并限期收敛，新代码必须立即遵循。
- 每季度（或重大框架发布后）做一次规则复核：核对框架版本/协议状态（LangGraph、MCP、A2A、国产卡适配等），更新基线与不确定项。

### 14.5 不确定项与时效

本规则基于 **2026-10-09** 前的技术状态（LangGraph 1.x、MCP 已成事实标准、国产卡适配快速演进）。标注为"需核实/需压测/需商务确认"的事项（如部分组件离线授权、国产卡成熟度、个别库精确版本），在投标、选型或上线前必须当日复核官方来源并据实更新，不得据此做不可逆承诺。

---

## 附：最小工程骨架（示意，以官方文档与实际版本为准）

- `langgraph.json`（示意）：

  ```json
  {
    "dependencies": ["."],
    "graphs": {
      "metallurgy_inspection": "./src/agent_platform/agents/metallurgy_inspection/graph.py:build_graph"
    },
    "env": ".env"
  }
  ```

- State 与节点（示意）：

  ```python
  from typing import Annotated, TypedDict
  from langgraph.graph.message import add_messages

  class InspectionState(TypedDict):
      messages: Annotated[list, add_messages]
      evidence: Annotated[list[str], operator.add]   # 聚合字段必须声明 reducer
      human_decision: dict | None                    # HITL 结果

  async def retrieve_evidence(state: InspectionState) -> dict:  # 纯节点：只读 state，返回部分更新
      docs = await knowledge_client.search(state["messages"][-1].content)  # 经封装、带超时/重试
      if not docs:
          return {"__route__": "clarify"}            # 显式处理无证据分支
      return {"evidence": [d.ref for d in docs]}
  ```

- 编译（示意，挂 checkpointer/护栏/追踪）：

  ```python
  graph = builder.compile(
      checkpointer=await get_postgres_saver(),
      store=await get_postgres_store(),
      # middleware=[GuardrailsMiddleware(...), TracingMiddleware(...)],
  )
  ```

> 上述片段仅用于说明规则，不是可直接运行的完整实现；真实实现必须遵守前文各章并通过第 9 章门禁。
