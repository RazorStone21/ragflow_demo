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
    KnowledgeParseTimeoutError,
)
from agent_platform.knowledge.ragflow_schemas import ChunkInfo, ChunkPage, DocumentInfo
from agent_platform.settings import Settings
from agent_platform.utils.text import content_fingerprint
from scripts import ingest

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
        return DocumentInfo(id=doc_id, name=path.name)

    async def start_parsing(self, dataset_id: str, document_ids: list[str]) -> None:
        del dataset_id
        self.started.extend(document_ids)

    async def wait_parse_done(self, dataset_id: str, document_ids: list[str]) -> None:
        del dataset_id, document_ids

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


def manifest_with(tmp_path: Path, **files: str) -> Path:
    """把 ``文件名=内容`` 写成一个"已完成"的清单文件，返回其路径。"""
    path = tmp_path / "ingest_manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": ingest.MANIFEST_VERSION,
                "files": {
                    name: {"fingerprint": content_fingerprint(content.encode("utf-8"))}
                    for name, content in files.items()
                },
            }
        ),
        encoding="utf-8",
    )
    return path


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
    tmp_path: Path, stack: dict[str, Any], settings: Settings
) -> None:
    """首跑：上传 → 解析 → 拉切分 → 嵌入 → 写 Milvus，并把结果记账进清单。"""
    corpus = write_corpus(tmp_path, {"a.pdf": "甲"})
    code = await ingest.run(make_args(corpus), settings)

    assert code == ingest.EXIT_OK
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
