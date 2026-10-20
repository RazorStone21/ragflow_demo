"""证据图片本地存储：魔数识别、大小上限、租户隔离与路径越界（ADR-0004）。

所有用例都在 ``tmp_path`` 下完成，不触碰真实 ``local_data/``。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent_platform.core.errors import ConfigurationError, KnowledgeError, ValidationFailedError
from agent_platform.knowledge.images import (
    LocalImageStore,
    detect_image_mime,
    open_image_store,
)
from agent_platform.knowledge.schemas import build_image_id
from agent_platform.settings import Settings

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _store(tmp_path: Path, *, max_bytes: int = 4096) -> LocalImageStore:
    return LocalImageStore(tmp_path / "images", max_bytes=max_bytes)


async def _put(store: LocalImageStore, data: bytes = _PNG, *, tenant: str = "demo") -> str:
    stored = await store.put(tenant_id=tenant, document_id="doc1", chunk_id="chunk1", data=data)
    return stored.image_id


# ---------------------------------------------------------------------------
# 魔数识别
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "image/png"),
        (b"GIF89a" + b"\x00" * 8, "image/gif"),
        (b"GIF87a" + b"\x00" * 8, "image/gif"),
        (b"II*\x00" + b"\x00" * 8, "image/tiff"),
        (b"MM\x00*" + b"\x00" * 8, "image/tiff"),
        (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4, "image/webp"),
        (b"BM" + b"\x00" * 12, "image/bmp"),
        (b"<html>not an image</html>", None),
        (b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 4, None),  # 不是 WEBP 的 RIFF
        (b"BM", None),  # 长度不足，不能把任意 BM 开头当作 BMP
        (b"", None),
    ],
)
def test_detect_image_mime(data: bytes, expected: str | None) -> None:
    """按魔数识别白名单内的六种图片；其余一律 None。"""
    assert detect_image_mime(data) == expected


# ---------------------------------------------------------------------------
# 写入与读取
# ---------------------------------------------------------------------------
async def test_put_and_get_round_trip(tmp_path: Path) -> None:
    """写入后可按同一组标识读回，摘要与 ID 对得上。"""
    store = _store(tmp_path)
    stored = await store.put(tenant_id="demo", document_id="doc1", chunk_id="chunk1", data=_PNG)

    assert stored.mime == "image/png"
    assert stored.size == len(_PNG)
    assert stored.sha256 == hashlib.sha256(_PNG).hexdigest()
    assert stored.image_id == build_image_id(stored.sha256)

    payload = await store.get(
        tenant_id="demo",
        document_id="doc1",
        chunk_id="chunk1",
        image_id=stored.image_id,
        mime=stored.mime,
    )
    assert payload == _PNG


async def test_put_is_idempotent_on_same_content(tmp_path: Path) -> None:
    """同一内容重复写入为覆盖，不产生第二份文件。"""
    store = _store(tmp_path)
    first = await _put(store)
    second = await _put(store)

    assert first == second
    files = list((tmp_path / "images").rglob("*.png"))
    assert len(files) == 1


async def test_put_rejects_empty_oversize_and_non_image(tmp_path: Path) -> None:
    """空内容、超出上限、非白名单格式一律拒收。"""
    store = _store(tmp_path, max_bytes=64)
    with pytest.raises(ValidationFailedError, match="为空"):
        await store.put(tenant_id="demo", document_id="d", chunk_id="c", data=b"")
    with pytest.raises(ValidationFailedError, match="超过上限"):
        await store.put(tenant_id="demo", document_id="d", chunk_id="c", data=_PNG + b"\x00" * 64)
    with pytest.raises(ValidationFailedError, match="受支持的图片格式"):
        await store.put(tenant_id="demo", document_id="d", chunk_id="c", data=b"<html>nope</html>")


async def test_get_missing_returns_none(tmp_path: Path) -> None:
    """读不到返回 None，由调用方统一按 404 处理。"""
    store = _store(tmp_path)
    missing = await store.get(
        tenant_id="demo",
        document_id="doc1",
        chunk_id="chunk1",
        image_id="img_" + "0" * 32,
        mime="image/png",
    )
    assert missing is None


async def test_get_does_not_leak_across_tenants(tmp_path: Path) -> None:
    """同一张图落在各自租户目录下，跨租户读不到（NO-19）。"""
    store = _store(tmp_path)
    image_id = await _put(store, tenant="tenant-a")

    assert (
        await store.get(
            tenant_id="tenant-a",
            document_id="doc1",
            chunk_id="chunk1",
            image_id=image_id,
            mime="image/png",
        )
        == _PNG
    )
    assert (
        await store.get(
            tenant_id="tenant-b",
            document_id="doc1",
            chunk_id="chunk1",
            image_id=image_id,
            mime="image/png",
        )
        is None
    )


async def test_delete_document_removes_only_that_document(tmp_path: Path) -> None:
    """删除文档图片不影响同租户的其他文档，也不影响其他租户。"""
    store = _store(tmp_path)
    await store.put(tenant_id="demo", document_id="doc1", chunk_id="c1", data=_PNG)
    await store.put(tenant_id="demo", document_id="doc2", chunk_id="c2", data=_JPEG)
    await store.put(tenant_id="other", document_id="doc1", chunk_id="c1", data=_PNG)

    assert await store.delete_document("demo", "doc1") == 1
    assert not (tmp_path / "images" / "demo" / "doc1").exists()
    assert (tmp_path / "images" / "demo" / "doc2").is_dir()
    assert (tmp_path / "images" / "other" / "doc1").is_dir()


async def test_delete_missing_document_returns_zero(tmp_path: Path) -> None:
    """删除不存在的目录返回 0，不抛异常（补偿删除需要可重入）。"""
    assert await _store(tmp_path).delete_document("demo", "nope") == 0


# ---------------------------------------------------------------------------
# 路径与参数校验
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["..", "../etc", "a/b", "", ".hidden"])
async def test_put_rejects_unsafe_path_components(tmp_path: Path, bad: str) -> None:
    """租户/文档/切分标识必须能安全拼进路径。"""
    store = _store(tmp_path)
    with pytest.raises(ValidationFailedError):
        await store.put(tenant_id=bad, document_id="doc1", chunk_id="chunk1", data=_PNG)
    with pytest.raises(ValidationFailedError):
        await store.put(tenant_id="demo", document_id=bad, chunk_id="chunk1", data=_PNG)
    with pytest.raises(ValidationFailedError):
        await store.put(tenant_id="demo", document_id="doc1", chunk_id=bad, data=_PNG)


@pytest.mark.parametrize("bad_id", ["img_XYZ", "../img_" + "0" * 32, "img_" + "0" * 31])
async def test_get_rejects_malformed_image_id(tmp_path: Path, bad_id: str) -> None:
    """图片 ID 由平台生成，调用方不得自行构造。"""
    with pytest.raises(ValidationFailedError, match="非法图片 ID"):
        await _store(tmp_path).get(
            tenant_id="demo",
            document_id="doc1",
            chunk_id="chunk1",
            image_id=bad_id,
            mime="image/png",
        )


async def test_get_rejects_unknown_mime(tmp_path: Path) -> None:
    """读取时的 MIME 也必须在白名单内，否则扩展名无从确定。"""
    with pytest.raises(ValidationFailedError, match="不支持的图片类型"):
        await _store(tmp_path).get(
            tenant_id="demo",
            document_id="doc1",
            chunk_id="chunk1",
            image_id="img_" + "0" * 32,
            mime="image/svg+xml",
        )


async def test_write_leaves_no_temp_files(tmp_path: Path) -> None:
    """临时文件在写入成功后必须清理干净。"""
    store = _store(tmp_path)
    await _put(store)
    leftovers = [path.name for path in (tmp_path / "images").rglob("*.tmp-*")]
    assert leftovers == []


async def test_read_failure_maps_to_knowledge_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件系统故障（非"不存在"）映射为 KnowledgeError，而不是静默返回空。"""
    store = _store(tmp_path)
    image_id = await _put(store)

    def boom(self: Path) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", boom)
    with pytest.raises(KnowledgeError, match="读取证据图片失败"):
        await store.get(
            tenant_id="demo",
            document_id="doc1",
            chunk_id="chunk1",
            image_id=image_id,
            mime="image/png",
        )


# ---------------------------------------------------------------------------
# 启动期校验与工厂
# ---------------------------------------------------------------------------
async def test_ensure_ready_creates_directory(tmp_path: Path) -> None:
    """启动期创建目录并验证可写。"""
    store = _store(tmp_path)
    await store.ensure_ready()
    assert (tmp_path / "images").is_dir()


async def test_ensure_ready_fails_when_root_is_a_file(tmp_path: Path) -> None:
    """目录无法创建时启动即失败（CLAUDE.md 3.5 fail-fast）。"""
    root = tmp_path / "images"
    root.write_bytes(b"not a directory")
    with pytest.raises(ConfigurationError, match="不可写"):
        await LocalImageStore(root, max_bytes=1024).ensure_ready()


async def test_open_image_store_uses_settings(tmp_path: Path) -> None:
    """工厂按配置构造存储（目录与字节上限都来自 ``Settings``）。"""
    settings = Settings.model_construct(
        evidence_image_store_dir=tmp_path / "imgs", evidence_image_max_bytes=64
    )
    store = open_image_store(settings)
    assert store.root == tmp_path / "imgs"
    with pytest.raises(ValidationFailedError, match="超过上限"):
        await store.put(tenant_id="demo", document_id="d", chunk_id="c", data=_PNG + b"\x00" * 64)
