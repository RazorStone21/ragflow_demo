#!/usr/bin/env python
"""语料入库脚本：RAGFlow 解析 → 切分 → 嵌入 → Milvus 索引（ADR-0002 双后端分工）。

链路（每一步的职责边界不可混淆，CLAUDE.md 2.3 / 第 7 章）::

    读文件 → 幂等前置判断 → RagFlowClient.upload_document
           → start_parsing → wait_parse_done → iter_chunks
           → 图片切分下载并落 ImageStore（ADR-0004）
           → ChunkInfo → ChunkRecord 映射（本脚本自建，库内无此转换器）
           → LiteLLMEmbedder.embed_documents → MilvusStore.upsert_chunks

幂等契约为**源文件相对路径 + 文件内容 sha256**：

- 指纹未变 → 整个文件跳过，不产生任何网络与写操作；
- 指纹变了（改了内容）→ 重新上传解析入库，并**先清掉旧 document_id 在 Milvus 与
  RAGFlow 里的产物**。这一步不可省：Milvus 主键沿用 RAGFlow 的 chunk_id，内容一改
  chunk_id 全变，旧 chunk 不会被 upsert 覆盖，只会变成永不命中的孤儿行。
- 清单文件（``INGEST_MANIFEST_PATH``）记录已完成文件，进程中断后重跑不会重复入库。

用法::

    # 先干跑：只读本地，不发任何请求
    uv run python scripts/ingest.py --source <语料目录> --subset scripts/energy_subset.txt --dry-run
    # 真实入库
    uv run python scripts/ingest.py --source <语料目录> --subset scripts/energy_subset.txt

退出码（脚本化调用依赖这些值，不得随意更改）::

    0  全部成功（--dry-run 走完也算成功）
    1  参数或配置错误：未发起任何入库
    2  部分文件失败：其余文件已成功入库，清单已记录，重跑只补失败的
    3  致命错误：无法建立前置条件（网关/向量库/数据集不可用），本次未完整执行
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_platform.core.errors import (
    AgentPlatformError,
    ConfigurationError,
    KnowledgeImageExportError,
    KnowledgeParseFailedError,
)
from agent_platform.core.logging import configure_logging, enable_utf8_console, get_logger
from agent_platform.knowledge.embedder import LiteLLMEmbedder
from agent_platform.knowledge.gateway_client import GatewayHttpClient
from agent_platform.knowledge.images import ImageStore, StoredImage, open_image_store
from agent_platform.knowledge.milvus_store import MilvusStore, open_milvus_store
from agent_platform.knowledge.ragflow_client import RagFlowClient
from agent_platform.knowledge.ragflow_schemas import ChunkInfo, DocumentInfo
from agent_platform.knowledge.schemas import (
    ChunkRecord,
    ImageBBox,
    ImageRef,
    MediaType,
    extract_page_numbers,
)
from agent_platform.settings import Settings, load_settings
from agent_platform.utils.text import content_fingerprint

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PARTIAL = 2
EXIT_FATAL = 3

MANIFEST_VERSION = 1
"""清单格式版本。结构变更时必须递增，旧清单将被视为不兼容而拒绝加载。"""

MEDIA_VERSION = 2
"""条目级媒体版本（ADR-0004）。

顶层清单版本**不升**：``manifest.files[*]`` 是逐条追加的，旧条目缺
``media_version`` 只影响该文件是否需要重入库，不影响清单能被读懂。缺该字段
或值不等于 2 的条目一律视为"未按多模态链路入库"，重跑一次即可补上。
"""

SUPPORTED_SUFFIXES = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".txt",
        ".md",
        ".csv",
        ".html",
        # 独立图片文件（ADR-0004）：经 RAGFlow OCR / VLM 转成文字后入库
        ".jpg",
        ".jpeg",
        ".png",
        ".bmp",
        ".webp",
        ".tif",
        ".tiff",
        ".gif",
    }
)
"""未指定 --subset 时递归扫描的文件类型。

刻意保守：只列本演示语料实际出现的类型（pdf/xlsx/图片）与 RAGFlow 明确支持的
常见文档类型。宁可漏扫让人补参数，也不要扫描未知二进制把噪声灌进知识库。
不放视频后缀：本仓没有播放与转码能力，扫进来只会是零切分失败。
"""

EMBED_BATCH_SIZE = 32
"""嵌入批大小：控制单次网关请求体量，避免超长 batch 被网关或上游拒绝。"""


@dataclass
class IngestOutcome:
    """单个文件的入库结果（成功与失败共用，便于统一结算退出码）。"""

    source_rel: str
    is_success: bool
    chunk_count: int = 0
    document_id: str = ""
    is_skipped: bool = False
    reason: str = ""
    image_count: int = 0
    image_chunks: int = 0
    missing_image_chunks: int = 0
    skipped_empty_image_chunks: int = 0


@dataclass(frozen=True)
class CollectResult:
    """单份文件的切分收集结果（含图片导出的计数，供清单与日志留痕）。"""

    written: int
    image_count: int = 0
    image_chunks: int = 0
    missing_image_chunks: int = 0
    skipped_empty_image_chunks: int = 0


@dataclass
class Manifest:
    """幂等清单（``INGEST_MANIFEST_PATH`` 的读写封装）。

    结构::

        {"version": 1, "files": {"<相对路径>": {"fingerprint": "...", ...}}}
    """

    path: Path
    files: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        """加载清单；不存在则返回空清单。

        Raises:
            ConfigurationError: 文件存在但不可解析，或版本不兼容。
        """
        if not path.exists():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                f"入库清单无法解析: {path}", detail="清单文件已损坏，请检查或删除后重跑。"
            ) from exc
        if not isinstance(raw, dict) or raw.get("version") != MANIFEST_VERSION:
            raise ConfigurationError(
                f"入库清单版本不兼容: {path}（期望 {MANIFEST_VERSION}）",
                detail="清单格式已变更，请删除该文件后重新入库。",
            )
        files = raw.get("files")
        return cls(path=path, files=files if isinstance(files, dict) else {})

    def save(self) -> None:
        """原子写入清单（先写临时文件再替换，避免中断留下半截 JSON）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": MANIFEST_VERSION, "files": self.files}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def fingerprint_of(self, source_rel: str) -> str | None:
        """已记录的文件指纹；未记录返回 ``None``。"""
        entry = self.files.get(source_rel)
        if not isinstance(entry, dict):
            return None
        value = entry.get("fingerprint")
        return str(value) if value else None

    def record(self, source_rel: str, payload: dict[str, Any]) -> None:
        """登记一个已成功入库的文件。"""
        self.files[source_rel] = payload


def is_stale_manifest_entry(entry: dict[str, Any] | None) -> bool:
    """该清单记录是否属于「零切分」的陈旧条目，因而不该被当作"已入库"跳过。

    当前代码里"解析成功、却一个可用切分都没产出"会直接抛
    :class:`KnowledgeParseFailedError`（见 ``collect_and_index``），所以
    **成功记录必然 ``chunk_count >= 1``**。清单里出现 ``chunk_count == 0``
    只可能来自修复前的缺陷版本——实测它把 5 份 ``run=FAIL`` 的文档记成
    "成功 5 份、切分 0 条"。若照旧按指纹跳过，这 5 份文件就再也进不了库，
    而且全程不报错、不留痕（CLAUDE.md 0.5「失败要出声」正指这种情形）。

    判定朝向"宁可重跑"：``chunk_count`` 缺失或类型不可解释时同样视为陈旧。
    重跑的代价只是再入一次库（Milvus 主键即 ``chunk_id``，重复写入是覆盖），
    而漏判的代价是数据永久缺失且无人察觉。
    """
    if not isinstance(entry, dict):
        return False
    count = entry.get("chunk_count")
    return not isinstance(count, int) or count <= 0


def should_skip(entry: dict[str, Any] | None, recorded: str | None, current: str) -> bool:
    """该文件是否可以跳过（干跑与真实入库共用，避免两处判断漂移）。

    三个条件都满足才跳过：指纹一致、不是零切分的陈旧条目、条目按当前
    ``MEDIA_VERSION`` 入库。升级入库链路（如 ADR-0004 引入图片）后旧条目
    必须重跑一次，否则库里只有文本、图片永远补不进来。
    """
    return (
        recorded is not None
        and recorded == current
        and not is_stale_manifest_entry(entry)
        and isinstance(entry, dict)
        and entry.get("media_version") == MEDIA_VERSION
    )


_MEDIA_BY_DOC_TYPE: dict[str, MediaType] = {
    "image": MediaType.IMAGE,
    "table": MediaType.TABLE,
}


def media_type_of(chunk: ChunkInfo) -> MediaType:
    """把 RAGFlow 的 ``doc_type_kwd`` 映射为平台媒介类型（ADR-0004）。

    只认 ``image`` 与 ``table``，其余（含空串、``text``、将来可能新增的
    ``video``）一律按 ``text`` 处理：未知类型当文本不会丢内容，而猜成图片
    会让一张不存在的图挡住整份文件的入库。
    """
    return _MEDIA_BY_DOC_TYPE.get(chunk.doc_type_kwd.strip().lower(), MediaType.TEXT)


def _image_position(chunk: ChunkInfo) -> tuple[int | None, ImageBBox | None]:
    """从切分的 ``positions`` 中取图片页码与坐标（取不到就返回 None）。

    坐标不合法（负数、字段不足）时只丢 bbox，不阻断入库：RAGFlow 各解析器
    输出的位置结构并不一致，为一条坐标放弃整张图不划算。
    """
    for position in chunk.positions:
        if len(position) >= 5:
            page, left, right, top, bottom = position[:5]
            if page >= 1 and min(left, right, top, bottom) >= 0:
                return page, ImageBBox(left=left, right=right, top=top, bottom=bottom)
    pages = extract_page_numbers(chunk.positions)
    return (pages[0] if pages else None), None


def image_caption(
    chunk: ChunkInfo, *, media_type: MediaType, document_name: str, page: int | None
) -> str:
    """生成图片图注（≤240 字符）。

    图片切分的正文通常是 VLM 描述或 OCR 文本，直接取用；表格截图与正文附图
    不能用 HTML/长正文当图注，改用"表格：文档名 第 N 页"这类短标签。
    """
    if media_type is MediaType.TABLE:
        label = f"表格：{document_name}" if document_name else "表格"
        return label if page is None else f"{label} 第 {page} 页"
    return " ".join(chunk.content.split())[:240]


def build_image_ref(
    chunk: ChunkInfo,
    stored: StoredImage,
    *,
    media_type: MediaType,
    document_name: str,
) -> ImageRef:
    """把已落盘的图片包装成对外引用（只含元数据，ADR-0004）。"""
    page, bbox = _image_position(chunk)
    return ImageRef(
        image_id=stored.image_id,
        sha256=stored.sha256,
        mime=stored.mime,
        caption=image_caption(chunk, media_type=media_type, document_name=document_name, page=page),
        page=page,
        bbox=bbox,
    )


def build_parser_config(settings: Settings) -> dict[str, Any]:
    """数据集级解析配置（键名取自 RAGFlow ``ParserConfig``）。

    这些键**不能**随上传接口传：v0.27.2 的上传接口只白名单
    ``table_column_mode`` / ``table_column_roles``，其余会被静默丢弃
    （见 ``ragflow_client.upload_document``）。
    """
    return {
        "layout_recognize": settings.ragflow_layout_recognize,
        "chunk_token_num": settings.ragflow_chunk_token_num,
        "image_context_size": settings.ragflow_image_context_size,
        "table_context_size": settings.ragflow_table_context_size,
    }


def load_subset(subset_path: Path) -> list[str]:
    """读取子集白名单（格式见 ``scripts/energy_subset.txt`` 自述）。

    每行一个相对文件名；``#`` 开头为注释；空行忽略；不去重、不做模糊匹配——
    重复行按重复处理并由调用方报错，静默去重会掩盖清单本身的错误。

    Raises:
        ConfigurationError: 文件不存在或没有任何有效条目。
    """
    if not subset_path.is_file():
        raise ConfigurationError(
            f"子集清单不存在: {subset_path}", detail="请检查 --subset 参数指向的文件路径。"
        )
    names: list[str] = []
    for line_no, raw_line in enumerate(
        subset_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "/" in line or "\\" in line:
            raise ConfigurationError(
                f"子集清单第 {line_no} 行含目录分隔符: {line}",
                detail="--subset 每行只允许文件名（不含目录）。",
            )
        names.append(line)
    if not names:
        raise ConfigurationError(f"子集清单为空: {subset_path}", detail="清单中没有任何文件名。")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigurationError(
            f"子集清单存在重复条目: {', '.join(duplicates)}",
            detail="同一文件在清单中出现多次，请先去重。",
        )
    return names


def resolve_files(source: Path, subset: Path | None) -> list[tuple[str, Path]]:
    """确定本次要处理的文件，返回 ``(相对路径, 绝对路径)`` 列表。

    Args:
        source: 语料根目录。
        subset: 子集白名单；``None`` 表示递归扫描 ``SUPPORTED_SUFFIXES``。

    Raises:
        ConfigurationError: 目录不存在，或白名单中的文件在语料根目录下找不到。
    """
    if not source.is_dir():
        raise ConfigurationError(
            f"语料目录不存在: {source}", detail="请检查 --source 参数指向的目录。"
        )
    if subset is not None:
        pairs: list[tuple[str, Path]] = []
        missing: list[str] = []
        for name in load_subset(subset):
            candidate = source / name
            if candidate.is_file():
                pairs.append((name, candidate))
            else:
                missing.append(name)
        if missing:
            raise ConfigurationError(
                f"子集清单中有 {len(missing)} 个文件在语料目录下不存在: {', '.join(missing[:5])}"
                + ("…" if len(missing) > 5 else ""),
                detail="请核对 --source 是否指向正确的语料根目录。",
            )
        return pairs
    pairs = []
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            pairs.append((path.relative_to(source).as_posix(), path))
    return pairs


def to_chunk_record(
    chunk: ChunkInfo,
    *,
    tenant_id: str,
    dataset_id: str,
    document_id: str,
    document_name: str,
    source_rel: str,
    updated_at: int,
    media_type: MediaType = MediaType.TEXT,
    images: list[ImageRef] | None = None,
) -> ChunkRecord:
    """把 RAGFlow 的 ``ChunkInfo`` 映射为 Milvus 的 ``ChunkRecord``。

    库内没有这个转换器，必须自建。三处刻意的取舍：

    - ``chunk_id`` 直接沿用 RAGFlow 主键：Milvus 集合以它为唯一主键（``auto_id=False``），
      因此重复入库是 upsert 覆盖而非新增——这正是幂等契约的落点。
    - ``content_fingerprint`` 取**该 chunk 内容**的 sha256，不是整份文件的：字段用于
      判断单条切分是否变化，用文件级指纹会让"文件里改一个字 → 全部 chunk 都显得变了"。
    - ``source_uri`` 存**相对路径**而非绝对路径：绝对路径随机器而变，一旦写入索引
      就成了不可复现的引用；相对路径配合数据集名足以定位来源。

    媒介类型与图片引用由调用方先算好再传入：图片的下载与落盘是 IO，不能塞进
    这个纯映射函数（下一阶段的单测才能继续保持零 IO）。
    """
    return ChunkRecord(
        chunk_id=chunk.id,
        content=chunk.content,
        tenant_id=tenant_id,
        dataset_id=dataset_id,
        document_id=document_id,
        document_name=document_name or chunk.docnm_kwd,
        positions=[list(position) for position in chunk.positions],
        source_uri=source_rel,
        content_fingerprint=content_fingerprint(chunk.content.encode("utf-8")),
        updated_at=updated_at,
        media_type=media_type,
        images=list(images or ()),
    )


def parse_failure_detail(parsed: DocumentInfo | None) -> str:
    """从 RAGFlow 的解析日志里抽出可操作的一行，作为失败详情。

    ``progress_msg`` 是多行日志，真正有信息量的是末尾那几行错误（例如
    ``[ERROR]Fail to bind embedding model: No default embedding model is set.``）。
    只取末尾 3 个非空行并截断，既给出根因又不把整段日志灌进异常与结构化日志。
    """
    if parsed is None:
        return "RAGFlow 未返回该文档的解析状态，请到控制台核对文档列表。"
    lines = [line.strip() for line in parsed.progress_msg.splitlines() if line.strip()]
    if not lines:
        return f"RAGFlow 未给出解析日志（状态 {parsed.run.value}），请到控制台查看解析详情。"
    return " / ".join(lines[-3:])[:500]


async def ingest_one(
    *,
    rel_path: str,
    abs_path: Path,
    tenant_id: str,
    dataset_id: str,
    ragflow: RagFlowClient,
    embedder: LiteLLMEmbedder,
    store: MilvusStore,
    image_store: ImageStore,
    image_max_bytes: int,
    image_concurrency: int,
    previous_document_id: str | None,
) -> IngestOutcome:
    """处理单个文件：上传 → 解析 → 拉取切分 → 导出图片 → 嵌入 → 写入 Milvus。

    任一环节失败即抛出平台异常，由调用方登记为失败文件并继续下一个——
    单个坏文件不得中断整批（CLAUDE.md 7.3 的"失败文件单独登记"要求）。

    Args:
        image_store: 证据图片存储（ADR-0004）。
        image_max_bytes: 单张图片的字节上限。
        image_concurrency: 单份文件内并发下载图片的上限。
    """
    if previous_document_id:
        # 内容变更后的重入：先清旧产物，否则旧 chunk 会以孤儿行形式永久留在索引里。
        removed = await store.delete_by_document(previous_document_id, tenant_id)
        # 图片目录按 document_id 分，旧文档的图片同样要清：新文档 ID 不同，
        # 旧目录不会被覆盖，留着就是无人引用的磁盘垃圾。
        removed_images = await image_store.delete_document(tenant_id, previous_document_id)
        # 尽力而为：这里删的是**上一轮尝试**的遗留物。RAGFlow 对"删一个不属于该
        # 数据集的 id"是**报错**而非幂等成功（实测源码 document_api.py 的
        # delete_documents：id 不在 dataset_doc_ids 里即返回错误）。于是只要那个文档
        # 已经不在了——上一轮的补偿删除、或操作者在控制台手工清理——照常调用就会让
        # 本文件在上传之前就失败。而"它已经不在了"恰恰是这一步想要的结果。
        await _delete_document_best_effort(
            ragflow,
            dataset_id,
            previous_document_id,
            rel_path,
            context="重入时清理上一轮遗留文档",
        )
        # 文案只陈述观察到的动作，不断言成因：触发清理的有两种情形（内容变更 / 清单记录
        # 陈旧），而本函数只拿得到 previous_document_id，分不出来。原先写"内容变更"，
        # 在陈旧那一支就是**假话**——会让操作者去找一个不存在的 diff（实测踩过）。
        # 成因由紧邻的 ingest.stale_manifest_entry 事件给出：有它即陈旧，无它即内容变更。
        logger.info(
            "重新入库前清理上一轮遗留的旧文档产物",
            extra={
                "event": "ingest.replaced",
                "source": rel_path,
                "document_id": previous_document_id,
                "removed_chunks": removed,
                "removed_images": removed_images,
            },
        )

    doc = await ragflow.upload_document(dataset_id, abs_path)
    try:
        await ragflow.start_parsing(dataset_id, [doc.id])
        statuses = await ragflow.wait_parse_done(dataset_id, [doc.id])
        # 「离开进行中」不等于「解析成功」：wait_parse_done 会带着 FAIL/CANCEL 一起返回。
        # 必须在这里判，否则失败文档会以"0 切分"的名义被记成成功（实测踩过，见
        # KnowledgeParseFailedError 的 docstring）。
        parsed = statuses.get(doc.id)
        if parsed is None or not parsed.run.is_success:
            state = parsed.run.value if parsed is not None else "UNKNOWN"
            # 根因（RAGFlow 的报错原文）进 message——按 core/errors.py 的契约，message 是
            # "进受控日志的内部诊断信息"，正是操作者排障要看的；detail 留给调用方指引。
            raise KnowledgeParseFailedError(
                f"解析未成功（{state}）: {rel_path} — {parse_failure_detail(parsed)}",
                detail=(
                    "常见原因：RAGFlow 未配置默认嵌入模型（控制台 → 模型设置）。"
                    "该文件未计入成功，处理后重跑本命令即可；已成功的文件会按指纹跳过。"
                ),
            )
        collected = await collect_and_index(
            doc=doc,
            rel_path=rel_path,
            tenant_id=tenant_id,
            dataset_id=dataset_id,
            ragflow=ragflow,
            embedder=embedder,
            store=store,
            image_store=image_store,
            image_max_bytes=image_max_bytes,
            image_concurrency=image_concurrency,
        )
    except AgentPlatformError:
        # 补偿：RAGFlow **拒绝同名重复上传**（实测 "Duplicated document name in the
        # same dataset."）。失败文档不入清单、也就没人替它收尸，留着它会让重跑次次
        # 撞在同名冲突上——脚本承诺的"重跑只补失败的"就永远补不上。
        await _delete_document_best_effort(
            ragflow, dataset_id, doc.id, rel_path, context="解析失败后的补偿删除"
        )
        # 图片与文档同生共死：已下载的图片若随失败文档留在盘上，就是一批
        # 没有证据引用、也永远不会被检索到的孤儿文件。
        await _delete_images_best_effort(image_store, tenant_id, doc.id, rel_path)
        raise

    return IngestOutcome(
        source_rel=rel_path,
        is_success=True,
        document_id=doc.id,
        chunk_count=collected.written,
        image_count=collected.image_count,
        image_chunks=collected.image_chunks,
        missing_image_chunks=collected.missing_image_chunks,
        skipped_empty_image_chunks=collected.skipped_empty_image_chunks,
    )


async def _delete_images_best_effort(
    image_store: ImageStore, tenant_id: str, document_id: str, rel_path: str
) -> None:
    """尽力删除文档图片；清理自身出错只记日志，不得掩盖原始异常（NO-06）。"""
    try:
        removed = await image_store.delete_document(tenant_id, document_id)
    except (OSError, AgentPlatformError) as cleanup_exc:
        logger.warning(
            "清理证据图片未成功，请检查图片存储目录",
            extra={
                "event": "ingest.image_cleanup_failed",
                "source": rel_path,
                "document_id": document_id,
                "error_type": type(cleanup_exc).__name__,
            },
        )
        return
    if removed:
        logger.info(
            "已清理证据图片",
            extra={
                "event": "ingest.images_removed",
                "source": rel_path,
                "document_id": document_id,
                "removed_images": removed,
            },
        )


async def _delete_document_best_effort(
    ragflow: RagFlowClient,
    dataset_id: str,
    document_id: str,
    rel_path: str,
    *,
    context: str,
) -> None:
    """尽力删除文档；清理自身出错只记日志，**不得掩盖原始异常**（NO-06）。

    Args:
        context: 触发清理的场景（写入日志，用于区分是补偿删除还是重入清理）。
    """
    try:
        await ragflow.delete_documents(dataset_id, [document_id])
    except AgentPlatformError as cleanup_exc:
        # 两种场景共用一条文案，且对两者都成立：文档仍在 → 会被同名冲突挡住；
        # 文档已不在 → 本次删除本就无事可做，无需动作。故只陈述事实并给出核对方式，
        # 不写"必须人工删除"那种在第二种场景下会误导人的结论。
        logger.warning(
            "清理文档未成功；若该文档仍在数据集中，重跑会撞同名冲突，请到 RAGFlow 控制台核对",
            extra={
                "event": "ingest.cleanup_failed",
                "source": rel_path,
                "document_id": document_id,
                "context": context,
                "error_code": cleanup_exc.code.value,
            },
        )


async def collect_and_index(
    *,
    doc: DocumentInfo,
    rel_path: str,
    tenant_id: str,
    dataset_id: str,
    ragflow: RagFlowClient,
    embedder: LiteLLMEmbedder,
    store: MilvusStore,
    image_store: ImageStore,
    image_max_bytes: int,
    image_concurrency: int,
) -> CollectResult:
    """拉取切分 → 导出图片 → 映射 → 嵌入 → 写入 Milvus。

    图片导出的失败语义（ADR-0004）按媒介类型分档：

    - ``image`` 切分的正文就是图片，取不到图 = 这条切分没有任何可用内容，
      **整份文件判失败**（``KnowledgeImageExportError``），由调用方补偿删除；
    - ``table`` 与 ``text`` 附图允许降级为 ``images=[]``，但必须记 WARNING
      并计入 ``missing_image_chunks``——"静默少一张图"与"从来没这张图"
      在检索结果里长得一模一样，不留痕就无从追查。

    Raises:
        KnowledgeParseFailedError: 解析成功但一条可用切分都没有（"零入库"不得算成功）。
        KnowledgeImageExportError: 图片切分的图片下载或落盘失败。
    """
    updated_at = int(datetime.now(UTC).timestamp())
    usable: list[ChunkInfo] = []
    skipped_empty_image_chunks = 0
    async for _page_index, page in ragflow.iter_chunks(dataset_id, doc.id):
        for chunk in page.chunks:
            if not chunk.available:
                # 被人工禁用的切分不入索引：它不应作为回答依据出现。
                continue
            if not chunk.content.strip():
                # 空切分无法向量化（ChunkRecord.content 要求 min_length=1），跳过。
                # 图片切分额外计数：它们可能只是 RAGFlow 没跑 OCR/VLM，值得显式观察。
                if media_type_of(chunk) is MediaType.IMAGE:
                    skipped_empty_image_chunks += 1
                continue
            usable.append(chunk)

    image_chunks = sum(1 for chunk in usable if media_type_of(chunk) is MediaType.IMAGE)
    missing_image_chunks = 0
    for chunk in usable:
        if media_type_of(chunk) is MediaType.IMAGE and not chunk.image_id:
            missing_image_chunks += 1
            logger.warning(
                "图片切分缺少 image_id，只能保留文字内容",
                extra={"event": "ingest.image_missing", "source": rel_path, "chunk_id": chunk.id},
            )

    stored = await _export_images(
        chunks=usable,
        rel_path=rel_path,
        tenant_id=tenant_id,
        document_id=doc.id,
        ragflow=ragflow,
        image_store=image_store,
        image_max_bytes=image_max_bytes,
        image_concurrency=image_concurrency,
    )
    missing_image_chunks += stored.missing
    for chunk_id in stored.missing_chunk_ids:
        logger.warning(
            "切分图片下载失败，已降级为无图",
            extra={"event": "ingest.image_degraded", "source": rel_path, "chunk_id": chunk_id},
        )

    records: list[ChunkRecord] = []
    for chunk in usable:
        media_type = media_type_of(chunk)
        stored_image = stored.images.get(chunk.id)
        images = (
            [build_image_ref(chunk, stored_image, media_type=media_type, document_name=doc.name)]
            if stored_image is not None
            else []
        )
        records.append(
            to_chunk_record(
                chunk,
                tenant_id=tenant_id,
                dataset_id=dataset_id,
                document_id=doc.id,
                document_name=doc.name,
                source_rel=rel_path,
                updated_at=updated_at,
                media_type=media_type,
                images=images,
            )
        )

    if not records:
        # 解析成功却一条可用切分都没有 ⇒ 这份文件对知识库零贡献。记成成功会把
        # "什么都没入库"写进幂等清单，之后每次重跑都跳过它——问题被永久掩盖。
        raise KnowledgeParseFailedError(
            f"解析完成但未产出任何可用切分: {rel_path}",
            detail="文档可能是空的、或是全部切分被人工禁用；本次不计入成功，重跑会再试。",
        )

    written = 0
    for start in range(0, len(records), EMBED_BATCH_SIZE):
        batch = records[start : start + EMBED_BATCH_SIZE]
        vectors = await embedder.embed_documents([record.content for record in batch])
        written += await store.upsert_chunks(batch, vectors)
    return CollectResult(
        written=written,
        image_count=sum(len(record.images) for record in records),
        image_chunks=image_chunks,
        missing_image_chunks=missing_image_chunks,
        skipped_empty_image_chunks=skipped_empty_image_chunks,
    )


@dataclass(frozen=True)
class _ExportedImages:
    """单份文件的图片导出结果。"""

    images: dict[str, StoredImage]
    missing_chunk_ids: list[str]

    @property
    def missing(self) -> int:
        """应到未到的图片数（表格截图、正文附图降级）。"""
        return len(self.missing_chunk_ids)


async def _export_images(
    *,
    chunks: list[ChunkInfo],
    rel_path: str,
    tenant_id: str,
    document_id: str,
    ragflow: RagFlowClient,
    image_store: ImageStore,
    image_max_bytes: int,
    image_concurrency: int,
) -> _ExportedImages:
    """有界并发地下载并落盘切分图片，返回 ``chunk_id`` → 图片摘要。

    图片切分失败直接抛异常；表格/正文附图失败只登记进 ``missing_chunk_ids``，
    由调用方记 WARNING 并降级。
    """
    targets = [chunk for chunk in chunks if chunk.image_id]
    if not targets:
        return _ExportedImages(images={}, missing_chunk_ids=[])

    semaphore = asyncio.Semaphore(image_concurrency)

    async def _export(chunk: ChunkInfo) -> StoredImage:
        async with semaphore:
            payload = await ragflow.download_document_image(
                chunk.image_id, max_bytes=image_max_bytes
            )
            return await image_store.put(
                tenant_id=tenant_id,
                document_id=document_id,
                chunk_id=chunk.id,
                data=payload,
            )

    results = await asyncio.gather(*(_export(chunk) for chunk in targets), return_exceptions=True)
    images: dict[str, StoredImage] = {}
    missing_chunk_ids: list[str] = []
    for chunk, result in zip(targets, results, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):  # 取消等 BaseException 必须原样上抛
                raise result
            if media_type_of(chunk) is MediaType.IMAGE:
                raise KnowledgeImageExportError(
                    f"图片切分导出失败: {rel_path}（chunk {chunk.id}）— {result}",
                    detail=(
                        "该切分的正文就是图片，取不到图时这条切分不能作为证据；"
                        "整份文件已判失败，请检查 RAGFlow 的解析产物，或把该文件移出多模态子集。"
                    ),
                ) from result
            missing_chunk_ids.append(chunk.id)
            continue
        images[chunk.id] = result
    return _ExportedImages(images=images, missing_chunk_ids=missing_chunk_ids)


async def _print_plan(pairs: list[tuple[str, Path]], existing: Manifest) -> None:
    """干跑输出：列出将处理/将跳过的文件。

    读文件走 ``to_thread``：本函数由 async 的 ``run()`` 调用，直接读会阻塞事件循环
    （NO-05 禁止 async 路径上的阻塞 IO），与主流程保持同一做法。
    """
    to_ingest: list[str] = []
    to_skip: list[str] = []
    for rel_path, abs_path in pairs:
        recorded = existing.fingerprint_of(rel_path)
        current = content_fingerprint(await asyncio.to_thread(abs_path.read_bytes))
        # 干跑必须与真实入库给出同一个答案，否则"先 --dry-run 确认"就成了误导。
        is_unchanged = should_skip(existing.files.get(rel_path), recorded, current)
        (to_skip if is_unchanged else to_ingest).append(rel_path)
    logger.info(
        "干跑完成：未发起任何网络请求",
        extra={
            "event": "ingest.dry_run",
            "total": len(pairs),
            "pending": len(to_ingest),
            "skipped": len(to_skip),
        },
    )
    for name in to_ingest:
        logger.info("待入库", extra={"event": "ingest.plan.pending", "source": name})
    for name in to_skip:
        logger.info("指纹未变，将跳过", extra={"event": "ingest.plan.skip", "source": name})


async def run(args: argparse.Namespace, settings: Settings) -> int:
    """执行入库，返回退出码。"""
    source = Path(args.source).expanduser().resolve()
    subset = Path(args.subset).expanduser().resolve() if args.subset else None
    pairs = resolve_files(source, subset)
    manifest = Manifest.load(settings.ingest_manifest_path)

    if not pairs:
        logger.warning("没有匹配到任何文件", extra={"event": "ingest.empty", "source": str(source)})
        return EXIT_OK

    if args.dry_run:
        await _print_plan(pairs, manifest)
        return EXIT_OK

    tenant_id: str = args.tenant or settings.demo_tenant_id
    dataset_name: str = args.dataset or settings.ragflow_dataset_name
    outcomes: list[IngestOutcome] = []

    # 三个客户端同时持有：RagFlow 解析、网关嵌入、Milvus 写入缺一不可，
    # 任一建不起来都属致命错误（配置/网络/服务不可用），应整体失败而不是逐文件撞墙。
    async with (
        RagFlowClient(settings) as ragflow,
        GatewayHttpClient(settings) as gateway,
        open_milvus_store(settings) as store,
    ):
        embedder = LiteLLMEmbedder(
            gateway, logical_model=settings.embedding_model, expected_dim=settings.milvus_dim
        )
        # 图片存储的目录在启动期建好并验证可写：等到第一份带图文件才失败，
        # 会让整批入库跑到一半才停（CLAUDE.md 3.5 fail-fast）。
        image_store = open_image_store(settings)
        await image_store.ensure_ready()
        dataset_id = await ragflow.ensure_dataset(
            dataset_name, parser_config=build_parser_config(settings)
        )
        created = await store.ensure_collection()
        logger.info(
            "集合就绪",
            extra={
                "event": "ingest.collection_ready",
                "collection": store.collection,
                # 键名不得用 created：它是 LogRecord 的保留属性（记录创建时间戳），
                # 作为 extra 键会在 makeRecord 里抛 KeyError。同类保留名见
                # tests/unit/test_logging.py 的守卫用例。
                "collection_created": created,
                "dim": store.dim,
                "dataset_id": dataset_id,
            },
        )
        for rel_path, abs_path in pairs:
            fingerprint = content_fingerprint(await asyncio.to_thread(abs_path.read_bytes))
            entry = manifest.files.get(rel_path)
            recorded = manifest.fingerprint_of(rel_path)
            if recorded == fingerprint and is_stale_manifest_entry(entry):
                # 这里的指纹一致是"假一致"：记录虽在，但它是零切分的陈旧条目，
                # 库里其实什么都没有。出声后照常入库——否则这批文件会被永久跳过。
                logger.warning(
                    "清单记录为零切分，视为未入库并重跑",
                    extra={
                        "event": "ingest.stale_manifest_entry",
                        "source": rel_path,
                        "recorded_chunk_count": entry.get("chunk_count") if entry else None,
                    },
                )
            elif recorded == fingerprint and not should_skip(entry, recorded, fingerprint):
                # 指纹一致但仍不跳过，只剩媒体版本这一种可能：条目来自引入图片
                # 证据之前的旧链路。重入库才能把图片补进索引，且必须出声。
                logger.warning(
                    "清单条目未按当前媒体版本入库，重跑该文件",
                    extra={
                        "event": "ingest.media_version_outdated",
                        "source": rel_path,
                        "recorded_media_version": entry.get("media_version") if entry else None,
                        "current_media_version": MEDIA_VERSION,
                    },
                )
            elif should_skip(entry, recorded, fingerprint):
                outcomes.append(
                    IngestOutcome(source_rel=rel_path, is_success=True, is_skipped=True)
                )
                logger.info("指纹未变，跳过", extra={"event": "ingest.skip", "source": rel_path})
                continue
            previous = None
            if isinstance(entry, dict) and entry.get("document_id"):
                previous = str(entry["document_id"])
            try:
                outcome = await ingest_one(
                    rel_path=rel_path,
                    abs_path=abs_path,
                    tenant_id=tenant_id,
                    dataset_id=dataset_id,
                    ragflow=ragflow,
                    embedder=embedder,
                    store=store,
                    image_store=image_store,
                    image_max_bytes=settings.evidence_image_max_bytes,
                    image_concurrency=settings.evidence_image_download_concurrency,
                    previous_document_id=previous,
                )
            except AgentPlatformError as exc:
                # 单文件失败不中断整批（CLAUDE.md 7.3）。``message`` 按契约就是
                # "进受控日志的内部诊断信息"（core/errors.py），不属 NO-07 要挡的
                # 密钥/PII；因此截断后记进常规日志——"失败文件到底为什么失败"是
                # 操作者当场就要回答的问题，压在 DEBUG 级等于没记（实测踩过）。
                outcome = IngestOutcome(
                    source_rel=rel_path, is_success=False, reason=exc.code.value
                )
                logger.error(
                    "文件入库失败",
                    extra={
                        "event": "ingest.file_failed",
                        "source": rel_path,
                        "error_code": exc.code.value,
                        "error_type": type(exc).__name__,
                        "error_message": exc.message[:300],
                    },
                )
                logger.debug("失败栈线索", extra={"source": rel_path, "repr": repr(exc)})
            outcomes.append(outcome)
            if outcome.is_success:
                manifest.record(
                    rel_path,
                    {
                        "fingerprint": fingerprint,
                        "document_id": outcome.document_id,
                        "dataset_id": dataset_id,
                        "tenant_id": tenant_id,
                        "chunk_count": outcome.chunk_count,
                        "media_version": MEDIA_VERSION,
                        "image_count": outcome.image_count,
                        "image_chunks": outcome.image_chunks,
                        "missing_image_chunks": outcome.missing_image_chunks,
                        "skipped_empty_image_chunks": outcome.skipped_empty_image_chunks,
                        "ingested_at": datetime.now(UTC).isoformat(),
                    },
                )
                # 逐文件落盘：进程中断后重跑只补未完成的，不是从头再来。
                manifest.save()

        # 收尾 flush：Milvus 是有界一致性，写完立刻检索可能查不到（实测 count() 立即得 0、
        # 约 1 秒后才为真值）。本脚本存在的意义就是"入库后马上能问"，所以必须显式收口。
        # 只在整个批次末尾做一次——放进 upsert_chunks 里按 32 条一批 flush 会封存大量小段。
        if any(o.is_success and not o.is_skipped for o in outcomes):
            await store.flush()

    succeeded = [o for o in outcomes if o.is_success]
    failed = [o for o in outcomes if not o.is_success]
    skipped = [o for o in succeeded if o.is_skipped]
    total_chunks = sum(o.chunk_count for o in succeeded)
    logger.info(
        "入库完成",
        extra={
            "event": "ingest.done",
            "total": len(outcomes),
            "succeeded": len(succeeded),
            "skipped": len(skipped),
            "failed": len(failed),
            "chunks": total_chunks,
            "images": sum(o.image_count for o in succeeded),
            "manifest": str(manifest.path),
        },
    )
    if failed:
        logger.error(
            "存在失败文件，重跑本命令即可只补这些文件（成功的已记录进清单）",
            extra={"event": "ingest.partial", "failed": [o.source_rel for o in failed]},
        )
        return EXIT_PARTIAL
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（``--help`` 即本文档开头的用法说明）。"""
    parser = argparse.ArgumentParser(
        prog="ingest.py",
        description="语料入库：RAGFlow 解析切分 → 嵌入 → Milvus 索引（幂等，可重复执行）",
        epilog="退出码：0 全部成功；1 参数或配置错误；2 部分文件失败；3 致命错误。",
    )
    parser.add_argument(
        "--source", required=True, metavar="DIR", help="语料根目录（本仓库不含语料，NO-17）"
    )
    parser.add_argument(
        "--subset",
        metavar="FILE",
        help="子集白名单，每行一个相对文件名（如 scripts/energy_subset.txt）；省略则递归扫描",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只读本地并列出计划，不发起任何网络或写操作"
    )
    parser.add_argument(
        "--tenant", default=None, metavar="ID", help="租户 ID（默认取 DEMO_TENANT_ID）"
    )
    parser.add_argument(
        "--dataset",
        default=None,
        metavar="NAME",
        help="RAGFlow 数据集名（默认取 RAGFLOW_DATASET_NAME）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """脚本入口：解析参数、加载配置、执行并映射退出码。"""
    # 必须在 parse_args 之前：--help 由 argparse 直接写 stdout 并 SystemExit，
    # 晚于此处调用就来不及改编码，Windows 下中文帮助会变成乱码。
    enable_utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        # 日志尚未按配置初始化，此处直写 stderr 保证错误一定可见
        sys.stderr.write(f"配置错误：{exc}\n")
        return EXIT_USAGE
    configure_logging(level=settings.log_level, force=True)
    try:
        return asyncio.run(run(args, settings))
    except ConfigurationError as exc:
        logger.error("参数或配置错误", extra={"event": "ingest.usage_error", "detail": str(exc)})
        return EXIT_USAGE
    except AgentPlatformError as exc:
        logger.error(
            "入库未能完整执行",
            extra={
                "event": "ingest.fatal",
                "error_code": exc.code.value,
                "error_type": type(exc).__name__,
                # 致命错误必须自带原因：退出码 3 是对脚本调用方的契约，操作者手里
                # 不会有别的线索（实测踩过：只见到 VALIDATION_FAILED，真实原因
                # "lacks permission for dataset" 被压在 DEBUG 级，白查一轮）。
                # 异常文本由客户端从**请求路径与上游响应体**拼出，不含请求头/凭据；
                # 截断 500 字符是防上游把一大段 HTML 塞进 message 撑爆日志。
                "detail": str(exc)[:500],
            },
        )
        logger.debug("失败详情（含堆栈线索）", extra={"detail": repr(exc)})
        return EXIT_FATAL


if __name__ == "__main__":
    raise SystemExit(main())
