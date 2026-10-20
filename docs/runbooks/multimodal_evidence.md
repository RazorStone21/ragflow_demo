# 多模态证据链路手册（解析 → 入库 → 检索 → 取图）

> 适用版本：ADR-0004 落地后的代码（CLAUDE.md v1.5.0）。
> 定位：扫描件、PDF 内嵌图、表格截图如何进入知识库，以及出问题时怎么查。
> 设计取舍与生产迁移见 [ADR-0004](../adr/0004-multimodal-document-evidence.md)。

## 链路一句话

RAGFlow 负责"看懂图"（OCR / 版面识别 / VLM 生成描述），Milvus 负责检索，平台把图片
二进制另存一份，检索证据与引用带上 `ImageRef` 元数据，API 按会话鉴权取图。答案生成
模型始终只读文本，图片的"看"发生在解析期。

```text
源文件 ──RAGFlow 解析──▶ chunk(content, doc_type_kwd, image_id)
          │                          │
          │                          ├─ image/table 切分：GET /documents/images/{image_id}
          │                          │    → ImageStore.put（本地目录，按租户隔离）
          │                          ▼
          │                      Milvus（content 向量 + media_type + images JSON）
          ▼
   检索命中 → EvidenceItem(media_type, images) → Citation(images)
          → GET /api/v1/threads/{thread_id}/evidence/{chunk_id}/images/{image_id}
```

## 前置检查（缺一项就会出现"看起来成功"的假象）

| 检查 | 命令 / 位置 | 缺了会怎样 |
|---|---|---|
| 网关有 `vision-chat` 路由 | `curl -s -H "Authorization: Bearer $LITELLM_MASTER_KEY" http://localhost:4000/v1/models` | RAGFlow 没有视觉模型可绑 |
| RAGFlow 默认视觉模型已设 | `GET /api/v1/models/default` 里应有 `model_type=vision` | **不报错**，图片框只剩零星文字或直接消失（见 ragflow_setup.md） |
| Milvus 集合是 `_v2` | `MILVUS_COLLECTION=gov_policy_chunks_v2` | 旧集合缺 `media_type`/`images`，`ensure_collection` 启动即 fail-fast |
| 图片目录可写 | `EVIDENCE_IMAGE_STORE_DIR`（默认 `local_data/evidence_images`） | API 启动时 `ensure_ready()` 失败，进程起不来 |

视觉模型的注册与默认设置步骤在 [ragflow_setup.md](ragflow_setup.md) 的「多模态解析额外要配的」
一节，这里不再重复。

## 入库

先干跑。干跑只读本地文件、不发请求，输出"将处理 / 将跳过"两份清单：

```bash
uv run python scripts/ingest.py --source "e:\BaiduNetdiskDownload\gov_doc" \
  --subset scripts/multimodal_subset.txt --dry-run
```

确认后去掉 `--dry-run`。`multimodal_subset.txt` 与 `energy_subset.txt` 分开跑，
同一文件重复入库按 manifest 的文件指纹 + `media_version` 跳过：

```bash
uv run python scripts/ingest.py --source "e:\BaiduNetdiskDownload\gov_doc" \
  --subset scripts/multimodal_subset.txt
```

manifest 里每份文件多出四个计数，用来判断"图到底进没进来"：

| 字段 | 含义 | 期望 |
|---|---|---|
| `image_count` | 实际存下的图片张数 | 扫描件/图表页 > 0 |
| `image_chunks` | 被识别为 `image` 的切分数 | 与图片页数大致对应 |
| `missing_image_chunks` | 应有图却拿不到的切分数（表格/正文附图降级） | 0；非 0 查 WARNING `ingest.image_degraded` |
| `skipped_empty_image_chunks` | 内容为空而被跳过的图片切分数 | 0；非 0 多半是视觉模型没生效 |

失败语义只有两档，没有"静默成功"：

- **图片切分**（正文就是图）取不到图 → 整份文件失败（`KnowledgeImageExportError`），
  平台侧会删掉已写入的 Milvus 行、图片目录与 RAGFlow 文档，重跑会再试。
- **表格/正文附图**取不到图 → 降级为无图，但记 WARNING 并计入 `missing_image_chunks`。

解析成功却一条可用切分都没有，同样判失败（`KnowledgeParseFailedError`），不会写进
manifest 的"已成功"名单。

## 验证取图

问答跑完后，`run.completed` 的 `citations[]` 里带 `images`。`scripts/demo_chat.py`
会把每条图片渲染成可直接打开的 URL：

```bash
uv run python scripts/demo_chat.py --question "……"
# 引用来源（N 条）：
#   [1] 0052.……通知.pdf 第 1 页
#       图片：http://127.0.0.1:8000/api/v1/threads/t1:u1:s1/evidence/c123/images/img_...
```

手工验证端点（注意 URL 里的 `thread_id` 要 URL 编码，浏览器会自动处理）：

```bash
curl -i -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/threads/$THREAD_ID/evidence/$CHUNK_ID/images/$IMAGE_ID"
# 200：Content-Type: image/png（或 jpeg/webp），Cache-Control: private, no-store
# 401：没带 Token
# 404：会话不存在 / 跨租户 / 证据里没有这条 chunk / 图不属于该 chunk / 文件缺失
```

404 **不区分原因**是刻意设计：区分它等于提供一个"某张图是否存在"的探针。服务端日志
（`evidence_image.not_in_session` / `evidence_image.missing_file`）才用来定位。

## 清理与回滚

- 只清图片：删 `EVIDENCE_IMAGE_STORE_DIR` 下对应 `{tenant_id}/{document_id}` 目录，
  或直接清空整个目录后重跑入库（Milvus 行与图片目录都由入库补偿逻辑维护）。
- 回到纯文本集合：把 `MILVUS_COLLECTION` 指回 `gov_policy_chunks` 并重启；
  旧集合未删，`_v2` 集合与图片目录可保留观察。
- 清掉多模态文件：从 manifest 删除对应条目并删除 Milvus 中该 `document_id` 的行，
  否则 fingerprint 未变会一直被跳过。重入库前还要删 RAGFlow 里的同名文档——
  RAGFlow 拒绝同名重复上传，失败文档不清会卡住后续重跑。

## 故障矩阵

| 现象 | 多半是 | 怎么办 |
|---|---|---|
| manifest 里 `skipped_empty_image_chunks` > 0，`image_count=0` | RAGFlow 没有默认视觉模型；图框内又没有文字，切分直接为空 | 按 ragflow_setup.md 设 `vision` 默认模型后重入库 |
| `missing_image_chunks` > 0，日志有 `ingest.image_degraded` | 单张图下载失败（超时/404/超过 `EVIDENCE_IMAGE_MAX_BYTES`） | 查 RAGFlow 图片端点与上限；表格截图允许缺，图片切分会直接判失败 |
| 入库报 `KnowledgeImageExportError` | 图片切分的图下载或落盘失败 | 查 RAGFlow 解析产物、磁盘空间、目录权限；该文件已整体回滚 |
| 启动报集合字段缺失，提示重建 | 用了旧集合名或未重入库 | 确认 `MILVUS_COLLECTION=gov_policy_chunks_v2`，按提示重入库 |
| 引用里有 `images`，取图 404 | 文件被清理、或会话发生在重入库之前 | 重新提问生成新会话；旧会话的图片不做保留期承诺 |
| 下载图片报 MIME 不符 | 上游返回 HTML 错误页而不是图片 | 查 RAGFlow 日志；不要放宽 MIME 白名单，错误页当图片存进去会让前端渲染乱码 |
| 解析超时（扫描件/大报告） | CPU 模式下 DeepDOC 逐页做版面与表格识别 | 提高 `RAGFLOW_PARSE_TIMEOUT_S`，或把文件移出本次子集；`RAGFLOW_TASK_PAGE_SIZE` 只影响分片，不减少总成本 |

## 边界（演示期）

- 视觉模型经 LiteLLM 指向云端 SiliconFlow（`vision-chat` → Qwen3-VL），**图片内容出域**。
  仅限公开演示语料；客户数据不得走这条链路。生产切私有化 VLM 只改网关映射。
- 图片二进制存本地文件系统，没有复制、备份与生命周期管理；`ImageStore` 是协议边界，
  生产换 MinIO/S3 走后续 ADR。
- 用户在对话中发图、生成答案时直接看像素、以图搜图都不在本期范围。
