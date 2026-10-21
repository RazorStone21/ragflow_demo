# 项目介绍：agent-platform（RAG 主线）

> 本文面向第一次接手这个仓库的人。目标是在一个文件里讲清楚四件事：项目是什么、
> 代码怎么组织、一次请求和一次入库按什么顺序执行、哪些规矩不能碰。
>
> 规则原文以 [`CLAUDE.md`](../CLAUDE.md) 为准；本文只做提炼和导览，冲突时按它执行。
> 架构决策见 [`docs/adr/`](adr/)，版本演进见 [`CHANGELOG.md`](../CHANGELOG.md)，
> 环境问题见 [`docs/runbooks/`](runbooks/)。

---

## 1. 项目是什么

### 1.1 目标场景

面向政务与能源政策文档的 RAG 问答平台。用户用自然语言提问，系统从已入库的政策
文件里检索证据，给出带引用的答案；证据不足时明确拒答，不编造。

当前交付的是**最小合规骨架 + RAG 主线**：一个智能体 `gov_policy_qa`、一条文档
入库链路、一套评估门禁。不是完整产品，但主链路上的每一环都在真实基础设施上
验证过——真 Postgres、真 Milvus、真 RAGFlow、真实网关。

### 1.2 技术骨架

| 层 | 选型 | 在本项目里的角色 |
|---|---|---|
| 编排 | LangGraph 1.2.15 | 唯一的业务编排中枢，图很小但契约完整 |
| 文档工程 | RAGFlow 0.27.2 | 上传、解析、OCR、切分、图片下载；**不做检索** |
| 检索 | Milvus 2.6.24 | 稠密 + BM25 混合检索、阈值、重排、租户过滤 |
| 模型网关 | LiteLLM Proxy | 所有模型调用的唯一出口，业务代码只认逻辑名 |
| API | FastAPI + SSE | 会话/运行语义对齐 LangGraph Server |
| 检查点 | PostgreSQL + AsyncPostgresSaver/Store | 图状态持久化、断点续跑、历史回放 |
| 评估 | 自研确定性判分器 + RAGAS 0.4.3 + 引用裁判 | 黄金集、门禁判定、基线对比、留痕 |

RAGFlow 与 Milvus 的分工是刻意的（[ADR-0002](adr/0002-ragflow-milvus-division-of-labor.md)）：
RAGFlow 负责"把文档变成切分"，Milvus 负责"把问题变成证据"。RAGFlow 自带的检索
接口在业务里没有调用点。同理，任何可视化画布都不能替代 LangGraph 承载编排
（NO-01）。

### 1.3 演示期与生产期的差别

下面这些都写在 ADR 里，不是疏忽。读代码时看到云端模型、本地图片目录，先想到这一节。

| 项 | 演示期现状 | 生产要求 | 切换方式 |
|---|---|---|---|
| Embedding / Rerank | 经网关指向 SiliconFlow 云端 Qwen3 | 私有化 Qwen3 | 只改网关映射 |
| 解析期视觉模型 | 云端 Qwen3-VL（逻辑名 `vision-chat`） | 私有化 VLM | RAGFlow 侧换模型注册 |
| 图片存储 | 本地文件 `local_data/evidence_images/` | MinIO / S3 | 实现新的 `ImageStore` |
| 裁判模型 | 网关 `judge` 指向云端 Qwen（与生成模型 DeepSeek 不同家族） | 私有化的本地裁判 | 改 `JUDGE_MODEL` 与 `JUDGE_UPSTREAM_MODEL` 映射 |
| RAGAS | `eval` 依赖组进程内判分，默认 `uv sync` 不安装 | 生产镜像不得包含；升级须重跑评估并更新 ADR | `uv run --group eval` |

演示期只允许公开演示语料，客户数据不得出域。这条边界写在 ADR-0002、ADR-0004 里。

---

## 2. 总体架构

### 2.1 分层与依赖方向

依赖方向单向向内，不得反向：

```text
api  →  agents  →  core / knowledge  →  utils
```

`evaluation` 是旁路，依赖方向为 `evaluation → agents/core/knowledge/settings`；
任何运行时模块都不许 import 评估层，由 AST 单测守着
（`tests/unit/test_evaluation_layering.py`）。

### 2.2 组件全景

```text
                 HTTP / SSE
                     │
                     ▼
        ┌─────────────────────────────┐
        │  FastAPI（api/）            │
        │  中间件 → 鉴权 → 路由 → SSE │
        └─────────────┬───────────────┘
                      │ 注入 AppDeps
                      ▼
        ┌─────────────────────────────┐
        │  LangGraph 图（agents/）    │
        │  retrieve → 路由 → generate │
        └──────┬──────────────┬───────┘
               │              │
               ▼              ▼
     ┌─────────────────┐  ┌──────────────────┐
     │ knowledge/      │  │ core/            │
     │ RAGFlow 客户端  │  │ 模型、检查点、   │
     │ Milvus 客户端   │  │ 流式、重试、错误 │
     │ 嵌入、重排      │  └──────────────────┘
     │ ImageStore      │
     └──────┬──────────┘
            │
   ┌────────┼──────────┬─────────────┬──────────────┐
   ▼        ▼          ▼             ▼              ▼
RAGFlow  Milvus   LiteLLM 网关   PostgreSQL      本地图片存储
(文档工程) (检索)    (模型出口)   (checkpoint)    (演示期)
                                   Redis（已起，当前未使用）
```

### 2.3 各层职责

- **api/**：HTTP 边界。只做鉴权、校验、路由、DTO 转换、SSE 帧输出，不含业务判断。
- **agents/**：编排逻辑唯一允许出现的地方。节点、边、State、提示词资产。
- **core/**：框架无关的通用能力。模型客户端、检查点、流式事件、异常层级、重试、
  日志、ID 生成、追踪钩子。
- **knowledge/**：第三方系统对接。RAGFlow、Milvus、嵌入、重排、图片存储的封装。
  节点里不允许裸发 HTTP。
- **tenants/**：租户上下文读取。节点只能从 `config["configurable"]["tenant_id"]`
  取租户，缺失即 401。
- **guardrails/**：护栏接口与不可信内容中和。目前只有 `sanitize.py` 是实打实的，
  其余是 PassThrough 占位。
- **evaluation/**：黄金集、确定性判分、裁判、门禁、基线。旁路，不进请求链路。
- **utils/**：纯函数。文本归一化、指纹、时间。

---

## 3. 目录结构与职责

### 3.1 源码目录

```text
src/agent_platform/
├── main.py                 # FastAPI 装配；无模块级 app，用工厂形态
├── settings.py             # 唯一读取环境变量的地方
├── core/
│   ├── models.py           # 经 LiteLLM 构造 ChatOpenAI，逻辑名引用，可选 fallback
│   ├── checkpoint.py       # AsyncPostgresSaver + AsyncPostgresStore
│   ├── streaming.py        # SSE 事件枚举、帧格式、心跳
│   ├── errors.py           # 异常层级与稳定错误码
│   ├── retry.py            # tenacity 指数退避
│   ├── logging.py          # 结构化 JSON 日志、UTF-8 控制台
│   ├── ids.py              # thread_id / run_id / request_id
│   └── tracing.py          # TracingHook 接口；当前只有 NoOp 实现
├── knowledge/
│   ├── ragflow_client.py   # 上传、解析、轮询、切分、取图
│   ├── milvus_store.py     # 集合管理、混合检索、upsert/delete/count
│   ├── milvus_schema.py    # 字段、索引、租户过滤表达式（唯一构造点）
│   ├── retriever.py        # 检索编排：混合召回 → 阈值 → 重排 → 证据
│   ├── embedder.py         # 网关 /v1/embeddings
│   ├── reranker.py         # 网关 /v1/rerank
│   ├── gateway_client.py   # 内部 httpx 客户端（trust_env=False）
│   ├── images.py           # ImageStore 协议 + 本地实现
│   ├── schemas.py          # ChunkRecord / EvidenceItem / ImageRef / RetrievalMeta
│   └── ragflow_schemas.py  # RAGFlow 接口响应模型与 doc_type 归一
├── agents/
│   ├── registry.py         # 智能体注册表，与 langgraph.json 保持一致
│   └── gov_policy_qa/
│       ├── graph.py        # 拓扑声明与编译
│       ├── state.py        # State Schema、reducer、状态枚举
│       ├── edges.py        # route_after_retrieve
│       ├── schemas.py      # Citation、引用下标解析
│       ├── prompts/        # 版本化提示词资产
│       └── nodes/          # 检索、生成、拒答、前缀门、结果组装
├── api/
│   ├── container.py        # AppDeps 与 open_production_deps
│   ├── deps.py             # 鉴权、租户上下文、会话授权、检查点读取
│   ├── sse.py              # LangGraph 事件流 → SSE 帧
│   ├── idempotency.py      # Idempotency-Key 缓存
│   ├── state_view.py       # 检查点 → 响应 DTO
│   ├── routes/             # threads / runs / evidence_images / health
│   ├── schemas/            # 请求响应 DTO
│   └── middleware/         # request_id、错误处理器
├── tenants/context.py      # tenant_id_from_config
├── guardrails/             # 护栏协议、PassThrough、sanitize
├── evaluation/             # schemas / loader / scoring / judge / runner / reporting
└── utils/                  # text.py、time.py
```

### 3.2 测试目录

| 路径 | 内容 | 运行条件 |
|---|---|---|
| `tests/unit/` | 39 个文件，外部依赖全部 fake；默认全量 `pytest` 为 869 passed / 8 skipped | 默认运行，必须快且确定 |
| `tests/integration/` | 7 个文件、28 条用例：RAGFlow、Milvus、LiteLLM、入库、SSE + 真 Postgres | `RUN_INTEGRATION_TESTS=true`，服务没起自动 skip |
| `tests/evals/` | 黄金集 32 条、阈值（schema v2）、评审协议、冒烟；两份 v1 基线只作历史 | `RUN_EVAL_TESTS=true`，默认 skip |

`tests/conftest.py` 提供 `FakeChatModel`、`FakeMilvusStore`、`FakeRetriever`、
`FakeImageStore` 和 `api_factory`。API 单测走真实 FastAPI lifespan，只替换依赖工厂。

### 3.3 脚本

| 脚本 | 用途 | 退出码 |
|---|---|---|
| `scripts/ingest.py` | 语料入库：RAGFlow → Milvus | 0 成功 / 1 参数错误 / 2 部分失败 / 3 致命 |
| `scripts/demo_chat.py` | 端到端 SSE 冒烟 | 0 完成 / 1 参数 / 2 HTTP / 3 run.error / 4 协议 |
| `scripts/eval_run.py` | 黄金集评估与门禁（检索三项、RAGAS、引用裁判） | 0 达标 / 1 用法 / 2 基建 / 3 跌破 / 4 基线不可比 |
| `scripts/eval_label_worksheet.py` | 生成可答题的相关文档全集工作表（只读） | 0 成功 / 1 用法 |
| `scripts/eval_audit_citations.py` | 引用准确率人工抽检（导出/汇总） | 0 成功 / 1 用法 |
| `scripts/eval_ragas.py` | 旧 RAGAS 旁挂判分，仅作对照；主流程冒烟通过后删除 | 0 / 1 / 2，不参与门禁 |

`scripts/energy_subset.txt` 是 19 份纯文本能源政策白名单，
`scripts/multimodal_subset.txt` 是扫描件与图表报告的多模态子集。

### 3.4 部署与文档

- `docker-compose.yml`：Postgres、Redis、Milvus（etcd + MinIO + standalone）、
  LiteLLM。RAGFlow 不在这里，它用官方 compose 独立部署并加入同一网络。
- `deploy/docker/litellm/config.yaml`：模型逻辑名到具体型号的映射。
- `deploy/docker/milvus/milvus.yaml`：16GB 单机的内存水位覆写。
- `deploy/k8s/`：只有占位 README，没有清单。
- `docs/adr/`：四篇决策记录，只增不改。
- `docs/runbooks/`：本机环境、RAGFlow 部署、多模态证据链路。

### 3.5 尚未创建的目录

`CLAUDE.md` 2.2 列出的以下路径在仓库里**不存在**，引用时先建目录，不要假设它已经在了：

```text
src/agent_platform/config/
src/agent_platform/memory/
src/agent_platform/tools/
src/agent_platform/agents/<name>/subgraphs/
docs/agent-kickoffs/
docs/claude-code-prompts.md
docs/api/
Dockerfile
.claude/commands/
.claude/agents/
```

`.claude/` 目前只有 `settings.json` 与 `settings.local.json`。前者是权限拒绝/确认
清单（读 `.env`、`local_data/**`、`git push`、`--no-verify` 等已被拦），后者是本地临时放行。

---

## 4. 启动与装配顺序

### 4.1 配置加载

`settings.py` 是全仓唯一读取环境变量的地方（`settings.py:32`）。`Settings` 是
frozen 的 pydantic-settings 模型，`.env` 自动加载。四项必填，缺一个就启动失败：

```text
AGENT_API_BEARER_TOKEN
POSTGRES_DSN
LITELLM_API_KEY
RAGFLOW_API_KEY
```

`load_settings()`（`settings.py:184`）把缺失项和非法项分别收集，抛
`ConfigurationError` 时直接列出变量名。启动期失败好过运行中才崩。

配置分组：

- HTTP：端口、Bearer Token、演示租户/用户、SSE 心跳、提问长度上限
- Postgres：DSN、连接超时；Redis 地址（已配置，当前无使用点）
- 网关：地址、密钥、`primary-chat` / `fallback-chat` / `embedding` / `rerank` / `judge`
  五个逻辑名、模型超时与重试
- RAGFlow：地址、API Key、数据集名、解析超时、版面识别、切分参数、详情并发
- 图片：存储目录、单张字节上限、下载超时与并发
- Milvus：地址、集合名、向量维度
- 检索：top_k=8、候选 64、融合阈值 0.2、重排阈值 0.3、稠密权重 0.7
- 评估：黄金集目录、阈值文件、产物/缓存目录、测试开关

`get_settings()` 是进程单例，测试里覆盖前先 `cache_clear()`。

### 4.2 create_app

`main.py:37` 的 `create_app(*, settings=None, deps_factory=open_production_deps)`：

1. 解析配置（显式传入优先，否则 `get_settings()`）
2. 定义 lifespan
3. 构造 `FastAPI`
4. 注册 `RequestContextMiddleware`
5. 注册四个异常处理器（RFC 9457 `application/problem+json`）
6. 挂载 `/healthz`、`/readyz`，再挂 `/api/v1` 下的 version、threads、runs、
   evidence_images 路由

模块导入期不读环境、不连库，所以没有模块级 `app = create_app()`。启动必须用工厂：

```bash
uv run uvicorn --factory agent_platform.main:create_app \
    --loop asyncio:SelectorEventLoop --host 127.0.0.1 --port 8000
```

Windows 上必须带 `--loop asyncio:SelectorEventLoop`。原因见 8.5。

### 4.3 lifespan 与依赖装配

lifespan（`main.py:53`）做两件事：配日志，然后进入 `open_production_deps`
（`api/container.py:93`）。装配顺序就是依赖顺序，任何一步失败由 `AsyncExitStack`
回滚已建立的资源：

1. `GatewayHttpClient`：指向 LiteLLM 的异步客户端，`trust_env=False`
2. `ImageStore`：创建目录并验证可写；启动时失败好过第一次取图才发现
3. `MilvusStore`：连接、`ensure_collection`、校验必需字段
4. `Retriever`：组合 Milvus、`LiteLLMEmbedder`、`LiteLLMReranker` 与检索阈值
5. `ChatModel`：`ChatOpenAI(model=逻辑名, base_url=网关, temperature=0, trust_env=False)`；
   配置了 `FALLBACK_CHAT_MODEL` 才挂应用层 fallback
6. `open_checkpoint_resources`：`AsyncPostgresSaver` + `AsyncPostgresStore`，
   各自 `setup()` 幂等建表
7. `build_graph(...)`：注入 retriever、chat、checkpointer、store、guardrail
8. 组装 `AppDeps`（frozen dataclass），交给 `app.state.deps`
9. 注册就绪探针：Postgres 用一次不写数据的 `aget_tuple`，Milvus 用 `count()`

`guardrail=None` 是有意的：NeMo Guardrails 还没接入，这里不假装有拦截能力。

### 4.4 关停

先 `del app.state.deps`，再让 `AsyncExitStack` 逆序释放。这样关停瞬间到达的请求
拿到"服务正在关停"的 5xx，而不是一个拆了一半的依赖对象。

---

## 5. 一次问答请求的执行顺序

### 5.1 入口：中间件与鉴权

1. `RequestContextMiddleware`（`api/middleware/request_context.py:45`）是纯 ASGI
   中间件：取调用方 `X-Request-Id`（只放行短 ASCII 白名单），没有就生成；写入
   `scope`、日志上下文；响应头回写；结束时记一条访问日志。
2. `require_auth`（`api/deps.py:59`）：解析 Bearer Token，用
   `hmac.compare_digest` 定长比较，解析出 `AuthContext(tenant_id, user_id)`。
   演示期是固定值 `demo` / `demo_user`。
3. `thread_id` 格式为 `{tenant_id}:{user_id}:{session_id}`，任一段非法或与认证上下文
   不符一律按 **404**，不返回 403——403 等于告诉探测者"会话存在但你无权"。

### 5.2 会话与运行端点

| 方法与路径 | 作用 | 关键行为 |
|---|---|---|
| `POST /api/v1/threads` | 创建会话 | 只签发 ID，不落库；201 |
| `GET /api/v1/threads/{id}` | 查询会话概况 | 无检查点即 404 |
| `POST /api/v1/threads/{id}/runs` | 非流式运行 | `graph.ainvoke`，返回终态 |
| `POST /api/v1/threads/{id}/runs/stream` | 流式运行 | SSE，事件序列见 5.4 |
| `GET /api/v1/threads/{id}/state` | 最新状态 | 读检查点，含 `next` / `has_interrupt` |
| `POST /api/v1/threads/{id}/history` | 检查点历史 | 游标分页，由新到旧 |
| `GET /api/v1/threads/{id}/evidence/{chunk_id}/images/{image_id}` | 取证据图 | 只认本会话证据 |
| `GET /healthz`、`/readyz` | 存活/就绪 | 不鉴权；就绪失败 503 |
| `GET /api/v1/version` | 版本与智能体清单 | 不鉴权 |

运行端点的统一前置校验 `_authorize_run`（`api/routes/runs.py:67`）：会话归属 →
智能体已注册 → 智能体已在本进程装配 → 提问长度不超过 `MAX_QUESTION_CHARS`
（默认 2000）。`RunRequest` 是 `extra="forbid"`，请求体里多一个 `tenant_id` 都会 422。

带 `Idempotency-Key` 时：

- 非流式：命中缓存直接返回首次 `RunResponse`，不重复执行
- 流式：命中时做确定性重放，只发 `run.started` + `run.completed` 两帧；
  终态一致，但事件序列不会逐字节相同。这是幂等承诺的边界。

### 5.3 图内执行

图只有三个节点，拓扑在 `agents/gov_policy_qa/graph.py:70`：

```text
START → retrieve_evidence
          ├─ 条件路由：证据充足 → generate_answer → END
          └─ 其余情形       → answer_without_evidence → END
```

**State**（`state.py:97`）：

| 字段 | 类型与 reducer | 含义 |
|---|---|---|
| `messages` | `list[AnyMessage]`，`add_messages` | 对话消息；节点只追加 |
| `evidence` | `list[EvidenceItem]`，`merge_evidence` | 按 `chunk_id` 去重，保留高分 |
| `retrieval_meta` | `RetrievalMeta \| None` | 路由依据 |
| `answer` | `str` | 最终答案 |
| `answer_status` | `str`（`AnswerStatus`） | `pending` / `answered` / `insufficient_evidence` / `citation_missing` / `guardrail_blocked` |
| `citations` | `list[Citation]` | 实际被引用的证据条目 |

`initial_state(question)` 是 API 层构造初始状态的唯一入口，保证字段齐全。

**1）retrieve_evidence**（`nodes/retrieve_evidence.py:37`）

- 取最后一条 HumanMessage；从 `config["configurable"]["tenant_id"]` 读租户，读不到即 401
- 调 `Retriever.search()`，得到证据与判定元数据
- 通过 `writer()` 推 `retrieval.evidence` 事件
- 返回 `{"evidence": ..., "retrieval_meta": ...}`

检索不到不是异常：返回空证据 + `is_sufficient=False` + 稳定 reason。只有基础设施
故障才向上抛，最终变成 `run.error`。

**2）route_after_retrieve**（`edges.py:26`）

确定性判断，不交给模型：

- 证据为空 → `answer_without_evidence`
- `retrieval_meta` 缺失或反序列化后不可解析 → `answer_without_evidence`
- `is_sufficient=False` → `answer_without_evidence`
- 其余 → `generate_answer`

**3a）generate_answer**（`nodes/generate_answer.py:112`）

- `build_answer_messages()`：证据经 `neutralize_untrusted` 中和后，用固定栅栏包进
  用户消息；系统提示词来自版本化资产
- `model.astream()` 逐 token 产出；`NoEvidencePrefixGate` 检查回答开头
- 正常文本经 `message.delta` 推给客户端
- 模型以 `【无据】` 开头：草稿整体丢弃，状态改 `insufficient_evidence`，
  返回本地固定拒答文案，并把检索元数据降级（`model_declared_insufficient_evidence`）
- 校验 `[n]` 下标都在证据范围内；没有合法引用 → `citation_missing` + 固定拒答
- 输出护栏检查（当前 PassThrough）；拦截 → `guardrail_blocked`
- 全部通过 → `answered`，`build_citations()` 按引用下标生成引用列表

**3b）answer_without_evidence**（`nodes/answer_without_evidence.py:52`）

不调模型。按检索 reason 选一条面向用户的建议，返回确定性拒答 + `insufficient_evidence`。
这条分支是正常业务路径，不是错误。

节点执行完，检查点由 AsyncPostgresSaver 落库。流式路径用 `durability="sync"`，
保证后面的 `aget_state()` 一定读到本轮终态。

### 5.4 SSE 事件序列

定义在 `core/streaming.py:28`，共 10 类，是平台统一枚举：

```text
run.started        node.entered       message.delta      tool.called
tool.result        retrieval.evidence human.interrupt    node.exited
run.completed      run.error
```

**当前实际会发出的只有 7 类**。`tool.called`、`tool.result` 没有工具可调，
`human.interrupt` 没有 `interrupt()`，全仓没有发送点。读 SDK 文档时不要按 10 类
都有来假设。

一次流式运行的帧顺序：

1. `run.started`（payload 含 agent）
2. 图执行期间：`node.entered` / `node.exited`（来自 `tasks` 流）、
   `retrieval.evidence`（检索节点）、`message.delta`（生成节点）
3. 空闲超过 `SSE_HEARTBEAT_S`（默认 15 秒）插入 `: ping`
4. 结束后读 `aget_state`，发 `run.completed`（答案、状态、引用、检索元数据）
5. 任何异常转成 `run.error` 后正常收流，不让客户端挂起

每条事件带 `type`、`seq`、`run_id`、`thread_id`、`request_id`、`payload`。

### 5.5 状态查询与历史

`GET /state` 和 `POST /history` 直接读 LangGraph 检查点，不另建会话状态表。
未知会话返回 404，而不是空列表——空列表会被误读成"跑过但没有记录"。
历史用 `checkpoint_id` 做游标，多取一条判断是否还有下一页。

### 5.6 取图

`api/routes/evidence_images.py:63`。先校验会话归属，再从该会话检查点的 `evidence`
里精确匹配 `(chunk_id, image_id)`，命不中一律 404。只有未认证是 401。

这样设计的理由：URL 里的 ID 不是全局查找键，拿到别处的图片 ID 也取不出字节。
响应带 `Cache-Control: private, no-store`、`X-Content-Type-Options: nosniff`、
`Content-Disposition: inline`。没有"按知识库取图"的端点，也不返回对象存储签名 URL。

---

## 6. 三条核心数据流

### 6.1 文档入库

入口 `scripts/ingest.py`。先跑 `--dry-run` 看清单，确认后再真实入库。

```bash
uv run python scripts/ingest.py --source <语料目录> --subset scripts/energy_subset.txt --dry-run
uv run python scripts/ingest.py --source <语料目录> --subset scripts/energy_subset.txt
uv run python scripts/ingest.py --source <语料目录> --subset scripts/multimodal_subset.txt
```

**幂等契约**：源文件相对路径 + 整文件 sha256 + `media_version`。清单存在
`local_data/ingest_manifest.json`。跳过条件三个都要满足：指纹一致、上次切分数非零、
`media_version == 3`。任一条件不满足就重入：先删掉旧 document_id 在 Milvus、
ImageStore 与 RAGFlow 里的产物，再重新上传——Milvus 主键沿用 RAGFlow 的 `chunk_id`，
内容一改 chunk_id 全变，旧行不会覆盖，只会变成永不命中的孤儿。

完整步骤：

1. 读文件、算指纹、查清单
2. `RagFlowClient.upload_document()` 上传
3. `start_parsing()` → `wait_parse_done()` 轮询到终态；`FAIL` / `CANCEL` 判失败
4. `iter_chunks()` 分页拉切分，过滤不可用切分
5. **逐条调用 `get_chunk()` 读详情**，归一 `doc_type_kwd` 为 `text` / `image` / `table`。
   不能只看列表接口：RAGFlow v0.27.2 的列表会把非字符串取值一律写成 `text`，
   表格和图片会全部判成文本。并发上限由 `RAGFLOW_CHUNK_DETAIL_CONCURRENCY` 控制
6. `chunk.image_id` 非空就下载图片：流式读取、单张字节上限、**按魔数判定 MIME**，
   落 `ImageStore`，路径为 `{tenant_id}/{document_id}/{chunk_id}/{image_id}.{ext}`；
   写入用临时文件 + `os.replace` 原子化
7. `ChunkInfo → ChunkRecord` 映射；`chunk_id` 直接沿用 RAGFlow 主键
8. `LiteLLMEmbedder.embed_documents()` 与 `MilvusStore.upsert_chunks()` 按同一批大小
   32 条处理
9. 全部文件处理完后统一 `flush()`——Milvus 是有界一致性，不显式 flush 时刚写完可能查不到

失败处理：

- `image` 类型切分取图失败 → **整份文件判失败**并补偿删除，不降级成"有切分无图片"
- `table` / `text` 附图失败 → 允许降级为 `images=[]`，记 WARNING 并计数
- 解析或取图失败 → 尽力删除该 document_id 在 RAGFlow 与图片目录的产物；失败文档
  不入清单，下次重跑会重新上传。RAGFlow 拒绝同名重复上传，不清理会导致重跑永远失败
- 索引阶段（嵌入 / Milvus 写入）中途失败不在这轮补偿范围内：已 upsert 的批次会留在
  Milvus，而失败文档不进清单，下一轮无从得知它的 document_id。这是当前已知的残留风险，
  排障时按日志里的 document_id 手动清一次

**Milvus schema**（`knowledge/milvus_schema.py:94`，集合默认 `gov_policy_chunks_v2`）：

| 字段 | 类型 | 用途 |
|---|---|---|
| `chunk_id` | VARCHAR 主键，`auto_id=False` | RAGFlow 主键，天然幂等 |
| `content` | VARCHAR + 中文分析器 | 切分原文，BM25 输入 |
| `content_sparse` | SPARSE | 由 BM25 函数自动生成，不直接写 |
| `content_embedding` | FLOAT_VECTOR(4096) | 稠密向量 |
| `tenant_id` / `dataset_id` / `document_id` | VARCHAR | 过滤与删除 |
| `document_name` / `source_uri` | VARCHAR | 引用展示 |
| `positions` | JSON | 页号提取 |
| `fingerprint` | VARCHAR | 内容指纹 |
| `updated_at` | INT64 | UTC epoch 秒 |
| `media_type` | VARCHAR | text / image / table |
| `images` | JSON | 图片元数据列表 |

索引：稠密 HNSW/COSINE（M=16，efConstruction=100），稀疏 SPARSE_INVERTED_INDEX/BM25，
`tenant_id` 与 `document_id` 走 AUTOINDEX。租户过滤表达式只在
`milvus_schema.tenant_filter()`（`milvus_schema.py:192`）构造，字符串值做转义，
调用方绕不过去（NO-19）。

### 6.2 检索与问答

`Retriever.search()`（`knowledge/retriever.py:77`）：

1. 空白归一；空问题直接返回 `empty_query`
2. `embed_query()` 经网关逻辑名 `embedding`
3. `MilvusStore.hybrid_search()`：稠密与稀疏各召回 `RETRIEVAL_CANDIDATES`（默认 64），
   `WeightedRanker(0.7, 0.3)` 融合，过滤表达式为租户（可选数据集）
4. 融合分低于 `SIMILARITY_THRESHOLD`（0.2）的丢弃
5. 重排：逻辑名 `rerank`，取前 `EVIDENCE_TOP_K`；失败则降级为融合分排序，
   记 WARNING，`rerank_applied=False`
6. 重排分低于 `RERANK_SCORE_THRESHOLD`（0.3）的丢弃
7. 保留前 8 条，产出 `RetrievalOutcome`

证据对象 `EvidenceItem` 带 `chunk_id`、正文、分数、文档名、页码、来源、媒体类型、
图片元数据。`vector_score` / `bm25_score` 恒为 `None`——Milvus 的
`hybrid_search + WeightedRanker` 只返回融合分，分路原始分没有取回。补齐要改检索
形态，属于检索策略变更，按 NO-16 得先有基线，所以当前只在评估层登记缺口。

答案生成见 5.3。引用对象 `Citation` 含序号、`chunk_id`、文档名、页码、来源、图片；
引用下标必须与正文 `[n]` 一致，且不越界。

### 6.3 评估

评估层分三层（`evaluation/__init__.py`）：

| 层 | 依赖 | 内容 |
|---|---|---|
| A 确定性判分 | 无，纯函数 | `scoring.py`，对已录制产物判分 |
| B 系统执行 | 真基建 + 网关 | `runner.py`，跑检索与图，产出待判分结果 |
| C 裁判判分 | 真基建 + 裁判模型 | `ragas_judge.py`（RAGAS 三项）与 `judge.py`（引用准确率） |

**七个确定性判分器**（`evaluation/scoring.py`）：

| 判分器 | 判什么 |
|---|---|
| `score_retrieval_metrics` | `recall_at_k` / `precision_at_k` / `mrr`，以及仅监控的 `document_precision_at_k`；依赖人工签署的 `relevant_documents` |
| `score_image_evidence_hit` | 按文档名 + 页码核对期望图片是否随证据出现 |
| `score_image_citation_integrity` | 引用图片与证据图片逐张一致，缺图/多图/错图皆失败 |
| `score_citations` | `[n]` 不越界、正文与引用列表下标一致、`chunk_id` 与文档名不错位、`answered` 必带引用 |
| `score_refusal` | 拒答状态、文案非空、无 `[n]` 残留、检索元数据自洽 |
| `score_tenant_isolation` | security 类样例中陌生租户零命中 |
| `score_prompt_hygiene` | 生产渲染路径中栅栏标记出现次数不超过预期 |

判分器全部零 I/O、零网络、零时钟，可以在单测里穷举分支。`status_accuracy` 与
`retrieval_hit` 已退役：状态兜底并入 `answer_correctness`（期望 `answered` 却拒答
或答案为空时直接记 `FAIL 0.0`）。

**RAGAS**（`evaluation/ragas_backend.py` + `ragas_judge.py`）：0.4.3，装在
`eval` 依赖组，`uv run --group eval` 进程内判分。判 `faithfulness`（0.90）、
`answer_relevancy`（0.85）、`answer_correctness`（0.85，需参考答案）；
`hallucination_rate = 1 − aggregate(faithfulness)` **仅监控**。期望非 `answered`
的样例三项全部 `N/A`，由 `refusal_correctness` 兜底。`StrictAnswerCorrectness`
覆写 ragas 的断言抽取：抽取为空时抛错并记 `ERROR`，避免 0.4.3 "抽取失败 = 事实性
满分 1.0"的陷阱。NaN、越界、第三方异常一律 `ERROR`、不带分数。导入前强制
`RAGAS_DO_NOT_TRACK=true`。升级 ragas 必须重验私有方法 `_generate_statements`
并重跑评估（ADR-0006）。

**引用裁判**（`evaluation/judge.py`）：提示词资产 `citation.judge.v1`，输入问题、
答案、引用清单与被引用证据块，只判被引用的断言是否被对应证据支持；分数 = 支持数
÷ 引用总数，无引用记 `N/A`。引用对不上证据块属测得出的坏结果，记 `FAIL 0.0`；
解析失败纠偏一次，两次失败记 `ERROR`。缓存键含"提示词名 + 裁判身份（逻辑名@上游
型号）+ 渲染后完整提示词"；`--no-judge-cache` 可强制绕过。2026-10-21 起网关把
`judge` 指向 SiliconFlow 的 Qwen，与生成模型 DeepSeek 不同家族；但 RAGAS 与引用
裁判**共用这一个模型**，独立性只在判分方法，不在模型。问题、证据、答案与参考答案
都会出域，产物里写明 `CLOUD_JUDGE_CAVEAT`，仅限公开演示语料，分数仍按"有偏测量"
解读（ADR-0005 / ADR-0006）。

**指标状态**：每个指标是 `pass` / `fail` / `not_applicable` / `error` 四取一。
`not_applicable` 与 `error` 都不带分数；`error` 不计入聚合分，但该样例判不通过、
门禁单列"测量失败"。"没测出来"绝不能记成 0 分——那等于凭空造一个质量数据点。

**黄金集**：32 条，分布在 `tests/evals/golden/`：

```text
typical.jsonl      8 条
boundary.jsonl     9 条
refusal.jsonl      5 条
security.jsonl     6 条
multimodal.jsonl   4 条
```

32 条题目全部经过人工逐条对照原文签署（`reviewed_by` / `reviewed_at` 非空）。
**21 条可答题的相关性标签（`relevant_documents`）尚未由人补齐**，这是 v2 检索
三项的前置条件；11 条拒答/安全样例不得有标签。标签另有独立签署字段
`relevance_reviewed_by` / `relevance_reviewed_at`，`--require-relevance-labels`
缺一条即退出码 1。AI 只生成工作表，不代签。

**阈值**在 `tests/evals/config/thresholds.toml`（`schema_version = 2`）：

| 指标 | 阈值 | 最小样本 |
|---|---|---|
| `recall_at_k` | 0.90 | 10 |
| `precision_at_k` | 0.60 | 10 |
| `mrr` | 0.75 | 10 |
| `faithfulness` | 0.90 | 10 |
| `answer_relevancy` | 0.85 | 10 |
| `answer_correctness` | 0.85 | 10 |
| `citation_accuracy` | 0.95 | 10 |
| `refusal_correctness` | 0.95 | 5 |
| `citation_integrity` | 1.00 | 5 |
| `tenant_isolation` | 1.00 | 5 |
| `prompt_hygiene` | 1.00 | 10 |
| `image_citation_integrity` | 1.00 | 3 |
| `image_evidence_hit` | 0.90 | 3 |

`hallucination_rate` 与 `document_precision_at_k` 只监控、不设阈值，写进阈值文件
会被加载器拒绝。阈值放文件而不是环境变量，是为了让每一次调整都必须经过提交与
评审，无法用一行 `export` 悄悄调松。

**执行层次**：

| 参数 | 调模型 | 判什么 |
|---|---|---|
| `--layer retrieval` | 否 | 检索三项（含监控项）与图片证据命中 |
| `--layer graph` | 是 | 上述 + 引用完整性、拒答、租户隔离、提示词卫生、图片引用完整性 |
| `--layer judge` / `--layer all` | 是，另加裁判 | 再加 RAGAS 三项与引用准确率 |

`--layer graph` 与 `--skip-judge` 会照常落产物，但判定强制为 `incomplete`，不能
当通过；要门禁结论必须跑 `all`。

**门禁判定**（`evaluation/reporting.py`）同时看三类问题：

1. 跌破阈值
2. 样本量不足（`min_samples`）
3. 测量失败（`error`）

任一类存在即 `Gate.ok = False`。有 `--baseline` 时再追加新失败样例、新通过样例、
消失样例与配置漂移；黄金集指纹变化会判"不可比"（退出码 4）。

**退出码**：0 达标；1 参数/配置；2 基础设施失败或全部样例执行失败；3 跌破阈值或
新失败；4 基线不可比。2 与 3 分开，是为了让人知道该查基础设施还是查质量。

**产物**：`local_data/evals/runs/<run_id>.json`，判分缓存
`local_data/evals/cache/`，引用人工核对表 `local_data/evals/audits/`。产物记录
git sha 与 dirty 状态、提示词指纹、模型/检索/语料/裁判快照、黄金集指纹与标签
覆盖计数。提升基线必须给 `--promote-by`，并拒绝测量失败、样本不足、草稿、标签
未齐、脏工作区、无人工抽检摘要的轮次。

**当前状态**：观察期。报告、判定、留痕、`--baseline` 检出都正常，退出码暂不接
pre-commit。v1 的两份基线（`2026-10-16-phase7-*.json`）是 schema v1，新代码读入
即报不可读，只作历史；**尚无 v2 基线**，因为标签与语料尚未就绪，也还没在真实
基础设施上跑过 v2 全量。

---

## 7. 必须遵守的工程规矩

### 7.1 二十条红线

完整表述见 `CLAUDE.md` 第 13 章。按主题记：

**编排**

- NO-01 不得用 Dify、RAGFlow、FastGPT、AutoGen、CrewAI 或任何画布替代 LangGraph
  承载核心编排
- NO-08 不得在生产长流程里只用内存 checkpointer
- NO-09 不得用自建标志位 + 轮询模拟人机协同，必须 `interrupt()` + `Command(resume=)`

**模型调用**

- NO-02 不得直连模型厂商 API，所有调用经 LiteLLM 网关
- NO-03 不得硬编码密钥、连接串、内部 URL、客户标识、模型名

**代码质量**

- NO-04 节点不得原地修改 State，必须返回部分状态字典
- NO-05 async 路径不得有阻塞调用，不得创建不受图管理的后台任务
- NO-06 不得裸 `except`、吞异常、注释测试、`# type: ignore`、`--no-verify` 制造假绿
- NO-07 不得用 `print` 调试，不得记录密钥、系统提示词全文、未脱敏 PII

**工具**

- NO-10 高危或有副作用的工具不得仅凭模型输出直接执行
- NO-11 不得给智能体超最小权限的工具集，不得让工具接受模型自由文本参数

**知识**

- NO-12 无检索证据、无引用时不得给出事实性结论；无结果/低置信分支不得忽略

**合规**

- NO-13 不得未经法务书面确认引入 AGPL / source-available / 商用受限组件
- NO-14 不得新增对已归档 `langchain-community` 的依赖；RAGAS 是唯一登记过的例外
- NO-15 不得让数据默认出域到 SaaS 可观测/评估平台
- NO-17 不得提交 `.env`、真实密钥、模型权重、大二进制、客户数据
- NO-18 不得在同一模型上混跑 H800 与国产加速卡

**门禁与多租户**

- NO-16 改 Prompt、模型、检索策略、图结构、护栏后，未跑评估回归并达标不得上线
- NO-19 不得破坏多租户隔离，不得信任前端传入的租户身份
- NO-20 新增依赖/目录/环境变量/跨模块约定却不更新 `CLAUDE.md`、`CHANGELOG.md`
  （及必要 ADR），视为代码与规则不一致

### 7.2 目录与依赖

- 编排只写在 `agents/<name>/`；`core/`、`knowledge/` 不得反向 import 具体智能体
- HTTP 边界只在 `api/`；节点、工具、core 不依赖 FastAPI 的 `Request` / `Response`
- 第三方对接只在 `knowledge/`、`tools/` 的封装里；节点里不裸发 HTTP、不拼 URL
- 环境变量只在 `settings.py` 读一次；其余位置注入 `Settings`
- 依赖方向 `api → agents → core/knowledge → utils`，不得循环
- 节点文件不超过约 150 行；文件名、目录名一律 snake_case

### 7.3 配置、日志与安全

- 配置启动期 fail-fast；`.env.example` 与 `settings.py` 字段同步（NO-20）
- 结构化 JSON 日志，字段统一：`timestamp/level/request_id/thread_id/tenant_id/
  agent/node/run_id/event/message`；禁止 `print`
- 每次 run 的 `request_id / thread_id / run_id / tenant_id` 贯穿日志与追踪
- 外部内容（检索结果、工具返回、用户上传）在 Prompt 里按不可信数据隔离，
  其中的指令不得执行
- 租户只从认证结果来；`thread_id`、记忆 namespace、向量过滤、图片目录、审计日志
  都按租户隔离

### 7.4 质量门禁

提交前按顺序跑：

```bash
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest                      # 单元测试
RUN_INTEGRATION_TESTS=true uv run pytest -m integration
RUN_EVAL_TESTS=true uv run pytest -m eval
```

pre-commit 实际包含：gitleaks → ruff format --check → ruff check → mypy →
文件格式钩子（行尾空白、EOF、YAML/TOML、合并冲突、大文件、私钥）。**不含 pytest、
coverage 与评估**。评估慢、要密钥、要整套基础设施，塞进 pre-commit 只会训练人加
`--no-verify`。

覆盖率：`core/`、`agents/`、`tools/`、`api/` 行覆盖 ≥ 80%；`evaluation/` 同标准，
确定性判分器要求 100%。唯一豁免是 `evaluation/runner.py`，理由写在
`pyproject.toml` 的 `omit` 注释里。

### 7.5 提交与 AI 协作

- 提交信息用 Conventional Commits：`<type>(<scope>): <subject>`；
  规则变更用 `docs(standards)`，ADR 落地用 `chore(adr)`
- `main` 受保护，只经 PR 合入；禁止 `--no-verify`，禁止绕过评审直推
- 非平凡任务先出实施计划（State/契约/文件清单/测试点/风险），经人确认再写代码
- AI 不得自行决定：新增/替换依赖、改架构或目录、改安全与多租户策略、连生产、
  `git push`、删除文件、跳过校验
- 引入新依赖、新目录、新环境变量或改变跨模块约定时，同一次提交更新 `CLAUDE.md`、
  `CHANGELOG.md`，必要时新增 ADR（NO-20）

---

## 8. 当前进度与已知边界

### 8.1 实现程度

| 模块 | 状态 |
|---|---|
| `settings.py`、`api/`、主图与节点 | 完整实现 |
| `core/` 的模型、检查点、流式、错误、重试、日志 | 完整实现 |
| `knowledge/` 的 RAGFlow、Milvus、嵌入、重排、图片 | 完整实现 |
| `evaluation/` | 完整实现，观察期 |
| `guardrails/sanitize.py` | 完整实现（确定性文本处理） |
| `core/tracing.py` | 只有 NoOp 实现，无 Langfuse/OTel 接线 |
| `guardrails/` 其余部分 | PassThrough，`container.py` 传 `guardrail=None` |
| `tools/` | 目录不存在，注册表 `tools=()` |
| HITL | 全仓没有 `interrupt()` |
| Redis / worker | Redis 已起，代码无使用点；没有 worker 进程 |
| `deploy/k8s/` | 只有占位 README |
| `langgraph.json` CLI 加载 | 当前不可用，见 8.4 |

### 8.2 演示期偏离

见 1.3 的表。再强调一次：云端嵌入、重排、视觉模型意味着语料出域，只允许公开演示
语料；图片存本地意味着没有备份与生命周期管理；裁判在云端意味着评估问答与证据
出域，且模型判定不等于人工金标准。这三条都登记在 ADR 中，不是遗漏。

### 8.3 已知缺陷

| 编号 | 状态 | 内容 |
|---|---|---|
| D1 | 2026-10-17 已修 | 生成侧原先没有"模型自称无法回答"分支，模型拒绝时仍记 `answered` 并保留引用。现由 `【无据】` 前缀门改判状态并丢弃草稿 |
| D2 | 2026-10-17 已修 | 裁判提示词缺少"即使答得出也必须拒答"这一档，把拒绝泄露系统提示词判成不当拒答。v2 提示词补齐 |
| D3 | 未裁定 | 边界题检索没取到支持答案的段落，模型照证据拒答，黄金集期望仍是 `answered`。是检索缺口还是期望过严，需人工裁定 |
| D11 | 2026-10-21 退役 | 拒答文案没有事实性断言，旧自研 faithfulness 裁判返回空 `claims`，被记成测量失败。v2 对非 `answered` 样例的 RAGAS 三项直接记 `N/A`，由 `refusal_correctness` 兜底 |
| D12 | 2026-10-21 已修 | 判分缓存键不含裁判身份，换 `judge` 上游型号后旧判定照样命中；快照也只记逻辑名。现键含"逻辑名@上游型号"，`_CACHE_SCHEMA_VERSION` 升到 2，快照新增 `upstream_model`，引用裁判与 RAGAS 缓存都适用 |
| D13 | 2026-10-21 已修 | 引用裁判在 `reason` 里写原文时混用中文引号与 ASCII 双引号，JSON 被提前终止；提高输出上限与纠偏重试都无效。现请求加 `response_format=json_object`，快照记录该约束并纳入缓存键 |
| D14 | 2026-10-21 已修 | RAGAS faithfulness 只拿到证据正文，丢了生成器看到的编号、文档名与页码；答案按文档名指称来源时被判无据。现按生成提示词的同一骨架构造上下文 |

v1 的两份基线记录着当时的失败，**都不代表达标**，且结构版本已不兼容、新代码读不
回来。基线的作用是度量后续改动，不是质量证书。当前 v2 还没有基线：相关性标签未
签署、语料未补齐，首轮全量评估的产出只作报告。

### 8.4 注释与代码不一致

接手时容易踩的四处，以代码为准：

1. `langgraph.json` 指向 `graph.py:build_graph`，但该函数要求注入 retriever、chat、
   checkpointer 等必填参数，没有零参包装。`langgraph dev` / CLI 实际加载不了。
   `graph.py` 的 docstring 自己也写了这一点，但入口声明仍指向它
2. `graph.py` 的 docstring 说生产装配见 `api/deps.py`，实际在 `api/container.py`
3. `graph.py` 说 store 未启用（`None`），实际 `container.py:131` 传了 `resources.store`
4. README 说 tracing "写好了但没接线"；实际 `generate_answer.py:148` 与
   `retriever.py:126` 已调用钩子，只是钩子本身是 NoOp，效果等同未接入

### 8.5 本机环境坑

Windows 11 + WSL2 + Docker Desktop 下会遇到：

- **psycopg 异步不支持 ProactorEventLoop**。Windows 默认事件循环是 Proactor，
  必须显式换 Selector：`--loop asyncio:SelectorEventLoop`。Linux 默认就是 Selector，
  这是纯本机问题
- **`POSTGRES_DSN` 写 `127.0.0.1`，不要写 `localhost`**。本机 `localhost` 先解析到
  `::1`，Docker 只监听 IPv4，libpq 会卡满超时才回落
- **系统代理会劫持发往 localhost 的请求**。httpx 默认 `trust_env=True`，在 Windows
  上读注册表里的 WinINET 代理。所有内部客户端都已 `trust_env=False`；新写的内部
  HTTP 客户端照抄
- **换行符**：本仓库必须 `core.autocrlf=false`，否则 `git status` 满屏幽灵改动。
  这是本地配置，不下发，新机器要手动设一次
- **控制台中文乱码**：Windows 默认 GBK，脚本入口统一调 `enable_utf8_console()`；
  临时看 pytest 输出用 `PYTHONIOENCODING=utf-8`
- **pytest 摘要不打印**：`pyproject.toml` 的 `addopts` 已含 `-q`，命令里再传 `-q`
  等于 `-qq`，摘要按设计不显示。看到"没有摘要"不必怀疑测试没跑

---

## 9. 常用命令

```bash
# 配置与依赖
cp .env.example .env          # 按注释补齐必填项；.env 严禁入库
uv sync

# 基础设施（RAGFlow 独立启动，见 runbook）
docker compose up -d
docker compose ps

# 启动 API（Windows 必须带 --loop）
uv run uvicorn --factory agent_platform.main:create_app \
    --loop asyncio:SelectorEventLoop --host 127.0.0.1 --port 8000

# 冒烟
uv run python scripts/demo_chat.py --question "2025年新能源电价机制的主要内容？"

# 入库
uv run python scripts/ingest.py --source <语料目录> --subset scripts/energy_subset.txt --dry-run
uv run python scripts/ingest.py --source <语料目录> --subset scripts/energy_subset.txt

# 门禁
uv run ruff format --check && uv run ruff check && uv run mypy && uv run pytest
RUN_INTEGRATION_TESTS=true uv run pytest -m integration

# 评估（RAGAS 在 eval 依赖组，一律 --group eval）
uv run --group eval python -X utf8 scripts/eval_run.py --validate-only --require-reviewed
uv run --group eval python -X utf8 scripts/eval_run.py --validate-only \
  --require-reviewed --require-relevance-labels
uv run --group eval python -X utf8 scripts/eval_run.py --layer all \
  --baseline tests/evals/baselines/<file>.json
uv run --group eval python -X utf8 scripts/eval_run.py --show <case_id> --from latest
uv run --group eval python -X utf8 scripts/eval_run.py --from latest --rejudge

# 相关性标签工作表（只读）与引用人工抽检
uv run --group eval python -X utf8 scripts/eval_label_worksheet.py \
  --output local_data/evals/relevant-documents.md
uv run --group eval python -X utf8 scripts/eval_audit_citations.py \
  --artifact local_data/evals/runs/<run_id>.json --sample 20
```

标签由人核对后直接改黄金集 JSONL，AI 不代签；人工抽检结论只写
`local_data/evals/audits/`，不入库。`scripts/eval_ragas.py` 是旧旁挂路径，仅作
对照，主流程在真实基础设施上冒烟通过后删除。

具体参数以各脚本 `--help` 为准。

---

## 10. 延伸阅读

按这个顺序读，先建立全局，再看细节：

1. [`README.md`](../README.md)：快速开始、边界摘要、当前进度
2. [`CLAUDE.md`](../CLAUDE.md)：最高工程规则。第 1 章技术栈、第 2 章目录、第 4 章
   LangGraph 规范、第 9 章测试与评估、第 13 章红线，优先读
3. [`docs/adr/0001`](adr/0001-langgraph-as-orchestration-backbone.md)：为什么锁 LangGraph
4. [`docs/adr/0002`](adr/0002-ragflow-milvus-division-of-labor.md)：RAGFlow 与 Milvus 的分工
5. [`docs/adr/0003`](adr/0003-evaluation-framework-and-judge.md)：旧评估框架（历史；隔离形态已被 0006 取代）
6. [`docs/adr/0004`](adr/0004-multimodal-document-evidence.md)：多模态解析与图片证据
7. [`docs/adr/0005`](adr/0005-cloud-qwen-judge.md)：云端 Qwen 裁判选型与出域边界
8. [`docs/adr/0006`](adr/0006-ragas-in-process-evaluation.md)：v2 评估方案、RAGAS `eval` 依赖组、旧指标退役
9. [`tests/evals/README.md`](../tests/evals/README.md)：评审协议、缺陷登记、读数约束
10. [`CHANGELOG.md`](../CHANGELOG.md)：每轮变更的动机与影响面
11. [`docs/runbooks/local_env_setup.md`](runbooks/local_env_setup.md)：换机时的环境准备
12. [`docs/runbooks/ragflow_setup.md`](runbooks/ragflow_setup.md)：RAGFlow 部署与模型注册
13. [`docs/runbooks/multimodal_evidence.md`](runbooks/multimodal_evidence.md)：多模态链路
    的配置、验证与故障处理

---

## 附：读代码的建议路径

如果目标是理解主链路，按这个顺序看，不要从目录树的第一个文件开始：

1. `settings.py` —— 有哪些配置
2. `main.py` + `api/container.py` —— 应用怎么装配起来
3. `api/routes/runs.py` + `api/sse.py` —— 请求怎么变成图执行和事件流
4. `agents/gov_policy_qa/graph.py` + `state.py` + `edges.py` —— 图怎么走
5. `agents/gov_policy_qa/nodes/` —— 每个节点做什么
6. `knowledge/retriever.py` + `milvus_store.py` —— 证据从哪来
7. `scripts/ingest.py` —— 证据怎么进去
8. `evaluation/__init__.py` + `scripts/eval_run.py` —— 质量怎么度量

每读完一段，对照 `tests/unit/` 里对应的测试文件。这个仓库的测试既是验证，也是
对设计取舍最直接的说明。
