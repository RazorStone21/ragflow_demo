#!/usr/bin/env python
"""语料入库脚本：RAGFlow 解析 → 切分 → 嵌入 → Milvus 索引（ADR-0002 双后端分工）。

链路（每一步的职责边界不可混淆，CLAUDE.md 2.3 / 第 7 章）::

    读文件 → 幂等前置判断 → RagFlowClient.upload_document
           → start_parsing → wait_parse_done → iter_chunks
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

from agent_platform.core.errors import AgentPlatformError, ConfigurationError
from agent_platform.core.logging import configure_logging, enable_utf8_console, get_logger
from agent_platform.knowledge.embedder import LiteLLMEmbedder
from agent_platform.knowledge.gateway_client import GatewayHttpClient
from agent_platform.knowledge.milvus_store import MilvusStore, open_milvus_store
from agent_platform.knowledge.ragflow_client import RagFlowClient
from agent_platform.knowledge.ragflow_schemas import ChunkInfo
from agent_platform.knowledge.schemas import ChunkRecord
from agent_platform.settings import Settings, load_settings
from agent_platform.utils.text import content_fingerprint

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PARTIAL = 2
EXIT_FATAL = 3

MANIFEST_VERSION = 1
"""清单格式版本。结构变更时必须递增，旧清单将被视为不兼容而拒绝加载。"""

SUPPORTED_SUFFIXES = frozenset(
    {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".csv", ".html"}
)
"""未指定 --subset 时递归扫描的文件类型。

刻意保守：只列本演示语料实际出现的类型（pdf/xlsx）与 RAGFlow 明确支持的常见文档
类型。宁可漏扫让人补参数，也不要扫描未知二进制把噪声灌进知识库。
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
) -> ChunkRecord:
    """把 RAGFlow 的 ``ChunkInfo`` 映射为 Milvus 的 ``ChunkRecord``。

    库内没有这个转换器，必须自建。三处刻意的取舍：

    - ``chunk_id`` 直接沿用 RAGFlow 主键：Milvus 集合以它为唯一主键（``auto_id=False``），
      因此重复入库是 upsert 覆盖而非新增——这正是幂等契约的落点。
    - ``content_fingerprint`` 取**该 chunk 内容**的 sha256，不是整份文件的：字段用于
      判断单条切分是否变化，用文件级指纹会让"文件里改一个字 → 全部 chunk 都显得变了"。
    - ``source_uri`` 存**相对路径**而非绝对路径：绝对路径随机器而变，一旦写入索引
      就成了不可复现的引用；相对路径配合数据集名足以定位来源。
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
    )


async def ingest_one(
    *,
    rel_path: str,
    abs_path: Path,
    tenant_id: str,
    dataset_id: str,
    ragflow: RagFlowClient,
    embedder: LiteLLMEmbedder,
    store: MilvusStore,
    previous_document_id: str | None,
) -> IngestOutcome:
    """处理单个文件：上传 → 解析 → 拉取切分 → 嵌入 → 写入 Milvus。

    任一环节失败即抛出平台异常，由调用方登记为失败文件并继续下一个——
    单个坏文件不得中断整批（CLAUDE.md 7.3 的"失败文件单独登记"要求）。
    """
    if previous_document_id:
        # 内容变更后的重入：先清旧产物，否则旧 chunk 会以孤儿行形式永久留在索引里。
        removed = await store.delete_by_document(previous_document_id, tenant_id)
        await ragflow.delete_documents(dataset_id, [previous_document_id])
        logger.info(
            "内容变更，已清理旧文档产物",
            extra={
                "event": "ingest.replaced",
                "source": rel_path,
                "document_id": previous_document_id,
                "removed_chunks": removed,
            },
        )

    doc = await ragflow.upload_document(dataset_id, abs_path)
    await ragflow.start_parsing(dataset_id, [doc.id])
    await ragflow.wait_parse_done(dataset_id, [doc.id])

    updated_at = int(datetime.now(UTC).timestamp())
    records: list[ChunkRecord] = []
    async for _page_index, page in ragflow.iter_chunks(dataset_id, doc.id):
        for chunk in page.chunks:
            if not chunk.available:
                # 被人工禁用的切分不入索引：它不应作为回答依据出现。
                continue
            if not chunk.content.strip():
                # 空切分无法向量化（ChunkRecord.content 要求 min_length=1），跳过。
                continue
            records.append(
                to_chunk_record(
                    chunk,
                    tenant_id=tenant_id,
                    dataset_id=dataset_id,
                    document_id=doc.id,
                    document_name=doc.name,
                    source_rel=rel_path,
                    updated_at=updated_at,
                )
            )

    if not records:
        return IngestOutcome(
            source_rel=rel_path, is_success=True, document_id=doc.id, chunk_count=0
        )

    written = 0
    for start in range(0, len(records), EMBED_BATCH_SIZE):
        batch = records[start : start + EMBED_BATCH_SIZE]
        vectors = await embedder.embed_documents([record.content for record in batch])
        written += await store.upsert_chunks(batch, vectors)

    return IngestOutcome(
        source_rel=rel_path, is_success=True, document_id=doc.id, chunk_count=written
    )


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
        (to_skip if recorded == current else to_ingest).append(rel_path)
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
        dataset_id = await ragflow.ensure_dataset(dataset_name)
        created = await store.ensure_collection()
        logger.info(
            "集合就绪",
            extra={
                "event": "ingest.collection_ready",
                "collection": store.collection,
                "created": created,
                "dim": store.dim,
                "dataset_id": dataset_id,
            },
        )
        for rel_path, abs_path in pairs:
            fingerprint = content_fingerprint(await asyncio.to_thread(abs_path.read_bytes))
            entry = manifest.files.get(rel_path)
            recorded = manifest.fingerprint_of(rel_path)
            if recorded == fingerprint:
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
                    previous_document_id=previous,
                )
            except AgentPlatformError as exc:
                # 单文件失败不中断整批（CLAUDE.md 7.3）。异常文本可能含内部信息，
                # 只记类型与错误码；细节留 DEBUG 级日志，不进常规日志（NO-07）。
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
                    },
                )
                logger.debug("失败详情", extra={"source": rel_path, "detail": str(exc)})
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
            },
        )
        logger.debug("失败详情", extra={"detail": str(exc)})
        return EXIT_FATAL


if __name__ == "__main__":
    raise SystemExit(main())
