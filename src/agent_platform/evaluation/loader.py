"""黄金集装载与覆盖度报告（CLAUDE.md 9.2）。

**严格 JSONL，不支持注释**：每个 ``.jsonl`` 一行一个 ``GoldenCase``。刻意不引入
``#`` 注释语法——一旦支持，文件就不再是标准 JSONL，``jq`` / 编辑器插件 / CI 校验器
全部失效，而"评审人要能直接用现成工具翻这份文件"恰恰是本层存在的意义。
需要解释时写进 ``notes`` 字段（它本来就是给评审人看的）。

**一律显式失败**：任何解析或校验问题都抛 ``GoldenSetError`` 并带上 ``文件:行号``。
绝不"跳过坏行继续跑"——那会让黄金集在无人察觉的情况下变短，而门禁照常报绿。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from agent_platform.agents.gov_policy_qa.state import AnswerStatus
from agent_platform.core.errors import GoldenSetError
from agent_platform.evaluation.schemas import CaseCategory, GoldenCase

GOLDEN_SUFFIX = ".jsonl"


@dataclass(frozen=True)
class CoverageReport:
    """黄金集覆盖度报告（人读；用于回答"这批题到底测了些什么"）。"""

    total: int
    reviewed: int
    drafts: int
    by_category: dict[CaseCategory, int]
    by_status: dict[str, int]
    missing_categories: tuple[CaseCategory, ...]
    answerable: int = 0
    relevance_labeled: int = 0

    def render(self) -> str:
        """渲染为终端可读的多行文本（脚本与测试共用同一份格式）。"""
        lines = [
            f"黄金集：{self.total} 条（已签署 {self.reviewed}，草稿 {self.drafts}）",
            "按类别："
            + "，".join(
                f"{category.value}={self.by_category.get(category, 0)}" for category in CaseCategory
            ),
            "按期望状态："
            + "，".join(f"{status}={count}" for status, count in sorted(self.by_status.items())),
            f"相关性标签：可答题 {self.answerable} 条，已签署 {self.relevance_labeled} 条",
        ]
        if self.missing_categories:
            lines.append("⚠ 类别空缺：" + "，".join(c.value for c in self.missing_categories))
        if self.relevance_labeled < self.answerable:
            lines.append("⚠ 仍有可答题缺少相关文档全集：检索三项不会采集，门禁判样本不足")
        return "\n".join(lines)


@dataclass(frozen=True)
class GoldenSet:
    """一批黄金集样例 + 内容指纹。

    ``sha256`` 覆盖**全部文件内容**（按文件名排序后拼接），用于在产物里留痕：
    两次评估的分数不可比时，第一件要查的就是黄金集有没有被改过。
    """

    cases: tuple[GoldenCase, ...]
    sha256: str
    source_dir: Path

    def by_id(self, case_id: str) -> GoldenCase | None:
        """按 id 取样例；不存在返回 ``None``（调用方决定是报错还是跳过）。"""
        for case in self.cases:
            if case.id == case_id:
                return case
        return None

    @property
    def reviewed(self) -> tuple[GoldenCase, ...]:
        """已签署样例（门禁只统计这一部分）。"""
        return tuple(case for case in self.cases if case.is_reviewed)

    @property
    def drafts(self) -> tuple[GoldenCase, ...]:
        """未签署样例（草稿或缺评审人）。"""
        return tuple(case for case in self.cases if not case.is_reviewed)


def load_golden_set(directory: Path) -> GoldenSet:
    """装载目录下全部 ``*.jsonl`` 黄金集。

    Args:
        directory: 黄金集目录（如 ``tests/evals/golden``）。

    Returns:
        按 ``id`` 排序的 :class:`GoldenSet`。

    Raises:
        GoldenSetError: 目录不存在、无样例文件、文件为空、某行不是合法 JSON、
            字段校验失败，或 ``id`` 在文件间重复。异常信息包含 ``文件:行号``。
    """
    if not directory.is_dir():
        raise GoldenSetError(
            f"黄金集目录不存在: {directory}",
            detail=f"黄金集目录不存在: {directory}",
        )
    files = sorted(p for p in directory.iterdir() if p.suffix == GOLDEN_SUFFIX)
    if not files:
        raise GoldenSetError(
            f"黄金集目录下没有任何 {GOLDEN_SUFFIX} 文件: {directory}",
            detail=f"黄金集目录下没有 {GOLDEN_SUFFIX} 文件: {directory}",
        )

    cases: list[GoldenCase] = []
    seen: dict[str, str] = {}
    for path in files:
        for line_no, raw in _iter_lines(path):
            case = _parse_case(raw, path=path, line_no=line_no)
            origin = f"{path.name}:{line_no}"
            if case.id in seen:
                raise GoldenSetError(
                    f"黄金集 id 重复: {case.id}（{seen[case.id]} 与 {origin}）",
                    detail=(
                        f"黄金集 id 重复: {case.id}；先出现于 {seen[case.id]}，"
                        f"又出现于 {origin}。id 是基线对比的键，重复会让分数互相覆盖。"
                    ),
                )
            seen[case.id] = origin
            cases.append(case)

    cases.sort(key=lambda c: c.id)
    return GoldenSet(cases=tuple(cases), sha256=_fingerprint(files), source_dir=directory)


def coverage_report(golden: GoldenSet) -> CoverageReport:
    """统计覆盖度。

    ``missing_categories`` 只报告 ``CaseCategory`` 里的空缺——缺哪一类是**事实**，
    要不要因此判失败由门禁决定（首版黄金集就存在已知空缺，见 CHANGELOG）。
    """
    by_category: dict[CaseCategory, int] = {}
    by_status: dict[str, int] = {}
    for case in golden.cases:
        by_category[case.category] = by_category.get(case.category, 0) + 1
        by_status[case.expected_status.value] = by_status.get(case.expected_status.value, 0) + 1
    missing = tuple(c for c in CaseCategory if c not in by_category)
    answerable = tuple(case for case in golden.cases if case.is_answerable)
    return CoverageReport(
        total=len(golden.cases),
        reviewed=len(golden.reviewed),
        drafts=len(golden.drafts),
        by_category=by_category,
        by_status=by_status,
        missing_categories=missing,
        answerable=len(answerable),
        relevance_labeled=sum(1 for case in answerable if case.has_relevance_labels),
    )


def unknown_statuses(golden: GoldenSet) -> tuple[str, ...]:
    """返回黄金集中出现的、不属于 ``AnswerStatus`` 五态的期望状态。

    ``GoldenCase.expected_status`` 已是 ``AnswerStatus`` 枚举，理论上不可能出现非法值；
    本函数存在的意义是给**路由旁路**留一道显式检查——若日后有人把该字段放宽为 ``str``，
    这里会立刻报出来，而不是让非法值一路漂到判分器里变成"永不通过"的静默失败。
    """
    known = {status.value for status in AnswerStatus}
    return tuple(sorted({c.expected_status.value for c in golden.cases} - known))


def _iter_lines(path: Path) -> Iterable[tuple[int, str]]:
    """逐行读取，产出 ``(行号, 非空原始文本)``；空行忽略（行号仍按原文件计）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GoldenSetError(
            f"黄金集文件不可读: {path}",
            detail=f"黄金集文件不可读: {path}（{type(exc).__name__}）",
        ) from exc
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            yield line_no, line


def _parse_case(raw: str, *, path: Path, line_no: int) -> GoldenCase:
    """把一行文本解析为 ``GoldenCase``；失败时抛出带定位的 ``GoldenSetError``。"""
    origin = f"{path.name}:{line_no}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GoldenSetError(
            f"黄金集第 {origin} 行不是合法 JSON: {exc.msg}",
            detail=f"黄金集解析失败 {origin}: {exc.msg}（列 {exc.colno}）",
        ) from exc
    if not isinstance(payload, dict):
        raise GoldenSetError(
            f"黄金集 {origin} 行必须是 JSON 对象",
            detail=f"黄金集 {origin} 行是 {type(payload).__name__}，要求每行一个 JSON 对象",
        )
    try:
        return GoldenCase.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise GoldenSetError(
            f"黄金集 {origin} 字段校验失败: {problems}",
            detail=f"黄金集 {origin} 字段校验失败: {problems}",
        ) from exc


def _fingerprint(files: list[Path]) -> str:
    """计算黄金集内容的联合 sha256（文件名 + 内容，按文件名排序）。"""
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


__all__: list[str] = [
    "GOLDEN_SUFFIX",
    "CoverageReport",
    "GoldenSet",
    "coverage_report",
    "load_golden_set",
    "unknown_statuses",
]
