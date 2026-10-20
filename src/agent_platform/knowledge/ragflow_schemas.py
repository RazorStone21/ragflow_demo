"""RAGFlow 外部系统响应的数据契约（CLAUDE.md 3.3：外部输入默认不可信）。

字段名与取值均依据 RAGFlow v0.27.2 源码核实（见 ADR-0002 与 CHANGELOG），
**不是**按文档示例臆测：文档中若干示例（如列表返回 ``total_datasets``、
删除文档只回 ``{"code": 0}``）已过时，以 v0.27.2 代码路径为准。

外部响应一律用 ``extra="ignore"`` 解析：RAGFlow 会在不同版本增删字段，
过度约束会让升级直接失败；只对我们**真正使用**的字段做校验。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ParseStatus(StrEnum):
    """文档解析状态（RAGFlow ``TaskStatus`` 的名称形式）。

    API 响应返回名称（``"DONE"``），数据库内部存数字（``"3"``）；
    列表接口的 ``run`` 过滤参数两者都接受。
    """

    UNSTART = "UNSTART"
    RUNNING = "RUNNING"
    CANCEL = "CANCEL"
    DONE = "DONE"
    FAIL = "FAIL"
    SCHEDULE = "SCHEDULE"

    @property
    def is_terminal(self) -> bool:
        """是否已结束（无论成功与否）。"""
        return self in (ParseStatus.DONE, ParseStatus.FAIL, ParseStatus.CANCEL)

    @property
    def is_success(self) -> bool:
        """是否解析成功。"""
        return self is ParseStatus.DONE


class DatasetInfo(BaseModel):
    """数据集（知识库）信息。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, description="数据集 ID（路径参数用）")
    name: str = Field(default="", description="数据集名称")
    document_count: int = Field(default=0, ge=0)
    chunk_count: int = Field(default=0, ge=0)


class DocumentInfo(BaseModel):
    """文档信息（含解析状态）。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, description="文档 ID")
    name: str = Field(default="", description="文件名")
    location: str = Field(default="", description="RAGFlow 内部存储路径")
    size: int = Field(default=0, ge=0, description="字节数")
    run: ParseStatus = Field(default=ParseStatus.UNSTART, description="解析状态")
    progress: float = Field(default=0.0, ge=-1.0, le=1.0, description="进度；取消时为 -1")
    progress_msg: str = Field(default="", description="解析日志摘要")
    chunk_count: int = Field(default=0, ge=0, description="切分数量")


class ChunkInfo(BaseModel):
    """切分单元。

    ``positions`` 为 ``[页号(1 起), left, right, top, bottom]`` 的 5 元组列表，
    由 RAGFlow ``add_positions()`` 生成；纯文本切分可能为空列表。

    ``image_id`` 与 ``doc_type_kwd`` 的取值规律见 ADR-0004：前者形如
    ``{dataset_id}-{chunk_id}``，任一位置都可能出现连字符，只能按**首个**
    连字符拆分。

    ``doc_type_kwd`` 在本接口上**不能用来判媒介类型**：RAGFlow v0.27.2 的
    ``chunk_api.py`` 遇到非字符串取值一律写成 ``"text"``，而 Infinity 引擎
    把 ``*_kwd`` 字段存成 keyword 列表，于是表格/图片切分也读成 ``"text"``。
    真实类型只能从切分详情接口取（``ChunkDetailInfo`` / ``RagFlowClient.get_chunk``）。
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, description="切分主键")
    content: str = Field(default="", description="切分正文")
    document_id: str = Field(default="", description="所属文档 ID")
    docnm_kwd: str = Field(default="", description="所属文档名（RAGFlow 字段名）")
    positions: list[list[int]] = Field(default_factory=list, description="位置信息")
    available: bool = Field(default=True, description="是否可用（未被人为禁用）")
    image_id: str = Field(
        default="", max_length=256, description="切分关联图片 ID（空串表示无图，ADR-0004）"
    )
    doc_type_kwd: str = Field(
        default="",
        max_length=32,
        description="列表接口的媒介类型；非字符串一律被上游压成 text，不可信（ADR-0004）",
    )


def normalize_doc_type_kwd(value: object) -> str:
    """把切分详情的 ``doc_type_kwd`` 归一成单个类型名。

    RAGFlow 的存储引擎决定字段形状：Infinity 把 ``*_kwd`` 当 keyword 列表
    存取，详情接口因此回 ``["table"]`` 或 ``[]``；换回关系型引擎时是字符串。
    两种形状都要认，空列表代表"既不是表格也不是图片"。

    ``["image", "table"]`` 同时出现时按 ``image`` 归类：图片切分的失败语义
    更严（取不到图整份文件判失败），宁可按严的一档处理。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, (list, tuple)):
        items = [item.strip().lower() for item in value if isinstance(item, str)]
        if "image" in items:
            return "image"
        if "table" in items:
            return "table"
        return items[0] if items else ""
    return ""


class ChunkDetailInfo(BaseModel):
    """切分详情：只取 ``doc_type_kwd`` 的**原始**取值。

    详情接口返回整条存储记录，字段远多于列表接口（``img_id``、``content``
    等），本仓只需要类型字段，其余字段 ``extra="ignore"`` 丢弃。这里刻意
    不声明图片字段：列表接口已提供 ``image_id``，详情接口的字段名在版本间
    出现过 ``img_id`` / ``image_id`` 两种写法，依赖它会平添一个版本雷。
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, description="切分主键")
    doc_type_kwd: str = Field(
        default="",
        max_length=32,
        description="归一后的媒介类型：table / image / 其余为空（ADR-0004）",
    )

    @field_validator("doc_type_kwd", mode="before")
    @classmethod
    def _normalize_doc_type_kwd(cls, value: object) -> str:
        """构造函数即归一，调用方拿到的永远是单个小写类型名。"""
        return normalize_doc_type_kwd(value)


class DocumentPage(BaseModel):
    """文档列表分页结果。

    ``total`` 取自 v0.27.2 代码路径 ``{"docs": [...], "total": n}``；
    文档示例中的 ``total_datasets`` 为过时字段，此处不采信。
    """

    model_config = ConfigDict(extra="ignore")

    docs: list[DocumentInfo] = Field(default_factory=list)
    total: int = Field(default=0, ge=0, description="总条数")


class ChunkPage(BaseModel):
    """切分列表分页结果。"""

    model_config = ConfigDict(extra="ignore")

    chunks: list[ChunkInfo] = Field(default_factory=list)
    total: int = Field(default=0, ge=0, description="总条数")
