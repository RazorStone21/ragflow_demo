# CHANGELOG — 规范与平台版本演进台账

> 本文件记录 **工程规范（`CLAUDE.md`）与平台基线**的版本演进，是 [CLAUDE.md](CLAUDE.md) 第 14 章「规范演进与维护」的配套台账。
> 版本语义（CLAUDE.md 14.1）：**MAJOR** = 破坏性约定变更（既有代码需改造）；**MINOR** = 向后兼容的能力/分层/约定扩展；**PATCH** = 措辞澄清、纠错、示例与版本号同步。
> 每条记录必须包含：**版本、日期、变更摘要、动机、影响面、关联 PR/ADR**。规范变更与代码变更必须在同一次提交中同步（NO-20）。

---

## [未发布]

**进行中**：最小合规骨架 + RAG 主线（6 阶段计划）。阶段一（`core/`）、阶段二（`knowledge/`）、阶段三（`agents/gov_policy_qa`）、阶段四（`api/` + SSE）已落地；阶段五（基础设施与入库）、阶段六（规则同步）尚未完成。

本条目将在全部阶段收尾时替换为正式的 **v1.2.0** 记录，届时必须包含：

- **变更**：新增 `agents/`（智能体注册表 + `gov_policy_qa` 图与节点）、`tenants/`（租户上下文）、`guardrails/sanitize.py`（不可信内容中和）、`knowledge/`（RAGFlow 客户端 + Milvus 存储 + 嵌入/重排 + 检索器）、`core/` 的平台原语；`langgraph.json` 入口声明；提示词资产化（`prompts/*.v1.jinja`）；`ChatModel.astream` 流式契约与 SSE 事件分层（节点推 `AgentStreamEvent`，API 层映射为带 `seq` 的帧）。
- **ADR**：[ADR-0001](docs/adr/0001-langgraph-as-orchestration-backbone.md)（LangGraph 编排主干）、[ADR-0002](docs/adr/0002-ragflow-milvus-division-of-labor.md)（RAGFlow / Milvus 双后端分工）。
- **基线版本实测值**（以 `pyproject.toml` / `uv.lock` / 镜像 tag 为权威落地值，覆盖 CLAUDE.md 第 1 章表格的意图值）：langgraph 1.2.15、langgraph-checkpoint-postgres 3.2.0、langchain-openai 1.7.2、fastapi 0.143.1、pydantic 2.14.1、pymilvus 2.6.x、RAGFlow v0.27.2、Milvus v2.6.24。
- **已知偏离（须如实记录）**：LiteLLM 镜像 tag（`main-v1.34.4`）与 CLAUDE.md 表格标注的基线（v1.104+）**编号体系不一致**，实际以镜像 tag 为准；偏离原因与后续处理在此说明。
- **新增环境变量**：见 `.env.example`（`MILVUS_DIM`、检索阈值、`PRIMARY_CHAT_MODEL`、`EMBEDDING_MODEL`、`RERANK_MODEL` 等）。
- **占位与顺延**：Langfuse/OTel 追踪、NeMo Guardrails + Presidio 护栏、黄金集 + RAGAS 评估门禁、K8s 部署、HITL 中断恢复、私有化 Qwen3 切换 —— 均为接口占位，实际实现顺延至阶段二。

### 未发布 · 工程门禁（`chore(ci)`）

**变更**：

- `.pre-commit-config.yaml` 新增**密钥扫描钩子**（CLAUDE.md 12.3 要求「pre-commit 至少包含 …… 密钥扫描（gitleaks 等）」）。取 `repo: local` + `language: system`，entry 与上游官方钩子逐字一致（`gitleaks git --pre-commit --redact --staged --verbose`），而非上游的 `language: golang` —— 后者需现场编译，要求 Go 工具链与公网拉取。
- 新增 **`.gitleaks.toml`**：仅 `[extend] useDefault = true` 继承官方规则集 + 一条 `.venv/` 路径豁免，**不新增、不削減任何检测规则**。段名采用 v8.25.0 起的现行写法 `[[allowlists]]`。
- 新增 **`.gitattributes`**：`* text=auto eol=lf`，统一行尾为 LF。
- `CLAUDE.md` 排除于 `trailing-whitespace` / `end-of-file-fixer` / `mixed-line-ending` 三个「就地改写」型钩子。

**动机与如实记录的问题**：

1. **门禁此前完全未生效**。实测：`uv`、`gitleaks`、`pre-commit` 均不在 PATH，`.git/hooks/` 无任何非 sample 钩子——即 `.pre-commit-config.yaml` 自阶段一起就是**纸面配置**，一次都未执行过。本次补齐工具链并 `pre-commit install`。
2. **`.env.example` 中曾误粘真实密钥**（人工发现并撤回，**未进入任何提交**）。据此补密钥扫描钩子。全历史扫描（`gitleaks git --log-opts=--all`，2 commits / 552 KB）实测 **no leaks found**。
3. **`CLAUDE.md` 是全仓唯一只读文件**（`-r--r--r--`），而 `end-of-file-fixer` 会无条件以 `rb+` 打开每个文件，导致**每次提交必然失败**——长期红着的门禁会把人训练成加 `--no-verify` 绕过，恰是 CLAUDE.md 12.3 / NO-06 明令禁止的。经确认后选择**保留只读保护、显式排除该文件**。
4. **行尾配置张力**：原仓库只有 `core.autocrlf=true` 且无 `.gitattributes`，检出为 CRLF，与钩子 `mixed-line-ending --fix=lf` 互相拉扯，每次 git 操作都打 `LF will be replaced by CRLF` 警告。已由 `.gitattributes` 定死为 LF。`git add --renormalize .` 实测**未改变任何已提交内容**（存储格式本就是 LF）。附带清除了 22 个纯行尾 stat 噪声造成的「幽灵改动」。
5. **误报处理**：`.venv` 内 numpy 测试向量与 protobuf 序列化描述符命中 `generic-api-key` 熵规则（7 处，全部误报，实测 0 处真实密钥）。已负向对照验证：豁免后仓库自有文件仍能检出植入的高熵假密钥，未过度放宽。

**新增外部前置条件**（非 Python 依赖，故不入 `pyproject.toml` / `uv.lock`）：
`uv`、`gitleaks ≥ 8.19`（`git --pre-commit` 子命令下限，本仓库实测 8.30.1）、`pre-commit` 需在 PATH 上。gitleaks 为 **MIT** 许可（CLAUDE.md 10.6 无需法务确认）。

**影响面**：仅本地与 CI 门禁，不改变运行时行为、对外契约与 `.env` 变量。门禁链现为 `gitleaks → ruff format --check → ruff check → mypy → 文件检查`，`pre-commit run --all-files` 12/12 通过。

### 未发布 · 阶段四（API 与 SSE，`feat(api)`）

**变更**：

- 新增 `main.py`（`create_app` 工厂 + lifespan）与 `api/`：`container.py`（依赖容器与生产装配）、`deps.py`（Bearer 鉴权、租户上下文、thread_id 归属校验）、`sse.py`（LangGraph 事件 → SSE 帧）、`state_view.py`（终态字段翻译层）、`idempotency.py`、`middleware/{request_context,errors}.py`、`routes/{health,threads,runs}.py`、`schemas/{health,threads,runs,events}.py`；`utils/{text,time}.py`。
- 路由：`/healthz`、`/readyz`、`/api/v1/version`（探针不带版本前缀，CLAUDE.md 11.6）；`POST|GET /api/v1/threads`、`POST /threads/{id}/runs`、`POST /threads/{id}/runs/stream`、`GET /threads/{id}/state`、`POST /threads/{id}/history`（语义对齐 LangGraph Server，CLAUDE.md 8.1）。
- 统一错误响应走 RFC 9457 Problem Details（`application/problem+json`），`request_id` 同时进响应头、响应体与**端点内部**的日志上下文（纯 ASGI 中间件，非 `BaseHTTPMiddleware`——后者会把下游扔进独立 anyio task，`contextvars` 跨不过去）。
- **SSE 契约**（CLAUDE.md 8.4）：`run.started → node.entered → retrieval.evidence → node.exited → node.entered → message.delta* → node.exited → run.completed`，失败走 `run.error`；每帧携带 `run_id/thread_id/request_id/seq`（`seq` 从 1 起单调）；心跳由 `core/streaming.heartbeat_frame()` 按 `SSE_HEARTBEAT_S` 发出，`EventSourceResponse` 自带 ping 显式关闭（`ping=0`），避免平台出现两种心跳格式。
- **幂等**（CLAUDE.md 8.3）：`Idempotency-Key` 命中时 `POST /runs` 直接返回缓存结果**不重跑图**；流式路径做**确定性重放**（`run.started` + 缓存终态帧，共 2 帧）。命名空间取 `(tenant_id, "idempotency")`，租户在**第一段**——`BaseStore` 的 namespace 本身即隔离边界，压平成字符串等于把跨租户隔离降级成「key 里恰好没有冒号」。
- **新增环境变量**：`READYZ_TIMEOUT_S`（默认 3，`/readyz` 单依赖探测超时）、`MAX_QUESTION_CHARS`（默认 2000，超限在 HTTP 边界 422 且**不触发检索**）。
- **ASGI 入口改为工厂形态**：`uvicorn --factory agent_platform.main:create_app`。模块级不设 `app` 实例——那会在 import 期读配置，缺失必填环境变量变成 `ImportError`（CLAUDE.md 4.8）。已有测试锁定该约束。

**动机**：阶段四为 CLAUDE.md 第 8 章（接口风格）与 4.7（流式）的落地；把「契约」写成可执行断言而非文档承诺。

**实测发现并已修复的产品缺陷**：

1. **非本层异常会让客户端拿到断连而非错误说明**。响应头发出后若逃逸的是非 `AgentPlatformError` 异常，Starlette 会抛 `Caught handled exception, but response already started.`，客户端只看到连接被掐断。`sse.py` 现捕获后记 `logger.exception` 并产出 `run.error`（`INTERNAL_ERROR`）；异常文本不出境（可能含拓扑与输入原文，NO-07 / 8.2）。收窄为 `Exception` 而非裸 `except`：`CancelledError` 继承 `BaseException`，客户端断开触发的取消必须照常向上传播。
2. **404 的 `detail` 误导调用方重试**。`AgentPlatformError` 基类默认 `detail` 是「服务暂时不可用，请稍后重试。」，而「智能体未在本进程装配」是**永久性**错误——重试一千次也不会变。`runs.py` 现显式给出 detail。
3. **默认测试夹具让所有「正常路径」用例悄悄跑在兜底分支上**。`generate_answer` 在输出边界做**确定性**引用校验，无 `[1]` 的文本被判 `citation_missing` 并整体替换。修 `conftest` 默认回答为带引用文本。

**门禁与验证**（本阶段收尾时实测）：`ruff format --check` 87 文件已格式化 → `ruff check` 全通过 → `mypy` 87 文件 0 问题 → `pytest tests/unit` **403 passed** → `core/` + `agents/` + `api/` 合并行覆盖率 **95.65%**（门槛 80%）。SSE 帧格式、`seq` 单调、事件序列、双 `data:` 前缀反例、心跳保活、幂等重放边界均有单测锁定；`api/container.py`（真实 Postgres/Milvus 装配）当前 78%，由阶段五的集成测试覆盖，**不以 fake 填充虚高**。

**已知缺口（如实记录，未掩饰）**：

1. **幂等条目无界增长**。`BaseStore` 的 TTL 仅在构造 store 时提供 `index` 配置才生效，当前 `open_checkpoint_resources` 未提供。演示期数据量可接受；登记的处置方式是在基础设施阶段给 store 加索引/TTL 配置，**不在 API 层偷偷加定时清理**——那会引入不受应用生命周期管理的后台任务（NO-05）。
2. **LangGraph 检查点反序列化白名单未收窄**。运行时告警：`Deserializing unregistered type ... from checkpoint. This will be blocked in a future version.`（涉及 `EvidenceItem`、`RetrievalMeta`、`AnswerStatus`、`Citation`）。当前 `JsonPlusSerializer` 处于宽松模式（`allowed_msgpack_modules=True`），未来版本将改为默认拒绝——**升级 langgraph 会直接打断历史 checkpoint 的恢复**。这是安全相关项（能写检查点库的攻击者可借反序列化执行代码），处置方式是用 `JsonPlusSerializer(with_msgpack_allowlist(...))` 显式登记自有类型，与「历史 checkpoint 可恢复」的升级验证（CLAUDE.md 9.3 / 4.4）合并处理。
3. **追踪钩子仍未接线**。`core/tracing.build_tracing_hook` 已就绪，但 `build_graph` 未开放该注入点；与 Langfuse/OTel 一并顺延至基础设施阶段。

**影响面**：新增对外 HTTP/SSE 契约（既定接口首次发布，尚无外部集成方）；新增 2 个环境变量（已同步 `.env.example`）；启动命令由 `agent_platform.main:app` 改为 `--factory agent_platform.main:create_app`。`CLAUDE.md` 本体尚未改动——按第 14 章，规则版本升级（v1.1.1 → v1.2.0）与基线表同步统一在阶段六提交（NO-20 的记账留痕见本条）。

---

## [v1.1.1] — 2026-10-09 — 规范基线（首版）

| 项 | 内容 |
|---|---|
| **版本** | v1.1.1（`CLAUDE.md` 文件头版本号为准） |
| **日期** | 2026-10-09 |
| **变更摘要** | `CLAUDE.md` 作为仓库**唯一事实来源**首次发布：技术栈锁定（LangGraph 1.x 编排主干、LiteLLM 网关、PostgreSQL/Redis、Milvus、RAGFlow、FastAPI、Pydantic v2、SSE、uv workspace 单体仓库）；标准目录结构与依赖方向约定；LangGraph 开发规范（State/Node/Edge/Checkpoint/HITL/子图/流式/装配）；模型、工具与 MCP、知识检索、API 风格四章专项规范；可观测性/评估/测试门禁链；安全与合规（含许可证红线）；部署与环境；Git 工作流与 AI 协作规则；**20 条 Hard No（NO-01 ~ NO-20）**；规范演进流程与 ADR 制度。 |
| **动机** | 绿场仓库需要一份**可执行、可被 AI 编程助手自动加载**的统一工程规则，避免"临时口头约定、网络教程、AI 即兴建议"主导架构决策；同时把企业级交付所需的私有化、多租户、合规与许可证约束前置为强制条款。 |
| **影响面** | 全仓库（人类工程师 + AI 编程助手）即时生效；所有后续提交的门禁以第 9 / 12 章为准；`docs/adr/` 与本源文件成为架构决策的留痕处。 |
| **关联** | `CLAUDE.md` v1.1.1（本仓库首份规则文件）；ADR-0001 在本版中被引用（文档于 2026-10-15 补录） |

---

## 约定

- 本文件**只追加**，不删改历史条目（历史记录即审计证据）；
- 规范变更提交格式为 `docs(standards): ...`，ADR 落地为 `chore(adr): ...`（CLAUDE.md 12.2）；
- 新增依赖、新目录、新环境变量、新跨模块约定，或既有约定被改变时，**必须**在提交规则变更的同一 PR 中同时更新 `CLAUDE.md`、本文件与必要的 ADR（NO-20）。
