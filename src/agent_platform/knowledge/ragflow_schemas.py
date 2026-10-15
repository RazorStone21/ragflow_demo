"""RAGFlow 外部系统响应的数据契约（CLAUDE.md 3.3：外部输入默认不可信）。

字段名与取值均依据 RAGFlow v0.27.2 源码核实（见 ADR-0002 与 CHANGELOG），
**不是**按文档示例臆测：文档中若干示例（如列表返回 ``total_datasets``、
删除文档只回 ``{"code": 0}``）已过时，以 v0.27.2 代码路径为准。

外部响应一律用 ``extra="ignore"`` 解析：RAGFlow 会在不同版本增删字段，
过度约束会让升级直接失败；只对我们**真正使用**的字段做校验。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, description="切分主键")
    content: str = Field(default="", description="切分正文")
    document_id: str = Field(default="", description="所属文档 ID")
    docnm_kwd: str = Field(default="", description="所属文档名（RAGFlow 字段名）")
    positions: list[list[int]] = Field(default_factory=list, description="位置信息")
    available: bool = Field(default=True, description="是否可用（未被人为禁用）")


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
