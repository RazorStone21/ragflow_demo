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

按下面覆写（左侧为键，右侧为本项目要求的取值与理由）：

| 键 | 取值 | 理由 |
|---|---|---|
| `RAGFLOW_IMAGE` | `infiniflow/ragflow:v0.27.2` | 锁定版本，与 CHANGELOG 登记一致；不要用 `latest` |
| `DOC_ENGINE` | `infinity` | 用内置 Infinity 而非 Elasticsearch：**不起 ES 容器**，省下约 4GB 内存与一次 JVM 预热 |
| `DEVICE` | `cpu` | 本机无 GPU |
| `HF_ENDPOINT` | `https://hf-mirror.com` | 首次解析要拉 DeepDoc 模型权重；直连 HuggingFace 在本机不通 |
| `SVR_HTTP_PORT` | `9380` | 必须与本仓 `.env` 的 `RAGFLOW_BASE_URL=http://localhost:9380` 一致 |

```bash
# 逐条改（sed -i 会就地改文件；先备份便于回退）
cp .env .env.orig
sed -i 's|^RAGFLOW_IMAGE=.*|RAGFLOW_IMAGE=infiniflow/ragflow:v0.27.2|'   .env
sed -i 's|^DOC_ENGINE=.*|DOC_ENGINE=infinity|'                           .env
sed -i 's|^DEVICE=.*|DEVICE=cpu|'                                        .env
sed -i 's|^HF_ENDPOINT=.*|HF_ENDPOINT=https://hf-mirror.com|'            .env
sed -i 's|^SVR_HTTP_PORT=.*|SVR_HTTP_PORT=9380|'                         .env
grep -nE '^(DOC_ENGINE|DEVICE|HF_ENDPOINT|SVR_HTTP_PORT|RAGFLOW_IMAGE)=' .env   # 复核
```

### 端口冲突检查（重要）

本平台的 `docker-compose.yml` 已经占用：**5432**（Postgres）、**6380**（Redis）、
**9000/9001**（Milvus 的 MinIO）、**19530/9091**（Milvus）、**4000**（LiteLLM）。
其中 Redis 刻意错开到 6380 就是为了避开 RAGFlow 的 6379。

起 RAGFlow 前先确认它实际会占哪些宿主端口：

```bash
docker compose config | grep -A3 'published:'
```

【未核实】上游 RAGFlow 的 compose 是否默认起 MinIO（占 9000/9001）随版本而异。
若与本仓 Milvus 的 MinIO 撞端口：改 RAGFlow 侧的 `MINIO_PORT`/`MINIO_CONSOLE_PORT`
（RAGFlow 的文档存储用哪个端口无所谓），**不要**动本仓 Milvus 侧的端口 ——
`MILVUS_URI` 与 Milvus 的 MinIO 端点都在本仓配置里互相引用。

## 步骤 3 · 启动与等待就绪

```bash
docker compose up -d
docker compose ps          # 等 ragflow 的 STATUS 变成 healthy
docker compose logs -f ragflow | head -50   # 首次启动要拉 DeepDoc 权重，可能数分钟
```

镜像拉取要走代理：Docker Desktop → **Settings → Resources → Proxies** 填
`http://127.0.0.1:7890`（`HTTPS_PROXY` 只对当前 shell 生效，docker daemon 看不到它）。

就绪判据：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:9380/api/v1/datasets   # 401 即为"服务在且要鉴权"
```

**401 是成功信号**（服务活着、只是没带 Key）。连不上才是没起来。

## 步骤 4 · 生成 API Key

1. 浏览器打开 `http://localhost:${SVR_HTTP_PORT:-80}`（RAGFlow 的 Web 控制台），
   首次访问需注册一个本地账号（数据全在本机，不出域）；
2. 右上角头像 → **API** → 生成 API Key；
3. 写进本仓库 `.env` 的 `RAGFLOW_API_KEY=`（`.env` 已 gitignore，NO-17）。

```bash
# 复核鉴权真的通了（把 KEY 换成你生成的）
curl -s -H "Authorization: Bearer $KEY" http://localhost:9380/api/v1/datasets | head -c 200
# 期望：{"code":0,...,"data":[...]}
```

## 步骤 5 · 核实 SiliconFlow 模型 ID 与维度

`deploy/docker/litellm/config.yaml` 里 `embedding`/`rerank` 两个映射的**模型 ID 拼写
未经运行时验证**（SiliconFlow 的 `/v1/models` 需要鉴权，无密钥环境下无法确认），
而 `MILVUS_DIM=4096` 是照着 `Qwen3-Embedding-8B` 填的。**首次部署必须先核实**，
否则会在真实入库时才以「维度不符」失败：

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

本机同时跑 Postgres + Redis + Milvus 三件套 + LiteLLM 已经吃掉相当一部分内存，
RAGFlow（含 Infinity）再叠上来会进入 swap 抖动。建议按**入库与问答分离**使用：

| 阶段 | RAGFlow | 说明 |
|---|---|---|
| 入库（解析切分） | **运行** | 解析完的内容已落进 Milvus，RAGFlow 不再参与 |
| 问答 / 演示 | **可停** | 检索走 Milvus，RAGFlow 停掉不影响问答链路 |

```bash
cd /e/Codes/ragflow-0.27.2/docker
docker compose stop               # 入库完成后释放内存（数据卷保留，下次 start 即可）
```

相关配置：
- `.wslconfig` 给 WSL 上限 12GB（见 [local_env_setup.md](local_env_setup.md)）；
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
