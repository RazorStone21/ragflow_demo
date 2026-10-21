# ADR-0005：评估裁判换为 SiliconFlow 云端 Qwen

| 项 | 值 |
|---|---|
| 状态 | Accepted（生效中） |
| 决策日期 | 2026-10-21 |
| 决策者 | 需求方指示换用硅基流动平台的 Qwen；平台架构组执行并登记偏离 |
| 关联 | [CLAUDE.md](../../CLAUDE.md) 第 5 章第 8 条、第 9 章 9.2、第 10 章 10.1、NO-15、NO-16；[ADR-0003](0003-evaluation-framework-and-judge.md)（本篇只取代其"四、裁判模型"一节，RAGAS 的授权形态不变） |
| 后续状态 | 不可就地修改；推翻时新增 supersede ADR |

## 背景

ADR-0003 选定 DeepSeek 作为裁判，前提是当时只有 DeepSeek 与 SiliconFlow 两家可用密钥，而后者只承担嵌入与重排。该选择有一个已登记的缺陷：**裁判与生成共用 `primary-chat` 上游**，`faithfulness` / `answer_relevancy` 存在自偏好偏置，分数偏乐观，不得当中性分数使用。

需求方于 2026-10-21 要求把裁判换成 SiliconFlow 平台的 Qwen 大模型。换型同时要解决一个在实施中暴露的缺陷（D12）：判分缓存键只有"提示词名 + 提示词"，**不含裁判身份**；逻辑名 `judge` 不变时，换上游型号后旧判定照样命中缓存，产物会写着新裁判、分数却来自旧裁判。

## 决策

### 一、网关路由

逻辑名仍是 `judge`，上游改为 `openai/Qwen/Qwen3.5-122B-A10B`，经 `https://api.siliconflow.cn/v1`，复用 `SILICONFLOW_API_KEY`：

```yaml
- model_name: judge
  litellm_params:
    model: openai/Qwen/Qwen3.5-122B-A10B
    api_base: https://api.siliconflow.cn/v1
    api_key: os.environ/SILICONFLOW_API_KEY
    temperature: 0.0
    max_tokens: 4096
```

选 122B 的依据是 2026-10-21 在网关容器内实测：`/v1/models` 可见该型号；用一个约 50 token 的 JSON 探测请求得到纯 JSON 输出，耗时约 3.7 秒。同轮也试了 `Qwen/Qwen2.5-72B-Instruct`、`Qwen/Qwen3-30B-A3B-Instruct-2507`、`Qwen/Qwen3.8-27B`，均能回 JSON；122B 是其中容量最大的一档。若真实裁判提示词（长证据 + 长答案）出现 JSON 截断或解析失败，先看 `judge_raw`，再退到 `Qwen/Qwen2.5-72B-Instruct`；换型必须改网关配置并重跑烟雾测试，不得在代码里偷偷换型号。

### 二、裁判身份进入缓存键与产物

- `JudgeCache.key` 的材料改为 `提示词名 + model_key + 渲染后提示词`，`model_key = 逻辑名@上游型号`；
- `_CACHE_SCHEMA_VERSION` 从 1 升到 2，旧缓存自然失效（不删除缓存目录）；
- `Judge` 新增 `upstream_model` 参数，`JudgeSnapshot.upstream_model` 记录型号；空串专表"未记录"的旧产物，**不编造默认型号**；
- 新增配置 `JUDGE_UPSTREAM_MODEL`（仅留痕与缓存键，不参与路由），必须与网关映射保持一致。

### 三、裁判不得走应用层 fallback

`build_chat_model(..., use_fallback=False)` 用于裁判构造。换裁判型号等于换测量工具，不能由一次网关抖动悄悄换成另一个模型判分。网关侧的 `router_settings.fallbacks` 仍未配置。

### 四、换型后的事实

- 与生成模型 DeepSeek **不同家族**，ADR-0003 登记的自偏好偏置在该路径上消除；
- 但裁判在云端，**评估数据（问题、检索证据、答案）会出域到 SiliconFlow**——这是本节最重要的一条边界；
- 模型判定仍不是人工金标准，分数按"有偏测量"解读；
- 2026-10-21 之前的产物由 DeepSeek 判分，与新产物**不可直接比较**。

## 备选方案与权衡

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 继续用 DeepSeek | 零改动；与旧基线同口径 | 自偏好偏置继续存在，分数不能当中性分 | 否决 |
| 云端 Qwen 122B（本篇） | 不同家族；容量足够处理长证据；复用既有密钥 | 评估数据出域；云端限流与可用性不受控 | **采用（仅限公开演示语料）** |
| 云端 Qwen 72B / 30B | 更快、更便宜 | 长提示词下的稳定性未压测；容量小于 122B | 作为 122B 解析失败时的备选 |
| 本地私有化裁判 | 满足 §5.8 与 10.1，数据不出域 | 本机 16 GB、无 GPU，无法承载有判断力的本地模型；需另行采购算力 | 生产环境的正确终态，本轮不具备条件 |

## 后果

**正面**：

- 消除裁判与生成同源的自偏好偏置；
- 裁判身份进入缓存键，换型号不再复用旧判定（D12）；
- 上游型号落入产物快照，换型在对比报告中显形。

**负面与风险**：

- **评估数据出域**：问题、证据、答案会发送到 SiliconFlow。仅限公开演示语料；客户数据、生产环境不得使用；
- 云端模型的版本与行为不受本平台控制，供应商侧升级可能改变分数；
- 旧基线（DeepSeek 裁判）与新产物不可直接比较，任何分数差异不能只归因于模型；
- 122B 对长提示词可能更慢，门禁运行时长增加。

## 合规与许可证

- 这是对 CLAUDE.md §5.8（生产裁判须本地）与 §10.1（数据不出域）的**演示期偏离**，已在 `.env.example`、`tests/evals/README.md`、`tests/evals/config/thresholds.toml` 与每份产物的 `run.judge.caveat` 中显著标注；
- 该偏离**只适用于公开演示语料**，不得类推到客户数据与生产环境；生产须换私有化裁判，切换只改网关映射与 `JUDGE_UPSTREAM_MODEL`，业务代码不改；
- Qwen 权重许可证以官方发布页与法务确认为准，本篇不据此作商业承诺；SiliconFlow 服务条款与数据处理协议需在正式使用前由法务确认。

## 迁移与回滚

**迁移**：改 `deploy/docker/litellm/config.yaml` 的 `judge` 条目 → `docker compose restart litellm` → 设 `JUDGE_UPSTREAM_MODEL=Qwen/Qwen3.5-122B-A10B` → 跑 `tests/integration/test_litellm_proxy.py` 与 `tests/evals/test_eval_smoke.py` → 重跑评估。旧缓存因 `_CACHE_SCHEMA_VERSION` 与 `model_key` 双重变化而失效，无需手工清理。

**回滚**：把 `judge` 条目改回 DeepSeek 映射、`JUDGE_UPSTREAM_MODEL` 改回对应型号（或留空）、重启网关。旧产物与旧基线仍在，可直接复用；但回滚后产生的新分数与 Qwen 判分**不可直接比较**。

## 未决与后续事项

- 真实裁判提示词下的 JSON 稳定性与耗时需在 50 条全量评估中观察；解析失败记 `ERROR`，不带分数；
- `scripts/eval_ragas.py` 与自研裁判共用同一 Qwen，独立性只在判分方法，产物 caveat 已注明；
- 生产环境的私有化裁判选型（本地 Qwen / bge 系列裁判模型）需另立 ADR，并完成算力与精度压测。
