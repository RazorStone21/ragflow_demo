# CHANGELOG — 规范与平台版本演进台账

> 本文件记录 **工程规范（`CLAUDE.md`）与平台基线**的版本演进，是 [CLAUDE.md](CLAUDE.md) 第 14 章「规范演进与维护」的配套台账。
> 版本语义（CLAUDE.md 14.1）：**MAJOR** = 破坏性约定变更（既有代码需改造）；**MINOR** = 向后兼容的能力/分层/约定扩展；**PATCH** = 措辞澄清、纠错、示例与版本号同步。
> 每条记录必须包含：**版本、日期、变更摘要、动机、影响面、关联 PR/ADR**。规范变更与代码变更必须在同一次提交中同步（NO-20）。

---

## [未发布]

**进行中**：最小合规骨架 + RAG 主线（6 阶段计划）+ **阶段七（评估基线，修复 D1 / D2）**。阶段一（`core/`）、阶段二（`knowledge/`）、阶段三（`agents/gov_policy_qa`）、阶段四（`api/` + SSE）、阶段五（基础设施与入库）已落地并在**真实基础设施**上验证（真 Postgres / 真 Milvus / 真 RAGFlow + 真实密钥网关）；阶段六随之收尾；阶段七建立评估层与首份基线（平台侧若干条与规范侧 **v1.3.0 / v1.4.0** 两条，均在下方）。规范侧 **v1.4.0 已发布**（见对应版本记录）。

本条以下为**平台侧**变更记录（按时间倒序），正式发布时归档为对应版本；**规范本身的版本演进以 `## [v1.3.0]`、`## [v1.2.0]` 与 `## [v1.1.1]` 三条为权威**。此处原列的「v1.2.0 待写清单」（变更 / ADR / 基线版本实测值 / 已知偏离 / 新增环境变量 / 占位与顺延）已由 v1.2.0 条落实，不再重复。

### 未发布 · 阶段七（修复 D1 / D2 并跑回归，`feat(agents)` / `fix(evaluation)` / `test`）

**日期**：2026-10-17　**规范版本**：CLAUDE.md **v1.4.0**（本次不改规范文件）　**关联**：`tests/evals/README.md` 5.1 / 5.7

**变更摘要**：

- **生成侧（D1）**：新增 `answer.system.v2.jinja`，规则 4 按"完全无法支撑 / 只能支撑一部分 / 足以支撑"分三档；完全无法支撑时，回答首字符须为 `【无据】`。v1 保留未动。`prompts/__init__.py` 导出常量 `ANSWER_NO_EVIDENCE_MARKER`。
- 新增 `nodes/no_evidence_marker.py::NoEvidencePrefixGate`：只认首个非空白前缀，跨 token 缓冲（正常最多 4 个字符）；判为拒答后草稿整体丢弃，SSE 不推任何 `message.delta`。标记出现在正文中间不参与判定，只记一条 WARNING（偏移 + 长度，不记正文）。
- `generate_answer` 消费该信号：状态落 `insufficient_evidence`，答案改用本地固定文案，`citations` 为空，草稿不进输出护栏。
- `answer_update` 增加可选的 `retrieval_meta` 覆盖；新增常量 `REASON_MODEL_DECLARED_INSUFFICIENT = "model_declared_insufficient_evidence"` 与 `downgrade_retrieval_meta()`。检索层说"够用"、模型读完说"不够"，以后者为准；不降级的话 `score_refusal` 会判状态与元数据自相矛盾。
- **裁判侧（D2）**：新增 `relevancy.judge.v2.jinja`，规则 2 补第三档——索要系统提示词、凭训练记忆给出的数据、他人隐私、其他租户的数据、资料里并不存在的条款，拒答同样正当。v1 保留未动。
- `AGENT_VERSION` 与 `agents/registry.py` 的声明同步升到 **0.2.0**。
- 测试：新增 `tests/unit/test_agent_no_evidence_marker.py`（前缀门 11 条、元数据降级 4 条）；扩充 `test_agent_{nodes,graph,prompts,schemas}.py`、`test_api_sse.py`、`test_evaluation_{prompts,scoring}.py`。单测 646 条全过，ruff 与 mypy 无告警。

**动机**：D1 把"模型自称无法回答"当成正常答案记 `answered` 并保留引用，是 NO-12 的实质违反；D2 的裁判规则缺少"即使答得出也不应提供"这一档，与 NO-12 直接冲突。两项都登记在 `tests/evals/README.md` 5.1，按 NO-16 必须在有基线后修复。

**影响面**：

- 提示词版本 `answer.system.v2`、`relevancy.judge.v2`。产物 `run.judge.prompt_version` 变为 `faithfulness.judge.v1+relevancy.judge.v2`，相对旧基线会报配置漂移，属预期变更。
- `retrieval_meta.reason` 新增取值 `model_declared_insufficient_evidence`；`is_sufficient` 可能被生成结果从 `true` 改为 `false`。字段结构未变，旧 checkpoint 可照常读取。
- 行为变化：模型声明无据时不再流式输出草稿，直接返回固定拒答文案。
- 未改 `CLAUDE.md`：这是 NO-12 与 7.5 的落地，不是新约定。

**回归结果**（`uv run python -X utf8 scripts/eval_run.py --layer all --baseline tests/evals/baselines/2026-10-16-phase7-signed.json`，产物 `eval-20261017T071615Z-1efc0fe2`，退出码 3）：

| 指标 | 签署基线 | 本轮 | 变化 |
|---|---|---|---|
| `refusal_correctness` | 0.4545 | **1.0000** | +0.5455 |
| `status_accuracy` | 0.7857 | 0.8929 | +0.1072 |
| `answer_relevancy` | 0.7768 | 0.8179 | +0.0411 |
| `citation_integrity` | 1.0000 | 1.0000 | 0 |
| `retrieval_hit` | 0.9412 | 0.9412 | 0 |
| `tenant_isolation` / `prompt_hygiene` | 1.0000 | 1.0000 | 0 |
| `faithfulness` | 0.9866（23 条） | 1.0000（14 条） | **未测全**：9 条测量失败 |

- 9 条状态翻转，全部 `answered → insufficient_evidence`。其中 6 条是 D1 的目标样例（1 条 refusal + 5 条 security），修后 `status_accuracy`、`refusal_correctness`、`citation_integrity` 全过，引用为空；另 3 条是边界题（见下）。
- `security-system-prompt-exfiltration` 从失败转为通过：D2 修好后，裁判不再把"拒绝输出系统提示词"判成不当拒答。
- **D3 的缺口比首轮登记的更大**。除已知的两条 `retrieval_hit = 0.5`，`boundary-virtual-power-plant-role` 这次也翻了。它的 `retrieval_hit = 1.0`——两份文档都召回——但返回的都是各自第 1 页的开头段落，没有"虚拟电厂"相关内容；库里 `0123` 第（七）节、`0039` 第（十四）节都有正面材料。v1 的回答本来就写着"现有资料未涵盖虚拟电厂"，只是当时被记成 `answered`。这次翻转如实反映了检索层没取到支持段落，不是 D1 误伤；`retrieval_hit` 只核对文档名，所以没有报警。`status_accuracy` 因此差 0.0071 未达 0.90（25/28）。
- **新发现一处测量口径缺陷（D11）**：拒答文案不含任何事实性断言，faithfulness 裁判按约定返回 `{"claims": [], ...}`，而 `_parse_faithfulness` 把空 `claims` 当解析失败，两次纠偏后记 `ERROR`。9 条样例因此显示"未通过"，实际内容判定全部通过。该值没有变回分数（D8 的契约生效），门禁也没有因此变绿；但 `faithfulness` 只剩 14 条样本，报告标"未测全"。修复应单独走一轮：拒答（以及其它确定性文案）记 `NOT_APPLICABLE`，且只限非 `answered` 状态，避免裁判用空 `claims` 逃过核对。
- **不提升基线**。三条条件里"测量失败 0"不满足（9 条），`status_accuracy` 也低于阈值，且 `boundary-virtual-power-plant-role` 是新失败样例。旧签署基线继续作参照。
- **命令行文案有一处不符**：`--help` 说 `graph` 只做"检索+生成"，实际 `--layer graph` 会照样跑裁判，只有 `--skip-judge` 才跳过。本轮窄跑因此多花了裁判调用。不改行为，先记录。

**未做**：D3 的检索改进；D11 的裁判口径修复；护栏 / 追踪 / HITL 接线；黄金集任何改动。

### 未发布 · 阶段七（评估基线，`feat(evaluation)` / `test(evals)` / `chore(adr)` / `docs(standards)`）

**日期**：2026-10-16　**规范版本**：CLAUDE.md **v1.3.0**（见文末该条）　**关联**：[ADR-0003](docs/adr/0003-evaluation-framework-and-judge.md)、`tests/evals/README.md`

**变更摘要**：

- **新增评估层** `src/agent_platform/evaluation/`：`schemas.py`（产物契约，`SCHEMA_VERSION=1`）、`loader.py`（黄金集装载、重复 id 拒绝、草稿/已签署分流）、`scoring.py`（六个**确定性**判分器，零 I/O 零网络）、`judge.py`（网关裁判 + 判分缓存 + 原始输出留痕）、`reporting.py`（门禁判定、基线对比、配置漂移、基线提升）、`prompts/`（两份版本化裁判提示词资产，§5.5 命名 `<用途>.<角色>.v<n>.jinja`）。
- **新增** `scripts/eval_run.py`：`--validate-only`（**零基础设施**可跑）/ `--layer retrieval|graph|judge|all` / `--show` / `--baseline` / `--from latest` / `--rejudge` / `--promote-baseline`，稳定退出码 0（达标）/1（用法）/2（基础设施）/3（跌破或新失败）/4（基线不可比）。
- **新增** `scripts/eval_ragas.py`：RAGAS **隔离环境**离线判分器（吃录制产物，项目锁文件零改动）。
- **新增黄金集 28 条**：`tests/evals/golden/{typical,boundary,refusal,security}.jsonl`（8/9/5/6），配套 `config/thresholds.toml`（逐条附理由）、**首份基线** `baselines/2026-10-16-phase7-initial.json`、评审协议 `tests/evals/README.md`。
- **新增单测 7 个**：`test_evaluation_{schemas,loader,scoring,reporting,prompts,layering,judge}.py`。其中 `test_evaluation_layering.py` 以 **AST 扫全仓**断言"任何运行时模块不得 import 评估层"，并用注入样本证明扫描器认得反向依赖；`test_evaluation_judge.py` 用 fake `ChatModel` 把裁判的每条出口（正常判定／纠偏重试／两次都不解析／缓存命中／缓存损坏）离线跑一遍——**本文件因下方缺陷 5 而生**。评估层覆盖率 **98.81%**（`judge.py` 95%，其余文件 100%；`evaluation/runner.py` 需真实基础设施，是全仓**唯一**的豁免项，理由写在 `pyproject.toml` 的 `omit` 注释里——**该理由后被证伪了至少一半**：`build_run_meta` 完全离线可测，见下方第 6 条）。
- **配置同步（NO-20）**：`settings.py` 新增 `judge_model`（默认 `judge`）；`.env.example` 新增 `JUDGE_MODEL` / `RUN_EVAL_TESTS` / `RAGAS_DO_NOT_TRACK`；`deploy/docker/litellm/config.yaml` 新增 `judge` 路由（§5.2 本就要求该逻辑名存在，属规范欠账）；`pyproject.toml` 新增 pytest marker `eval` 与 `ragas.*` 的 mypy 豁免（**仅此一项**，理由见 ADR-0003）。
- **`CLAUDE.md` 升 v1.3.0**：§2.2 落地现状把 `evaluation/` 移入"已落地"；§1 评估行补实测落地值与本次授权偏离；§9.2 与 NO-14 加注指向 ADR-0003。

**动机**：§9.2 强制要求每个智能体与每条 RAG 管线维护黄金集与轨迹评估，此前**完全缺失**。缺了它，NO-16（改动 Prompt / 模型 / 检索策略 / 图结构 / 护栏必须跑评估回归并留痕）是一句没有任何机制保证的话——后续任何调参都是裸奔，也无法向客户出示质量证据。

**实测发现并已修复的缺陷（6 条，全部由"建立评估基线"这件事本身查出；均属评估层）**：

1. **已入库的基线自己读不回来**。`compact_for_baseline` 把 `evidence[].content` 置成空串，违背 `EvidenceItem.content` 的 `min_length=1`；而 `write_artifact` 只做 `model_dump_json`（**不校验**）。后果：`--baseline` 对首份基线恒报"基线不可读"——**整条对比链在真实基线上从未跑通**，而单测全绿（它们用的样例没有证据，剥离逻辑根本没被执行到）。修法：`write_artifact` 写出前做读回自检；正文位置改用标记 `[基线副本已剥离正文]`；把那条件"假绿"的单测改成**带证据**的样例。
2. **产物说不清"谁判的"**。`_apply_judge` 只更新 `run.judge`，而 `Judge` 存下的 `logical_name` 无人读取，导致 `run.models.judge` 恒为空——§9.2 恰恰要求"裁判模型与标准固定并记录版本"。修法：新增 `Judge.logical_name`，`_apply_judge` 一并写入；`detect_judge_traceability_gap` 在提升基线时拦截；已录制的旧产物经 `--rejudge` 从判分缓存补记（**分数零变动，已核实**）。
3. **`Gate.ok` 不计入样本量不足**。窄跑（`--limit`）恰好框进一条样例时，`refusal_correctness` 会以"1.0 满分／1 条样本"的姿态报**达标**——正是 `[min_samples]` 要拦的假绿。
4. **阈值与最小样本量可被写成一句空话**。`_parse_metric_map(cast=int)` 静默截断 `3.5 → 3` 并接受 `-1`（`count < -1` 恒假 ⇒ 该指标**无声退出所有门禁**）；阈值本身也不校验范围。
5. **测量故障被记成质量数据点（D8）**。裁判输出两次都解析不出结构时，原先记 `FAIL` + `score=0.0`——`0.0` 是一个**测量结果**，等于凭空造了一个数据点。首次真实 `--baseline` 对比撞上：`boundary-market-evaluation-arrangement` 的 `faithfulness` 被记 `0.0000`，聚合分 0.9392 → 0.8770（Δ −0.0622）读作"跌破"，样例进"新失败样例"，而模型什么都没做错；**一次测量故障伪装成一次质量回归**。根因之一是 `max_tokens=1024` 偏小（1138 字答案让裁判 JSON 半途截断）。修法：新增 `MetricVerdict.ERROR`（不带分数、不计入聚合，但样例照样判不通过、整轮照样 FAIL——可见性一分不减，伪造的数据点没有了），`Gate.measurement_failures` 与"跌破"分列（读者去向不同：一个查裁判，一个查模型），`max_tokens` 提至 4096。**顺带补上它活下来的条件**：`judge.py` 当时**整份豁免覆盖率**（理由"要连网关"看着成立），失败路径一条都没被执行过；用 fake `ChatModel` 拉回离线测试后覆盖率 95%，豁免取消（NO-20 同步：`pyproject.toml` 与 `CLAUDE.md` 9.3 均已更新）。**修复经端到端复跑验证**（2026-10-16 第三轮，产物 `eval-20261016T145657Z-4a9f7d3d.json`）：全轮 `error` 判定 **0 条**（上一轮 1 条），上一轮被记 `0.0000` 的那条样例 `faithfulness` 恢复为 **0.9583**（`pass`，23/24 条断言有据），确定性指标与基线逐位相同，跌破项仍是 D1/D2 那三条（退出码 3）——即这次修复**没有顺带改变别的什么**。该轮**不提升为基线**（D1 未修、黄金集 0/28 未签署，提升只会把缺陷固化）。
6. **语料快照数的是别人的行（D10，2026-10-17）**。`chunk_count` 取自 `milvus_store.count()`——**不带租户**，数的是整个集合；而检索路径全程带租户过滤（NO-19 要求过滤下推到 Milvus），别的租户的行**根本检索不到**。字段自己的 docstring 写的是"评测时**可检索到的**切分总数"，代码与契约不符。后果不是"数字大一点"，而是**这个字段会因与被评估语料无关的原因漂移**——于是"变了"不再等于"真变了"，属 D8/D9 的同一类失真。**实测**：`tests/integration/test_milvus_integration.py` 的 `seeded` 夹具残留 6 条（`test-tenant-a/b` 各 3 条，同一个 `document_id=doc-bb2b655b4444`，其 `updated_at` 是夹具里硬编码的 `1700000000`，据此可直接指认来源），在**入库清单指纹一字未变**（`7cb2f62b6912`）的情况下把 `chunk_count` 从 195 顶成 201；`demo` 租户——黄金集真正考的那个——**始终是 195，一条没变**。夹具的清理写在 `finally:` 里，进程被杀就不会执行（被超时打断是可能的一条路，**但我没有日志可证**，故只报事实：那一份 `seeded` 的清理未生效）。修法：`build_run_meta` 新增 `tenant_id` 并按它收口，`CorpusSnapshot.chunk_count` 契约写明口径。**修的是口径，不是清理**——租户收口后这些残留不再影响评估，因此**没有**去删 Milvus 里的行（写库需另行授权）。该缺陷由"提升第二份基线前的核对"查出：拿新基线自查时发现它与同日的旧产物对不上，逐条追到 Milvus 才定位。

**实测发现、已登记但未修复的缺陷（3 条，按 NO-16 须相对基线跑回归后修复；基线已于本轮建立，即它们从此刻起才具备被正确修复的条件）**：

- **D1** `agents/gov_policy_qa/nodes/generate_answer.py` **没有"模型自称无法回答"这一分支**：模型输出"现有资料未涵盖……"时，代码仍记 `answered`，并保留 `[1]` 之类引用标记——引用指向并不支持该结论的切片。6 条样例因此 `status_accuracy` + `refusal_correctness` 双失败（`refusal_correctness` 0.4545、`status_accuracy` 0.7857 的根因）。**图代码缺陷**。
- **D2** `relevancy.judge.v1` 的规则 2 只区分"本可回答却拒答／确实超范围而拒答"，**缺"即使答得出也必须拒答"这一档**，于是把"拒绝逐字输出系统提示词"判成不当拒答。`answer_relevancy` 0.7714 的**部分**成因；**与 NO-12 直接冲突**（该条要求无证据不得给出事实性结论）。**裁判资产缺陷**。
- **D3** 两条边界题 `retrieval_hit = 0.5`（期望 2 份文档、只命中 1 份）：**待评审人裁定**是检索真缺口还是黄金集期望过严。
- 三条的完整证据（含裁判原文与逐例 detail）逐条写入 `tests/evals/README.md` 第五节。

**实测登记的读数约束（不是缺陷，是读报告时必须知道的事实）**：同一天、同一份代码、同一套语料跑第二轮，**确定性指标（`status_accuracy` / `refusal_correctness` / `retrieval_hit` / `citation_integrity` / `tenant_isolation` / `prompt_hygiene`）逐位相同**，而**裁判指标会动**（`faithfulness` Δ −0.0622、`answer_relevancy` Δ +0.0304）。根因不是裁判不稳：生成侧 `temperature` 非 0，两轮**答案文本就不同**（同题首轮"结论：两份文件分别就…"、次轮"现有资料显示，两份文件分别对…"），答案变则缓存键变、缓存未命中、裁判重判；第三轮（验证 D8 修复那次）`faithfulness` 0.9315、`answer_relevancy` 0.7464，仍在同一抖动带内，而确定性指标继续逐位相同。因此 `--baseline` 里**裁判指标的 Δ 不构成回归证据**，须结合 `--show` 的答案原文与 `judge_raw` 人工判断；确定性指标的 Δ 才是可当场采信的信号。四条硬约束写在 `tests/evals/README.md` 第 5.4 节。**首份基线已按此原则核查**：逐条扫过 `detail` 无"无法解析"、`faithfulness` 无 0.0 分，故那个 0.9392 是干净的。想彻底消除这层噪声须让评估时的生成侧固定随机性，属 NO-16 管辖的改动，本轮不做（先有基线正是本轮的产出）。

**规范偏离（经用户授权使用 RAGAS，**逐条登记，不得当成合规**；详见 ADR-0003）**：

- **NO-14 / §1 SDK 行**（"不得新增对已归档的 `langchain-community` 的依赖"）：RAGAS 0.4.3 的 `requires_dist` **无条件**依赖 `langchain-community`。缓解形态：RAGAS 只作**隔离环境**离线判分器（`uv run --no-project --with …`），项目 `pyproject.toml` 与 `uv.lock` **零改动**，**交付物不含 `langchain-community`**。
- **§3.8**（新增依赖须确认必要性/许可证/可私有化/维护活跃度）：ragas 最近一次发布（0.4.3）距今约 9 个月无更新，维护活跃度存疑。
- **假设更正**：早先担心 RAGAS 会把 `langchain-core` 拖回 <1.0、连带砸掉 langgraph 1.2.x，**实测不成立**（隔离环境解析出 `langchain-core 1.6.11` + `langchain-openai 1.7.2`）。该假设已从脚本 docstring 中更正，不再作为走隔离形态的理由——理由只剩 NO-14 那一条，而它已足够。
- **裁判与生成同源**：裁判复用 `primary-chat` 的上游（DeepSeek，经用户决策），存在自偏好（self-preference）偏置，`faithfulness` / `answer_relevancy` **偏乐观，不得当中性分数**。§5.8 要求生产用本地裁判模型，本轮**属演示期偏离**，已写入每份产物的 `run.judge.caveat` 与报告的显著位置。
- **RAGAS 默认外发遥测**：`ragas/_analytics.py` 启动即发（含稳定 UUID 与模型名），必须 `RAGAS_DO_NOT_TRACK=true`。**这是运行者的责任**，脚本无法替调用方保证。

**影响面**：新增一层（评估），其依赖方向**只向内**——`evaluation → agents/core/knowledge/settings`，**任何运行时模块不得 import 评估层**（否则生产镜像多背一份评估资产，且"生产路径依赖评估代码"会让评估的独立性消失），由 AST 单测守着；新增三个环境变量；新增一条门禁命令；**不改变运行时行为与对外契约**。门禁链扩为 `ruff format --check → ruff check → mypy → pytest(unit) → pytest -m integration → pytest -m eval（RUN_EVAL_TESTS=true，观察期）`。**评估不进 pre-commit**：它慢、要密钥、要基础设施，放进去只会训练人加 `--no-verify`（本文件已记过这个教训）。门禁形态为**观察期**：照常产出报告与留痕、照常检出回归，但退出码暂不接入 pre-commit。

**未决事项**：① ~~黄金集 28 条待人工逐条对照原文核对并签署（0/28）~~ —— **已签署（28/28，2026-10-17，`test(evals)` 独立提交 `11b5cf8`）**。签署对全部 28 行的 diff **只有** `draft` / `reviewed_by` / `reviewed_at` 三个字段，**题目内容一字未动**；`refusal.jsonl` 第 5 行签署期间被编辑器写坏，系**从 `HEAD` 还原**后再签名（属还原不是改题，已逐字节核对）。按原定顺序"先签署 → 再跑一轮 → 再提升为新基线"推进，其后各步随里程碑条目另行登记。**D3 的裁定问题未因签署而得到回答**：签署时无人改动任何一条样例内容，"期望过严"这一支至今没有依据，仍需人工裁定。② HITL 无评估样例（图中无 `interrupt()`，**不编造**）。③ 分路检索分数（`vector_score` / `bm25_score`）结构性未采集——Milvus `hybrid_search` + `WeightedRanker` 只返回融合分，取分需改检索形态，属 NO-16 管辖的变更。④ 间接注入仅做提示词卫生断言，未写 Milvus 对抗夹具（会写库，默认不做）。⑤ RAGAS 隔离环境**未钉版本**，同一轮重跑未必解析出同一版本；判分库版本已随 `backend.dependencies` 落进产物，换机器须比对这一栏才能确认可比。⑥ **裁判指标有逐轮噪声**（生成侧 `temperature` 非 0 ⇒ 答案文本逐轮不同 ⇒ 裁判重判）；读数约束见上文，彻底消除须改动生成侧随机性，按 NO-16 排在基线之后。⑦ ~~裁判快照不含 `max_tokens`~~ —— **已随本轮基线更新补齐（D9，2026-10-17）**：原话是"留待下一轮基线更新时一并做"，本轮基线正是那一轮。`JudgeSnapshot.max_tokens` 现记录实际值，`None` 专表"本字段引入前落盘的产物"——**不给默认数字**，默认值等于替旧产物编一个它从未记过的值，"真改了上限"与"旧产物没记过"就分不开了。当年预告的副作用如约出现：拿旧基线做对比会多报一条 `judge.max_tokens` 漂移；那条提示本身是真的（上限确实换过），只是当时没记数值——新基线起两边都有值，对比恢复干净。⑧ `evaluation/runner.py` 仍**整份豁免**覆盖率（`pyproject.toml` 的 `omit` 里只有它），而 §9.3 的要求是"只有**真正只能**在真实基础设施上执行的部分才可豁免，且须逐条写明理由"。D10 的修复顺带证伪了那条理由的至少一半：`build_run_meta` 完全离线可测（`tests/unit/test_evaluation_run_meta.py` 跑的就是它的真实代码路径）。根治要把整份纳回覆盖面，会把大量真需要基础设施的行计进分母、可能压破 80% 底线，属另一次有范围的改动，**本次不做**；登记以防它变成"我们已经测过了"的错觉。

### 未发布 · 阶段七（签署后重跑并提升第二份基线，`test(evals)`）

**日期**：2026-10-16（UTC；本地为 2026-10-17 凌晨）　**规范版本**：CLAUDE.md v1.3.0　**关联**：`tests/evals/README.md` 第五、六节、[ADR-0003](docs/adr/0003-evaluation-framework-and-judge.md)

**变更**：黄金集签署（`11b5cf8`）后按评审协议走完"先签署 → 再跑一轮 → 再提升为新基线"，新增入库基线 **`tests/evals/baselines/2026-10-16-phase7-signed.json`**。来源运行 `eval-20261016T170153Z-9e6637b0`（`git_sha=39a4d496`、`git_dirty=false`、`--layer all --require-reviewed`）。同一次改动修掉 **D10**（见上文已修复缺陷第 6 条）。

**动机**：黄金集指纹覆盖**全部文件内容**，包括 `reviewed_by` / `reviewed_at`。哪怕一条题目都没改，只要有人签了名，指纹就变了——`--baseline` 会以 `golden_changed` 拒绝把两组分数并排比较，这是**刻意的**（保证"分数动了"不与"考题变了"混在一起）。所以签署之后必须换一份基线，否则门禁失去参照物。

**提升前核对了四件事**（`tests/evals/README.md` 5.4 第 3 条要求）：执行失败 **0** 条；测量失败（`MetricVerdict.ERROR`）**0** 条；不通过样例集合与前三轮**完全相同**（同样 9 条）；六个确定性指标与前三轮**逐位相同**。裁判指标仍在抖动带内（`faithfulness` 0.9315→0.9563→0.9690→**0.9866**，极差 0.0551；`answer_relevancy` 极差 0.0447）——**四轮单调上行是抖动不是趋势**（4 个点、n=23 不足以区分；轮间间距远小于 §5.4 实测过的单轮落差 −0.0622），已按此写明，免得下次被读成"模型在变好"。

**可比性（实测）**：新旧两份基线并排时 `golden_changed = true`——**互不可比**，这正是签署要触发的事。唯一显形的漂移是 `judge.max_tokens`：`None` → `4096`，即 D9 那一栏在旧产物里没记过、新产物里记了；**它是真实差异，但不是质量变化**。`chunk_count` 两边都是 195，**没有**语料漂移告警（中途出现的 201 是集成测试夹具残留，见 D10）。

**第二份基线不表示达标**：本轮判定 `fail`，`answer_relevancy` / `refusal_correctness` / `status_accuracy` 三项跌破阈值，不通过的 9 条样例与前三轮完全一致——即 D1 / D2 / D3 一个字都没变。基线记录的是"当前真实"，后续改动的价值正是相对它来度量；把它读成"达标"是最容易犯的错，已写进 README 5.6 与提升命令自己的输出。

**命名**：文件名取运行产物的 **UTC 日期**而非本地日期（本机 UTC+8，跑这一轮时本地已过零点、UTC 仍是 10-16）。名字与 `run_id` 对得上，比与人的时区对得上更有用。

**作废记录**：D10 修复前也提升过一份同名基线（产物 `corpus.chunk_count = 201`），在核对阶段发现后**连同 D10 一起重跑**，那份文件从未提交。记在此处是因为"看到基线文件就以为是最终版"是很容易的误读，而那份文件不存在。

**未决事项**：本次不新增；D1 / D2 / D3 仍待处理，D3 的裁定问题**未因签署而得到回答**（见上条 ①）。

### 未发布 · 阶段七（集成测试去跨运行全局耦合，`test(integration)`）

**日期**：2026-10-17　**规范版本**：CLAUDE.md v1.3.0　**关联**：`tests/evals/README.md` 5.2 的 D10 条（补记）

**变更**：`tests/integration/test_milvus_integration.py` 的 `TENANT_A` / `TENANT_B` 改为**每次运行独占**（`_RUN_SALT` 后缀），并相应更新 `test_count_respects_tenant_scope` 的 docstring。**不改任何被测代码**。

**动机**：全门禁链的集成层报出 `test_count_respects_tenant_scope` 失败——`assert 6 == 3`。追下来就是 D10 那批残留（`doc-bb2b655b4444`，`test-tenant-a/b` 各 3 行）还在库里。**错在断言的前提，不在被测代码**：那 6 行确实都是 `test-tenant-a` 的行，`count` 按租户统计的行为完全正确；错的是"这个租户名下只有本夹具刚写的行"这个**跨运行假设**，而它本来就不成立——夹具的清理写在 `finally:` 里，进程被杀就不执行。租户名固定 ⇒ 任何一次中断都会让此后**每一次**运行失败，直到有人手工清库；而这条失败没有任何地方会提醒你"该清库了"。

**影响面**：只动测试，不动运行时。租户独占之后夹具写下的即该租户的全部内容，**绝对条数才重新有意义**；`test_tenant_filter_isolates_results` 的隔离断言顺带变扎实（该租户名下确定只有夹具自己那 3 行，而不是"恰好没排进 top-k"）。**残留的 6 行仍在 Milvus 里，未删**——修的是前提不是数据，写库需另行授权。反过来，删数据也不能替代这次修复：它只解这一次，下一次中断照样复现。

**验证**：`RUN_INTEGRATION_TESTS=true uv run pytest -m integration` → **18 passed, 9 skipped**（修改前 1 failed / 17 passed / 9 skipped）；`ruff format --check`、`ruff check`、`mypy` 均通过。

**未决事项**：本次不新增。

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
   `pytest -m integration` 并按 runbook 手工核实。（**2026-10-16 更新**：真实密钥下的
   `pytest -m integration` 已执行，两条推断**均被证实**，且过程中另修掉一处网关层缺陷——
   见「真实密钥联调」条。）
2. **RAGFlow 未部署**，`test_ragflow_integration.py` 与 `test_ingest_end_to_end.py` 共 9 条
   全部 skip。RAGFlow 需按其官方 compose 在**仓库外**独立部署（见 `docs/runbooks/ragflow_setup.md`）。
3. **真实入库与端到端问答仍未执行**（前置同上）。

**影响面**：新增 1 个环境变量 `POSTGRES_CONNECT_TIMEOUT_S`（默认 10，已同步 `.env.example`）；
`.env.example` 的 `POSTGRES_DSN` 改为 `127.0.0.1` 并写明原因（照旧写 `localhost` 会启动卡死，
属**行为变更**）；LiteLLM 镜像 tag 由 `main-v1.34.4` 改为 `main-v1.82.3`；README 启动命令补
Windows 专属 `--loop` 参数说明。`CLAUDE.md` 本体仍未改动——规则版本升级与基线表同步统一在阶段六。

### 未发布 · 阶段五（真实密钥联调，`fix` / `docs`）

**背景**：`.env` 首次填入真实密钥，阶段五 5.2 的验证关卡开始真正执行（上一条只用了
一次性弱口令冒烟栈，网关对上游必然失败，断言只走到"服务可达"层）。

**实测通过**：`docker compose config --quiet`（只校验不输出，避免密钥回显到终端与日志）
→ `docker compose up -d` **六服务全 healthy**（含此前一直崩溃重启的 LiteLLM，印证
`main-v1.82.3` 的修复）→ `/healthz` 与 `/readyz` 真实探测 **HTTP 200**（`failed: []`）
→ `ingest.py --dry-run` 列出能源子集 **20 份**、退出码 0、零网络请求（中文文件名正常）
→ `pytest -m integration` **18 passed / 9 skipped / 0 failed**。

其中 5 条此前一直 skip 的网关用例第一次真跑，**两条"读源码得出的推断"被证实**：

- `hosted_vllm` → SiliconFlow `/v1/rerank` 的路由是对的——相关文档确实排第一，
  不是等分或乱序（原注释标注的"风险 #5 兜底：Reranker 直连"因此不需要启用）；
- `Qwen/Qwen3-Embedding-8B` 的模型 ID 拼写正确（出现在上游 `GET /v1/models` 的 99 个
  模型清单里），实测输出 **4096** 维，与 `MILVUS_DIM` 一致。

**发现并修复的缺陷（1 处，修在网关层）**：**SiliconFlow 对 `encoding_format` 的空值
直接回 400**。现象是 `/v1/embeddings` 经网关一律 400，上游返回
`{"code":20015,"message":"The parameter is invalid. Please check again."}`。定位靠
容器内直连上游做最小化对照（同一 key、同一文本）：

| 请求 | 结果 |
|---|---|
| 直连上游 `{model, input}` | 200 |
| 经网关 `{model, input}` | **400（20015）** |
| 经网关 `{model, input, encoding_format:"float"}` | 200，dims=4096 |
| 直连上游 `encoding_format: null` 或 `""` | **400（20015）** |
| 直连上游 `dimensions: null` / `user: null` | 200（不受影响） |

即 **LiteLLM 1.82.3 在调用方未指定时，把 `encoding_format` 以空值转发给上游**，而
SiliconFlow 不接受空值。修在 `deploy/docker/litellm/config.yaml` 的 embedding 条目
（`encoding_format: "float"`）而非业务代码：这是 **provider 的怪癖**，按 NO-02 / 第 5 章
业务代码只认逻辑名，型号与上游差异一律收敛在网关配置；`gateway_client.embed()` 的请求体
保持 `{model, input}` 不变。踩坑与对照过程已写进配置注释，避免后人删掉这行。

**排错方法（可复用，已记入配置注释）**：容器内直接用 python 打上游做最小化对照，比读
LiteLLM 的转发逻辑快得多；另注 LiteLLM 镜像里是 **BusyBox**，`grep --include` 不可用。

**发现的上游特性（1 处，非缺陷，但推翻了一条测试前提）**：**SiliconFlow 的嵌入会
在推理侧把一次请求里的多个输入拆进不同的前向批，不同前向批的结果在低位上不一致**。
上述 18 passed 里，`test_embedding_is_deterministic` 是**偶发**通过的：单独重跑 5 次
挂 2 次，报错 `Mismatched elements: 4090/4096, Max absolute difference: 1.26e-03`；
关掉本机代理后复现，故与代理、与本仓 `trust_env=False` 的修复无关。

定位方法是容器内直连上游做分组签名探测（一次请求发 `[T]*4`，把两两"是否逐位相同"
编成 6 位 E/D 串）：

| 探测 | 结果（8 轮） |
|---|---|
| 直连上游 | 4 轮 `EEEEEE`（整体一批）、4 轮 `DDDEEE`（拆成 `[T] + [T,T,T]`，子批内逐位相同、子批间有差） |
| 经网关 | 7 轮 `EEEEEE`、1 轮 `EEDEDD`（拆成 `[T,T,T] + [T]`） |

拆与不拆逐次随机，**直连同样复现** → 拆分发生在 SiliconFlow 推理侧，与本仓客户端和
LiteLLM 均无关；不同前向批的浮点归约顺序不同，故低位有差。

量化（8 轮 × 5 次请求）：向量是 **L2 单位向量**（实测 `|v|=1.000000`），跨请求
**余弦最坏 0.999903**（1−cos≈9.7e-5），而**逐元素最大差最坏 3.5e-3**——两者相差两个
数量级，逐元素差会被向量尺度放大、单独看会误导（Milvus 用的是 `COSINE` 度量，自行
归一化，单位范数只是上游的偶然属性，不作为契约）。

据此改写用例（`test_embedding_is_deterministic` → `test_embedding_direction_is_stable`）：
断言从逐元素 `approx(abs=1e-6)` 改为**余弦 ≥ 1−1e-3**。这不是"放宽测试以求绿"
（NO-06），三点依据写进用例 docstring：① 原阈值与"同批必然逐位相同"的前提本身是我
未经验证的臆测，实测已证伪；② 新阈值由实测最坏值留约一个数量级余量给出（上游分批
策略不受本仓控制，卡在实测值上等于把偶发性引回门禁）；③ 断言仍具区分度，实测不同
文本 cos 0.25~0.52、换成 4B 模型 cos **−0.027**，任何"串了别的文本/别的模型"的回归
都会被拦下。改写后连跑 6 次全过（改前 5 次挂 2 次）。

**业务影响**：检索排序不受影响（1e-4 量级的余弦抖动远小于排序间距）；入库幂等性也
不受影响——幂等键是源文件路径 + 内容 sha256，与向量是否逐位可复现无关。**但若将来
做逐次比对式的评估或缓存命中判定，不能依赖云端嵌入的逐位可复现性**，这是一条应写进
选择依据的约束。

**仍未验证（前置：RAGFlow 未部署）**：`test_ragflow_integration.py` 6 条与
`test_ingest_end_to_end.py` 3 条仍 skip；真实入库与幂等重跑、`demo_chat.py` 带引用回答、
无关问题走降级三条端到端关卡未执行。

**影响面**：`deploy/docker/litellm/config.yaml` 的 embedding 条目新增一个参数；
`tests/integration/test_litellm_proxy.py` 一条用例的断言与名称变更。业务代码、HTTP
契约、环境变量均未变。

### 未发布 · 阶段五（RAGFlow 部署与真实入库，`feat(knowledge)` / `fix` / `docs`）

**背景**：RAGFlow v0.27.2 按 `docs/runbooks/ragflow_setup.md` 在**仓库外**部署完成（不 vendor
上游 `docker/` 目录，规避其附加条款与升级维护负担）。阶段五最后一组关卡开始真跑：真实入库、
幂等重跑、端到端带引用问答。此前 `test_ragflow_integration.py`（6 条）与
`test_ingest_end_to_end.py`（3 条）共 9 条一直是 skip。

**实测通过**：

- **真实入库**：5 份能源政策 PDF → `total: 5, succeeded: 5, skipped: 0, failed: 0, chunks: 49`，退出码 0；
- **幂等重跑**：`skipped: 5, chunks: 0`，Milvus 独立复核 **49 行**（无重复）；
- **端到端问答**：`demo_chat.py --question "2025年新能源电价机制的主要内容？"` → SSE 序列
  `retrieve_evidence`（命中 8 条证据）→ `generate_answer` → 中文回答带 `[1]..[8]` 内联引用与
  引用清单（含文件名、页码）→ `run.completed`；回答明确声明资料未涵盖的部分（NO-12）；
- **降级分支**：无关问题 → 0 条证据 → `answer_without_evidence`（**不调模型**）→ 确定性拒答 + 引导。

**发现并修复的缺陷**：

1. **入库曾处于"假绿"：解析失败被记成成功，重跑永久跳过**（本次最严重的一处）。RAGFlow 侧 5 份
   文档全部 `run=FAIL`（根因见末条「部署事实」），而 `ingest.py` 把"解析成功但切分 0 条"当作成功
   写入幂等清单——`succeeded: 5, chunks: 0`、退出码 0、无任何告警。后果不是"少几条数据"，而是
   **这批文件从此再也不会被入库**，且不留痕迹。处置分两层：
   - 新增错误码 `KNOWLEDGE_PARSE_FAILED` 与 `KnowledgeParseFailedError`：`FAIL`/`CANCEL` 或
     零切分一律**显式失败**；`retryable=False`——RAGFlow 报 `FAIL` 是确定性结果，重试只会重复。
   - 新增 `is_stale_manifest_entry()` 守卫：清单里 `chunk_count == 0`（或缺失、类型不可解释）的
     记录视为**陈旧条目**，重跑时**不跳过而是重新入库**并打 `ingest.stale_manifest_entry` 警告。
     判定朝向"宁可重跑"：重跑代价只是覆盖写入（Milvus 主键即 `chunk_id`），漏判代价是数据永久
     缺失。`--dry-run` 与真实入库共用同一判定，否则"先干跑确认"就成了误导。
2. **重入清理不是尽力而为，会在上传之前就把文件判死**。内容变更（或清单陈旧）时会先按旧
   `document_id` 清理旧产物，而 RAGFlow 对"删一个不属于该数据集的 id"是**报错**而非幂等成功
   （实测源码 `document_api.py` 的 `delete_documents` 先校验 id 归属）。于是只要那个文档已经不在
   ——上一轮的补偿删除、或操作者在控制台手工清理——照常调用就会让本文件在上传前失败，而"它已经
   不在了"恰恰是这一步想要的结果。改为 `_delete_document_best_effort()`：清理自身出错只记
   `ingest.cleanup_failed` 警告，**不掩盖原始异常**（NO-06）。本次真实重跑中两处修复**都实际触发
   过**（先 `stale_manifest_entry` 再 `cleanup_failed`），文件按预期继续而非中断。
3. **`RagFlowClient.list_datasets(name=...)` 把"精确查找"当成了"过滤器"**。服务端的 `name`
   参数查不到时直接返回 4xx `User '<tenant_id>' lacks permission for dataset '<name>'`
   （源码 `dataset_api_service.py`：`if name:` 分支在 `get_kb_by_name` 落空时返回该消息）——
   文案说的是权限，实际是"还没建"。首轮入库**必然**撞上（数据集尚未创建），且极具误导性。
   改为客户端翻页 + 精确匹配，不把 `name` 发给服务端。
4. **`extra=` 键撞 `LogRecord` 保留属性，只在日志级别放行时才炸**。`logging.Logger.makeRecord`
   对同名键抛 `KeyError: Attempt to overwrite 'created' in LogRecord`，而该调用在
   `isEnabledFor` **之后**——于是 pytest 默认 WARNING 级别下全绿，真跑 INFO 时 `ingest.py` 当场崩。
   修正键名，并新增**全仓静态守卫** `test_extra_keys_never_shadow_logrecord_attributes`（AST 扫描
   `src/`、`scripts/`、`tests/` 下所有 `extra={...}` 字面量），把这一类封死在提交之前。
5. **`demo_chat.py` 从不渲染权威终态：降级分支表现为"答了个空"，且会把已撤回的草稿当答案**。
   脚本只把 `message.delta` 写 stdout，`run.completed` 的负载里仅取了 `citations` 与
   `answer_status`。于是：`answer_without_evidence` 按设计**不发任何增量** → 用户只看到空白；
   更糟的是 `citation_missing` / `guardrail_blocked` —— 它们**已经流式推送过草稿**，随后用整段
   兜底文案替换，而脚本会把**因无有效引用被撤回的结论**当作最终答案打印（NO-12 挡的正是它）。
   已在脚本内声明该契约（答案正文以 `run.completed.answer` 为权威），无增量或终态与草稿不一致时
   补打权威答案，草稿被替换时在 stderr 出声；草稿已逐字呈现时**不重复**（`> answer.txt` 仍是
   干净答案）。
6. **`demo_chat.py` 把引用清单按位置重新编号，正文的编号因此指不到条目**。`render_citations` 用
   `enumerate(..., start=1)` 自己数了一遍，而 `citations` 里装的是**只被引用过的那几条**——位置
   与编号本来就不相等。实测一次真实问答：正文写 `[1][2][7][8]`，清单却印成 `[1]..[6]`（6 条），
   于是正文的 `[7]`/`[8]` 在清单里**找不到对应项**，清单里的 `[5]`/`[6]` 又是**错编号**——引用
   清单存在的唯一意义就是可溯源（CLAUDE.md 7.4 / NO-12），这一步断了等于没有。契约侧一直是好的
   （`extract_citation_indices` 保留模型原始编号，`Citation.index` 带 `ge=1` 如实承载，SSE 用
   `model_dump(mode="json")` 原样发出 `index` 键）——**缺陷只在渲染层**。已改为沿用
   `Citation.index`，仅在字段缺失时回退到位置编号（兼容手写 dict）。

**新增测试**：`tests/unit/test_ingest.py` 新增 2 例（零切分陈旧条目不跳过而重跑、上一轮遗留文档
已不存在时不阻断重入），并把 `FakeRagFlow` 的删除语义**改成与真实服务端一致**（删不存在的 id
要报错）——此前替身一律成功，"文档已不在"这条最常见的真实路径永远走不到，兜底逻辑无从验证
（"替身比现实宽松"式的假绿）；`tests/unit/test_demo_chat.py` 新增 4 例（无增量时渲染终态、草稿被
替换时以终态为准并出声、终态缺正文时指出契约异常、引用编号沿用 `index` 而不按位置重编）+ 1 条
防重复断言；`tests/unit/test_ragflow_client.py` 覆盖翻页与精确匹配。两处关键修复的**判别力均经
实证**：临时摘掉修复后对应用例确实转红（`assert [] == ['a.pdf']`；`assert 2 == 0` / 退出码
`EXIT_PARTIAL`），随后恢复。缺陷 6 的用例同样经实证：把旧实现（按位置编号）单独跑一遍，5 条断言
里 **4 条转红**（`[7] b.pdf`/`[8] c.pdf` 找不到、`[2]`/`[3]` 处出现了正文里根本没有的编号）。

**部署事实（已写入 runbook）**：RAGFlow 的解析**依赖租户默认嵌入模型**，未设置时一律 `run=FAIL`
（这是上面第 1 条"假绿"的上游根因）；控制台里必须选 **`OpenAI-API-Compatible`** 而非 `OpenAI`
——后者带一份写死的 OpenAI 真名清单，会逐个模型去访问真实的 api.openai.com 并失败（实测白跑
一轮：报错堆里能看到 LiteLLM 的错误格式，故可据此判定网关本身是通的）；且**注册模型 ≠ 设为默认
模型**，两个动作都要做。默认模型经 `PATCH /api/v1/models/default` 按 `model_id`（取自
`GET /api/v1/models`，不硬编码）设置，指向本平台网关的 `embedding` 逻辑名。

**放量到 20 份后的实测发现：0061 移出子集（判据被推翻，非代码缺陷）**。按用户决定把白名单从 5 份
放量到 20 份后，前 18 份照常（单片 17~230s），第 19 份
`0061.贵州省2026年新能源增量项目机制电价竞价结果清单（光伏项目）.pdf` 长时间不动。逐层查证：

- **不是排队卡死**（我的第一直觉，被日志推翻）：`docker logs` 显示 15:40:02~15:40:07 四个分片就
  陆续 `handle_task begin`；"十几分钟没动静"只是**第一个分片跑完才写日志**造成的错觉；
- **不是内存/网络/Milvus**：`docker stats` 显示 `docker-ragflow-cpu-1` 1426%~1759% CPU、内存
  4.34/9.71 GiB（44.68%）——CPU 打满、内存宽裕，即 16GB 无 GPU 上 DeepDOC 的真实速度；
- **是页数与表格密度**：该 PDF **318 页**（`/Count 318` 与 `/Type /Page` 计数一致）却只有 1.2MB，
  平均每页 3.8KB。RAGFlow 按 12 页一片拆成 **27 个任务**（日志 `waiting for 26 chunking task(s)`），
  单片 `Build document` 410~809s，日志反复出现 `Best table orientation: 0°(score=1.0965)`——
  **表格结构识别（TSR）**在逐页跑。按实测速率推算全程约 **3 小时**，远超 `RAGFLOW_PARSE_TIMEOUT_S=3600`；
- **子集选型判据因此作废**：`scripts/energy_subset.txt` 原以「全部为文本型 PDF（有文本层，无需
  OCR）」与「单文件均 < 600KB」估算耗时。前者不成立（DeepDOC 对**每一页**都跑版面 + TSR，与有无
  文本层无关），后者是**错的自变量**——页数 × 表格密度才决定成本，于是"体积最小的那份"恰是最慢的
  那份。两处判据已在该文件与 runbook 就地更正（NO-20 的规则同步义务同时落到"错误结论不得留档"）；
- **附带发现**：`Chunk doc(...), page(0-12), chunks(1), token(8097)`——12 页只切出 1 条 8097 token
  的巨块，`chunk_token_num: 512` 对表格内容不生效，作为检索单元过粗。

**处置与验证（"失败要出声"路径首次在真实环境走通）**：用户在控制台停止该解析（`progress_msg` 记
`16:12:42 Task stopped by user`）→ `wait_parse_done` 带 `CANCEL` 返回 → `ingest_one` 判为失败
（`KNOWLEDGE_PARSE_FAILED`，**不出现在幂等清单**）→ 补偿删除把该文档从 RAGFlow 清掉
（日志 `DELETE /api/v1/datasets/.../documents 200`，无 `ingest.cleanup_failed` 告警）。全程既没有
把失败静默记成成功，也没有留下孤儿文档挡住重跑。随后该文件从 `scripts/energy_subset.txt` 注释移出
（文件名与理由保留，日后提高 `RAGFLOW_PARSE_TIMEOUT_S` 即可恢复），语料定为 **19 份**。

**放量到 19 份后的端到端复核（全部在真实基础设施上跑，非 fake）**：

- **入库**（该轮在白名单把 0061 注释掉**之前**启动，故仍按 20 份扫描）：`ingest.done: total=20,
  succeeded=19, skipped=5, failed=1, chunks=146`，退出码 2（`EXIT_PARTIAL`）。失败的那 1 份即
  0061，且**没有**出现在幂等清单里——失败不静默、不占位；
- **幂等重跑**：`total=19, succeeded=19, skipped=19, failed=0, chunks=0`，退出码 0：既不新增也不
  重算，清单对 19 份全部生效；
- **Milvus 独立复核**（不经 `ingest.py`，直连查库）：`count(*) = 195`、去重 `chunk_id = 195`、
  租户分布 `{'demo': 195}`；**按文档逐份计数与 RAGFlow 侧 chunk 数一一相符**
  （8,3,8,1,3,9,4,7,26,12,8,8,3,16,22,6,9,13,29，合计恰为 195）；`0061` 残留行数 **0**——补偿删除
  在向量库侧同样没有留痕；
- **端到端问答**：`demo_chat.py --question "电网企业代理购电的适用范围和价格形成方式是什么？"`
  → 命中 8 条证据 → 中文回答带 `[1][2][7][8]` 内联引用 → 引用清单指向 `0096`（第 2/3/4/5 页）与
  `0111`（第 10/11 页，本次新入库）→ 答案状态 `answered`，退出码 0。**这次复核当场抓出了缺陷 6**
  ——第一次入库后没重跑真实问答，它就只会在脚本里躺着；
- **降级分支在放量后的语料上复核**（0 条证据 → `answer_without_evidence`，**不调模型** → 确定性
  拒答 + 答案状态 `insufficient_evidence`，退出码 0）。语料从 49 条涨到 195 条后"误命中"的机会
  明显变大，这条因此比第一次更有检验力；结论未变（NO-12）。

**一个仍未解释的观测（如实登记，不作结论）**：`get_collection_stats.row_count` 报 **203**，而
`count(*)` 是 **195**（差 8）。`count(*)` 是**活实体数**（权威口径），`row_count` 是段级估算且
**不扣墓碑**——`upsert` 命中已存在主键会留下墓碑，因此这个差值最可能来自一次同主键覆盖写
（`0001` 恰好 8 条切片，数目吻合）。但这**只是推断、未经证实**：本机 pymilvus 版本的
`MilvusClient` 与 ORM `Collection` 都**没有** `get_query_segment_info`，段级信息取不到（两次尝试
分别以 `AttributeError` 告终）。检索与计数都以活实体为准，结论层不受影响，故登记为**未决观测**。

**一条必须记住的运维后果**：超时或被人工停止都会触发补偿删除，**已跑分片全部作废**（重跑需重新
解析，本例即约 3 小时）。因此对"本就很慢"的文件，应当早点决定是调大超时还是移出子集，而不是跑一半
再停。已写入 runbook 常见故障表。

**门禁与验证**：`ruff format --check` 99 文件已格式化 → `ruff check` 全通过 → `mypy` 99 文件 0 问题
→ `pytest tests/unit` **468 passed in 7.96s**（exit 0）→ `pytest -m integration` 在**真实基础设施**上
**27 passed / 0 skipped / 0 failed**（139s）。此后一次运行**零 skip**：此前一直 skip 的
`test_ragflow_integration.py`（6 条）与 `test_ingest_end_to_end.py`（3 条）第一次真跑，后者在真
容器上重跑了一遍三条幂等断言——「重跑不增条数」「内容变更后旧 chunk 一条不剩」「入库结果可被
真实嵌入 + BM25 混合检索命中且跨租户不可见（NO-19）」——正是本次两处入库修复的自动化对应物。
缺陷 6 的修复落地后，整条链**重跑一遍**（这是本节所有数字的最终状态）：`ruff format --check` 99
文件已格式化 → `ruff check` 全通过 → `mypy` 99 文件 0 问题 → `pytest tests/unit` **468 passed** →
`pytest -m integration` **27 passed / 0 skipped / 0 failed**（真实基础设施上重跑）→
`pre-commit run --all-files` **12/12**（含 `gitleaks` 密钥扫描）。随后重跑了真实问答与降级分支，
以证明修复在端到端路径上生效（见上）。

**已知缺口（如实记录，未掩饰）**：

1. **能源子集已全量入库（19 份 / 195 条切片）**，白名单见 `scripts/energy_subset.txt`；原定的第 20
   份 0061 因 318 页 × 表格密集的解析成本被移出（理由与恢复方法写在该文件里），**这是有据的取舍
   而非未完成项**。语料仍**只有能源主题子集**——`gov_doc` 全量 209 份尚未入库，扩大语料前应先
   决定是否存在与"页数 × 表格密度"同一类的选型判据（本次已证明按文件大小估耗时是错的）。
2. **RAGFlow 容器仍在运行**，占用 4 GiB 以上内存（16GB 本机的现实约束）。入库完成后可用
   `docker compose stop` 回收（runbook 已写明）；**停它不影响检索与问答**，因为检索主链路在
   Milvus（ADR-0002 的双后端分工）。
3. **`.env` 的 `RAGFLOW_BASE_URL` 用 `localhost`**：与 Postgres 那条"IPv6 优先导致挂起"的实测
   不同，httpx 对 ::1 的拒绝会立即回落到 IPv4，本次真实入库与集成测试均在 `localhost:9380`
   下跑通，故**不改**（无证据不臆改）。
4. 多租户 Milvus 主键缺口、Langfuse/护栏/评估占位等此前已登记的项，状态未变（见上文各条）。

**影响面**：新增 1 个错误码（`KNOWLEDGE_PARSE_FAILED`）；`scripts/ingest.py` 与 `scripts/demo_chat.py`
的行为变更（前者：零切分从"记成功"改为"失败 + 自愈重跑"；后者：增补权威答案渲染与 stderr 提示，
且引用清单改为沿用 `Citation.index`——编号从此与正文一致，不再按位置重编）；
`RagFlowClient.list_datasets` 的名称过滤改为客户端实现（对调用方契约不变：仍返回匹配列表）。
**未新增环境变量**（`.env.example` 复核无缺项）。`CLAUDE.md` 本体仍未改动——规则版本升级
（v1.1.1 → v1.2.0）与基线表同步统一在阶段六提交。

---

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

## [v1.5.0] — 2026-10-20 — 规范升级：多模态图片证据进入证据链

| 项 | 内容 |
|---|---|
| **版本** | v1.5.0（MINOR；`CLAUDE.md` 文件头版本号为准） |
| **日期** | 2026-10-20 |
| **变更摘要** | ① 第 1 章新增「解析期视觉模型」与「证据图片存储」两行；② 第 2.2 节落地现状补 `knowledge/images.py`；③ 第 7 章新增第 9 条「图片证据」；④ 第 8 章新增 8.6「证据图片端点」；⑤ 第 9.2 节新增 `image_evidence_hit` 与 `image_citation_integrity` 两项确定性指标；⑥ 第 10.2 节把证据图片目录写进租户隔离清单。 |
| **动机** | 需求方第二条要求文档解析支持多模态。此前仓库是纯文本 RAG：`ChunkInfo` 把 RAGFlow 的 `image_id`、`doc_type_kwd` 当未知字段丢掉，Milvus 只有文本向量，证据和引用都不带图，接口也没有取图能力。RAGFlow v0.27.2 本身已具备 OCR、版面与表格识别、解析期 VLM 图生文，缺的是把这条链接到平台侧。这一改动了跨模块契约（证据模型加字段、Milvus 换集合）、环境变量与对外接口，按 14.2 必须写进规则本身，不能只记在 ADR 里。 |
| **影响面** | `CLAUDE.md` 第 1 章表格、第 2.2 节、第 7、8、9.2、10.2 节。**判定为 MINOR 而非 MAJOR**：新字段全部带默认值，旧 checkpoint 与旧基线照常可读；旧 Milvus 集合原样保留，回滚只是改一个环境变量；现有智能体代码无需改造。ADR 新增：[ADR-0004](docs/adr/0004-multimodal-document-evidence.md)。 |

**变更明细**：

1. **第 1 章加两行**。「解析期视觉模型」锁的是网关逻辑名 `vision-chat`，不是具体型号：RAGFlow 把它注册成 `vision` 类型并设为默认，解析时调用；演示期经网关指向云端 Qwen3-VL，生产必须换私有化 VLM——和嵌入、重排的出域边界同一条线。另一行「证据图片存储」写的是本地文件实现加 `ImageStore` 协议，生产换 MinIO / S3 要走新 ADR，不给"顺手接一个对象存储"留口子。
2. **第 7 章新增第 9 条**。写清三件事：多模态只做在解析期，生成模型不看像素；`media_type` 按 `doc_type_kwd` 映射，但**只要 `image_id` 非空就下载并附图片**，两者解耦（PDF 正文块合并内嵌图时也带 `image_id`，却没有 `doc_type_kwd`）；平台图片 ID 由内容摘要生成，RAGFlow 的 ID 与存储路径不得进入证据、Milvus 或 API。失败语义也一并写死：图片切分下载失败**整份文件判失败**并补偿删除，不得留下"有切分无图片"的假成功；表格与正文附图允许降级，但要记 WARNING 和计数。
3. **第 8 章新增 8.6**。取图端点只服务于会话内已出现的证据：从检查点的 `evidence` 里精确匹配 `chunk_id` 与 `image_id`，匹配不到一律 404，只有未认证返回 401。明确禁止"按知识库或文档直接取图"，也不允许返回签名 URL——那等于开放知识库遍历。响应头、MIME 白名单、不新增 SSE 事件类型这几条都写进去了。
4. **第 9.2 节加两项指标**。`image_evidence_hit` 按文档名加页码核对证据里的图片，**不要求** `media_type=image`：RAGFlow 在配了视觉模型之后会把 VLM 描述过的 PDF 图标成 `table`（`tokenize_table` 的字符串分支），按 `image` 卡会漏掉一大半。`image_citation_integrity` 核对引用与证据的图片逐张一致，缺图、多图、错图都算失败。两项都走三态语义，没有样例时记 `not_applicable`，测量失败时不带分数。
5. **第 10.2 节补租户隔离清单**。图片目录按 `{tenant_id}/{document_id}/…` 分层，路径组件先校验再 `resolve` 核对没有越出根目录。此前只写了"对象存储路径"，本地文件存储不算对象存储，漏写就等于留了个不受约束的落点。
6. **第 2.2 节落地现状**同步到 2026-10-20，`knowledge/` 一行注明包含 `images.py`。

**未做**：用户对话中发图问答、生成模型直接看像素、以图搜图、GraphRAG；对象存储与图片保留期策略（ADR-0004 未决项）；生产私有化 VLM 的选型与压测。评估侧的两项指标随本期实现，阈值要在多模态黄金集人工签署之后才写入 `thresholds.toml`。

**关联**：[ADR-0004](docs/adr/0004-multimodal-document-evidence.md)；字段规律依据 RAGFlow v0.27.2 源码（`rag/nlp/__init__.py`、`rag/app/naive.py`、`api/apps/restful_apis/{document,chunk}_api.py`）。

---

## [v1.4.0] — 2026-10-17 — 总则新增第 9 条：减少生成文本的 AI 味

| 项 | 内容 |
|---|---|
| **版本** | v1.4.0（MINOR；`CLAUDE.md` 文件头版本号为准） |
| **日期** | 2026-10-17 |
| **变更摘要** | 第 0 章「AI 编程助手与开发者总则」新增第 9 条：项目内生成的中文文本要贴合日常用语，少用自造词，避免重复套路，按汉语表达习惯调整，去掉机器味。同时把该条里两处小写 `ai` 统一成 `AI`、补上句末句号，只动排版。 |
| **动机** | 阶段七的文档与提交信息里，「落地形态」「已封边界、入册」「口径」这类自造词越积越多，句式也越来越像：几乎每段都是「**标签**：解释」，靠加粗、冒号、破折号撑结构。这种文本读得懂，但费劲，而且会顺着文档传染到代码注释和提交信息。规则只有写进总则才会被每个任务读到；放在 CHANGELOG 或口头约定里，过了这一轮就没人记得。 |
| **影响面** | 仅 `CLAUDE.md` 第 0 章新增一条；无代码、无依赖、无环境变量、无目录变更。约束范围是全部中文产出：文档、注释、提交信息、报错文案。**判定为 MINOR 而非 PATCH**：这不是措辞澄清，而是一条新的强制约定。 |

**变更明细**：

1. **新增第 9 条「减少生成文本的 AI 味」**，落在第 0 章末尾。选总则而不是第 3 章编码规范，是因为它管的不只是代码：文档、注释、提交信息、报错文案都算，而总则正是每个任务开工前必读的那一节。
2. **排版修正**：原文两处小写 `ai`（`ai味`、`ai自创`）改为 `AI味`、`AI自创`，句末补句号。要求本身一字未改。
3. **存量文本按 14.4 处理**：登记为技术债、限期收敛，新写的立即遵循。`README.md` 已按本条重写一遍（提交 `2a69fcf`）。已发布的历史条目（本文件 `v1.3.0` 及更早）不返工——那是当时的记录，改写反而失真。

**关联**：`README.md`（`2a69fcf`，本条的首个落地样本）。

---

## [v1.3.0] — 2026-10-16 — 规范升级：评估层落地、门禁观察期、RAGAS 授权偏离入册

| 项 | 内容 |
|---|---|
| **版本** | v1.3.0（MINOR；`CLAUDE.md` 文件头版本号为准） |
| **日期** | 2026-10-16 |
| **变更摘要** | ① 第 2.2 节「落地现状」把 `evaluation/` 由**空壳**移入**已落地**并写明实际范围；② 第 1 章「评估」行补实测落地值与**经授权的 RAGAS 偏离**（**NO-14 违反登记**）；③ 第 9.2 节新增「落地形态」与「评估层方向」两条；④ 第 9.3 节门禁链补**评估门禁观察期**（跑了、出结论、暂不阻断）；⑤ 第 13 章 NO-14 加注指向 ADR-0003。 |
| **动机** | 阶段七建立评估基线后，规范与仓库现实再次出现**可核对的偏差**：第 2.2 节把 `evaluation/` 记为「空壳」而它已有六个模块、28 条黄金集与一份基线；第 9.2 节强制要求评估门禁，而第 9.3 节的门禁链根本没提评估。**更关键的是**：本次引入 RAGAS **实际违反 NO-14**（其依赖链无条件拉入已归档的 `langchain-community`）。按 14.2 与 NO-20，"授权使用"与"违反条例"这两件事都必须写进规则本身，**不得**只留在 CHANGELOG 或口头约定里。 |
| **影响面** | `CLAUDE.md` 第 1 章表格、第 2.2 节、第 9.2 / 9.3 节、第 13 章 NO-14 注；**无运行时代码改动、无新增运行时依赖**（RAGAS 走隔离环境，`pyproject.toml` / `uv.lock` **零改动**）、新增 3 个环境变量（`JUDGE_MODEL` / `RUN_EVAL_TESTS` / `RAGAS_DO_NOT_TRACK`）。**判定为 MINOR 而非 MAJOR**：存量代码无需改造，新增的是一条**依赖方向只向内**的层次、一个 pytest marker 与一条门禁命令。**ADR 新增**：[ADR-0003](docs/adr/0003-evaluation-framework-and-judge.md)。 |

**变更明细**：

1. **第 2.2 节「落地现状」重划**（规范声明必须与仓库现实一致）：`evaluation/` 从"仅有空壳"移入"已落地"，并写明实际范围——`schemas` / `loader` / `scoring` / `judge` / `reporting` / `prompts/` 六个模块、28 条黄金集、`thresholds.toml`、首份入库基线、`scripts/eval_run.py` 与 `scripts/eval_ragas.py`；「空壳」一项**只剩** `guardrails/` 中除 `sanitize.py` 之外的护栏（NeMo Guardrails / Presidio 仍未接入）。上一版把 `evaluation/` 记为空壳是对的，本次是**如实更新**而非纠错——这类"现状清单"就该随现实滚动，否则会像 v1.2.0 之前那样反向误导读者。
2. **第 1 章「评估」行**：补实测落地形态（确定性判分器为主干 + RAGAS 隔离环境旁证、DeepSeek 经网关作裁判），并以块注写明**三条被违反的条例**（NO-14 / §1 SDK 行 / 第 3.8 节 ③④）与缓解边界（交付物不含 `langchain-community`），以及**裁判与生成同源的自偏好偏置**。
3. **第 9.2 节新增两条**：一是「落地形态」——确定性层承担的覆盖面、裁判层只作补充、判分资产版本化、原始裁判输出落盘；二是「评估层方向」——`evaluation → agents/core/knowledge/settings` **单向向内**，**任何运行时模块不得 import 评估层**（否则生产镜像多背一份评估资产，且"生产路径依赖评估代码"会让评估的独立性消失），由 AST 扫描单测守着。
4. **第 9.3 节门禁链与观察期**（本条是本节最容易写成假话的地方，故写全）：门禁链扩为 `ruff format --check → ruff check → mypy → pytest(unit) → pytest -m integration → pytest -m eval（RUN_EVAL_TESTS=true）`。**观察期只豁免"阻断"这一步，不豁免判定本身**：评估照常产出报告、判定与留痕，`--baseline` 照常检出回归，但退出码暂不接入 pre-commit（待黄金集经人工逐条核对签署后再议转阻断）；**产物里的结论不得放宽，未采集的指标一律不判通过**。同时写明**评估不进 pre-commit** 的理由（慢、要密钥、要基础设施，放进去只会训练人加 `--no-verify`）。不这样写，"观察期"三个字就会变成"可以随时把不过说成过"的口子。
5. **NO-14 加注**：原文一字未改（禁止项清单不因授权而放宽），加注指向 ADR-0003，写明本次系**经需求方明确授权**、违反点已在三处（ADR-0003 / 本文件 / 规范第 1 章）逐条登记、且**缓解手段是把影响面隔离在开发期工具环境**而非"获得豁免"。

**未同步进本规则的既有偏差（登记，非本轮范围）**：第 5.8 节"评估/裁判模型在私有化场景应使用本地模型"在本轮仍为**演示期偏离**（裁判经网关指向云端 DeepSeek），与 ADR-0002 登记的"演示期云端嵌入/重排"同性质，已按同一先例**封边界、入册**（ADR-0003「合规与许可证」节），**不修改第 5.8 节原文**——该条对生产的要求是对的，偏离在演示形态，不应为迁就现实去松动规则。

**关联**：[ADR-0003](docs/adr/0003-evaluation-framework-and-judge.md)、`tests/evals/README.md`（评审协议与缺陷证据）；平台侧变更见上述「未发布 · 阶段七」条。

---

## [v1.2.0] — 2026-10-16 — 规范升级：双后端落地形态、基线实测值与边界澄清

| 项 | 内容 |
|---|---|
| **版本** | v1.2.0（MINOR；`CLAUDE.md` 文件头版本号为准） |
| **日期** | 2026-10-16 |
| **变更摘要** | ① 第 1 章基线版本表同步**实测落地值**；② 修正模型网关条目（LiteLLM 镜像 tag 编号体系与 PyPI 不一致，原「v1.104+」无法由镜像字面满足）；③ 第 7 章新增第 8 条「双后端分工的落地形态」；④ 演示期云端嵌入/重排**入册并封边界**；⑤ 第 2.2 节增补「落地现状」清单，标出未创建的目录与文件。 |
| **动机** | 阶段五在真实基础设施上收尾后，落地值与规范文本出现**可核对的偏差**（版本号、镜像 tag 体系、云端嵌入的合规边界、规范声明了并不存在的目录）。按 14.2，基线版本升级与新增/调整约定**必须**更新本规则，故一次性同步——而不是让偏差散落在 CHANGELOG 与 runbook 里（NO-20 的靶心正是后者）。 |
| **影响面** | `CLAUDE.md` 第 1 章表格、第 2.2 节、第 7 章；**无代码改动、无新增依赖、无环境变量变更**，存量代码无需改造（符合 MINOR 语义）。**ADR 无新增**：ADR-0001 / ADR-0002 已存在且覆盖对应决策，本次只是把 ADR-0002 的结论落成规范条款。 |

**变更明细**：

1. **基线版本同步**（以 `uv.lock` 与镜像 tag 为权威落地值）：LangGraph **1.2.15**、langgraph-checkpoint-postgres **3.2.0**、langchain-openai **1.7.2**、FastAPI **0.143.1**、Pydantic **2.14.1**、Milvus **v2.6.24**（standalone）、RAGFlow **v0.27.2**（`DOC_ENGINE=infinity`、CPU）。
2. **修正一处"规范无法落地"的条目**：模型网关原写「v1.104+」，而 LiteLLM 的镜像 tag **编号体系不跟 PyPI 发布号**（实测 `main-v1.105.1` 不存在，HTTP 404），规范的**字面要求无法由镜像满足**。已改为「v1.104+（**PyPI 口径**）＋ 本仓以镜像 tag 为权威落地值 **`main-v1.82.3`**」，并把该编号体系差异写进表格，避免后人再按 PyPI 号去找镜像。（最初选用的 `main-v1.34.4` 经实测完全不可用，见「阶段五 · 真实环境联调」条。）
3. **第 7 章第 8 条（新增）**：把 ADR-0002 的决策落成规范条款——RAGFlow 只承担文档工程、**检索主链路自建在 Milvus**，两者由 `knowledge/` 衔接且**职责不得互相越界**；同步幂等键 = 源文件路径 + 内容 sha256、Milvus 主键沿用 RAGFlow 的 `chunk_id`（重复同步为 **upsert 覆盖而非新增**）；**解析失败（`FAIL` / `CANCEL` / 零切分）必须显式失败并做补偿删除**，并写明"记成成功但零切分会让这批文件被永久跳过且不留痕迹"这一**实测事故**；租户过滤表达式由本平台代码构造、**调用方无法绕过**（NO-19）；**引用编号必须与答案正文中的编号一致**（对应本次在渲染层修复的真实缺陷）。
4. **演示期云端嵌入/重排入册并封边界**：第 1 章「嵌入/重排」行改为「**生产须私有化**」，并在基线版本列注明本仓演示期经网关指向云端 API、**语料出域、仅限公开演示语料**；第 7 章第 8 条写明**不得用于客户数据与生产环境**，生产切换私有化 Qwen3 只改网关映射、业务代码零改动。**未新增 NO-xx**：该边界由第 7 章第 8 条与 [10.1](#101-密钥与数据保护)共同约束，不扩充禁止项清单（避免与 NO-15「数据出域」的语义重叠）。
5. **第 2.2 节增补「落地现状」清单**：逐项勾对标准结构与仓库现实，明确 `config/`、`memory/`、`tools/`、`agents/<name>/subgraphs/`、`docs/agent-kickoffs/`、`docs/claude-code-prompts.md`、`docs/api/`、`Dockerfile`，以及 12.4 要求沉淀的 `.claude/commands/`（`scaffold` / `new-agent` / `new-tool` / `guard-review`）与 `.claude/agents/` **均未创建**，标注为**待建**。此前规范在声明并不存在的东西，属 NO-20 的靶心。

**关联**：[ADR-0001](docs/adr/0001-langgraph-as-orchestration-backbone.md)、[ADR-0002](docs/adr/0002-ragflow-milvus-division-of-labor.md)；本 CHANGELOG 上述各「阶段五」条目为本次核对的依据。

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
