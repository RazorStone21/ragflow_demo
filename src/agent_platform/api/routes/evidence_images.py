"""会话证据图片路由（ADR-0004 / CLAUDE.md 8.6）。

``GET /api/v1/threads/{thread_id}/evidence/{chunk_id}/images/{image_id}``

取图**不做**"按 ID 查库"：URL 里的 ``chunk_id`` 与 ``image_id`` 只当作
**在本次会话证据里的定位键**，命不中就是 404。这样即便有人拿到别处流传的
图片 ID，也必须有属于自己租户、且证据里确实有这张图的会话才取得出字节——
比单独做一套图片权限表更简单，也更难出错（NO-19）。

失败语义：只有"没带对凭据"是 401，其余（会话不存在、跨租户、证据里没有这条
chunk、图不属于该 chunk、文件缺失、读失败）一律 404。区分它们等于提供一个
存在性探针；路径参数也**不**用 ``Path(pattern=...)`` 校验—— FastAPI 会先返回
422，那同样泄露了"格式对不对"这一位信息。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from agent_platform.api.deps import AuthDep, DepsDep, authorize_thread_id, require_existing_thread
from agent_platform.core.errors import AgentPlatformError, NotFoundError
from agent_platform.core.logging import get_logger
from agent_platform.knowledge.images import MIME_EXTENSIONS
from agent_platform.knowledge.schemas import EvidenceItem, ImageRef

logger = get_logger(__name__)

router = APIRouter(prefix="/threads/{thread_id}/evidence", tags=["evidence"])


def _find_image(
    values: dict[str, Any], *, chunk_id: str, image_id: str
) -> tuple[EvidenceItem, ImageRef] | None:
    """在会话证据里精确匹配 (chunk_id, image_id)。

    证据条目可能是反序列化后的原始 dict（旧 checkpoint），逐条按契约校验；
    校验失败的条目跳过而不是让整个端点 500——一张坏证据不该拖垮整个取图。
    """
    for raw in values.get("evidence") or []:
        try:
            item = EvidenceItem.model_validate(raw)
        except ValueError:
            continue
        if item.chunk_id != chunk_id:
            continue
        for ref in item.images:
            if ref.image_id == image_id:
                return item, ref
    return None


@router.get(
    "/{chunk_id}/images/{image_id}",
    response_class=Response,
    summary="读取会话证据图片",
    responses={
        200: {"content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}}},
        404: {"description": "会话、证据或图片不存在（不区分原因）"},
    },
)
async def get_evidence_image(
    thread_id: str,
    chunk_id: str,
    image_id: str,
    deps: DepsDep,
    auth: AuthDep,
) -> Response:
    """返回会话证据中一张图片的二进制。

    响应头 ``Cache-Control: private, no-store`` 与 ``nosniff`` 是取图端点的
    最低要求：前者避免图片落进共享缓存，后者阻止浏览器按内容猜类型执行。

    Raises:
        NotFoundError: 任何"取不到"的情形（见模块说明）。
    """
    authorize_thread_id(thread_id, auth)
    snapshot = await require_existing_thread(deps, thread_id)
    values = snapshot.values if isinstance(snapshot.values, dict) else {}

    found = _find_image(values, chunk_id=chunk_id, image_id=image_id)
    if found is None:
        logger.warning(
            "会话证据中没有该图片，按 404 处理",
            extra={
                "event": "evidence_image.not_in_session",
                "thread_id": thread_id,
                "chunk_id": chunk_id,
                "image_id": image_id,
            },
        )
        raise NotFoundError("图片不存在", detail="该图片不在此会话的引用证据中。")

    item, ref = found
    try:
        data = await deps.image_store.get(
            tenant_id=auth.tenant_id,
            document_id=item.document_id,
            chunk_id=item.chunk_id,
            image_id=ref.image_id,
            mime=ref.mime,
        )
    except AgentPlatformError as exc:
        # 文件系统故障与"文件不存在"对调用方是同一个 404，但服务端必须留下 ERROR：
        # 前者需要运维介入，静默按 404 会让磁盘故障伪装成"用户点错了链接"。
        logger.error(
            "读取证据图片失败",
            extra={
                "event": "evidence_image.read_failed",
                "thread_id": thread_id,
                "chunk_id": chunk_id,
                "image_id": image_id,
                "error": type(exc).__name__,
            },
        )
        raise NotFoundError("图片不存在", detail="图片暂时无法读取。") from exc
    if data is None:
        logger.warning(
            "证据图片文件缺失",
            extra={
                "event": "evidence_image.missing_file",
                "thread_id": thread_id,
                "chunk_id": chunk_id,
                "image_id": image_id,
            },
        )
        raise NotFoundError("图片不存在", detail="图片文件已不存在，请重新入库。")

    extension = MIME_EXTENSIONS.get(ref.mime, "bin")
    return Response(
        content=data,
        media_type=ref.mime,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{ref.image_id}.{extension}"',
        },
    )


__all__: list[str] = ["router"]
