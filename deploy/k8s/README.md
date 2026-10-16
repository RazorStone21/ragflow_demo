# Kubernetes 部署（占位）

本目录当前**尚无可用清单**，只有这份说明。CLAUDE.md 2.2 要求该目录存在，且
11.1 明确部署目标是 Kubernetes 而非 docker-compose（compose 仅用于本地/单机）。

在此写下为什么是空的，以及补齐时该做什么，比放一堆不能用的 YAML 更有用。

## 当前状态

阶段一至阶段五交付的是**本地单机可运行的最小合规骨架**：依赖由仓库根
`docker-compose.yml` 提供，应用以 `uvicorn --factory agent_platform.main:create_app`
单进程运行。K8s 清单属于第二阶段的交付内容。

## 补齐时必须满足的约束（摘自 CLAUDE.md，不是本目录的发明）

| 约束 | 出处 | 落地要点 |
|---|---|---|
| api-server 与 worker 分离 | 11.2 | 同镜像、不同启动命令；**两者都不挂 GPU** |
| worker 按队列深度扩缩 | 11.2 | HPA 指标取 `redis_queue_depth`，不是 CPU |
| GPU 推理独立成池 | 11.3 | vLLM/SGLang 提供 OpenAI 兼容端点回接 LiteLLM；同一模型不得混跑 H800 与国产卡（NO-18） |
| 探针与优雅关停 | 11.6 | `/healthz`（存活）、`/readyz`（就绪，探测关键依赖）；关停需排空在跑的 run |
| 一套镜像多环境 | 11.5 | 仅配置不同；禁止镜像内固化环境地址 |
| 密钥 | 10.1 / 10.5 | 经 Secret 注入，不进镜像层；基础镜像固定 digest、非 root 运行 |
| 网络最小开放 | 10.5 | api-server/worker 不直接暴露；注意本地 compose 为便于排查暴露了 MinIO 9000/9001，生产**不得**照搬 |
| 有状态依赖高可用 | 11.4 | Postgres/Redis/Milvus 的备份与恢复演练，checkpoint 与知识库纳入保留策略 |

## 与本地编排的对应关系

本地 compose 里的服务在 K8s 下的归属需要明确，否则容易误把有状态组件也塞进集群：

- **进集群**：api-server（Deployment）、worker（Deployment + HPA）。
- **按组织策略决定**：PostgreSQL、Redis、Milvus。用托管/独立集群时，应用侧仅改
  `POSTGRES_DSN` / `REDIS_URL` / `MILVUS_URI` 三个环境变量，无需改代码。
- **不进集群**：RAGFlow。它是文档工程后端，按官方 compose 或官方 Helm 独立部署
  （见 `docs/runbooks/ragflow_setup.md`），通过 `RAGFLOW_BASE_URL` 接入。
- **GPU 推理池**：独立节点池，回接 LiteLLM；本仓不负责其编排。

## 参考

- `docker-compose.yml`（仓库根）：本地依赖与端口映射的真实来源。
- `docs/runbooks/local_env_setup.md`：本地环境从零到可跑的步骤。
- `CLAUDE.md` 第 11 章：部署与环境规则的完整表述。
