"""证据图片的本地文件存储（ADR-0004）。

演示期把图片二进制落在 ``EVIDENCE_IMAGE_STORE_DIR`` 下，按
``{tenant_id}/{document_id}/{chunk_id}/{image_id}.{ext}`` 分目录；生产换
MinIO / S3 时实现同一个 ``ImageStore`` 协议即可，调用方不感知。

几条不能省的保护：

- 路径组件只接受 ``[A-Za-z0-9][A-Za-z0-9._-]{0,127}``，拼接后再 ``resolve``
  核对没有越出根目录。两道防线防的是同一件事：调用方把 ``../`` 带进来（NO-19）。
- 图片类型按**魔数**判定，不信任上游的扩展名或 Content-Type；白名单之外的
  一律拒收，避免取图端点把任意内容当图片回给浏览器。
- 单张图片有字节上限，超出即拒收；空字节同样拒收。
- 写入先落临时文件再 ``os.replace``，进程中断不会留下半截文件。
- 全部文件 IO 走 ``asyncio.to_thread``（NO-05）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_platform.core.errors import ConfigurationError, KnowledgeError, ValidationFailedError
from agent_platform.core.logging import get_logger
from agent_platform.knowledge.schemas import ImageMime, build_image_id
from agent_platform.settings import Settings

logger = get_logger(__name__)

MIME_EXTENSIONS: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
}
"""MIME → 落盘扩展名。与 ``ImageMime`` 白名单保持一致。"""

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IMAGE_ID_PATTERN = re.compile(r"^img_[0-9a-f]{32}$")


@dataclass(frozen=True)
class StoredImage:
    """一张已落盘图片的摘要信息（存储层返回，不含路径）。"""

    image_id: str
    sha256: str
    mime: ImageMime
    size: int


class ImageStore(Protocol):
    """图片资产存储协议（本地文件是演示实现，生产可换对象存储）。"""

    async def put(
        self,
        *,
        tenant_id: str,
        document_id: str,
        chunk_id: str,
        data: bytes,
    ) -> StoredImage:
        """校验并保存一张图片，返回摘要信息。

        Raises:
            ValidationFailedError: 内容为空、超出上限或不是白名单内的图片类型。
        """
        ...

    async def get(
        self,
        *,
        tenant_id: str,
        document_id: str,
        chunk_id: str,
        image_id: str,
        mime: str,
    ) -> bytes | None:
        """读取图片；不存在返回 ``None``（调用方据此统一按 404 处理）。"""
        ...

    async def delete_document(self, tenant_id: str, document_id: str) -> int:
        """删除某文档的全部图片，返回删除的文件数（不存在时返回 0）。"""
        ...


def detect_image_mime(data: bytes) -> ImageMime | None:
    """按魔数判定图片类型；无法识别返回 ``None``。

    只认白名单内的六种格式。BMP 的 ``BM`` 前缀只有两个字节，额外要求最小
    文件长度，避免把任意以 ``BM`` 开头的二进制误判为图片。
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 14 and data.startswith(b"BM"):
        return "image/bmp"
    return None


class LocalImageStore:
    """基于本地文件系统的 ``ImageStore`` 实现。

    Args:
        root: 存储根目录；所有租户、文档的图片都在其下。
        max_bytes: 单张图片的字节上限。
    """

    def __init__(self, root: Path, *, max_bytes: int) -> None:
        self._root = Path(root)
        self._max_bytes = max_bytes

    @property
    def root(self) -> Path:
        """存储根目录（日志与测试断言用）。"""
        return self._root

    async def ensure_ready(self) -> None:
        """创建根目录并验证可写；失败即启动失败（CLAUDE.md 3.5 fail-fast）。

        Raises:
            ConfigurationError: 目录无法创建或不可写。
        """
        await asyncio.to_thread(self._ensure_ready_sync)

    def _ensure_ready_sync(self) -> None:
        probe = self._root / ".write_probe"
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            raise ConfigurationError(
                f"证据图片目录不可写: {self._root}",
                detail="请检查 EVIDENCE_IMAGE_STORE_DIR 指向的路径与权限。",
            ) from exc

    async def put(
        self,
        *,
        tenant_id: str,
        document_id: str,
        chunk_id: str,
        data: bytes,
    ) -> StoredImage:
        """校验并保存图片（同一内容重复写入为覆盖，幂等）。"""
        if not data:
            raise ValidationFailedError("图片内容为空", detail="RAGFlow 返回了空图片，已中止入库。")
        if len(data) > self._max_bytes:
            raise ValidationFailedError(
                f"图片大小 {len(data)} 字节超过上限 {self._max_bytes}",
                detail="请调整 EVIDENCE_IMAGE_MAX_BYTES，或将该文档移出多模态子集。",
            )
        mime = detect_image_mime(data)
        if mime is None:
            raise ValidationFailedError(
                "下载内容不是受支持的图片格式",
                detail="仅支持 JPEG / PNG / WEBP / GIF / BMP / TIFF。",
            )
        sha256 = hashlib.sha256(data).hexdigest()
        image_id = build_image_id(sha256)
        path = self._image_path(
            tenant_id=tenant_id,
            document_id=document_id,
            chunk_id=chunk_id,
            image_id=image_id,
            mime=mime,
        )
        await asyncio.to_thread(_write_atomic, path, data)
        return StoredImage(image_id=image_id, sha256=sha256, mime=mime, size=len(data))

    async def get(
        self,
        *,
        tenant_id: str,
        document_id: str,
        chunk_id: str,
        image_id: str,
        mime: str,
    ) -> bytes | None:
        """读取图片二进制；不存在返回 ``None``。

        Raises:
            KnowledgeError: 路径参数非法之外的文件系统错误（权限、磁盘故障）。
        """
        path = self._image_path(
            tenant_id=tenant_id,
            document_id=document_id,
            chunk_id=chunk_id,
            image_id=image_id,
            mime=mime,
        )
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise KnowledgeError(
                f"读取证据图片失败: {image_id}（{type(exc).__name__}）",
                detail="图片存储暂时不可用，请稍后重试。",
            ) from exc

    async def delete_document(self, tenant_id: str, document_id: str) -> int:
        """删除某文档的全部图片（重入库与失败补偿用）。"""
        directory = self._document_dir(tenant_id, document_id)
        return await asyncio.to_thread(_remove_tree, directory)

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def _document_dir(self, tenant_id: str, document_id: str) -> Path:
        self._require_component(tenant_id, "tenant_id")
        self._require_component(document_id, "document_id")
        directory = self._root / tenant_id / document_id
        self._ensure_within_root(directory)
        return directory

    def _image_path(
        self,
        *,
        tenant_id: str,
        document_id: str,
        chunk_id: str,
        image_id: str,
        mime: str,
    ) -> Path:
        self._require_component(chunk_id, "chunk_id")
        if not _IMAGE_ID_PATTERN.match(image_id):
            raise ValidationFailedError(
                f"非法图片 ID: {image_id}",
                detail="图片 ID 由平台生成，调用方不得自行构造。",
            )
        extension = MIME_EXTENSIONS.get(mime)
        if extension is None:
            raise ValidationFailedError(
                f"不支持的图片类型: {mime}",
                detail="仅支持 JPEG / PNG / WEBP / GIF / BMP / TIFF。",
            )
        path = self._document_dir(tenant_id, document_id) / chunk_id / f"{image_id}.{extension}"
        self._ensure_within_root(path)
        return path

    @staticmethod
    def _require_component(value: str, name: str) -> None:
        if not _SAFE_COMPONENT.match(value):
            raise ValidationFailedError(
                f"非法的 {name}: {value!r}",
                detail="标识只能包含字母、数字、点、下划线与连字符。",
            )

    def _ensure_within_root(self, path: Path) -> None:
        root = self._root.resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValidationFailedError(
                "证据图片路径越出存储根目录",
                detail="请求被拒绝。",
            )


def open_image_store(settings: Settings) -> LocalImageStore:
    """按配置构造本地图片存储（不建立连接，目录由 ``ensure_ready`` 创建）。"""
    return LocalImageStore(
        settings.evidence_image_store_dir, max_bytes=settings.evidence_image_max_bytes
    )


def _write_atomic(path: Path, data: bytes) -> None:
    """原子写入：临时文件 + ``os.replace``（同目录替换是原子操作）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                # 清理失败不影响正确性：临时文件名带进程与对象标识，不会覆盖正式文件。
                logger.debug(
                    "证据图片临时文件清理失败", extra={"event": "images.tmp_cleanup_failed"}
                )


def _remove_tree(directory: Path) -> int:
    """删除目录树并返回删除的文件数；目录不存在返回 0。"""
    if not directory.is_dir():
        return 0
    files = [path for path in directory.rglob("*") if path.is_file()]
    for path in sorted(files, reverse=True):
        try:
            path.unlink()
        except OSError:
            logger.warning(
                "证据图片删除失败",
                extra={"event": "images.delete_failed", "file_name": path.name},
            )
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_dir():
            with contextlib.suppress(OSError):
                path.rmdir()
    with contextlib.suppress(OSError):
        directory.rmdir()
    return len(files)


__all__: list[str] = [
    "MIME_EXTENSIONS",
    "ImageStore",
    "LocalImageStore",
    "StoredImage",
    "detect_image_mime",
    "open_image_store",
]
