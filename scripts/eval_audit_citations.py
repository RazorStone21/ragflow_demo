"""引用准确率的人工抽检：导出核对表、汇总一致率（结论只落 local_data）。

引用裁判给出的是模型判定。每轮抽最多 20 条引用，由人回证据原文核对，
再用一致率回答一个很具体的问题：**这一轮的分能不能当门禁依据**。
一致率低于 0.90 时，先修裁判提示词并重跑，不给提升基线的资格
（``eval_run.py --promote-baseline`` 会检查本脚本生成的摘要）。

抽检结论不进仓库：核对表里有答案与证据正文，属于演示语料之外的运行数据。
默认写在 ``local_data/evals/audits/``。

用法::

    # 导出核对表（按 run_id 确定性抽样，最多 20 条）
    uv run python scripts/eval_audit_citations.py \
        --artifact local_data/evals/runs/<run_id>.json --sample 20

    # 人工把每行的 human_supported 改为 true/false、按需补 note，然后汇总
    uv run python scripts/eval_audit_citations.py --summarize \
        --audit local_data/evals/audits/<run_id>.json --reviewed-by <你的名字>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_platform.core.errors import (
    AgentPlatformError,
    ArtifactIncompatibleError,
    ConfigurationError,
)
from agent_platform.core.logging import enable_utf8_console
from agent_platform.evaluation.prompts import CITATION_JUDGE_PROMPT
from agent_platform.evaluation.reporting import read_artifact
from agent_platform.evaluation.schemas import CaseResult
from agent_platform.settings import Settings, load_settings

EXIT_OK = 0
EXIT_USAGE = 1

MAX_SAMPLE = 20
"""每轮最多抽 20 条：引用准确率是红线附近的指标，20 条足以发现系统性误判，
再多会把人工核对变成一件没人愿意做的事。"""

AUDIT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class _Row:
    """一条可抽检的引用（脚本判定已可用）。"""

    case_id: str
    citation_index: int
    payload: dict[str, Any]


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="eval_audit_citations.py",
        description="引用准确率人工抽检：导出核对表或汇总一致率（结论只写 local_data）",
    )
    parser.add_argument("--artifact", default=None, help="原始产物路径（导出模式必填）")
    parser.add_argument(
        "--sample",
        type=int,
        default=MAX_SAMPLE,
        help=f"抽样条数（1–{MAX_SAMPLE}，默认 {MAX_SAMPLE}）",
    )
    parser.add_argument(
        "--out", default=None, help="核对表输出路径（默认 local_data/evals/audits/<run_id>.json）"
    )
    parser.add_argument("--force", action="store_true", help="覆盖已存在的核对表（会丢人工结论）")
    parser.add_argument(
        "--summarize", action="store_true", help="汇总模式：读取已填写的核对表并生成摘要"
    )
    parser.add_argument("--audit", default=None, help="汇总模式的核对表路径")
    parser.add_argument(
        "--reviewed-by", default="", metavar="HUMAN", help="汇总模式必填：人工核对人"
    )
    return parser


def _audits_dir(settings: Settings) -> Path:
    """人工结论的固定目录（与 ``eval_run.py`` 的摘要查找一致）。"""
    return settings.eval_runs_dir.parent / "audits"


def _citation_section(judge_raw: str) -> str | None:
    """取出 ``judge_raw`` 里引用裁判那一段（它是最后追加的一段）。"""
    marker = f"[{CITATION_JUDGE_PROMPT}]"
    position = judge_raw.rfind(marker)
    if position < 0:
        return None
    return judge_raw[position + len(marker) :].lstrip("\n")


def _parse_verdicts(raw: str, expected: frozenset[int]) -> dict[int, tuple[bool, str]] | None:
    """从裁判原始输出里找出逐条判定。

    ``judge_raw`` 保存的是模型原样输出（可能夹带解释或纠偏前后的两段），所以这里
    扫描所有 JSON 对象，取第一个**索引集合完全匹配**的。找不到返回 ``None``——
    宁可少抽几条，也不把解析出来的半截结构当成脚本判定。
    """
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw):
        try:
            payload, _ = decoder.raw_decode(raw[match.start() :])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        items = payload.get("citations")
        if not isinstance(items, list):
            continue
        verdicts: dict[int, tuple[bool, str]] = {}
        ok = True
        for item in items:
            if not isinstance(item, dict):
                ok = False
                break
            index = item.get("index")
            supported = item.get("supported")
            reason = item.get("reason", "")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not isinstance(supported, bool)
                or index in verdicts
            ):
                ok = False
                break
            verdicts[index] = (supported, reason if isinstance(reason, str) else "")
        if ok and set(verdicts) == expected:
            return verdicts
    return None


def _rows_for_case(case: CaseResult) -> tuple[list[_Row], str]:
    """把一条样例拆成可抽检的行；返回 ``(行, 跳过原因)``。"""
    if case.error:
        return [], "样例执行失败"
    if not case.citations:
        return [], ""
    expected = frozenset(citation.index for citation in case.citations)
    section = _citation_section(case.judge_raw)
    if section is None:
        return [], "缺少引用裁判原始输出"
    verdicts = _parse_verdicts(section, expected)
    if verdicts is None:
        return [], "引用裁判原始输出无法解析"

    evidence = {item.chunk_id: item for item in case.evidence}
    rows: list[_Row] = []
    for citation in case.citations:
        supported, reason = verdicts[citation.index]
        block = evidence.get(citation.chunk_id)
        rows.append(
            _Row(
                case_id=case.case_id,
                citation_index=citation.index,
                payload={
                    "case_id": case.case_id,
                    "citation_index": citation.index,
                    "document_name": citation.document_name,
                    "pages": citation.page_numbers,
                    "answer": case.answer,
                    "evidence": block.content if block is not None else "",
                    "script_supported": supported,
                    "script_reason": reason,
                    "human_supported": None,
                    "note": "",
                },
            )
        )
    return rows, ""


def _sample(rows: list[_Row], *, run_id: str, sample_size: int) -> list[_Row]:
    """按 ``run_id`` 确定性抽样。

    同一个 ``run_id`` 重跑导出得到同一批引用——否则"这轮抽过哪些"无法复现，
    一致率也就没法跟某一轮产物绑定。
    """
    seed = int.from_bytes(hashlib.sha256(run_id.encode("utf-8")).digest()[:8], "big")
    chosen = random.Random(seed).sample(rows, min(sample_size, len(rows)))
    return sorted(chosen, key=lambda row: (row.case_id, row.citation_index))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """写 JSON（目录自动创建，UTF-8，保留中文）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _export(args: argparse.Namespace, settings: Settings) -> int:
    """导出核对表。"""
    if not args.artifact:
        sys.stderr.write("导出模式需要 --artifact\n")
        return EXIT_USAGE
    if not 1 <= args.sample <= MAX_SAMPLE:
        sys.stderr.write(f"--sample 必须在 1–{MAX_SAMPLE} 之间\n")
        return EXIT_USAGE
    try:
        artifact = read_artifact(Path(args.artifact))
    except (ArtifactIncompatibleError, AgentPlatformError) as exc:
        sys.stderr.write(f"产物不可读：{exc}\n")
        return EXIT_USAGE

    rows: list[_Row] = []
    skipped: list[str] = []
    for case in artifact.cases:
        case_rows, reason = _rows_for_case(case)
        rows.extend(case_rows)
        if reason:
            skipped.append(f"{case.case_id}：{reason}")
    if not rows:
        sys.stderr.write(
            "本轮没有可抽检的引用（可能未跑引用裁判、答案没有引用，或裁判输出无法解析）。\n"
        )
        for item in skipped[:10]:
            sys.stderr.write(f"  - {item}\n")
        return EXIT_USAGE

    selected = _sample(rows, run_id=artifact.run.run_id, sample_size=args.sample)
    target = Path(args.out) if args.out else _audits_dir(settings) / f"{artifact.run.run_id}.json"
    if target.exists() and not args.force:
        sys.stderr.write(
            f"核对表已存在：{target}\n"
            "为避免覆盖已填写的人工结论，默认不覆盖；确认要重来请加 --force。\n"
        )
        return EXIT_USAGE

    _write_json(
        target,
        {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "run_id": artifact.run.run_id,
            "artifact": str(Path(args.artifact)),
            "available": len(rows),
            "sampled": len(selected),
            "instructions": (
                "逐条对照 evidence 与 answer 中该引用所支撑的断言，把 human_supported "
                "改为 true 或 false，必要时在 note 写明理由；全部填完后用 --summarize 汇总。"
            ),
            "rows": [row.payload for row in selected],
        },
    )
    sys.stdout.write(
        f"核对表已写入：{target}\n"
        f"抽样 {len(selected)}/{len(rows)} 条引用"
        + (f"；{len(skipped)} 条样例未纳入（见 stderr）" if skipped else "")
        + "\n"
    )
    for item in skipped:
        sys.stderr.write(f"  - {item}\n")
    return EXIT_OK


def _load_audit(path: Path) -> dict[str, Any]:
    """读取核对表；结构不符时抛出 ``ValueError``。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"核对表不存在：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"核对表不可读：{path}（{type(exc).__name__}）") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("rows"), list):
        raise ValueError(f"核对表结构不符：{path}")
    return raw


def _summarize(args: argparse.Namespace, settings: Settings) -> int:
    """汇总人工与脚本的一致率，写出 ``_promote`` 会检查的摘要。"""
    if not args.audit:
        sys.stderr.write("--summarize 需要 --audit 指定核对表路径\n")
        return EXIT_USAGE
    if not args.reviewed_by.strip():
        sys.stderr.write("--summarize 需要 --reviewed-by：一致率要有人负责\n")
        return EXIT_USAGE
    try:
        audit = _load_audit(Path(args.audit))
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_USAGE

    rows = [row for row in audit["rows"] if isinstance(row, dict)]
    if not rows:
        sys.stderr.write("核对表里没有行\n")
        return EXIT_USAGE
    unfilled = [
        f"{row.get('case_id')}#{row.get('citation_index')}"
        for row in rows
        if not isinstance(row.get("human_supported"), bool)
    ]
    if unfilled:
        sys.stderr.write(
            f"还有 {len(unfilled)} 条未填写 human_supported：{'、'.join(unfilled[:10])}\n"
            "全部填完再汇总；只统计填了的部分会悄悄放大一致率。\n"
        )
        return EXIT_USAGE

    matches = sum(1 for row in rows if row["human_supported"] == row["script_supported"])
    agreement = matches / len(rows)
    run_id = audit.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        sys.stderr.write("核对表缺少 run_id\n")
        return EXIT_USAGE

    summary_path = _audits_dir(settings) / f"{run_id}.summary.json"
    _write_json(
        summary_path,
        {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "run_id": run_id,
            "sampled": audit.get("sampled", len(rows)),
            "reviewed": len(rows),
            "agreement_rate": round(agreement, 4),
            "reviewed_by": args.reviewed_by.strip(),
            "reviewed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    sys.stdout.write(
        f"一致率 {agreement:.2f}（{matches}/{len(rows)} 条），摘要已写入：{summary_path}\n"
    )
    if agreement < 0.90:
        sys.stderr.write(
            "⚠️ 一致率低于 0.90：本轮 citation_accuracy 不得作为门禁依据，"
            "先修裁判提示词并重跑，再抽检。\n"
        )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """脚本入口。"""
    enable_utf8_console()
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        sys.stderr.write(f"配置错误：{exc}\n")
        return EXIT_USAGE
    if args.summarize:
        return _summarize(args, settings)
    return _export(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
