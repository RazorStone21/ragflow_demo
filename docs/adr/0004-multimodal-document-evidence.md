# ADR-0004：多模态文档解析与图片证据

| 项 | 值 |
|---|---|
| 状态 | Accepted（生效中） |
| 决策日期 | 2026-10-20 |
| 决策者 | 平台架构组；需求方已确认范围：文档解析多模态、图片可引用、演示期经网关调云端视觉模型 |
| 关联 | [CLAUDE.md](../../CLAUDE.md) 第 7 章（知识与 RAG）、7.5、第 8 章、9.2、10.1 / 10.2、NO-02 / NO-12 / NO-16 / NO-17 / NO-19 / NO-20；[ADR-0002](0002-ragflow-milvus-division-of-labor.md)、[ADR-0003](0003-evaluation-framework-and-judge.md) |
| 后续状态 | 不可就地修改；生产换对象存储或私有化 VLM 时，新增 supersede ADR 指向本篇 |

## 背景

草稿纸里甲方第二条要求"文档解析要支持多模态"。在本篇之前，仓库是一条纯文本 RAG：`RunRequest.message` 只收字符串，`ChunkInfo` 把 RAGFlow 的 `image_id`、`doc_type_kwd` 当未知字段丢掉，Milvus 只有文本向量，API 没有任何取图能力。扫描件 PDF、内嵌图片、表格与独立图片文件即使能上传，产出的也只有 OCR 文字，图片本身进不了证据链，回答里点不到出处。

RAGFlow v0.27.2 平台侧其实已经具备这些能力：DeepDoc 做 OCR、版面分析与表格结构识别；租户默认模型里可以注册 `vision` 类型模型，解析时把图片转成文字描述写进切分正文；切分配置里有 `layout_recognize`、`image_context_size`、`table_context_size` 等开关；切分接口会回 `image_id` 与 `doc_type_kwd`（取值含 `image`、`table`），并另有一个图片下载端点。本仓此前一项都没启用，也没消费。

所以这一篇要回答的不是"能不能做"，而是**接到哪一层、图片放在哪、证据契约怎么长、演示期的合规边界划在哪**。

## 决策

### 一、多模态只做在解析期，答案生成仍然只读文本

范围限定为入库链路：扫描件、PDF / DOCX 内嵌图片、表格、独立图片文件都经 RAGFlow 解析。图片在这一步由 VLM 转成文字描述进入 `content`，后续的嵌入、BM25、检索、答案生成全部沿用现有文本链路，一行不用改。

用户在对话里发图问答、让生成模型直接看像素、以图搜图、图像向量检索，本期都不做。理由很直接：生成侧看图要动提示词、模型输入契约与评估口径三处，而解析期转写已经能覆盖"资料里的图说了什么"这个需求，先把证据链打通。

### 二、VLM 经 LiteLLM 网关调用，演示期用云端模型

新增逻辑名 `vision-chat`，在网关配置里映射到云端视觉模型（SiliconFlow 的 Qwen3-VL 系列，具体型号实施时从 `/v1/models` 核实后落配置）。RAGFlow 把它注册成 `vision` 类型的自定义模型并设为默认，解析时自行调用。业务代码只认逻辑名，型号换动不碰代码（NO-02）。

**这是演示期偏离**：云端 VLM 意味着待解析的图片会离开内网。边界与 ADR-0002 登记的云端嵌入 / 重排相同——**仅限公开演示语料，不得用于客户数据与生产环境**。生产必须换私有化 VLM，RAGFlow 侧改模型注册即可，平台代码不变。

本机有 RTX 3060 Laptop，跑 3B 级别的视觉模型在显存上可行，私有化这条路径不是纸上谈兵；但本期还要同时跑 RAGFlow、Milvus、Postgres 与网关，16GB 内存先留给解析链路，VLM 走网关。本地私有化作为后续预研，不并进这一期。

### 三、图片资产落本地文件存储，用协议隔离

演示期把图片二进制存到 `local_data/evidence_images/{tenant_id}/{document_id}/{chunk_id}/{image_id}.{ext}`，由 `knowledge/images.py` 的 `ImageStore` 协议封装：`put` / `get` / `delete_document`，外加启动期的目录可写校验。所有文件 I/O 走 `asyncio.to_thread`（NO-05），写入用临时文件加 `os.replace` 保证原子性，读取前把路径 `resolve` 后校验没有越出根目录。

生产要换 MinIO / S3。本篇不引入对象存储依赖：演示语料量小，本地盘够用，而多一套基础设施就要多一份部署、备份与授权成本。换实现的落点是 `ImageStore`，调用方不感知。

### 四、图片 ID 由平台生成，RAGFlow 的 ID 不出库

`ImageRef.image_id = "img_" + sha256(图片字节)[:32]`，与 `sha256` 字段互相印证。RAGFlow 的 `image_id`（形如 `{dataset_id}-{chunk_id}`）只在入库下载时用，不写进证据、不写进 Milvus、不出现在 API 里。

对外展示的图片元数据只有：平台 `image_id`、`sha256`、MIME、caption、页码、可选 bbox。**不含**路径、URL、base64、存储 key、RAGFlow 内部 ID。这既是 NO-19 的要求，也避免把 RAGFlow 的租户标识与内网拓扑泄露给客户端。

### 五、证据契约加法式扩展

`knowledge/schemas.py` 新增 `MediaType`（`text` / `image` / `table`）、`ImageBBox`、`ImageRef`；`ChunkRecord` 与 `EvidenceItem` 增加 `media_type` 和 `images`，`Citation` 增加 `images`。全部带默认值（`MediaType.TEXT`、空列表），旧 checkpoint、旧基线、旧幂等缓存照常可读。

`Citation.images` 必须由 `build_citations` 显式从对应 `EvidenceItem.images` 复制。这一步漏了，API 就永远拿不到图，而类型系统不会提醒——单测专门锁这一条。

### 六、Milvus 新建集合，缺字段就启动失败

集合新增 `media_type`（VARCHAR 16）与 `images`（JSON）两个字段。默认集合名改为 `gov_policy_chunks_v2`，旧集合原样保留用于回滚，语料重入库一次。

`ensure_collection` 从"只校验向量维度"升级为"校验维度 + 必需字段"。集合存在但缺新字段时直接抛 `ConfigurationError`，错误信息给出新集合名与重入库命令。不自动删集合——那是不可逆操作，不该由一次启动触发。

### 七、入库失败的语义：图片缺失必须出声

先说清 v0.27.2 经典 `naive` 解析器实际给的字段（读源码核实，与"按 `doc_type_kwd` 一刀切"的直觉有出入）：

- `doc_type_kwd` 由 `rag/nlp/__init__.py` 的 `tokenize_table` 与 `doc_tokenize_chunks_with_images` 写入，不是所有切分都有。PDF 表格、DOCX 表格、XLSX/CSV 的字符串行为 `table`；PDF 图（未配视觉模型）的列表行、DOCX 图片为 `image`；PDF 正文块不带该字段，切分接口会回落成 `text`。
- 配了视觉模型之后，PDF 图由 `VisionFigureParser` 转成文字描述，再交给 `tokenize_table`，于是**也被标成 `table`**。这是 RAGFlow 自己的标注，本仓不替它改口——没有任何可靠字段能把"VLM 描述过的图"和真正的表格区分开。
- `image_id`（即 `img_id`，形如 `{dataset_id}-{chunk_id}`）只要切分带图就会写，与 `doc_type_kwd` 无关：PDF 正文块合并了内嵌图时也带 `image_id`，但 `doc_type_kwd` 为空。

因此映射规则是：`doc_type_kwd` 为 `table` 记 `MediaType.TABLE`，为 `image` 记 `MediaType.IMAGE`，其余（含 `text`、空、未知值）记 `MediaType.TEXT`；**只要 `image_id` 非空就下载并附 `ImageRef`，不看媒体类型**。评估的 `image_evidence_hit` 也不要求 `media_type=image`，理由同上。

失败语义按这个划分：

- `doc_type_kwd=image` 且有 `image_id`：下载、校验、落盘任一步失败，**整份文件判失败**并走既有补偿删除（Milvus 行、图片目录、RAGFlow 文档）。图片是这类切分的全部价值，缺了它入库等于入库一条没有正文的空壳。
- 表格切分（含 VLM 描述过的图）：`content` 保留 RAGFlow 给的 HTML 或描述文字；若带 `image_id`，截图尽力下载，失败只降级为 `images=[]` 并记 WARNING 与计数。文本内容本身可检索，降级有实际意义。
- `doc_type_kwd=image` 但没有 `image_id`：保留 `content`，`images=[]`，记 WARNING 与计数。
- 空 `content` 的切分：沿用现有跳过逻辑，**不造占位 caption**。占位文本会污染 BM25 与嵌入，还没法作为可核对证据。

### 八、取图端点是会话内的，不是知识库的

`GET /api/v1/threads/{thread_id}/evidence/{chunk_id}/images/{image_id}`。鉴权之后先校验会话归属，再从该会话检查点的 `evidence` 里精确匹配 `chunk_id` 与 `image_id`，匹配不到一律 404——包括路径非法、会话不存在、跨租户、块不在证据里、图片不属于该块、文件缺失。只有未认证返回 401。

这样做的目的有两个：一是客户端只能看到自己这轮问答真正用过的证据图片，不能拿 URL 遍历知识库；二是会话证据本来就有租户隔离，图片访问自动继承。

### 九、评估补两项确定性指标

`image_evidence_hit`：黄金集标注期望图片（文档名 + 页码），核对检索证据里是否带对应图片。`image_citation_integrity`：核对被引用且证据带图的条目，引用里的图片必须与证据一致，不多不少。

两项都进确定性判分层，零 I/O 零网络，和现有六个判分器同一形态。`SCHEMA_VERSION` 保持 1（纯增量字段），但黄金集增加 `expected_images` 后指纹会变，需新建基线。

## 备选方案与权衡

| 方案 | 权衡 | 结论 |
|---|---|---|
| 平台直接转发 RAGFlow 的图片端点 | 省掉转存，但把 RAGFlow 的租户标识、内部 ID 与可用性绑进对外契约；RAGFlow 停机即引用图全挂 | 否决 |
| 只存 caption，不存图片 | 实现最省，但"图片可引用展示"是需求方明确要的，caption 展示不了原图 | 否决 |
| 业务代码直连厂商 VLM SDK | 违反 NO-02，密钥撒进业务代码 | 否决 |
| 本期就上 MinIO / S3 | 生产形态，但多一套服务、备份与授权；演示期语料用不上 | 延后，协议先留好 |
| 新增 SSE 事件类型（如 `retrieval.image`） | 事件更细，但要改流式契约与精确事件序列测试，客户端也得同步升级 | 否决：图片随 `retrieval.evidence` 与 `citation` 自然带出 |
| 图像向量检索 / 以图搜图 | 真正意义上的多模态检索，但需要图像嵌入模型与双路索引，属另一期工程 | 延后 |

## 后果

**正面**

- 扫描件、内嵌图、表格与独立图片文件进入同一条检索链路，答案可以引用到具体图片，证据链从文字延伸到图片。
- 图片 ID 与存储路径与 RAGFlow 解耦，后续换对象存储只动一个实现类。
- 契约是加法式的，旧数据、旧基线、旧客户端不受影响。
- 多模态带来的失败面（下载失败、类型不对、超限）全部有显式判定与补偿，不会变成"成功但没图"的假绿。

**负面与风险**

- **解析成本上升**。本机是 16GB 内存加 RTX 3060 Laptop，但 RAGFlow 演示部署跑 CPU 模式（`DOC_ENGINE=infinity`），DeepDoc 逐页做版面与表格识别本来就慢，扫描件还要加 OCR 与 VLM 调用。入库只能用页数少、表格密度低的文件先验收。
- **云端 VLM 数据出域**（见决策二）。边界靠"公开演示语料"这条口径守着，不是靠代码。
- **本地文件存储没有冗余与生命周期管理**。磁盘损坏即图片丢失，重入库才能恢复；旧引用在重入库后取图会 404。
- **表格 HTML 会撑大 `content`**。token 数上升、嵌入成本上升，检索时也可能挤掉更相关的文本切分。先用评估观察，不做特殊截断。
- **RAGFlow 对独立图片有一条硬编码的跳过规则**：OCR 文本超过 32 字符时不再调用 VLM。文字密集的图片只能拿到 OCR 文本，拿不到图生文描述。这是上游行为，验收时避开，补丁列为后续。
- **旧引用会失效**。重入库后 `chunk_id` 全变，历史会话里的图片链接失效。客户端按占位图处理，runbook 里写明。

## 合规与许可证

- **NO-02**：VLM 经网关调用，业务代码没有厂商 SDK 与厂商密钥。
- **NO-12**：图片不能替代文本证据。检索命中仍要求 `content` 非空，无证据照常拒答；图片只是证据的附加展示。
- **NO-17**：仓库只放一张小于 50KB 的公开合成测试图；真实语料、真实图片一律不入库。pre-commit 的 2MB 大文件钩子照常生效。
- **NO-19**：图片目录按 `tenant_id` 分段；取图端点从认证上下文与会话证据双重推导，不接受调用方传路径。
- **10.1 / NO-15**：演示期云端 VLM 属已登记的出域偏离（与 ADR-0002 / ADR-0003 同性质），仅限公开演示语料。
- **许可证**：Qwen3-VL 权重为 Apache-2.0，演示期经 SiliconFlow API 调用，不随交付物分发；生产私有化部署须复核模型权重的许可证与商用条款。本地文件存储与新增代码不引入第三方依赖。

## 迁移与回滚

| 对象 | 处理 |
|---|---|
| 旧 checkpoint | `EvidenceItem.images` / `Citation.images` 有默认空列表，可读；旧证据取图返回 404 |
| 旧 Milvus 集合 | 缺新字段，启动时 fail-fast；新建 `gov_policy_chunks_v2` 并重入库，旧集合保留 |
| 旧入库清单 | 顶层 `version` 仍为 1；条目缺 `media_version` 即重入库，并沿用旧 `document_id` 清理旧 RAGFlow 文档，避免同名冲突 |
| 旧评估基线 | 产物结构版本不变，可读；黄金集指纹变化会让对比报 `golden_changed`，须新建基线 |
| 回滚 | 代码回到旧版本；`MILVUS_COLLECTION` 指回 `gov_policy_chunks`；入库清单换回重入库前的副本；图片目录可整体删除（旧代码不读） |

## 未决与后续

1. **云端 VLM 的具体型号与网关透传尚未实测**。实施时先 `GET /v1/models` 核实型号，再发一次带 `image_url` 的 chat completion 确认 LiteLLM 能透传，最后才配置 RAGFlow。
2. **本机私有 VLM 的可行性待试**。RTX 3060 Laptop 的显存跑 3B 级视觉模型没有理论障碍，但内存要同时负担 RAGFlow 与 Milvus，本期不切换。生产路径的预研从这里开始。
3. **表格截图是否值得长期保留**。表格的价值主要在 HTML 文本，截图只是兜底展示。若发现截图增加的成本大于收益，后续可去掉，不影响证据契约。
4. **对象存储迁移**。生产换 MinIO / S3 时新增 supersede ADR，`ImageStore` 加一个实现即可。
5. **图片保留期**。重入库会让旧 `image_id` 永久失效。是否按文档版本保留历史图片，等实际出现"旧会话回看"的需求再定。
6. **RAGFlow 的 32 字符跳过规则**。属上游硬编码，能否通过 `image_context_size` 之外的配置绕过尚未验证；文本密集图片本期只保证 OCR 文本可检索。
