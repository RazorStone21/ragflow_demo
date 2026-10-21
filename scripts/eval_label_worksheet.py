"""生成相关文档全集核对工作表（只读，不写黄金集）。

检索三项（Recall@K / Precision@K / MRR）的分母是人工补齐的 ``relevant_documents``。
这份工作表把每道可答题的已知线索摊在一处——问题、must_hit、参考答案出处、
期望图片——让核对人回原文逐条确认"还有没有别的文件能实质支撑答案"。

**它不代签，也不写入黄金集。** 标签由人直接改 ``tests/evals/golden/*.jsonl``；
脚本能做的是把模板和核对锚点准备好，以及（可选）检查文件名是否真的存在于语料目录。
把"从检索结果里抄来的文档"当标签，会让 Recall 恒为满分——那是这套指标最容易
出现的自欺，工作表刻意不列本次检索命中了什么。

用法::

    uv run python scripts/eval_label_worksheet.py
    uv run python scripts/eval_label_worksheet.py --output local_data/evals/relevant-documents.md
    uv run python scripts/eval_label_worksheet.py --source E:/path/to/gov_doc
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

from agent_platform.core.errors import ConfigurationError, GoldenSetError
from agent_platform.core.logging import enable_utf8_console
from agent_platform.evaluation.loader import GoldenSet, load_golden_set
from agent_platform.evaluation.schemas import GoldenCase
from agent_platform.settings import Settings, load_settings

EXIT_OK = 0
EXIT_USAGE = 1


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="eval_label_worksheet.py",
        description="生成可答题的相关文档全集核对工作表（只读，不改黄金集）",
    )
    parser.add_argument("--golden-dir", default=None, help="黄金集目录（默认 EVAL_GOLDEN_DIR）")
    parser.add_argument("--output", default=None, help="写入该 Markdown 文件；缺省打印到 stdout")
    parser.add_argument(
        "--source",
        default=None,
        metavar="DIR",
        help="语料目录；给出时校验 must_hit / 参考答案出处 / 现有标签的文件名是否存在",
    )
    return parser


def _fmt_pages(pages: Iterable[int]) -> str:
    """页码列表 → ``第 1、3 页``；空列表 → 空串。"""
    values = list(pages)
    return f"第 {'、'.join(str(page) for page in values)} 页" if values else ""


def _case_section(number: int, case: GoldenCase) -> str:
    """渲染一道题的核对区块。"""
    lines = [
        f"## {number}. `{case.id}`",
        "",
        f"- 问题：{case.question}",
        f"- 类别 / 能力：{case.category.value} / {case.capability}",
    ]
    must_hit = [doc for doc in case.expected_documents if doc.must_hit]
    if must_hit:
        lines.append(
            "- must_hit 文档："
            + "；".join(f"{doc.name}（{_fmt_pages(doc.pages) or '页码未定'}）" for doc in must_hit)
        )
    if case.reference_answer_source is not None:
        source = case.reference_answer_source
        page_text = _fmt_pages([source.page]) if source.page is not None else ""
        page = f" {page_text}" if page_text else ""
        quote = f"；摘录：{source.quote}" if source.quote else ""
        lines.append(f"- 参考答案出处：{source.file}{page}{quote}")
    if case.expected_images:
        image_parts = []
        for image in case.expected_images:
            page_text = _fmt_pages([image.page]) if image.page is not None else "页码未定"
            image_parts.append(f"{image.document_name}（{page_text}）")
        lines.append("- 期望图片：" + "；".join(image_parts))
    if case.relevant_documents:
        lines.append(
            "- 现有标签："
            + "；".join(
                f"{doc.file_name}（{_fmt_pages(doc.pages) or '页码未定'}）"
                for doc in case.relevant_documents
            )
        )
    else:
        lines.append("- 现有标签：未填写")

    template = [
        "- 待填（写进该题 JSONL 对象）：",
        "",
        "  ```json",
        '  "relevant_documents": [',
        '    {"file_name": "示例.pdf", "pages": [1], "basis": "第 1 页给出了该容量对应的固定成本口径"}',
        "  ],",
        '  "relevance_reviewed_by": "<核对人>",',
        '  "relevance_reviewed_at": "2026-10-21"',
        "  ```",
        "",
    ]
    lines.extend(template)
    if case.notes:
        lines.extend([f"- 出题备注：{case.notes}", ""])
    return "\n".join(lines)


def render_worksheet(golden: GoldenSet) -> str:
    """渲染整份工作表（Markdown）。"""
    answerable = [case for case in golden.cases if case.is_answerable]
    labeled = sum(1 for case in answerable if case.has_relevance_labels)
    lines = [
        "# 相关文档全集核对工作表",
        "",
        f"- 黄金集指纹：`{golden.sha256}`",
        f"- 可答题：{len(answerable)} 条；已签署标签：{labeled} 条",
        "",
        "**定义**：任何能**实质支撑**该题正确答案（或可接受答案）的文件。",
        "只提关键词、只能作背景、不能支撑结论的不算；多文档综合题要列全。",
        "",
        "**怎么填**：逐题回原文找全集，**不要**只从检索结果里挑——那会把",
        '"当前检索命中"当成"相关全集"，Recall 永远满分。填完后在对应 JSONL 对象里',
        "加上 `relevant_documents` 与两个签署字段；题目本身的 `reviewed_by/at` 不动。",
        "",
        "**边界**：只把实际可检索（已入库）的文件写进标签。关键证据只在未入库文件里的题，",
        "要么先完成入库，要么不要当可答题。",
        "",
        "---",
        "",
    ]
    for number, case in enumerate(answerable, start=1):
        lines.append(_case_section(number, case))
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _collect_referenced_files(golden: GoldenSet) -> dict[str, list[str]]:
    """收集可答题里引用到的所有文件名 → 出现该名字的题号。"""
    referenced: dict[str, list[str]] = {}
    for case in golden.cases:
        if not case.is_answerable:
            continue
        names = [doc.name for doc in case.expected_documents]
        if case.reference_answer_source is not None:
            names.append(case.reference_answer_source.file)
        names.extend(doc.file_name for doc in case.relevant_documents)
        for name in names:
            referenced.setdefault(name, []).append(case.id)
    return referenced


def _check_source(golden: GoldenSet, source: Path) -> list[str]:
    """校验引用到的文件名是否存在于语料目录；返回缺失说明。"""
    available = {path.name.casefold() for path in source.rglob("*") if path.is_file()}
    missing = [
        f"{name}（{'、'.join(case_ids)}）"
        for name, case_ids in sorted(_collect_referenced_files(golden).items())
        if name.casefold() not in available
    ]
    return missing


def main(argv: list[str] | None = None) -> int:
    """脚本入口。"""
    enable_utf8_console()
    args = build_parser().parse_args(argv)
    try:
        settings: Settings = load_settings()
    except ConfigurationError as exc:
        sys.stderr.write(f"配置错误：{exc}\n")
        return EXIT_USAGE

    golden_dir = Path(args.golden_dir) if args.golden_dir else settings.eval_golden_dir
    try:
        golden = load_golden_set(golden_dir)
    except GoldenSetError as exc:
        sys.stderr.write(f"黄金集不可读：{exc}\n")
        return EXIT_USAGE

    worksheet = render_worksheet(golden)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(worksheet, encoding="utf-8")
        sys.stdout.write(f"工作表已写入：{target}\n")
    else:
        sys.stdout.write(worksheet)

    if args.source:
        source = Path(args.source)
        if not source.is_dir():
            sys.stderr.write(f"--source 不是目录：{source}\n")
            return EXIT_USAGE
        missing = _check_source(golden, source)
        if missing:
            sys.stderr.write(
                f"\n警告：{len(missing)} 个被引用的文件名在 {source} 中找不到。"
                "若它们尚未入库，题目不能作为可答题，标签也不能写还没入库的文件：\n"
            )
            for item in missing:
                sys.stderr.write(f"  - {item}\n")
            return EXIT_USAGE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
