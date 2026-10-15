"""GovPolicyQa 的结构化契约（CLAUDE.md 2.2 / 5.6 / 7.4）。

包含两部分：
- ``Citation``：答案引用的展示用条目（不复制证据全文，避免响应体膨胀）；
- 引用标记的解析与校验：把模型答案里的 ``[n]`` 标记解析为证据下标。这是
  "无引用不得作为事实结论"（NO-12）在输出边界的确定性校验——不做语义猜测，
  只承认指向已有证据的合法下标。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from agent_platform.knowledge.schemas import EvidenceItem

# 半角 ``[1]`` 与全角 ``【1】`` 都接受：中文模型偶发全角括号，语义无歧义；
# 逗号分隔的多引用 ``[1,3]`` 拆为两个下标。
_CITATION_PATTERN = re.compile(r"[\[【]\s*(\d+(?:\s*[,，]\s*\d+)*)\s*[\]】]")
_INDEX_SPLIT_PATTERN = re.compile(r"[,，]")


class Citation(BaseModel):
    """答案引用条目（供客户端渲染参考文献列表）。"""

    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=1, description="答案正文中使用的引用序号（从 1 起）")
    chunk_id: str = Field(min_length=1, description="证据切分主键")
    document_name: str = Field(default="", description="文档名")
    page_numbers: list[int] = Field(default_factory=list, description="页码")
    source_uri: str = Field(default="", description="来源路径/URI")


def extract_citation_indices(answer: str, *, max_index: int) -> list[int]:
    """解析答案中的引用标记，返回去重且保序的合法下标（1 基）。

    Args:
        answer: 模型输出文本。
        max_index: 允许的最大下标（= 提供给模型的证据条数）。

    Returns:
        合法下标列表；越界下标与非法标记被忽略（不抛异常，交由调用方判定
        "无引用"并走兜底分支）。
    """
    found: list[int] = []
    for match in _CITATION_PATTERN.finditer(answer):
        for raw in _INDEX_SPLIT_PATTERN.split(match.group(1)):
            index = int(raw)
            if 1 <= index <= max_index and index not in found:
                found.append(index)
    return found


def build_citations(evidence: list[EvidenceItem], indices: list[int]) -> list[Citation]:
    """按引用下标构造引用条目（下标即证据在提示词中的编号，1 基）。

    Args:
        evidence: 本次提供给模型的证据列表（顺序 = 提示词编号顺序）。
        indices: ``extract_citation_indices`` 的产物。

    Returns:
        引用条目列表，顺序与 ``indices`` 一致；越界下标被跳过。
    """
    citations: list[Citation] = []
    for index in indices:
        if 1 <= index <= len(evidence):
            item = evidence[index - 1]
            citations.append(
                Citation(
                    index=index,
                    chunk_id=item.chunk_id,
                    document_name=item.document_name,
                    page_numbers=list(item.page_numbers),
                    source_uri=item.source_uri,
                )
            )
    return citations


__all__: list[str] = ["Citation", "build_citations", "extract_citation_indices"]
