"""``scripts/ingest.py`` 的单元测试（CLAUDE.md 9.3：离线、确定、不碰外部服务）。

本文件重点锁定的是**幂等契约**，而不是"函数能跑"：入库脚本最大的风险不是崩溃，
而是悄悄重复入库、或改了内容之后旧切分变成永不命中的孤儿行。这两件事在真实环境里
都表现为"检索结果里冒出重复/过时证据"，很难现场归因，因此必须由单测钉死。
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_platform.core.errors import (
    ConfigurationError,
    KnowledgeError,
    KnowledgeParseFailedError,
    KnowledgeParseTimeoutError,
)
from agent_platform.knowledge.ragflow_schemas import (
    ChunkInfo,
    ChunkPage,
    DocumentInfo,
    ParseStatus,
)
from agent_platform.settings import Settings
from agent_platform.utils.text import content_fingerprint
from scripts import ingest

PARSE_FAILURE_MSG = (
    "\n14:39:17 Task has been received.\n"
    "14:39:17 Page(1~8): [ERROR]Fail to bind embedding model: No default embedding model is set.\n"
    "14:39:17 [ERROR][Exception]: No default embedding model is set.\n"
)
"""RAGFlow 真实报出的解析失败日志（2026-10-16 实测原文，用于锁失败详情提取）。"""

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeRagFlow:
    """RagFlow 客户端的离线替身（只实现脚本实际调用的方法）。"""

    def __init__(self, chunks_per_doc: int = 2) -> None:
        self.chunks_per_doc = chunks_per_doc
        self.include_noise = False
        """置位后每个文档额外产出"被人工禁用"与"空白正文"两种切分。"""
        self.uploaded: list[str] = []
        self.started: list[str] = []
        self.deleted: list[list[str]] = []
        self.parse_failures: set[str] = set()
        """文件名进入此集合 ⇒ 上传阶段即抛超时（模拟"根本没提交成功"）。"""
        self.fail_on_parse: set[str] = set()
        """文档 ID 进入此集合 ⇒ 解析以 ``FAIL`` 告终（模拟"提交了但解析失败"）。"""
        self.existing: set[str] = set()
        """当前存在于 RAGFlow 中的文档 ID。

        真实服务端对"删除不属于该数据集的 id"是**报错**而非幂等成功（实测源码
        ``document_api.py`` 的 ``delete_documents`` 先校验 id 属于该数据集）。替身若
        一律成功，"上一轮遗留文档已不在"这条最常见的真实路径就永远走不到，兜底逻辑
        也就无从验证——这正是"替身比现实宽松"式的假绿。
        """
        self._doc_seq = 0

    async def ensure_dataset(self, name: str) -> str:
        del name
        return "ds-1"

    async def upload_document(self, dataset_id: str, path: Path) -> DocumentInfo:
        del dataset_id
        self._doc_seq += 1
        doc_id = f"doc-{self._doc_seq}"
        self.uploaded.append(path.name)
        if path.name in self.parse_failures:
            raise KnowledgeParseTimeoutError(f"fake 解析超时: {path.name}")
        self.existing.add(doc_id)
        return DocumentInfo(id=doc_id, name=path.name)

    async def start_parsing(self, dataset_id: str, document_ids: list[str]) -> None:
        del dataset_id
        self.started.extend(document_ids)

    async def wait_parse_done(
        self, dataset_id: str, document_ids: list[str]
    ) -> dict[str, DocumentInfo]:
        """按真实契约返回**每个文档的最终状态**，不是 ``None``。

        替身此前返回 ``None``，于是"调用方丢掉状态判断"这个缺陷在单测里永不可见
        ——真实契约允许带着 ``FAIL`` 一起返回，"离开进行中"并不等于"解析成功"。
        """
        del dataset_id
        return {
            doc_id: DocumentInfo(
                id=doc_id,
                name=f"{doc_id}.pdf",
                run=ParseStatus.FAIL if doc_id in self.fail_on_parse else ParseStatus.DONE,
                progress_msg=PARSE_FAILURE_MSG if doc_id in self.fail_on_parse else "",
            )
            for doc_id in document_ids
        }

    async def iter_chunks(
        self, dataset_id: str, document_id: str
    ) -> AsyncIterator[tuple[int, ChunkPage]]:
        del dataset_id
        chunks = [
            ChunkInfo(
                id=f"{document_id}-c{index}",
                content=f"第 {index} 段：居民电价按年度核定并公示。",
                document_id=document_id,
                docnm_kwd="新能源电价政策.pdf",
                positions=[[1, 0, 0, 0, 0]],
            )
            for index in range(self.chunks_per_doc)
        ]
        if self.include_noise:
            chunks.append(
                ChunkInfo(
                    id=f"{document_id}-dis",
                    content="被人工禁用的一段",
                    document_id=document_id,
                    available=False,
                )
            )
            chunks.append(
                ChunkInfo(id=f"{document_id}-blank", content="  \n ", document_id=document_id)
            )
        yield 1, ChunkPage(chunks=chunks, total=len(chunks))

    async def delete_documents(self, dataset_id: str, document_ids: list[str]) -> int:
        del dataset_id
        missing = [doc_id for doc_id in document_ids if doc_id not in self.existing]
        if missing:
            raise KnowledgeError(f"Document not found: {', '.join(missing)}")
        self.existing.difference_update(document_ids)
        self.deleted.append(list(document_ids))
        return len(document_ids)


class FakeMilvusStore:
    """MilvusStore 的离线替身（记录写入的行，不做真实索引）。"""

    def __init__(self) -> None:
        self.collection = "fake_chunks"
        self.dim = 8
        self.rows: dict[str, str] = {}
        self.deleted: list[tuple[str, str]] = []
        self.upsert_batches = 0
        self.flush_calls = 0

    async def ensure_collection(self) -> bool:
        return True

    async def upsert_chunks(self, records: list[Any], embeddings: list[list[float]]) -> int:
        assert len(records) == len(embeddings), "记录与向量必须一一对应"
        self.upsert_batches += 1
        for record in records:
            # 主键沿用 chunk_id ⇒ 重复入库是覆盖而非新增（幂等契约的落点）
            self.rows[record.chunk_id] = record.content
        return len(records)

    async def flush(self) -> None:
        """收尾刷盘（Milvus 有界一致性：不 flush 则新数据一段时间内查不到）。

        替身里没有可见性窗口，因此本方法只记账。它的价值在于**保证替身与真实
        ``MilvusStore`` 的接口一致**——少了它会直接 ``AttributeError``，
        而不是让"忘了 flush"这种缺陷悄悄溜过单测。
        """
        self.flush_calls += 1

    async def delete_by_document(self, document_id: str, tenant_id: str) -> int:
        self.deleted.append((document_id, tenant_id))
        removed = [key for key in self.rows if key.startswith(f"{document_id}-")]
        for key in removed:
            del self.rows[key]
        return len(removed)


class FakeEmbedder:
    """确定性嵌入替身；可注入异常以覆盖 fail-fast 分支。"""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.dim = 8
        self.raises = raises
        self.calls: list[list[str]] = []

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.raises is not None:
            raise self.raises
        return [[0.1] * self.dim for _ in texts]


@asynccontextmanager
async def _open(value: Any) -> AsyncIterator[Any]:
    """把任意对象包成异步上下文管理器。"""
    yield value


class Spy:
    """记录被实例化次数的替身工厂。

    ``--dry-run`` 的承诺是"不发任何请求"，只有断言**客户端压根没被构造**才能证明：
    光看最终结果无法区分"没发请求"与"发了请求但没写库"。
    """

    def __init__(self, opener: Callable[[], Any]) -> None:
        self._opener = opener
        self.count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self.count += 1
        return self._opener()


# ---------------------------------------------------------------------------
# 夹具与构造辅助
# ---------------------------------------------------------------------------


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把 ingest 模块里的四个外部依赖全部替换为替身。

    替身经 monkeypatch 注入模块属性，而不是给 ``run()`` 加依赖参数——脚本的真实
    调用路径必须原样走一遍，否则测到的就不是产品代码会走的代码。
    """
    ragflow = FakeRagFlow()
    store = FakeMilvusStore()
    embedder = FakeEmbedder()
    spies = {
        "ragflow": Spy(lambda: _open(ragflow)),
        "gateway": Spy(lambda: _open(object())),
        "milvus": Spy(lambda: _open(store)),
        "embedder": Spy(lambda: embedder),
    }
    monkeypatch.setattr(ingest, "RagFlowClient", spies["ragflow"])
    monkeypatch.setattr(ingest, "GatewayHttpClient", spies["gateway"])
    monkeypatch.setattr(ingest, "open_milvus_store", spies["milvus"])
    monkeypatch.setattr(ingest, "LiteLLMEmbedder", spies["embedder"])
    return {"ragflow": ragflow, "store": store, "embedder": embedder, "spies": spies}


@pytest.fixture
def settings(fake_settings: Settings, tmp_path: Path) -> Settings:
    """在共享的 ``fake_settings`` 上只改清单路径。

    刻意不另造一份 Settings：必填配置散落多处就会漂移，而 ``fake_settings`` 是
    conftest 里已定的单一约定。
    """
    return fake_settings.model_copy(
        update={"ingest_manifest_path": tmp_path / "ingest_manifest.json"}
    )


def write_corpus(tmp_path: Path, files: dict[str, str]) -> Path:
    """在临时目录下写出语料，返回语料根目录。"""
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    for name, content in files.items():
        (corpus / name).write_text(content, encoding="utf-8")
    return corpus


def write_subset(tmp_path: Path, lines: list[str]) -> Path:
    """写出子集白名单文件。"""
    subset = tmp_path / "subset.txt"
    subset.write_text("\n".join(lines), encoding="utf-8")
    return subset


def make_args(
    source: Path,
    *,
    subset: Path | None = None,
    dry_run: bool = False,
    tenant: str | None = "t1",
    dataset: str | None = "ds",
) -> argparse.Namespace:
    """经**真实**命令行解析器构造参数，顺带覆盖参数定义本身。"""
    argv = ["--source", str(source)]
    if subset is not None:
        argv += ["--subset", str(subset)]
    if dry_run:
        argv.append("--dry-run")
    if tenant is not None:
        argv += ["--tenant", tenant]
    if dataset is not None:
        argv += ["--dataset", dataset]
    return ingest.build_parser().parse_args(argv)


def log_extras(record: logging.LogRecord) -> dict[str, Any]:
    """取日志记录上经 ``extra=`` 注入的自定义字段。

    ``LogRecord`` 不对这些字段做静态声明（它们是运行时挂上去的），直接 ``record.total``
    既过不了 mypy、也读不出拼错的键；统一走这里，键名写错时是 KeyError 而不是静默 None。
    """
    return vars(record)


def make_chunk(chunk_id: str = "c-1", content: str = "居民电价按年度核定。") -> ChunkInfo:
    """构造一个切分单元。"""
    return ChunkInfo(id=chunk_id, content=content, document_id="doc-1", docnm_kwd="文件.pdf")


def _write_manifest(tmp_path: Path, files: dict[str, dict[str, Any]]) -> Path:
    """按 ``{"version": ..., "files": ...}`` 结构写出清单文件，返回其路径。"""
    path = tmp_path / "ingest_manifest.json"
    path.write_text(
        json.dumps({"version": ingest.MANIFEST_VERSION, "files": files}), encoding="utf-8"
    )
    return path


def manifest_with(tmp_path: Path, **files: str) -> Path:
    """把 ``文件名=内容`` 写成一个"已完成"的清单文件，返回其路径。

    载荷必须与真实入库写下的形状一致（含 ``chunk_count`` 与 ``document_id``）：
    只写指纹的"半个记录"会命中 ``is_stale_manifest_entry``（``chunk_count`` 缺失即
    视为零切分陈旧条目），于是这个替身会悄悄把"跳过"测成"重跑"——测试照样是绿的，
    但它验证的已经不是产品代码的路径了。
    """
    return _write_manifest(
        tmp_path,
        {
            name: {
                "fingerprint": content_fingerprint(content.encode("utf-8")),
                "document_id": "doc-1",
                "chunk_count": 2,
            }
            for name, content in files.items()
        },
    )


def manifest_with_zero_chunk_entry(tmp_path: Path, name: str, content: str) -> Path:
    """写一条 ``chunk_count == 0`` 的记录——修复前缺陷版本留下的真实形状。

    实测该版本把 5 份 ``run=FAIL`` 的文档记成"成功 5 份、切分 0 条"，且带上了
    ``document_id``（指向那个已 FAIL 的文档）。
    """
    return _write_manifest(
        tmp_path,
        {
            name: {
                "fingerprint": content_fingerprint(content.encode("utf-8")),
                "document_id": "doc-gone",
                "chunk_count": 0,
            }
        },
    )


# ---------------------------------------------------------------------------
# 子集清单解析
# ---------------------------------------------------------------------------


def test_load_subset_ignores_comments_and_blank_lines(tmp_path: Path) -> None:
    """注释与空行不参与解析，其余行按原样保留。"""
    path = write_subset(tmp_path, ["# 说明", "", "a.pdf", "  ", "# 又一行注释", "b.xlsx"])
    assert ingest.load_subset(path) == ["a.pdf", "b.xlsx"]


def test_load_subset_rejects_path_separators(tmp_path: Path) -> None:
    """白名单只允许文件名：带目录会让"相对哪个根"变得含糊。"""
    path = write_subset(tmp_path, ["sub/a.pdf"])
    with pytest.raises(ConfigurationError, match="目录分隔符"):
        ingest.load_subset(path)


def test_load_subset_rejects_duplicates(tmp_path: Path) -> None:
    """重复条目直接报错而非静默去重——静默会掩盖清单自身的问题。"""
    path = write_subset(tmp_path, ["a.pdf", "a.pdf", "b.pdf"])
    with pytest.raises(ConfigurationError, match="重复条目"):
        ingest.load_subset(path)


def test_load_subset_rejects_empty(tmp_path: Path) -> None:
    """全是注释等同于空清单，应报错而不是当作"没有要入库的文件"。"""
    path = write_subset(tmp_path, ["# 只有注释"])
    with pytest.raises(ConfigurationError, match="为空"):
        ingest.load_subset(path)


def test_load_subset_rejects_missing_file(tmp_path: Path) -> None:
    """清单文件本身不存在时给出明确错误。"""
    with pytest.raises(ConfigurationError, match="子集清单不存在"):
        ingest.load_subset(tmp_path / "nope.txt")


# ---------------------------------------------------------------------------
# 文件解析
# ---------------------------------------------------------------------------


def test_resolve_files_reports_missing_members(tmp_path: Path) -> None:
    """白名单里有、语料目录里没有的文件必须报错（往往意味着 --source 指错了）。"""
    corpus = write_corpus(tmp_path, {"a.pdf": "内容"})
    subset = write_subset(tmp_path, ["a.pdf", "b.pdf"])
    with pytest.raises(ConfigurationError, match="不存在"):
        ingest.resolve_files(corpus, subset)


def test_resolve_files_filters_by_suffix_without_subset(tmp_path: Path) -> None:
    """未指定白名单时递归扫描，且只收受支持的扩展名（大小写不敏感）。"""
    corpus = write_corpus(tmp_path, {"a.pdf": "x", "b.txt": "y", "c.bin": "z"})
    (corpus / "nested").mkdir()
    (corpus / "nested" / "d.PDF").write_text("w", encoding="utf-8")
    assert {rel for rel, _ in ingest.resolve_files(corpus, None)} == {
        "a.pdf",
        "b.txt",
        "nested/d.PDF",
    }


def test_resolve_files_rejects_missing_source(tmp_path: Path) -> None:
    """语料目录不存在时报错，而不是当成"没有文件"静默成功。"""
    with pytest.raises(ConfigurationError, match="语料目录不存在"):
        ingest.resolve_files(tmp_path / "nope", None)


# ---------------------------------------------------------------------------
# 映射：ChunkInfo → ChunkRecord
# ---------------------------------------------------------------------------


def test_to_chunk_record_maps_fields() -> None:
    """逐字段对齐，且指纹取**该 chunk 内容**而非整份文件。"""
    record = ingest.to_chunk_record(
        make_chunk("c-1", "居民电价按年度核定。"),
        tenant_id="t1",
        dataset_id="ds-1",
        document_id="doc-1",
        document_name="",
        source_rel="0001.文件.pdf",
        updated_at=1700000000,
    )
    assert record.chunk_id == "c-1", "主键必须沿用 RAGFlow chunk_id，否则 upsert 退化为新增"
    assert record.tenant_id == "t1"
    assert record.dataset_id == "ds-1"
    assert record.source_uri == "0001.文件.pdf", "存相对路径：绝对路径随机器而变，不可复现"
    assert record.document_name == "文件.pdf", "document_name 为空时回落到 chunk.docnm_kwd"
    assert record.updated_at == 1700000000
    assert len(record.content_fingerprint) == 64, "sha256 十六进制应恰好 64 位"

    other = ingest.to_chunk_record(
        make_chunk("c-2", "另一段内容。"),
        tenant_id="t1",
        dataset_id="ds-1",
        document_id="doc-1",
        document_name="文件.pdf",
        source_rel="0001.文件.pdf",
        updated_at=1700000000,
    )
    assert record.content_fingerprint != other.content_fingerprint, "指纹须随内容变化"


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


def test_manifest_roundtrip(tmp_path: Path) -> None:
    """清单可写可读，指纹按文件相对路径索引。"""
    path = tmp_path / "m.json"
    manifest = ingest.Manifest.load(path)
    manifest.record("a.pdf", {"fingerprint": "f1", "document_id": "doc-1"})
    manifest.save()

    reloaded = ingest.Manifest.load(path)
    assert reloaded.fingerprint_of("a.pdf") == "f1"
    assert reloaded.fingerprint_of("b.pdf") is None
    assert reloaded.files["a.pdf"]["document_id"] == "doc-1"


def test_manifest_rejects_version_mismatch(tmp_path: Path) -> None:
    """版本不兼容必须拒绝加载：宁可报错让人处理，也不要按错误结构静默重跑。"""
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"version": 99, "files": {}}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="版本不兼容"):
        ingest.Manifest.load(path)


def test_manifest_rejects_corrupt_json(tmp_path: Path) -> None:
    """损坏的清单不能当成"空清单"，否则会静默全量重跑。"""
    path = tmp_path / "m.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="无法解析"):
        ingest.Manifest.load(path)


# ---------------------------------------------------------------------------
# 干跑
# ---------------------------------------------------------------------------


async def test_dry_run_makes_no_network_calls(
    tmp_path: Path, stack: dict[str, Any], settings: Settings
) -> None:
    """--dry-run 的承诺是"不发任何请求"，用"客户端没被构造"来证明。"""
    corpus = write_corpus(tmp_path, {"a.pdf": "甲", "b.pdf": "乙"})
    code = await ingest.run(make_args(corpus, dry_run=True), settings)

    assert code == ingest.EXIT_OK
    for name, spy in stack["spies"].items():
        assert spy.count == 0, f"干跑不得构造 {name} 客户端"
    assert not settings.ingest_manifest_path.exists(), "干跑不得写清单"


async def test_dry_run_distinguishes_pending_from_skipped(
    tmp_path: Path, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """干跑必须区分"待入库"与"指纹未变将跳过"，否则它给不出决策信息。"""
    corpus = write_corpus(tmp_path, {"a.pdf": "甲", "b.pdf": "乙"})
    manifest_with(tmp_path, **{"a.pdf": "甲"})

    with caplog.at_level(logging.INFO):
        code = await ingest.run(make_args(corpus, dry_run=True), settings)

    assert code == ingest.EXIT_OK
    events = {log_extras(record).get("event", ""): record for record in caplog.records}
    assert "ingest.dry_run" in events, f"干跑未输出汇总事件，实际为：{sorted(events)}"
    summary = log_extras(events["ingest.dry_run"])
    assert (summary["total"], summary["pending"], summary["skipped"]) == (2, 1, 1)
    assert log_extras(events["ingest.plan.pending"])["source"] == "b.pdf"
    assert log_extras(events["ingest.plan.skip"])["source"] == "a.pdf"


async def test_invalid_subset_fails_before_any_client(
    tmp_path: Path, stack: dict[str, Any], settings: Settings
) -> None:
    """参数错误必须在建客户端之前暴露，否则一次手误要等连完库才发现。"""
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})
    subset = write_subset(tmp_path, ["missing.pdf"])
    with pytest.raises(ConfigurationError):
        await ingest.run(make_args(corpus, subset=subset, dry_run=True), settings)
    assert stack["spies"]["ragflow"].count == 0


# ---------------------------------------------------------------------------
# 入库与幂等
# ---------------------------------------------------------------------------


async def test_run_ingests_files_and_records_manifest(
    tmp_path: Path, stack: dict[str, Any], settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """首跑：上传 → 解析 → 拉切分 → 嵌入 → 写 Milvus，并把结果记账进清单。

    刻意开 INFO 级跑完整条路径：``logging`` 只在 ``isEnabledFor`` 通过后才调用
    ``makeRecord``，所以 pytest 默认级别下 INFO 语句**根本不执行**——实测
    ``extra={"created": ...}``（LogRecord 保留属性）正是这样躲过了单测、
    在真实入库时以 ``KeyError`` 炸掉。级别开在这里等于让每条日志都过一次真火。
    """
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})
    with caplog.at_level(logging.INFO):
        code = await ingest.run(make_args(corpus), settings)

    assert code == ingest.EXIT_OK
    events = {log_extras(record).get("event", ""): record for record in caplog.records}
    assert "ingest.collection_ready" in events, f"缺少集合就绪事件，实际为：{sorted(events)}"
    assert "ingest.done" in events, f"缺少完成事件，实际为：{sorted(events)}"
    assert stack["ragflow"].uploaded == ["a.pdf"]
    assert stack["ragflow"].started == ["doc-1"]
    assert set(stack["store"].rows) == {"doc-1-c0", "doc-1-c1"}
    # 批次末尾必须收口 flush，且只做一次：Milvus 有界一致性下不 flush 会导致
    # "刚入库却检索不到"（NO-12 降级分支），而按 32 条一批 flush 又会封存大量小段。
    assert stack["store"].flush_calls == 1

    manifest = ingest.Manifest.load(settings.ingest_manifest_path)
    assert manifest.fingerprint_of("a.pdf") == content_fingerprint("甲".encode())
    assert manifest.files["a.pdf"]["document_id"] == "doc-1"
    assert manifest.files["a.pdf"]["chunk_count"] == 2
    assert manifest.files["a.pdf"]["tenant_id"] == "t1"


async def test_unavailable_and_blank_chunks_never_reach_index(
    tmp_path: Path, stack: dict[str, Any], settings: Settings
) -> None:
    """被人工禁用的切分与空白正文不得入索引（NO-12：不得作为回答依据）。"""
    stack["ragflow"].include_noise = True
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})
    await ingest.run(make_args(corpus), settings)

    assert set(stack["store"].rows) == {"doc-1-c0", "doc-1-c1"}


async def test_parse_failure_is_never_recorded_as_success(
    tmp_path: Path, stack: dict[str, Any], settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """RAGFlow 报 ``FAIL`` 时必须记为失败，且**不得写进幂等清单**。

    实测事故（2026-10-16）：5 份文档全部因 "No default embedding model is set"
    解析失败，脚本却报"成功 5 份、切分 0 条"，并把它们记进清单——于是重跑全部跳过，
    问题被永久掩盖。**"离开进行中"不等于"解析成功"**。
    """
    stack["ragflow"].fail_on_parse = {"doc-1"}
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})

    with caplog.at_level(logging.INFO):
        code = await ingest.run(make_args(corpus), settings)

    assert code == ingest.EXIT_PARTIAL
    assert stack["store"].rows == {}, "解析失败的文档不得写入任何向量"
    manifest = ingest.Manifest.load(settings.ingest_manifest_path)
    assert manifest.fingerprint_of("a.pdf") is None, "失败的文件不得进清单，否则重跑会跳过"
    # 失败文档必须当场清掉：RAGFlow 拒绝同名重复上传，留着它重跑必撞
    # "Duplicated document name in the same dataset."，失败文件就再也补不上
    assert stack["ragflow"].deleted == [["doc-1"]], "失败文档未做补偿删除"

    events = {log_extras(record).get("event", ""): record for record in caplog.records}
    failure = log_extras(events["ingest.file_failed"])
    assert failure["error_code"] == "KNOWLEDGE_PARSE_FAILED"
    # 失败原因必须当场可见：只给错误码等于让操作者自己去猜
    assert "No default embedding model" in failure["error_message"]


async def test_cleanup_failure_does_not_mask_the_original_error(
    tmp_path: Path,
    stack: dict[str, Any],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """补偿删除自身失败时只记 WARNING，原始失败照旧上报（NO-06：失败要出声）。"""
    stack["ragflow"].fail_on_parse = {"doc-1"}

    async def boom(dataset_id: str, document_ids: list[str]) -> int:
        del dataset_id, document_ids
        raise KnowledgeError("fake 删除失败")

    monkeypatch.setattr(stack["ragflow"], "delete_documents", boom)
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})

    with caplog.at_level(logging.INFO):
        code = await ingest.run(make_args(corpus), settings)

    assert code == ingest.EXIT_PARTIAL, "清理失败不得把失败改写成成功"
    events = [log_extras(record).get("event") for record in caplog.records]
    assert "ingest.cleanup_failed" in events, "清理失败必须留痕，否则孤儿文档无从追查"
    assert "ingest.file_failed" in events, "原始失败原因不得被清理异常顶掉"


async def test_ingest_one_exposes_parse_failure_with_root_cause(
    tmp_path: Path, stack: dict[str, Any], settings: Settings
) -> None:
    """单文件入口直接抛解析失败异常，且**根因进 message**（操作者看的就是这一条）。"""
    del settings
    stack["ragflow"].fail_on_parse = {"doc-1"}
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})

    with pytest.raises(KnowledgeParseFailedError) as excinfo:
        await ingest.ingest_one(
            rel_path="a.pdf",
            abs_path=corpus / "a.pdf",
            tenant_id="t1",
            dataset_id="ds-1",
            ragflow=stack["ragflow"],
            embedder=stack["embedder"],
            store=stack["store"],
            previous_document_id=None,
        )

    assert excinfo.value.code.value == "KNOWLEDGE_PARSE_FAILED"
    assert "No default embedding model" in excinfo.value.message
    # 有界：至多摘末尾 3 个非空行（本例恰好只有 3 行，故全取），不整段灌进异常与日志
    assert excinfo.value.message.count(" / ") <= 2


def test_parse_failure_detail_falls_back_when_status_is_missing() -> None:
    """RAGFlow 没返回状态/日志时给出可执行的下一步，而不是空字符串。"""
    assert "控制台" in ingest.parse_failure_detail(None)
    assert "控制台" in ingest.parse_failure_detail(
        DocumentInfo(id="doc-1", name="a.pdf", run=ParseStatus.FAIL, progress_msg="  \n ")
    )


async def test_done_without_usable_chunks_is_a_failure(
    tmp_path: Path, stack: dict[str, Any], settings: Settings
) -> None:
    """解析成功但一条可用切分都没有 ⇒ 失败（"零入库"不得表现为成功）。"""
    stack["ragflow"].chunks_per_doc = 0
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})

    code = await ingest.run(make_args(corpus), settings)

    assert code == ingest.EXIT_PARTIAL
    assert stack["store"].flush_calls == 0, "没有任何成功项时不应做收尾 flush"
    manifest = ingest.Manifest.load(settings.ingest_manifest_path)
    assert manifest.fingerprint_of("a.pdf") is None


async def test_rerun_skips_unchanged_files(
    tmp_path: Path, stack: dict[str, Any], settings: Settings
) -> None:
    """幂等核心：内容未变时重跑**不得**产生任何上传或写入。

    用"上传次数仍为 1"断言，而不是看最终行数——重复入库若被主键覆盖，行数不变
    但请求已经发出，那正是要防的浪费与风险。
    """
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})
    await ingest.run(make_args(corpus), settings)
    batches_after_first = stack["store"].upsert_batches

    code = await ingest.run(make_args(corpus), settings)

    assert code == ingest.EXIT_OK
    assert stack["ragflow"].uploaded == ["a.pdf"], "第二次不得再上传"
    assert stack["store"].upsert_batches == batches_after_first, "第二次不得再写 Milvus"
    assert len(stack["embedder"].calls) == 1, "第二次不得再调嵌入"


async def test_changed_content_replaces_old_document(
    tmp_path: Path, stack: dict[str, Any], settings: Settings
) -> None:
    """内容变更后必须清掉旧 document 的产物。

    Milvus 主键是 chunk_id，内容一改 chunk_id 全变，旧行不会被 upsert 覆盖，
    只会变成永不命中的孤儿——检索时表现为"过时证据与现行证据并存"。
    """
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})
    await ingest.run(make_args(corpus), settings)
    old_chunk_ids = set(stack["store"].rows)

    (corpus / "a.pdf").write_text("乙", encoding="utf-8")
    code = await ingest.run(make_args(corpus), settings)

    assert code == ingest.EXIT_OK
    assert stack["store"].deleted == [("doc-1", "t1")], "应先按旧 document_id 清 Milvus"
    assert stack["ragflow"].deleted == [["doc-1"]], "同时清 RAGFlow 侧旧文档"
    assert not (old_chunk_ids & set(stack["store"].rows)), "旧 chunk 不得残留"


async def test_zero_chunk_manifest_entry_is_reingested_not_skipped(
    tmp_path: Path, stack: dict[str, Any], settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """``chunk_count == 0`` 的清单记录必须当作"未入库"重跑，而不是按指纹跳过。

    这类记录在当前代码里已不可能产生（零切分直接抛 ``KnowledgeParseFailedError``），
    所以它是修复前缺陷版本的化石。若照旧相信它，这批文件会被**永久**跳过——不报错、
    不留痕，比当场失败更难发现。
    """
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})
    manifest_with_zero_chunk_entry(tmp_path, "a.pdf", "甲")

    with caplog.at_level(logging.INFO):
        code = await ingest.run(make_args(corpus), settings)

    assert code == ingest.EXIT_OK
    assert stack["ragflow"].uploaded == ["a.pdf"], "零切分的陈旧记录不得让文件被跳过"
    assert set(stack["store"].rows) == {"doc-1-c0", "doc-1-c1"}
    events = {log_extras(record).get("event", "") for record in caplog.records}
    assert "ingest.stale_manifest_entry" in events, "静默重跑与静默跳过一样不可接受"
    manifest = ingest.Manifest.load(settings.ingest_manifest_path)
    assert manifest.files["a.pdf"]["chunk_count"] == 2, "重跑后应写下真实切分数"


async def test_absent_previous_document_does_not_block_reingest(
    tmp_path: Path, stack: dict[str, Any], settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """上一轮遗留文档已不在 RAGFlow 时，重入仍须成功。

    真实服务端对"删除不属于该数据集的 id"是**报错**而非幂等成功，而这恰恰是最常见的
    情形：上一轮的补偿删除已经把失败文档清掉了，或操作者在控制台手工清理过。把这一步
    当成硬失败，文件会在上传之前就卡死——"重跑只补失败的"这一承诺随之落空。
    """
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})
    manifest_with(tmp_path, **{"a.pdf": "旧内容"})  # 指纹不同 ⇒ 走重入；其文档已不在 RAGFlow

    with caplog.at_level(logging.INFO):
        code = await ingest.run(make_args(corpus), settings)

    assert code == ingest.EXIT_OK, "旧文档不存在不该让整个文件失败"
    assert stack["ragflow"].uploaded == ["a.pdf"], "清理失败后仍须继续上传新文档"
    assert stack["store"].deleted == [("doc-1", "t1")], "Milvus 侧旧产物仍应清掉"
    events = {log_extras(record).get("event", "") for record in caplog.records}
    assert "ingest.cleanup_failed" in events, "清理未成功必须留痕"


async def test_tenant_override_reaches_stored_records(
    tmp_path: Path, stack: dict[str, Any], settings: Settings
) -> None:
    """--tenant 覆盖配置默认值，且租户落到每条记录上（NO-19 多租户隔离）。"""
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})
    await ingest.run(make_args(corpus, tenant="tenant-b"), settings)

    manifest = ingest.Manifest.load(settings.ingest_manifest_path)
    assert manifest.files["a.pdf"]["tenant_id"] == "tenant-b"


# ---------------------------------------------------------------------------
# 失败处理
# ---------------------------------------------------------------------------


async def test_failed_file_is_registered_and_batch_continues(
    tmp_path: Path, stack: dict[str, Any], settings: Settings
) -> None:
    """单个坏文件不能中断整批：其余照常入库，退出码为"部分失败"。"""
    corpus = write_corpus(tmp_path, {"bad.pdf": "坏", "good.pdf": "好"})
    stack["ragflow"].parse_failures = {"bad.pdf"}

    code = await ingest.run(make_args(corpus), settings)

    assert code == ingest.EXIT_PARTIAL
    assert stack["ragflow"].uploaded == ["bad.pdf", "good.pdf"], "失败后应继续处理下一个"
    manifest = ingest.Manifest.load(settings.ingest_manifest_path)
    assert manifest.fingerprint_of("good.pdf") is not None
    assert manifest.fingerprint_of("bad.pdf") is None, "失败文件不得记账，否则重跑会被当成已完成"


async def test_embedding_failure_does_not_write_vectors(
    tmp_path: Path, stack: dict[str, Any], settings: Settings
) -> None:
    """嵌入失败时不得写入任何向量：宁可整文件失败，也不要半截数据进索引。"""
    stack["embedder"].raises = KnowledgeError("嵌入式维度不符: 返回 3 维，配置 MILVUS_DIM=4096")
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})

    code = await ingest.run(make_args(corpus), settings)

    assert code == ingest.EXIT_PARTIAL
    assert stack["store"].rows == {}
    assert ingest.Manifest.load(settings.ingest_manifest_path).fingerprint_of("a.pdf") is None


async def test_setup_failure_aborts_whole_run(
    tmp_path: Path, stack: dict[str, Any], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前置条件（数据集）建不起来属致命错误：整体抛出，不逐文件撞墙。"""

    async def _boom(name: str) -> str:
        del name
        raise KnowledgeError("RAGFlow 不可达")

    monkeypatch.setattr(stack["ragflow"], "ensure_dataset", _boom)
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})

    with pytest.raises(KnowledgeError):
        await ingest.run(make_args(corpus), settings)
    assert stack["ragflow"].uploaded == [], "前置失败后不得再上传任何文件"


# ---------------------------------------------------------------------------
# 命令行契约
# ---------------------------------------------------------------------------


def test_parser_requires_source() -> None:
    """--source 必填：语料不在仓库里（NO-17），没有可用的默认值。"""
    with pytest.raises(SystemExit):
        ingest.build_parser().parse_args([])


def test_exit_codes_are_distinct_and_documented() -> None:
    """退出码是对外契约：既不得重复，也不得与 --help 的声明脱节。"""
    codes = (ingest.EXIT_OK, ingest.EXIT_USAGE, ingest.EXIT_PARTIAL, ingest.EXIT_FATAL)
    assert len(set(codes)) == len(codes)
    epilog = ingest.build_parser().epilog
    assert epilog is not None
    for code in codes:
        assert str(code) in epilog
