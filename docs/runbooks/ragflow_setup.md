# RAGFlow 部署手册（文档工程后端）

> 适用版本：RAGFlow **v0.27.2**（镜像 `infiniflow/ragflow:v0.27.2`，CPU）
> 定位：本平台双后端分工中的**文档工程**角色（解析 / 切分 / 文档管理），
> 见 [ADR-0002](../adr/0002-ragflow-milvus-division-of-labor.md)。
> **检索不在这里**：向量与检索统一走 Milvus 主链路。RAGFlow 的画布与内置检索
> 不得承载核心流程（CLAUDE.md NO-01）。

## 为什么不把 RAGFlow 的 compose 放进本仓库

上游 `docker/` 目录约 15 个文件（近 500 行的 `.env`、12KB 的 base compose、
模板、`nginx/`、多个 entrypoint）。CLAUDE.md 10.6 特别点名 RAGFlow 的 Apache-2.0
**带附加条款**，把上游文件落进本仓等于把再分发的解释权交给运气；同时每次升级
都要人工 diff 一遍上游改动。因此本仓库只维护**覆写清单**，文件本体留在仓库外。

## 前置

- Docker Desktop（WSL2 后端）可用：`docker run --rm hello-world` 成功
  （装法见 [local_env_setup.md](local_env_setup.md)）
- 本机可访问 GitHub 的代理（实测本机直连不通）：`HTTPS_PROXY=http://127.0.0.1:7890`
- 内存：RAGFlow 官方最低 16GB，本机正好 16GB —— 见文末「内存安排」

## 步骤 1 · 克隆到仓库外

克隆路径**必须在本仓库之外**，否则会污染 `git status` 并可能被误提交（NO-17）。

```bash
cd /e/Codes                     # 仓库的上一级
HTTPS_PROXY=http://127.0.0.1:7890 git clone --depth 1 --branch v0.27.2 \
  https://github.com/infiniflow/ragflow.git ragflow-0.27.2
cd ragflow-0.27.2/docker
```

## 步骤 2 · 覆写 .env

先确认上游版本里这些键**确实存在**（上游改过键名，缺键就是信号，别硬加）：

```bash
grep -nE '^(DOC_ENGINE|DEVICE|HF_ENDPOINT|SVR_HTTP_PORT|RAGFLOW_IMAGE)=' .env
```

> **实测结论（v0.27.2，2026-10-16）**——先查再改的价值就在这里：
>
> - **`HF_ENDPOINT` 在 v0.27.2 的 `.env` 里不存在**（只有一行注释掉的 `# HF_ENDPOINT=...`）。
>   官方镜像已在 `Dockerfile:296` 写入 `ENV HF_ENDPOINT=https://hf-mirror.com`，即**镜像默认就走
>   镜像站**。所以**不要**硬加这一项（缺键就是信号）；启动后按「步骤 3」在容器里复核即可。
> - `DOC_ENGINE`/`DEVICE` 是 `${DOC_ENGINE:-elasticsearch}` 这种 compose 插值写法，改的是整个表达式；
>   `SVR_HTTP_PORT=9380`、`RAGFLOW_IMAGE=infiniflow/ragflow:v0.27.2` **本来就是我们要的值**，
>   不必动 —— 改了只会平白给未来的 diff 添噪声。
>
> 因此**真正需要覆写的只有 3 行**：

| 键 | 上游原值 | 覆写为 | 理由 |
|---|---|---|---|
| `DOC_ENGINE` | `${DOC_ENGINE:-elasticsearch}` | `infinity` | 用内置 Infinity 而非 Elasticsearch：**不起 ES 容器**，省下约 4GB 内存与一次 JVM 预热。注意 `.env:38` 的 `COMPOSE_PROFILES=${DOC_ENGINE},${DEVICE},metadata-${METADATA_DB_PROFILE}` 用它选 profile —— 改这一个键就等于把服务集从 `es01` 切到 `infinity` |
| `MINIO_PORT` | `9000` | `9002` | 与本平台 Milvus 的 MinIO 撞宿主端口 |
| `MINIO_CONSOLE_PORT` | `9001` | `9003` | 同上。RAGFlow 自身经 docker 网络内的 `minio:9000` 访问它，宿主端口号改成多少都不影响功能 |
| `INFINITY_PSQL_PORT` | `5432` | `5433` | **实测起容器才炸出来的第三个冲突**：Infinity 也讲 PostgreSQL 线协议、默认就抢 5432，而本平台 Postgres 正是 5432（报错 `Bind for 0.0.0.0:5432 failed: port is already allocated`）。这三个端口不是一次看出来的——`grep` 只覆盖了你想到的键，剩下的靠"起服务时它响亮地失败" |

```bash
cp -n .env .env.orig        # 备份便于回退（-n：已有备份时不覆盖）
# 然后逐行改这三个键 —— 用编辑器，不要用 shell 的 sed，原因见下
```

**工具注意（本机实测，会浪费半小时的坑）**：在本仓库环境下，Bash 命令里**只要出现 `.env`
字面量就会被仓库的 deny 规则整条拦下**（规则把参数按**仓库根目录**解析，`cd` 到别处也照样拦；
`sed -i … .env` 一起被拦，且是**整条命令不执行**，不是部分执行）。改用编辑器工具打开
**绝对路径** `E:\Codes\ragflow-0.27.2\docker\.env` 逐行修改。

### 端口冲突检查（重要）

本平台的 `docker-compose.yml` 已经占用：**5432**（Postgres）、**6380**（Redis）、
**9000/9001**（Milvus 的 MinIO）、**19530/9091**（Milvus）、**4000**（LiteLLM）。
其中 Redis 刻意错开到 6380 就是为了避开 RAGFlow 的 6379。

【已核实（v0.27.2）】base compose 里 `minio` 与 `redis` **不带 profile（必起）**，且都发布宿主端口：
`${MINIO_PORT}:9000`、`${MINIO_CONSOLE_PORT}:9001`、`${REDIS_PORT}:6379`（另有 `mysql:3306`，
由 `metadata-mysql` profile 带起）。与本平台的唯一冲突是 **MinIO 的 9000/9001**（本仓已占，
见上表覆写）；redis 上游默认 6379、本仓刻意用的是 6380，不冲突。

**查端口别用 netstat / `Get-NetTCPConnection`**：本机实测 Docker Desktop 发布的端口
**不出现在宿主 socket 列表里**（9000/4000/5432/6380 全查不到，尽管 `docker compose ps`
显示已发布），拿它当判据只会得出"全空闲"的错误结论。可信的判据有两个：

```bash
# 1) 容器实际发布了什么（权威）
docker compose ps --format "{{.Service}} -> {{.Ports}}"
# 2) 直接起服务 —— 真冲突时 compose 立刻报 "port is already allocated"，失败是响亮且即时的
```

端口真冲突时的处置：改的是 **RAGFlow 侧**的 `MINIO_PORT`/`MINIO_CONSOLE_PORT`/`REDIS_PORT`
（RAGFlow 自己经 docker 网络内的服务名访问这些依赖，宿主端口号无所谓），**不要**动本仓
Milvus 侧的端口 —— `MILVUS_URI` 与 Milvus 的 MinIO 端点都在本仓配置里互相引用。

## 步骤 3 · 启动与等待就绪

**起之前先校验配置**（`--services` 只输出服务名、不打印 `.env` 里的口令，可放心跑）：

```bash
docker compose config --services | sort
# 期望正好这 5 个：infinity / minio / mysql / ragflow-cpu / redis
# 出现 es01 说明 DOC_ENGINE 覆写没生效（profile 选错了引擎）
```

```bash
docker compose pull        # RAGFlow CPU 镜像数 GB，第一次要等
docker compose up -d
docker compose ps          # 等 ragflow-cpu 的 STATUS 变成 healthy
docker compose logs -f ragflow-cpu | head -50   # 首次启动要拉 DeepDoc 权重，可能数分钟
```

镜像拉取走的是 **Docker Desktop 自带的代理**，不是 shell 的 `HTTPS_PROXY`（`docker info`
里能看到 `HTTP Proxy: http.docker.internal:3128`）。它在本机是否通，取决于 **Clash 是否开着**
—— 拉不动就先确认代理。

就绪判据：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:9380/api/v1/datasets   # 401 即为"服务在且要鉴权"

# 顺带复核镜像里 HF_ENDPOINT 的实际取值（步骤 2 说"不要硬加"的依据就在这里）
docker compose exec -T ragflow-cpu printenv HF_ENDPOINT    # 期望 https://hf-mirror.com
```

**401 是成功信号**（服务活着、只是没带 Key）。连不上才是没起来。

## 步骤 4 · 生成 API Key

**这一步用浏览器，不要试图用 curl 绕过**（实测，省得再踩一次）：

- **没有预置账号**。镜像里确实有播种代码（`api/db/init_data.py` 的 `admin@ragflow.io` /
  `admin`），但只在服务端显式带 `--init-superuser` 启动时才创建，而上游 compose **没传**
  这个 flag（`docker-compose.yml` 的 `command:` 只有 `--enable-adminserver
  --init-model-init-provider-tables`），所以默认没有可用账号。
- **注册与登录都对口令做 RSA 加密**：`POST /api/v1/users`（注册）与 `POST /api/v1/auth/login`
  都要求口令是**用公钥加密后的 base64**（前端 `web/` 里加密后再发）。直接发明文会得到
  `CryptPayloadError('password payload is not valid base64')`（实测注册接口原样返回）。
  也就是说 API Key 的获取链（注册 → 登录拿会话 → `POST /api/v1/system/tokens`）**无法在
  不实现前端加密的前提下脚本化**，而实现它的收益只有"省几次点击"——不值。

1. 浏览器打开 `http://localhost`（宿主 80 端口，即 `${SVR_WEB_HTTP_PORT}` 的默认值），
   首次访问需注册一个本地账号（数据全在本机，不出域）；
2. 右上角头像 → **API** → 生成 API Key；
3. 写进本仓库 `.env` 的 `RAGFLOW_API_KEY=`（`.env` 已 gitignore，NO-17）。

> 本地演示用 **`admin` / `admin` 这类弱口令即可，但仅在"端口不对外"的前提下**——
> 控制台（80）与 API（9380）都发布在 `0.0.0.0`，别把它放到能被别人访问的网络上（CLAUDE.md 10.1）。

```bash
# 复核鉴权真的通了（把 KEY 换成你生成的）
curl -s -H "Authorization: Bearer $KEY" http://localhost:9380/api/v1/datasets | head -c 200
# 期望：{"code":0,...,"data":[...]}
```

## 步骤 5 · 配置默认嵌入模型（**跳过这步，解析必定失败**）

> **实测踩过（2026-10-16）**：5 份文档上传成功、`start_parsing` 也返回成功，最终却全部
> `run=FAIL`，`progress_msg` 里是
> `[ERROR]Fail to bind embedding model: No default embedding model is set.`。
> 根因在 RAGFlow 侧：`api/db/joint_services/tenant_model_service.py:196` 在租户没有默认
> 嵌入模型时直接抛 `No default {model_type} model is set.`，由 `rag/svr/task_executor.py`
> 包成上面那句。**上传与提交解析都不校验这一点**——它只在最晚的环节以"解析失败"的形态
> 暴露，而错误文案不提"模型没配"，极容易误判成文件坏或引擎没起来。

先看当前状态（本仓的 API Key 能读这两个端点，无需浏览器）：

```bash
curl -s -H "Authorization: Bearer $KEY" http://localhost:9380/api/v1/models/default
# 全新部署实测：{"code":0,"data":{"models":[]}}   ← 空即"没配"
```

### 为什么必须在浏览器里配

- **v1 REST 没有注册供应商的端点**：只有 `GET /api/v1/models`、`GET /api/v1/models/default`、
  `PATCH /api/v1/models/default`。`PATCH` 只能把**已存在**的模型指定为默认，不能创建。
- 注册供应商走的是另一套 `web/` 会话鉴权路由，且注册/登录对**口令做 RSA 加密**
  （见步骤 4）——脚本化的成本远高于收益。
- 管理 CLI 需要 `ragflow-cli==0.27.2` **外加一个超级用户口令**，而上游 compose 未传
  `--init-superuser`（步骤 4 已述：默认没有可用账号）。
- **不要选 `Builtin` 供应商**：其嵌入实现被 COMPOSE_PROFILE 门控 ——
  `rag/llm/embedding_model.py:224-241` 里 `BuiltinEmbed` 仅在
  `"tei-" in os.getenv("COMPOSE_PROFILES", "")` 时才初始化，而本部署的 profile 是
  `infinity,cpu,metadata-mysql`（不含 `tei-`），所以 Builtin 在我们这里**根本不可用**，
  除非另外起一个 TEI 容器。
- **不要选 `SILICONFLOW` 供应商**（RAGFlow 确实内置了它）：那会把厂商密钥留在 RAGFlow
  里并绕开网关，违反 CLAUDE.md 5.1 与 NO-02（模型调用一律经 LiteLLM）。

### 要填的值

控制台 `http://localhost` → 右上角头像 → **模型提供商** → 添加 **OpenAI-API-Compatible**：

| 字段 | 值 |
|---|---|
| API-Key | 本仓 `.env` 里的 **`LITELLM_MASTER_KEY`**（不是 SiliconFlow 的 key） |
| Base URL | `http://host.docker.internal:4000/v1` |
| 模型类型 / 模型名 | **`embedding`** ← 填 LiteLLM 的**逻辑名**，不是 `Qwen/Qwen3-Embedding-8B` |

> `host.docker.internal` 可用是**实测**的：在 `ragflow-cpu` 容器内对
> `host.docker.internal:4000` 与 `172.17.0.1:4000` 都能建立连接。
> 填逻辑名 `embedding` 也是刻意的 —— 型号映射只活在
> `deploy/docker/litellm/config.yaml`，换模型不动 RAGFlow（CLAUDE.md 第 5 章：业务只认逻辑名）。
> 该表单若另有"维度 / dimension"一项，填 **4096**（与 `MILVUS_DIM` 一致）。

顺手把 **chat** 的模型名 `primary-chat` 也加上（同一个供应商实例即可）：RAGFlow 的其它
功能会用到它，且这同样经网关，不违反 NO-02。

**不需要手工添加模型名**：该工厂在 `conf/llm_factories.json` 里的模型清单是**空的**，
于是 `api/apps/services/provider_api_service.py` 会退回**模型发现** —— 主动
`GET {base_url}/models`，再用 `rag/llm/model_meta.py` 的
`OpenAIAPICompatible._infer_model_types` 按名字关键词定类型：含 `embed`/`bge` → embedding，
含 `rerank` → rerank，其余 → chat。

【已实测（2026-10-16）】本仓网关的 `/v1/models` 恰好返回三个逻辑名，正好对上这三条启发式，
**自动发现即全部通过**：

```
primary-chat → chat     embedding → embedding     rerank → rerank
```

（控制台里那个 **添加自定义模型** 按钮是"要强制某个名字"时才用的，本例用不上。）

### 供应商别选错：`OpenAI` ≠ `OpenAI-API-Compatible`（实测白跑过一轮）

选 **`OpenAI`** 会看到满屏 `Fail to access model(OpenAI/gpt-3.5-turbo)`、`…/gpt-5.5`、
`Fail to access embedding model(text-embedding-3-large)`、`tts-1`、`whisper-1` 之类。

**这不是接线错**：`OpenAI` 工厂在 `llm_factories.json` 里带一份**写死的 OpenAI 真名清单**，
RAGFlow 拿它逐个探测你的网关，网关当然全部拒绝。判据有两个，都能一眼认出：

- 报错里的模型名是 **OpenAI 的真名**（gpt-*、text-embedding-3-*、whisper-1…）；
- 错误体里嵌着 **LiteLLM 的错误格式**（`Call /v1/models to view available models for your key.`）
  —— 这反过来证明网关是通的，只是名字不认识。

只要换成 **`OpenAI-API-Compatible`**（空清单 → 走发现），同一组 base_url/key 就全过。

`base_url` 对非 VLLM 供应商是**原样透传**的（`_normalize_provider_base_url` 只给 VLLM 补
`/v1`），所以填 `http://host.docker.internal:4000/v1` 不会再被追加一层。

### 必须再走「设置默认模型」

**注册 ≠ 默认。** 三个模型都通过验证、`GET /api/v1/models` 能列出来，解析照样会失败 ——
`tenant_model_service.py` 读的是 `Tenant` 表的 `embedding_id` / `tenant_embedding_id`，
那是「设置默认模型」这一步写的。控制台自己也有这句提示：*“请先在模型提供商中添加 Embedding
模型和 LLM，然后在『设置默认模型』中设置它们。”*

到 **设置默认模型** 里把 **Embedding model** 选成 `embedding`、**Chat model** 选成
`primary-chat`。

### 复核

```bash
curl -s -H "Authorization: Bearer $KEY" http://localhost:9380/api/v1/models/default
# 期望 data.models 里出现 model_type=embedding、model_name=embedding 的一条
```

若供应商已注册、只是默认没选上，可用 PATCH 补（这条是 v1 REST，**可脚本化**，
【已于 2026-10-16 实测生效】）——传 `model_id` 最省事，文档写明它优先于
`model_provider/model_instance/model_name` 三元组，且带存在性、归属与类型校验：

```bash
MID=$(curl -s -H "Authorization: Bearer $KEY" http://localhost:9380/api/v1/models \
      | python -c "import json,sys; print([m['model_id'] for m in json.load(sys.stdin)['data'] if m['name']=='embedding'][0])")
curl -s -X PATCH http://localhost:9380/api/v1/models/default \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"model_id\":\"$MID\",\"model_type\":\"embedding\"}"
# 期望 {"code":0}；随后 GET /models/default 应出现 model_type=embedding 的一条
```

> `model_id` 从 `GET /api/v1/models` 现取、不要硬编码：抄串了会静默把默认指到别的模型上，
> 而 PATCH 仍返回成功。

**顺序很重要**：先做完这一步再跑 `scripts/ingest.py`。否则会得到一批 `run=FAIL` 的文档，
而 RAGFlow **拒绝同名重复上传**（下一节），必须先删掉失败文档才能重跑。

### 多模态解析额外要配的：视觉模型（model_type=`vision`）

扫描件整页、PDF 内嵌图与表格截图由 DeepDOC 的 `VisionFigureParser` 交给视觉模型生成
描述，描述进入 chunk 正文，后续检索与作答仍是文本链路（ADR-0004）。网关侧的逻辑名是
`vision-chat`（`deploy/docker/litellm/config.yaml`，映射 SiliconFlow 的
`Qwen/Qwen3-VL-30B-A3B-Instruct`）。

**发现规则与 chat/embedding 不同，要注意**：`rag/llm/model_meta.py` 的
`_infer_model_types` 先按名字猜主类型（含 `embed`/`bge` → embedding，含 `rerank` →
rerank，其余 → chat），再对名字里含 `vision` 的模型**追加** `vision`
（`_VISION_HINTS` 见同文件）。因此 `vision-chat` 会被发现成 **chat + vision** 两条类型：
既不用手工"添加自定义模型"，也不要把它的默认 chat 设成它。

- 若该供应商是在本版网关新增 `vision-chat` **之前**注册的，模型发现不会自己刷新，
  要给它补一条。控制台可重新拉取模型列表；也可以直接用 v1 REST（**2026-10-20 实测
  生效**，实例名从 `GET /api/v1/providers/OpenAI-API-Compatible/instances` 取）：

```bash
curl -s -X POST http://localhost:9380/api/v1/providers/OpenAI-API-Compatible/instances/kevin/models \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model_name":"vision-chat","model_type":"vision"}'
# 期望 {"code":0}；随后 GET /providers/.../models 应出现 model_type=["vision"] 的 vision-chat
# 新加模型 verify 为 "unknown" 是正常的：设置默认只校验 status=active，
# 真正的验证在第一次解析（或直接对网关发一张图）。
```

- 到 **设置默认模型** 把 **Vision model** 选成 `vision-chat`；界面若无此槽位，用 v1
  REST 补（`model_type` 必须是 `vision`；写成 `chat` 会返回"成功"，但解析时仍然找不到
  视觉模型）：

```bash
MID=$(curl -s -H "Authorization: Bearer $KEY" http://localhost:9380/api/v1/models \
      | python -c "import json,sys; print([m['model_id'] for m in json.load(sys.stdin)['data'] if m['name']=='vision-chat'][0])")
curl -s -X PATCH http://localhost:9380/api/v1/models/default \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"model_id\":\"$MID\",\"model_type\":\"vision\"}"
# 期望 {"code":0}；随后 GET /models/default 应出现 model_type=vision 的一条
```

**没配视觉模型的后果是"安静地少东西"，不是报错**：`vision_figure_parser_pdf_wrapper`
用 `except Exception: vision_model = None` 吞掉查找失败（`deepdoc/parser/figure_parser.py:130-135`），
随后跳过图片描述、原样返回。缺少描述时，图片框内的文字（多半只有图注或零散标签）
仍会被当成 chunk 正文；框内一个字都没有的图则在 `pdf_parser._extract_table_figure`
里就被 `if not txt: continue` 丢掉。两种情形都不会产生可检索的图片描述，入库脚本
也拿不到像样的文本——最终现象是"这批文件里没有图"，日志里找不到任何错误。所以入库前
必须先确认 `GET /models/default` 里有 `model_type=vision` 的一条。

> 该路由指向云端 API，图片内容出域。仅限公开演示语料，客户数据不得走这条链路；
> 生产切私有化 VLM 时只改网关映射，RAGFlow 与业务代码都不动。

## 步骤 6 · 核实 SiliconFlow 模型 ID 与维度

> **已于 2026-10-16 用真实密钥核实完毕**：`Qwen/Qwen3-Embedding-8B` 确实出现在上游
> `GET /v1/models` 的 99 个模型清单里，实测输出 **4096** 维、与 `MILVUS_DIM` 一致；
> `hosted_vllm → /v1/rerank` 的路由也在真实网关上跑通（详见 CHANGELOG「真实密钥联调」
> 与 `deploy/docker/litellm/config.yaml` 的注释）。**本节保留为"换模型/换维度时的复核
> 规程"**，首次部署不必再走一遍。

`MILVUS_DIM=4096` 是照着 `Qwen3-Embedding-8B` 填的，两者必须一致，否则会在真实入库时
才以「维度不符」失败——换型号时按下面的最小证据链复核：

```bash
# 1) 看 SiliconFlow 上实际可用的模型 ID（把 KEY 换成 SILICONFLOW_API_KEY）
curl -s https://api.siliconflow.cn/v1/models -H "Authorization: Bearer $KEY" | head -c 800

# 2) 实测嵌入维度（最直接的一条证据）
curl -s https://api.siliconflow.cn/v1/embeddings \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-Embedding-8B","input":["测试"]}' \
  | python -c "import json,sys; print('dim =', len(json.load(sys.stdin)['data'][0]['embedding']))"
```

- 模型 ID 拼错 → 改 `deploy/docker/litellm/config.yaml` 对应那行；
- 维度不等于 4096 → 改本仓 `.env` 的 `MILVUS_DIM`（改维度后**必须重建集合**：
  现有集合的索引维度对不上，`ensure_collection` 会 fail-fast 报维度冲突，
  此时删除集合重跑入库即可）。

改完网关映射后**必须重启网关**：`docker compose restart litellm`。

跑集成测试可一次性验证这三件事（含重排路由）：

```bash
RUN_INTEGRATION_TESTS=true uv run pytest -m integration -k litellm -v
```

## 步骤 7 · 与本平台联通（可选，当前不需要）

本平台只从**宿主机**按 `RAGFLOW_BASE_URL` 访问 RAGFlow，因此两者不在同一 Docker
网络里也能工作。若将来需要容器间直连（例如把 RAGFlow 的 MCP 服务挂进编排），
把它接入本仓创建的网络：

```bash
docker network connect agent-platform-net ragflow-server   # 容器名以 docker compose ps 为准
```

## 内存安排（16GB 单机）

**实测数字（2026-10-16）**：`.wslconfig` 里 `memory=10GB`，WSL 虚拟机实得 **9.7Gi**。
两套栈同时起来后 `free -h`：**used 7.1Gi / available 2.6Gi**。逐容器账面（`docker stats --no-stream`）：

| 容器 | 内存（空闲态） | 谁在用 |
|---|---|---|
| ragflow-cpu | **4.12 GiB** | RAGFlow 服务 + 任务执行器（解析时还会涨） |
| ap-litellm | **1.56 GiB** | 模型网关（入库时的嵌入调用要它，停不得） |
| ap-milvus-standalone | 325 MiB | 检索主链路，入库目标 |
| mysql / minio / infinity（RAGFlow 侧） | 633 MiB | RAGFlow 自身依赖 |
| ap-postgres + ap-redis | **59 MiB** | checkpoint / 队列，入库路径用不到 |

两个被实测推翻的直觉，都记下来免得后人再算错一次：

1. **真正的约束是 WSL 内核，不是容器上限**。Infinity 的 `mem_limit: ${MEM_LIMIT}` = 7.52GiB
   比可用量还大 —— 调小它只会让 OOM 来得更早，不是解法；要让路的只能是"少开点东西"。
2. **"入库时停掉 postgres/redis 腾内存"没用**：实测两个加起来只有 59 MiB。
   要腾内存得看上面那张表里真正的大头。

因此策略是：**入库分批做**（实测路径：5 份跑通端到端 → 放量到 19 份，两批都验证了幂等重跑）、
**入库完成后停 RAGFlow**、解析期间不要在宿主上跑别的重活：

```bash
cd /e/Codes/ragflow-0.27.2/docker
docker compose stop                       # 入库完成后释放 4 GiB+（数据卷保留，下次 start 即可）
```

| 阶段 | RAGFlow | 说明 |
|---|---|---|
| 入库（解析切分） | **运行** | 解析完的内容已落进 Milvus，RAGFlow 不再参与 |
| 问答 / 演示 | **可停** | 检索走 Milvus，RAGFlow 停掉不影响问答链路 |

相关配置：
- `.wslconfig`：`memory=10GB` + `networkingMode=mirrored` + `autoProxy=true`
  （见 [local_env_setup.md](local_env_setup.md)）；
- `deploy/docker/milvus/milvus.yaml` 已把 Milvus 内存反压水位压到 0.70/0.85。

## 常见故障

| 现象 | 原因与处置 |
|---|---|
| `docker compose up` 卡在拉镜像 | Docker Desktop 未配代理（Settings → Resources → Proxies） |
| ragflow 容器反复重启，日志有 `nltk` / `huggingface` 下载失败 | `HF_ENDPOINT` 未改成镜像站，或容器无外网 |
| 上传返回 `code=109` | `RAGFLOW_API_KEY` 错或失效，重新生成 |
| 文档 `run=FAIL`，`progress_msg` 含 `Fail to bind embedding model: No default embedding model is set.` | **租户没配默认嵌入模型**，见步骤 5。错误文案不提模型，极易误判成文件坏 |
| 解析长时间 `RUNNING` | **头号原因不是扫描件，而是「页数多 / 表格密集」**：`layout_recognize=DeepDOC` 对**每一页**都跑版面识别与表格结构识别（TSR，日志表现为反复出现 `Best table orientation: 0°`），与页面有无文本层无关。实测 0061（318 页竞价结果表，仅 1.2MB）被拆成 27 个 12 页任务、单片 `Build document` 410~809s、全程约 3 小时，超出 `RAGFLOW_PARSE_TIMEOUT_S`（默认 3600）。**按文件大小估耗时是错的判据**，页数 × 表格密度才是。处置：调大该值，或把这类大页数表格件移出子集（见 `scripts/energy_subset.txt` 文末）。查进度看 `docker logs docker-ragflow-cpu-1 --since 5m` 里的 `Task done` 行；`docker stats` 里 CPU 打满而内存不高即属此类 |
| 超时或人工停止后，RAGFlow 里的文档不见了 | **设计行为，非异常**：超时、`FAIL`、`CANCEL` 都会触发 `scripts/ingest.py` 的补偿删除（`ingest_one` 的 `except AgentPlatformError` 分支）。不删则残留文档挡住同名重传，重跑永远补不上。代价是**已经跑掉的分片全部作废**（重跑要重新解析）——所以对"本就很慢"的文件，应早点决定是调大超时还是移出子集，而不是跑一半再停 |
| 重新上传报 `Duplicated document name in the same dataset.` | 见下节「同名文档」 |
| 入库报"文件名超过 255 字节" | RAGFlow 按**字节**限长，中文名约 85 字就到顶，重命名后再入库 |
| 9380 连不上但控制台能开 | `SVR_HTTP_PORT` 被改成了别的值，或容器未 healthy |

### 同名文档：RAGFlow 拒绝重复上传（会卡死重跑）

【已核实（v0.27.2 源码）】`api/apps/restful_apis/document_api.py` 的上传处理器先查
`DocumentService.query(name=name, kb_id=dataset_id)`，非空即返回
`Duplicated document name in the same dataset.` —— **同名不是覆盖，是拒绝**。

推论很关键：**任何残留在数据集里的文档（尤其是解析失败的）都会挡住同名文件的重新入库**，
而失败文档不会被记进幂等清单、也就没人替它收尸。`scripts/ingest.py` 因此有两处兜底：

1. 入库失败时**补偿删除**刚上传的文档（`ingest.file_failed` 后跟 `ingest.cleanup_failed` 即为它没删掉）；
2. 重入时清理上一轮遗留文档是**尽力而为**的 —— 该文档大概率已被第 1 条删掉，而 RAGFlow 对
   "删除不属于该数据集的 id"是**报错**而非幂等成功，所以这一步失败不判整体失败
   （否则重跑会在上传之前就卡死）。

两处都失败时（例如 RAGFlow 不可用），到控制台手工删掉同名文档再重跑。

## 升级

不要 `git pull` 后 `docker compose up` 直接覆盖 —— 先看上游 release notes 是否
涉及 API 路径或响应字段（本仓 `knowledge/ragflow_client.py` 的契约依据是 v0.27.2
的源码）。升级步骤：

1. 记录当前行为基线：`uv run pytest -m integration -k ragflow -v`；
2. 换 tag、改 `RAGFLOW_IMAGE`、重建容器；
3. 重跑上一步，**契约测试全绿再继续**；
4. 在 `CHANGELOG.md` 登记版本与结论（NO-20）。
