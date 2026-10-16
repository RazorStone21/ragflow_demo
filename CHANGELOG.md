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
- **已知偏离（须如实记录）**：LiteLLM 镜像 tag 与 CLAUDE.md 表格标注的基线（v1.104+）**编号体系不一致**，实际以镜像 tag 为准（本仓落地值 **`main-v1.82.3`**，最初选用的 `main-v1.34.4` 经实测完全不可用，见「真实环境联调」条）；偏离原因与后续处理在此说明。
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

### 未发布 · 阶段五（基础设施与入库，`feat(knowledge)` / `build` / `test`）

**变更**：

- **基础设施**：新增 `docker-compose.yml`（postgres:16-alpine / redis:7-alpine / Milvus standalone 三件套（etcd + `milvusdb/minio` + milvus v2.6.24）/ LiteLLM Proxy；共享网络 `agent-platform-net`，卷统一落 `local_data/`）、`deploy/docker/litellm/config.yaml`（逻辑名 → 型号映射，密钥一律 `os.environ/` 注入）、`deploy/docker/milvus/milvus.yaml`（16GB 单机的内存反压水位覆写；挂为 `user.yaml` 而非 `milvus.yaml`，后者会整份替换默认配置）、`deploy/k8s/README.md`（占位）。
- **脚本**：新增 `scripts/ingest.py`（RAGFlow 解析切分 → 嵌入 → Milvus 索引，带 `--dry-run`/`--subset`/`--tenant`/`--dataset` 与稳定退出码 0/1/2/3）、`scripts/demo_chat.py`（SSE 客户端冒烟，解析 CLAUDE.md 8.4 事件类型并打印带引用的回答）、`scripts/energy_subset.txt`（首批能源子集白名单）。
- **测试**：新增 `tests/unit/{test_ingest,test_demo_chat}.py`（+49 例）与 `tests/integration/`（`conftest.py`、`_support.py`、`test_sse_run_stream.py`、`test_milvus_integration.py`、`test_litellm_proxy.py`、`test_ragflow_integration.py`、`test_ingest_end_to_end.py`，共 27 例）；新增 `tests/fixtures/sample_energy_doc.txt`（合成政务政策文本，NO-17：不含任何客户数据）。
- **文档**：新增 `docs/runbooks/ragflow_setup.md`（RAGFlow v0.27.2 在仓库外部署、覆写清单、端口冲突检查、SiliconFlow 模型 ID 与维度核实、入库/问答分离的内存安排）与 `docs/runbooks/local_env_setup.md`（WSL2 + Docker Desktop + uv 的一次性准备与验证关卡）。
- **配置与工具链**：`pyproject.toml` 的 mypy `files` 纳入 `scripts`；pytest 增加 `pythonpath = ["."]`；新增 `[tool.ruff.lint.isort] known-first-party = ["scripts", "tests"]`（两者都在 `src/` 之外，ruff 的 src 根推导会把它们误判为第三方，导致 import 分组读出错误的依赖方向）。新增 `enable_utf8_console()`（`core/logging.py`）：Windows 默认 GBK 代码页会把中文日志与 `--help` 输出变成乱码。
- **新增环境变量**（已同步 `.env.example`，NO-20）：`INGEST_MANIFEST_PATH`（幂等清单路径）。**修正**：`.env.example` 此前只列 `settings.py` 的字段，而 `docker compose up -d` 还需要 `POSTGRES_PASSWORD` / `LITELLM_SALT_KEY` / `DEEPSEEK_API_KEY` / `SILICONFLOW_API_KEY` 等 compose 侧变量（compose 对前几个标了 `:?`，缺失即拒绝启动），现已补成独立小节并说明「settings 读取」与「compose 读取」的分界。

**幂等契约（ADR-0002 的落地形态）**：键 = **源文件相对路径 + 文件内容 sha256**。指纹未变 → 整文件跳过，零网络零写操作；指纹变了 → 先按旧 `document_id` 清理 Milvus 与 RAGFlow 中的旧产物，再重新上传解析。清理不可省：Milvus 主键沿用 RAGFlow 的 `chunk_id`，内容一改 `chunk_id` 全变，旧行不会被 upsert 覆盖，只会变成永不命中的孤儿行。清单逐文件落盘（原子替换），进程中断后重跑只补未完成的文件。

**动机**：CLAUDE.md 第 7 章（知识与 RAG）与 11.1 的落地；把「入库幂等」从文档承诺变成可执行断言——集成测试 `test_ingest_end_to_end.py` 用真容器验证「重跑不增条数」「内容变更后旧 chunk 一条不剩」。

**实测发现并已修复的缺陷**：

1. **README 的启动命令不可能成功**：写的是 `uvicorn agent_platform.main:app`，而阶段四刻意采用工厂形态（模块级无 `app`，否则 import 期就读配置）。改为 `--factory agent_platform.main:create_app`，并补上缺失的 `docker compose ps` 等待步骤。
2. **`.env.example` 无法让 compose 起来**（见上方「新增环境变量」）。
3. **`.env.example` 对 `FALLBACK_CHAT_MODEL` 的注释误导**：原文称「留空则仅由网关层 fallback 兜底」，而网关侧根本未配 `router_settings.fallbacks`（只有一家对话模型密钥）。注释已改为直述当前为空及其原因。
4. **ruff 把 `scripts`/`tests` 当第三方**，import 分组与实际依赖方向不符（详见上方配置项）。
5. **`tests/` 无 `__init__.py` 导致 conftest 双重导入**：`from tests.integration.conftest import ...` 会得到第二个模块对象（同名文件两种导入路径），函数恰好能用而夹具会分叉。共享工具移入 `tests/integration/_support.py`，导入路径唯一。
6. **conftest 里的 `pytestmark` 被 pytest 静默忽略**（既不生效也不报错）。改为各测试模块自行声明 `pytest.mark.integration`。
7. **集成测试门控做成两层**：`RUN_INTEGRATION_TESTS` 开关 **且** 逐服务可达性探测。二者混成同一个红灯会训练出「看到集成测试失败就重跑」的习惯，等于没有门禁。Milvus 的探测刻意用裸客户端的 `has_collection` 而非 HTTP：19530 是 gRPC 端口，用 HTTP 探它在服务健康时同样失败。
8. **上游 Milvus compose 引用的 MinIO 镜像已从 Docker Hub 消失**（首次 `docker compose pull` 实测暴露）：官方 v2.6.24 的 standalone compose 写的是 `minio/minio:RELEASE.2024-05-28T17-19-04Z`，而 Docker Hub 上 `minio/minio` 仓库已被移除（`pull access denied ... repository does not exist`，Hub API 亦返回 `object not found`），**上游这份 compose 对全新安装是坏的**。上游 master 已改用 Milvus 自建的 `milvusdb/minio:RELEASE.2024-12-18T13-15-44Z`（实测可拉取，digest `sha256:1dce27c4…`），本仓随之改用该镜像并在 compose 内写明原因 —— 这是**第一处非上游 v2.6.24 文件的字面取值**（另两处为有意的 milvus 版本选择与卷路径）。

**如实记录的版本事实**：

1. **Milvus 镜像 tag 偏离上游文件**：官方 `v2.6.24` git tag 下的 standalone compose 内 pin 的仍是 `milvusdb/milvus:v2.6.23`（上游未同步），而 `v2.6.24` 镜像确实存在。本仓按 CHANGELOG 已登记的 v2.6.24 落地，属**有意偏离**上游文件字面内容。
2. **LiteLLM 镜像 tag 编号体系与 PyPI 不一致**：PyPI 已发布 v1.105.x，而 GHCR `berriai/litellm` 并无 `main-v1.105.1`（实测 manifest 404）。本仓最终 pin **`main-v1.82.3`**（详见下一条「真实环境联调」中对最初 `main-v1.34.4` 为何不可用的实测记录）。CLAUDE.md 第 1 章标注的「v1.104+」无法由镜像 tag 字面满足，须以镜像 tag 为权威落地值（阶段六更新基线表时处理）。
3. **RAGFlow 不能使用外部 Milvus**（上游 issue #7749 明确拒绝，`DOC_ENGINE` 仅支持 elasticsearch/infinity 等），这正是双后端分工（ADR-0002）的由来。

**门禁与验证**（收尾时实测）：`ruff format --check` 99 文件已格式化 → `ruff check` 全通过 → `mypy` 99 文件 0 问题 → `pytest tests/unit` **452 passed** → 全仓行覆盖率 **93.13%**（门槛 80%；`core/`、`agents/`、`api/`、`knowledge/` 均高于门槛）→ `pytest -m integration` 在**真实基础设施**上 **13 passed / 14 skipped / 0 failed**（下一条「真实环境联调」记录该结果的来由；环境未就绪时为全量 27 skipped）→ `docker compose config` 退出码 0（真实变量替换与端口解析）。

**已知缺口（如实记录，未掩饰）**：

1. **模型 fallback 链缺失（CLAUDE.md 5.4）**：网关侧无 `router_settings.fallbacks`，应用侧 `FALLBACK_CHAT_MODEL` 为空——只有 DeepSeek 一家对话模型密钥，补了第二家才谈得上 fallback。
2. **SiliconFlow 模型 ID 与 `hosted_vllm` 重排路由未经运行时验证**：两者都是读 LiteLLM/SiliconFlow 源码得出的推断（依据写在 `deploy/docker/litellm/config.yaml` 的注释里）。`tests/integration/test_litellm_proxy.py` 已针对这两条写好断言，**等 compose 起来跑一次才算证据**；runbook 中给了手工核实命令。（**2026-10-16 更新**：真实环境已跑通一次，但用的是占位密钥，这几条断言只走到"服务可达"层——仍未成为证据；详见「真实环境联调」条的已知缺口 1。）
3. **端到端验证待环境就绪**：`docker compose up -d` 全部健康、真实入库与幂等重跑、`demo_chat` 带引用回答、无关问题降级 —— 这些必须在真实基础设施上跑，不以 fake 填充（CLAUDE.md 0.8 / 12.4）。（**2026-10-16 更新**：本机 Docker 已修复可用（WSL2 问题，步骤见 `docs/runbooks/local_env_setup.md`），真 Postgres / 真 Milvus 的集成测试已能执行；**真实密钥仍未填入 `.env`**，故上述端到端链路仍未执行——见「真实环境联调」条的已知缺口。）
4. **`deploy/k8s/` 与 `tests/evals/` 仍为占位**（CLAUDE.md 11 / 9.2 的实现在阶段二）。

**影响面**：新增 1 个环境变量、1 个 compose 编排、3 个脚本与 2 个文档目录；`.env.example` 新增 compose 侧变量小节（照抄旧模板会让 `docker compose up -d` 失败，属**行为变更**）；README 启动命令修正。`CLAUDE.md` 本体仍未改动 —— 规则版本升级（v1.1.1 → v1.2.0）与基线表同步统一在阶段六提交。

### 未发布 · 阶段五（真实环境联调，`fix` / `docs`）

**背景**：本机 Docker（WSL2 修复后）首次可用，阶段四/五的集成测试**第一次真正执行**。
此前「门禁全绿」是**假象**——所有集成测试都在 skip（无 Docker），真正跑的只有单测（内存
checkpointer + fake 模型/检索器）。本节记录的缺陷**全部是这次真跑才暴露的**，其中多处
在此前的离线验证里不可能被发现。这也印证了阶段四遗留项当初的判断：写永远 skip 的用例
等于交付未验证代码。

**运行时缺陷（已修复）**：

1. **LiteLLM 镜像 `main-v1.34.4` 完全不可用，网关崩溃重启循环**。实测容器内
   `litellm==1.34.4` 的 `get_llm_provider` 既不认 `deepseek/` 也不认 `hosted_vllm/`
   （两者都抛 `BadRequestError: LLM Provider NOT provided`），而本配置三个逻辑名**全部**
   依赖这两个前缀 → 容器 `Restarting (1)`，端口从未真正服务过。升到 **`main-v1.82.3`**
   （内含 `litellm==1.82.3`）后三个前缀实测全部解析成功。**教训**：镜像 tag 只能证明"拉得到"，
   证明不了"跑得起来"——`docker compose pull` 成功与 `docker compose up` 之后稳定服务是两回事。
   注：LiteLLM 定位为**演示期网关**，上线前应评估自研薄网关或换用已验证的内部网关。
2. **宿主系统代理劫持内网请求（3 处，全部修复）**。httpx 默认 `trust_env=True`，在 Windows 上
   经 `urllib.request.getproxies()` 读**注册表**里的 WinINET 代理（**不看** `HTTP_PROXY` 环境变量）。
   实测本机开着 Clash 时，连 `127.0.0.1:9380`（**根本没有进程监听**）都能拿到 `502`，而同一请求
   在 `trust_env=False` 下如实返回连接失败。三处均改为显式构造无代理客户端
   （`git show HEAD:<file>` 核对：三个文件在 `HEAD` 中**一处都没有** `trust_env`，
   属整条链路集体遗漏，不是哪一处回退）：
   - `knowledge/gateway_client.py`、`knowledge/ragflow_client.py`（网关与 RAGFlow 客户端）；
   - **`core/models.py`：`ChatOpenAI` 此前未传 `http_async_client`，即对话主链路仍走宿主代理**
     ——这是本次最要紧的一处，提示词与回答会路过第三方代理进程，与 10.1 / NO-15 的数据不出域相悖，
     且"能否连通内网网关"取决于本机有没有开代理。现由 `build_http_async_client()` 统一构造并注入。
3. **Milvus 有界一致性导致"刚入库却检索不到"**。实测 standalone v2.6.24：`upsert` 返回后立即
   `count()` 得 **0**，约 1 秒后才为真值。对"入库后紧接着提问"的流程，这直接触发 NO-12 的无证据
   降级分支——用户看到"没找到依据"。新增 `MilvusStore.flush()`，由 `scripts/ingest.py` 在**批次末尾**
   调用一次（刻意不放进 `upsert_chunks`：按批 flush 会封存大量小段，抵消批量写入的意义）。
4. **Windows 上 psycopg 异步直接拒绝工作**。报
   `InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode`。根因是
   uvicorn 0.54 通过 `loop_factory` **直接构造**事件循环，`asyncio.set_event_loop_policy()`
   **会被完全绕过**（表现为"设了 policy 却没生效"）。解法：`--loop asyncio:SelectorEventLoop`
   （uvicorn 对未命中五个已知名字的 `--loop` 值按任意导入串处理），pytest 侧用
   `pytest_asyncio_loop_factories` hook。已写入 README 与 runbook。
5. **`POSTGRES_DSN` 写 `localhost` 会无限期挂起**。本机 `localhost` 解析为 `['::1', '127.0.0.1']`，
   libpq 先试 IPv6，而 Docker Desktop 只把 Postgres 发布在 IPv4 上 → 启动阶段无任何输出地卡死。
   实测 `localhost` **10.1s**、`127.0.0.1` **0.0s**（同一 `saver.setup()`）。
   两处处置：文档与 `.env.example` 改推荐 `127.0.0.1`；`core/checkpoint.py` 新增
   `apply_connect_timeout()` 兜住最坏情况。**如实说明**：`connect_timeout` 按**每个地址**计算，
   它是把"无限期挂起"变成"有界等待"，**并不能让 `localhost` 报错**——真正消除停顿的是改写主机名。
   另：用 `setdefault` 而非 `make_conninfo(dsn, connect_timeout=...)`，实测后者会**覆盖**连接串里
   已有的同名参数，使运维的显式设置被静默吃掉。

**测试缺陷（已修复，均为阶段四/五写到一半、从未真正执行过的用例）**：

1. **`answer_status` 断言的枚举值 `"grounded"` 在 `src/` 中根本不存在**（3 处）。实际取值是
   `AnswerStatus.ANSWERED = "answered"`；改为引用 `state.py` 的 `STATUS_*` 单一来源，与单测一致。
2. **`retryable` 断言与实现矛盾**。集成测试断言 `is True`，而 `RetrievalFailedError` 继承
   `KnowledgeError` 的默认 `retryable=False`。**判定依据**：同一异常的兄弟类
   `KnowledgeParseTimeoutError` 显式置 `True`、`ModelAllUnavailableError` 同为 503 却保持 `False`
   ——可见本仓的约定是**按"瞬时性"逐类显式声明，而非从 `http_status` 推导**。且该异常也被用于
   维度不符这类永久性配置错误（`milvus_store.hybrid_search`），叫客户端重试只会白耗配额。
   故改测试而非改实现；断言处写明了这条推理。
3. **`except httpx.HTTPStatusError` 是永不执行的死分支**（`test_litellm_proxy.py`）。
   `ChatOpenAI` 把传输层错误包装成 `openai.*` 系列（实测抛 `OpenAIConnectionError` /
   `OpenAIAPIError`），从不抛 `httpx.HTTPStatusError`。一个看起来友好、实际不可能触发的
   "请检查 DEEPSEEK_API_KEY" 提示比没有提示更误导人，已删除并让真实异常原样暴露。
4. **同一份集成用例在服务未起时语义不一致**：`test_primary_chat_answers_via_gateway` 未依赖
   可达性夹具，网关没起时是 fail，而同文件其余 4 条是 skip。已改为经 `usefixtures("gateway")`
   挂上同一道门禁。
5. **`_support.http_ready()` 的探测结果不可靠**，同一份代码两次运行给出**不同的 skip/fail 组合**。
   根因即上述第 2 条：探测走宿主代理、而真实客户端不走，**答的不是同一个问题**——探测可以对一个
   根本没在监听的端口报"服务在"。已统一 `trust_env=False`，实测两轮结果完全一致。
6. **`FakeMilvusStore` 缺 `flush()`**，新增的收尾 flush 一上线就让 6 个单测 `AttributeError`。
   替身补上该方法（并加断言锁定"批次末尾只 flush 一次"），否则"忘了 flush"这类缺陷会悄悄绕过单测。

**多租户遗留项（登记，未修复）**：Milvus 主键是**裸 `chunk_id`**，`tenant_id` 仅作过滤列。
这意味着若两个租户共享同一份 RAGFlow 数据集（相同 `chunk_id`），后写者会**覆盖**前者的行，
租户过滤挡不住这种覆盖。当前演示形态（单租户单数据集）不触发，但它是 NO-19 的一个真实缺口，
须在阶段六以 ADR 记明并给出处置（如主键改为 `tenant_id:chunk_id` 复合键，代价是重建集合）。
另注：同一批 upsert 内出现重复主键时 Milvus **直接拒收**（`code=1100`）而非"后写覆盖前写"，
这个差异只有真库会报——集成测试夹具即因此踩过一次。

**门禁与验证**（本次收尾实测）：`ruff format --check` 99 文件已格式化 → `ruff check` 全通过 →
`mypy` 99 文件 0 问题 → `pytest tests/unit` **452 passed** → 全仓行覆盖率 **93.13%** →
`pytest -m integration`（真 Postgres + 真 Milvus）**13 passed / 14 skipped / 0 failed**，且
**连续两轮结果完全一致**（此前会漂移）。其中 13 条通过的是 `test_sse_run_stream.py`（真
Postgres 检查点 + SSE 全序列）与 `test_milvus_integration.py`（真 Milvus 增删查 + 租户隔离），
两者均不依赖外部密钥；14 条跳过的是需要真实 DEEPSEEK/SILICONFLOW 密钥或 RAGFlow 容器的用例，
按门控 skip 并打印启用步骤。

**已知缺口（如实记录，未掩饰）**：

1. **SiliconFlow 模型 ID 与 `hosted_vllm` 重排路由仍未验证**。本次联调用的是**一次性弱口令
   冒烟栈**（不触碰仓库 `local_data/` 与用户 `.env`），密钥为占位值，网关对上游必然 502，
   因此这几条断言只走到了"服务可达性"这一层。要变成证据，仍需用户填好真实密钥后跑一次
   `pytest -m integration` 并按 runbook 手工核实。
2. **RAGFlow 未部署**，`test_ragflow_integration.py` 与 `test_ingest_end_to_end.py` 共 9 条
   全部 skip。RAGFlow 需按其官方 compose 在**仓库外**独立部署（见 `docs/runbooks/ragflow_setup.md`）。
3. **真实入库与端到端问答仍未执行**（前置同上）。

**影响面**：新增 1 个环境变量 `POSTGRES_CONNECT_TIMEOUT_S`（默认 10，已同步 `.env.example`）；
`.env.example` 的 `POSTGRES_DSN` 改为 `127.0.0.1` 并写明原因（照旧写 `localhost` 会启动卡死，
属**行为变更**）；LiteLLM 镜像 tag 由 `main-v1.34.4` 改为 `main-v1.82.3`；README 启动命令补
Windows 专属 `--loop` 参数说明。`CLAUDE.md` 本体仍未改动——规则版本升级与基线表同步统一在阶段六。

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
