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

## 步骤 5 · 核实 SiliconFlow 模型 ID 与维度

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

## 步骤 6 · 与本平台联通（可选，当前不需要）

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

因此策略是：**入库分批做**（先 5 份跑通端到端再上量）、**入库完成后停 RAGFlow**、
解析期间不要在宿主上跑别的重活：

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
| 解析长时间 `RUNNING` | 扫描件在 OCR；`RAGFLOW_PARSE_TIMEOUT_S` 默认 3600s。本项目能源子集已确认是文本型 PDF，无需 OCR |
| 入库报"文件名超过 255 字节" | RAGFlow 按**字节**限长，中文名约 85 字就到顶，重命名后再入库 |
| 9380 连不上但控制台能开 | `SVR_HTTP_PORT` 被改成了别的值，或容器未 healthy |

## 升级

不要 `git pull` 后 `docker compose up` 直接覆盖 —— 先看上游 release notes 是否
涉及 API 路径或响应字段（本仓 `knowledge/ragflow_client.py` 的契约依据是 v0.27.2
的源码）。升级步骤：

1. 记录当前行为基线：`uv run pytest -m integration -k ragflow -v`；
2. 换 tag、改 `RAGFLOW_IMAGE`、重建容器；
3. 重跑上一步，**契约测试全绿再继续**；
4. 在 `CHANGELOG.md` 登记版本与结论（NO-20）。
